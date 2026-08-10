"""디스패치 — central↔worker HTTP 프로토콜(중앙 전용).

역할:
    폴러/웹훅이 매핑한 잡을 스케줄러에 등록(enqueue)하고, 그 사용자의 worker가
    HTTP로 다음 잡을 가져가고(GET .../next) 실행 상태·로그를 회신(POST .../status)
    하는 중앙 측 디스패치 허브. worker는 Jira를 직접 보지 않고 오직 이 HTTP로만
    잡을 주고받는다. **enqueue는 반드시 스케줄러 경유**(레포락/동시성 판단).

역할 소속: **central**.

구현 Phase: **Phase 3** (레지스트리 + 디스패치 큐/HTTP).

HTTP 계약(dispatch_bp, main.py가 등록):
    GET  /dispatch/<user>/next
        worker 폴링 진입점. 스케줄러가 그 사용자에게 dispatch한(running) 잡을
        반환. 없으면 204. (재폴링 시 같은 잡 멱등 반환 → --resume)
    POST /dispatch/<user>/<job>/status
        worker가 상태/로그/결과를 회신(채널 F). 본문: {status, log_summary?,
        reset_at?, branch?, session_id?, mr_url?, audit_refs?, rolledback?, error?}.
        terminal(완료/실패)이면 scheduler.on_complete(레포락 해제→다음 dispatch),
        interrupted면 scheduler.on_interrupt(reset_at 재적격), cancelled면
        scheduler.confirm_cancelled(레포락+dedup 해제), 그 외는 진행 갱신.
    GET  /dispatch/<user>/<job>/control
        worker가 실행 중 주기적으로 폴링하는 취소 제어 채널(§10.4). 응답:
        {"cancel": bool}. cancel=true면 worker가 abort+롤백 후 cancelled 회신.

인증:
    - worker 인증 = ``X-Worker-Secret`` 헤더(config WORKER_SHARED_SECRET). 설정 시
      상수시간 비교로 검증하고 불일치는 401. 미설정(사내망 신뢰)이면 통과(경고).
    - 교차 사용자 클레임 방지: job.user != <user> 이면 403.
"""

from __future__ import annotations

import hmac
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from app.queue import Job

# 라우트는 이 모듈에서 dispatch_bp에 직접 배선하고, main.py는 등록만 한다.
dispatch_bp = Blueprint("dispatch", __name__)

# Flask app.config 에서 Dispatcher를 꺼내는 키.
DISPATCHER_KEY = "JAD_DISPATCHER"


class Dispatcher:
    """스케줄러 앞단 HTTP 어댑터 + 잡 조회(central)."""

    def __init__(self, registry, scheduler, worker_secret: str = "") -> None:
        """의존성 주입(레지스트리·스케줄러) + worker 공유 시크릿."""
        self.registry = registry
        self.scheduler = scheduler
        self.worker_secret = worker_secret or ""

    # -- 프로그램적 API --

    def enqueue(self, user: str, job: Job) -> list:
        """사용자 user 소유로 태깅해 스케줄러에 등록(레포락/동시성 판단은 스케줄러).

        반환: 이 enqueue로 즉시 dispatch된 잡 id 목록.
        """
        job.user = user
        return self.scheduler.enqueue(job)

    def next_job(self, user: str) -> Optional[Job]:
        """그 사용자에게 dispatch된(running) 잡 1개(없으면 None)."""
        return self.scheduler.next_for_user(user)

    def report_status(self, user: str, job_id: str, payload: dict) -> dict:
        """worker 회신을 상태머신/스케줄러에 반영.

        Returns: {"ok": True, "dispatched": [...]} 또는 오류 dict.
        Raises: PermissionError(교차 사용자), KeyError(미존재 잡).
        """
        job = self.scheduler.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.user != user:
            raise PermissionError(f"교차 사용자 클레임 거부: {user} != {job.user}")

        status = (payload or {}).get("status", "")
        # 채널 F가 넘길 수 있는 부가 필드만 화이트리스트로 전달.
        fields = {}
        for k in ("log_summary", "reset_at", "branch", "session_id", "mr_url",
                  "audit_refs", "rolledback"):
            if k in (payload or {}):
                fields[k] = payload[k]
        # 별칭: worker가 'log'로 보낼 수도.
        if "log" in (payload or {}) and "log_summary" not in fields:
            fields["log_summary"] = payload["log"]

        dispatched = self.scheduler.report(job_id, status, **fields)
        return {"ok": True, "dispatched": dispatched}

    def control(self, user: str, job_id: str) -> dict:
        """취소 제어 채널(§10.4) — worker 폴링용 {"cancel": bool}.

        Raises: PermissionError(교차 사용자), KeyError(미존재 잡).
        """
        job = self.scheduler.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.user != user:
            raise PermissionError(f"교차 사용자 클레임 거부: {user} != {job.user}")
        return {"cancel": bool(job.cancel_requested)}

    def list_jobs(self, user: Optional[str] = None) -> list:
        """잡 현황(관리 UI용). user 지정 시 필터."""
        jobs = self.scheduler.jobs.list_jobs()
        if user is not None:
            jobs = [j for j in jobs if j.user == user]
        return jobs

    # -- 인증 --

    def verify_secret(self, provided: Optional[str]) -> bool:
        """X-Worker-Secret 검증(상수시간). 시크릿 미설정이면 통과."""
        if not self.worker_secret:
            return True
        return bool(provided) and hmac.compare_digest(str(provided), self.worker_secret)


# --- HTTP 핸들러 ---


def _get_dispatcher() -> Dispatcher:
    disp = current_app.config.get(DISPATCHER_KEY)
    if disp is None:
        raise RuntimeError("Dispatcher가 app.config에 배선되지 않았습니다")
    return disp


def handle_next(dispatcher: Dispatcher, user: str):
    """GET /dispatch/<user>/next — 다음 잡 1건 반환(없으면 204)."""
    if not dispatcher.verify_secret(request.headers.get("X-Worker-Secret")):
        return jsonify({"error": "unauthorized"}), 401
    job = dispatcher.next_job(user)
    if job is None:
        return ("", 204)
    return jsonify(job.to_dict())


def handle_status(dispatcher: Dispatcher, user: str, job: str, req):
    """POST /dispatch/<user>/<job>/status — worker 상태/로그 회신 수신."""
    if not dispatcher.verify_secret(req.headers.get("X-Worker-Secret")):
        return jsonify({"error": "unauthorized"}), 401
    payload = req.get_json(silent=True) or {}
    try:
        result = dispatcher.report_status(user, job, payload)
    except KeyError:
        return jsonify({"error": "unknown job", "job": job}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify(result)


def handle_control(dispatcher: Dispatcher, user: str, job: str, req):
    """GET /dispatch/<user>/<job>/control — 취소 제어 채널 폴링(§10.4)."""
    if not dispatcher.verify_secret(req.headers.get("X-Worker-Secret")):
        return jsonify({"error": "unauthorized"}), 401
    try:
        result = dispatcher.control(user, job)
    except KeyError:
        return jsonify({"error": "unknown job", "job": job}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    return jsonify(result)


@dispatch_bp.route("/dispatch/<user>/next", methods=["GET"])
def route_next(user: str):
    return handle_next(_get_dispatcher(), user)


@dispatch_bp.route("/dispatch/<user>/<job>/status", methods=["POST"])
def route_status(user: str, job: str):
    return handle_status(_get_dispatcher(), user, job, request)


@dispatch_bp.route("/dispatch/<user>/<job>/control", methods=["GET"])
def route_control(user: str, job: str):
    return handle_control(_get_dispatcher(), user, job, request)
