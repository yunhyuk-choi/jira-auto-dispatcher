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
        worker가 실행 중 주기적으로 폴링하는 제어 채널(§10.4 + 담당자 변경 핸드오프).
        응답: {"cancel": bool, "action": "none"|"cancel"|"handoff"}.
        - action=cancel(=cancel:true) → worker가 abort+롤백 후 cancelled 회신.
        - action=handoff → worker가 WIP를 커밋·push(checkpoint, 롤백X) 후 handed_off 회신.
        (하위호환: 구 worker는 "cancel" 불리언만 읽어도 동작 — 핸드오프는 cancel:false라
         구 worker에 오취소를 유발하지 않는다.)

인증:
    - worker 인증 = ``X-Worker-Secret`` 헤더(config WORKER_SHARED_SECRET). 설정 시
      상수시간 비교로 검증하고 불일치는 401. 미설정(신뢰 네트워크 전제)이면 통과(경고).
    - 교차 사용자 클레임 방지: job.user != <user> 이면 403.
"""

from __future__ import annotations

import hmac
from typing import Optional

from flask import Blueprint, current_app, jsonify, request

from app import queue as q
from app.pending import PendingRegistry
from app.queue import Job

# 라우트는 이 모듈에서 dispatch_bp에 직접 배선하고, main.py는 등록만 한다.
dispatch_bp = Blueprint("dispatch", __name__)

# Flask app.config 에서 Dispatcher를 꺼내는 키.
DISPATCHER_KEY = "JAD_DISPATCHER"


class Dispatcher:
    """스케줄러 앞단 HTTP 어댑터 + 잡 조회(central)."""

    # dlc-meta 단일 라이터가 커밋을 시도하는 완료 상태(terminal). cancelled는 롤백이라
    # 사이클로그를 커밋하지 않는다(interrupted는 완료가 아님).
    _COMMIT_STATUSES = frozenset({q.DONE, q.FAILED, q.HANDED_OFF})

    def __init__(self, registry, scheduler, worker_secret: str = "",
                 dlc_meta_writer=None, pending=None) -> None:
        """의존성 주입(레지스트리·스케줄러·dlc-meta 라이터) + worker 공유 시크릿.

        ``dlc_meta_writer``(선택, central 전용): 잡 완료 회신 시 공유 dlc-meta 클론의
        사이클로그를 커밋·push하는 **단일 라이터**(:class:`app.dlc_meta_writer.DlcMetaWriter`).
        None이면 커밋 단계를 건너뛴다(테스트/워커 격리).

        ``pending``(선택, Phase 3b-1): 명시적 id 요청/응답 pending 레지스트리
        (:class:`app.pending.PendingRegistry`). None이면 스케줄러의 잡 스토어 위에
        기본 인스턴스를 만든다 — 영속 잡 상태에서 파생되므로 재시작 안전.
        """
        self.registry = registry
        self.scheduler = scheduler
        self.worker_secret = worker_secret or ""
        self.dlc_meta_writer = dlc_meta_writer
        # 명시적 id pending 레지스트리(3b-1). 진실원 = scheduler.jobs(jobs.json).
        self.pending = pending if pending is not None else PendingRegistry(scheduler.jobs)

    # -- 프로그램적 API --

    def enqueue(self, user: str, job: Job) -> list:
        """사용자 user 소유로 태깅해 스케줄러에 등록(레포락/동시성 판단은 스케줄러).

        반환: 이 enqueue로 즉시 dispatch된 잡 id 목록.
        """
        job.user = user
        return self.scheduler.enqueue(job)

    def next_job(self, user: str, exclude: Optional[set] = None) -> Optional[Job]:
        """그 사용자에게 dispatch된(running) 잡 1개(없으면 None).

        ``exclude`` = worker가 이미 처리 중인 티켓 집합(동시성 ≥2). 그와 다른 running
        잡을 반환해 worker가 서로 다른 잡을 동시에 수령하게 한다(Increment 2).
        """
        return self.scheduler.next_for_user(user, exclude=exclude)

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
        # correlation_id(3b-1): 워커가 위임 id를 되싣어 보내면 잡에 명시 영속한다 —
        # 구 워커는 안 보내므로 field는 그대로(None → corr_id가 티켓으로 폴백, 하위호환).
        fields = {}
        for k in ("log_summary", "reset_at", "branch", "session_id", "mr_url",
                  "audit_refs", "rolledback", "correlation_id"):
            if k in (payload or {}):
                fields[k] = payload[k]
        # 별칭: worker가 'log'로 보낼 수도.
        if "log" in (payload or {}) and "log_summary" not in fields:
            fields["log_summary"] = payload["log"]

        dispatched = self.scheduler.report(job_id, status, **fields)
        result = {"ok": True, "dispatched": dispatched}

        # dlc-meta 단일 라이터(central): 완료(terminal) 회신이면 그 잡의 사이클로그를
        # 공유 클론에서 커밋·push하고, 커밋한 상대경로를 회신에 실어 워커의 완료 알림이
        # `사이클로그: <relpath>` 를 붙일 수 있게 한다. best-effort — 실패해도 상태 회신은
        # 정상 반환한다(락/dedup 해제는 이미 스케줄러가 처리).
        if self.dlc_meta_writer is not None and q.normalize_status(status) in self._COMMIT_STATUSES:
            try:
                relpath = self.dlc_meta_writer.commit_cycle_log(job)
            except Exception:  # noqa: BLE001 — 커밋 실패가 회신을 막지 않는다
                relpath = None
            if relpath:
                result["cycle_log_path"] = relpath
                # 3b-1: 결과 payload가 재시작 후에도 사이클로그 경로를 surfacing하도록
                # 잡에 영속(meta로 간다 — Job에 해당 field 없음). pending 폴러가 읽는다.
                try:
                    self.scheduler.jobs.update(job_id, cycle_log_path=relpath)
                except KeyError:
                    pass
        return result

    # -- 명시적 id 요청/응답(Phase 3b-1) — 미래 Tier-2가 위임·회수에 사용 --

    def register_pending(self, correlation_id: str, *, ticket: Optional[str] = None) -> str:
        """위임을 correlation id로 pending 등록(pending 레지스트리 위임). 기본 id=티켓."""
        return self.pending.register_pending(correlation_id, ticket=ticket)

    def poll_result(self, correlation_id: str) -> dict:
        """한 correlation id의 done/pending + 결과 payload 조회(pending 레지스트리 위임)."""
        return self.pending.poll_result(correlation_id)

    def poll_results(self, correlation_ids) -> dict:
        """여러 correlation id를 한 번에 폴 — 각 결과를 정확한 id에 매칭."""
        return self.pending.poll_results(correlation_ids)

    def control(self, user: str, job_id: str) -> dict:
        """제어 채널(§10.4 + 핸드오프) — worker 폴링용 {"cancel": bool, "action": str}.

        action: "handoff"(담당자 변경 checkpoint) 우선, 아니면 "cancel"(취소) / "none".
        cancel 불리언은 하위호환 필드(handoff는 cancel:false — 구 worker 오취소 방지).

        Raises: PermissionError(교차 사용자), KeyError(미존재 잡).
        """
        job = self.scheduler.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        if job.user != user:
            raise PermissionError(f"교차 사용자 클레임 거부: {user} != {job.user}")
        if getattr(job, "control_action", "none") == "handoff":
            return {"cancel": False, "action": "handoff"}
        if job.cancel_requested:
            return {"cancel": True, "action": "cancel"}
        return {"cancel": False, "action": "none"}

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


def _parse_exclude(req) -> set:
    """?exclude=T1,T2 쿼리를 티켓 집합으로 파싱(worker가 처리 중인 티켓 배제).

    비었거나 없으면 빈 집합. 콤마 구분, 공백 strip. (구 worker는 이 파라미터를
    안 보내므로 빈 집합 = 첫 running 잡 반환 = 기존 동작.)
    """
    raw = req.args.get("exclude", "") or ""
    return {t.strip() for t in raw.split(",") if t.strip()}


def handle_next(dispatcher: Dispatcher, user: str, req=None):
    """GET /dispatch/<user>/next — 다음 잡 1건 반환(없으면 204).

    ``?exclude=`` 쿼리(worker가 이미 처리 중인 티켓)를 배제하고 **다른** running 잡을
    반환해 per-user 동시 실행을 가능케 한다(Increment 2).
    """
    req = req if req is not None else request
    if not dispatcher.verify_secret(req.headers.get("X-Worker-Secret")):
        return jsonify({"error": "unauthorized"}), 401
    job = dispatcher.next_job(user, exclude=_parse_exclude(req))
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
