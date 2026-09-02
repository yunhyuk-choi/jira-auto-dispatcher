"""디스패치 — 잡 등록 + 완료 상태머신 + 관측/Tier-2 pending(중앙 전용, 인프로세스).

역할:
    폴러/웹훅이 매핑한 잡을 스케줄러에 등록(enqueue)하고, 완료 회신을 상태머신에
    반영(report_status)하며, 잡 현황 조회(list_jobs)와 명시적 id 요청/응답(Tier-2
    pending)을 제공하는 중앙 측 인프로세스 어댑터. **enqueue는 반드시 스케줄러 경유**
    (레포락/동시성 판단).

    ⚠️ 프랙탈 P2 — 레거시 worker-facing HTTP 서빙 제거:
        구 모델에서는 사용자 worker 가 GET /next 로 잡을 폴링하고 POST /status·GET
        /control 로 회신하는 HTTP 프로토콜(``dispatch_bp``)이 있었다. 이 폴링 소비자
        (app/worker.py::worker_loop)는 상주 센트럴 라이브 세션(worker_dispatch.py docker
        exec)과 **이중 실행**을 일으켜 같은 티켓을 두 번 처리(중복 MR·브랜치·Jira 코멘트)
        했으므로, **worker_loop 소비자와 함께 GET /next·POST /status·GET /control 서빙
        엔드포인트를 전부 제거**했다. 실행은 이제 센트럴 세션이 docker exec 로 직접 주입
        하는 프랙탈 경로가 유일하다.

        남는 인프로세스 표면(fractal/Tier-2 인접): :meth:`enqueue`(스케줄러 등록),
        :meth:`report_status`(완료 상태머신 + dlc-meta 단일 라이터 커밋 + pending 해소),
        :meth:`list_jobs`(관측), :meth:`register_pending`/:meth:`poll_result`(Tier-2 3b-1).

역할 소속: **central**.
"""

from __future__ import annotations

from typing import Optional

from app import queue as q
from app.pending import PendingRegistry
from app.queue import Job


class Dispatcher:
    """스케줄러 앞단 인프로세스 어댑터 + 완료 상태머신 + 잡 조회(central)."""

    # dlc-meta 단일 라이터가 커밋을 시도하는 완료 상태(terminal). cancelled는 롤백이라
    # 사이클로그를 커밋하지 않는다(interrupted는 완료가 아님).
    _COMMIT_STATUSES = frozenset({q.DONE, q.FAILED, q.HANDED_OFF})

    def __init__(self, registry, scheduler, dlc_meta_writer=None, pending=None) -> None:
        """의존성 주입(레지스트리·스케줄러·dlc-meta 라이터).

        ``dlc_meta_writer``(선택, central 전용): 잡 완료 시 공유 dlc-meta 클론의
        사이클로그를 커밋·push하는 **단일 라이터**(:class:`app.dlc_meta_writer.DlcMetaWriter`).
        None이면 커밋 단계를 건너뛴다(테스트/워커 격리).

        ``pending``(선택, Phase 3b-1): 명시적 id 요청/응답 pending 레지스트리
        (:class:`app.pending.PendingRegistry`). None이면 스케줄러의 잡 스토어 위에
        기본 인스턴스를 만든다 — 영속 잡 상태에서 파생되므로 재시작 안전.
        """
        self.registry = registry
        self.scheduler = scheduler
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

    def list_jobs(self, user: Optional[str] = None) -> list:
        """잡 현황(관리 UI용). user 지정 시 필터."""
        jobs = self.scheduler.jobs.list_jobs()
        if user is not None:
            jobs = [j for j in jobs if j.user == user]
        return jobs
