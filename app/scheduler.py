"""결정적 레포락 스케줄러(중앙 전용) — RECURSIVE-DISPATCH §4.

역할:
    central의 직렬/병렬 판단(프레임워크 책임 3의 재귀 적용)을 **결정적 레포락**
    으로 구현한다.

        - **레포 단위 락** — 한 레포에는 활성(running) 잡이 1개만. 서로 다른 레포의
          잡은 병렬. 판단 입력 = 대상 레포 잠금 여부 + 전역 동시성 cap + per-user cap.
        - **겹치면 defer** — 대상 레포가 잠겨 있으면 큐에서 대기. 완료 notify(채널 F)
          수신 시 레포 락 해제 → 다음 적격 잡 dispatch(완료-구동 루프).
        - **전역 동시성 cap** — run.global_concurrency(기본 3). per-user cap =
          run.concurrency_per_worker(기본 1, 루프방지 §6 "worker당 동시성 1").
        - **미해석(target_repos=[]) = 전역 직렬** — REPO-MAP으로 레포를 못 정한 잡은
          보수적으로 단독 실행(다른 모든 잡을 배제하고 혼자 running).
        - **재개(interrupted+reset_at)** — 중단 잡은 레포락을 놓고 대기하다, reset_at이
          지나면 tick의 적격 풀로 복귀해 재-dispatch(resume)된다.

역할 소속: **central** (실제 실행은 각 사용자 worker가 한다).

구현 Phase: **Phase 3~5** (레포락 스케줄러 + 재개).

설계 메모:
    - 레포 락/전역 락/running 수는 **running 잡 상태의 순수 함수**로 매 tick 재계산한다
      (별도 락 테이블을 영속하지 않음 → 재시작 후에도 jobs.json에서 자동 복원).
    - dispatch = 잡을 running으로 표시(해당 레포를 사실상 점유). worker는
      GET /dispatch/<user>/next 로 자기 running 잡을 수령한다.
    - reset_at 비교 기준 시각은 now_provider로 주입 가능(테스트 결정성).
"""

from __future__ import annotations

import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

from app import queue as q
from app.queue import Job, JobQueue


def _parse_iso(value: str) -> Optional[datetime]:
    """ISO8601(옵션 'Z') → aware datetime. 실패 시 None."""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Scheduler:
    """결정적 레포락 스케줄러."""

    def __init__(
        self,
        config,
        job_queue: JobQueue,
        now_provider: Optional[Callable[[], datetime]] = None,
        gate=None,
    ) -> None:
        self.config = config
        self.jobs = job_queue
        self._lock = threading.RLock()
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        # dedup 게이트(취소/재오픈 시 dedup 해제 — §10.4). 없으면 no-op(테스트 격리).
        self.gate = gate

        run = getattr(config, "run", None)
        self.global_cap = int(getattr(run, "global_concurrency", 3)) if run else 3
        self.per_user_cap = int(getattr(run, "concurrency_per_worker", 1)) if run else 1

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def enqueue(self, job: Job) -> list:
        """잡을 큐에 등록(queued)하고 즉시 tick. dispatch된 잡 id 목록 반환."""
        self.jobs.enqueue(job)
        return self.tick()

    def tick(self) -> list:
        """적격 잡을 가능한 만큼 dispatch(running 표시 + 레포 점유). id 목록 반환.

        적격 = (queued) 또는 (interrupted & reset_at 도래) 이면서
               전역/유저 cap 여유 && 대상 레포 전부 unlock && 전역직렬 충돌 없음.
        """
        dispatched: list = []
        with self._lock:
            while True:
                snap = self._snapshot()
                if snap["running_count"] >= self.global_cap:
                    break
                # 전역직렬 잡이 이미 running이면 아무것도 못 뜬다.
                if snap["global_lock"]:
                    break
                picked = self._pick_eligible(snap)
                if picked is None:
                    break
                self._dispatch(picked)
                dispatched.append(picked.ticket)
        return dispatched

    def on_complete(self, job_id: str, status: str = q.DONE, **fields) -> list:
        """terminal 보고(done/failed) — 레포 락 해제 → 상태 확정 → tick.

        레포 락은 잡 상태의 함수이므로 status를 terminal로 바꾸면 자동 해제된다.
        """
        norm = q.normalize_status(status)
        if norm not in q.TERMINAL_STATUSES:
            norm = q.DONE
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, norm, **fields)
        return self.tick()

    def on_interrupt(self, job_id: str, reset_at: Optional[str] = None, **fields) -> list:
        """중단 보고(interrupted) — 레포 락 해제 → interrupted+reset_at 기록 → tick.

        이 잡은 reset_at이 지날 때까지 적격 풀에서 제외되고, 다른 잡은 진행한다.
        """
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, q.INTERRUPTED, reset_at=reset_at, **fields)
        return self.tick()

    def on_progress(self, job_id: str, **fields) -> None:
        """진행중 보고 — 상태(running) 유지, 부가 필드만 갱신(락 불변)."""
        with self._lock:
            self.jobs.update(job_id, **fields)

    def report(self, job_id: str, status: str, **fields) -> list:
        """채널 F 통합 라우터(dispatch.report_status가 사용).

        cancelled → confirm_cancelled(락+dedup 해제), terminal → on_complete,
        interrupted → on_interrupt, 그 외 → on_progress.
        """
        norm = q.normalize_status(status)
        # cancelled는 TERMINAL에 속하지만 dedup 해제까지 해야 하므로 먼저 분기.
        if norm == q.CANCELLED:
            return self.confirm_cancelled(job_id, **fields)
        if norm in q.TERMINAL_STATUSES:
            return self.on_complete(job_id, norm, **fields)
        if norm == q.INTERRUPTED:
            reset_at = fields.pop("reset_at", None)
            return self.on_interrupt(job_id, reset_at=reset_at, **fields)
        self.on_progress(job_id, **fields)
        return []

    # ------------------------------------------------------------------
    # 취소 / 재오픈 (RECURSIVE-DISPATCH §10)
    # ------------------------------------------------------------------

    def _release_dedup(self, ticket: str) -> None:
        """dedup 게이트에서 티켓 claim 해제(재오픈 대비). 게이트 없으면 no-op."""
        if self.gate is not None:
            self.gate.release(ticket)

    def cancel_job(self, job_id: str) -> list:
        """취소 신호 반영(§10.3).

        - 이미 종결(done/failed/cancelled): no-op.
        - 실행 중(running/cancelling): `cancelling` 표시 + worker 취소 플래그 세팅.
          레포 락은 **유지**한다(worker의 cancelled 회신을 기다린다). tick 안 함.
        - 큐 대기(queued/interrupted): 즉시 `cancelled`로 드롭 + dedup 해제 → tick
          (막혀 있던 다음 잡을 dispatch할 수 있음).
        """
        do_tick = False
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            if job.status in q.TERMINAL_STATUSES:
                return []
            if job.status in q.ACTIVE_STATUSES:
                # 실행 중 → worker에 취소 위임(회신 대기). 레포 락 유지.
                self.jobs.set_status(job_id, q.CANCELLING, cancel_requested=True)
                return []
            # 큐 대기(락 미점유) → 즉시 드롭 + dedup 해제.
            self.jobs.set_status(job_id, q.CANCELLED, cancel_requested=False)
            self._release_dedup(job_id)
            do_tick = True
        return self.tick() if do_tick else []

    def confirm_cancelled(self, job_id: str, **fields) -> list:
        """worker의 cancelled 회신 확정(§10.4) — 락 해제 + dedup 해제 → tick.

        레포 락은 상태의 함수이므로 cancelled로 바꾸면 자동 해제된다. dedup까지
        풀어야 재오픈(취소됨→해야할일) 때 같은 티켓을 다시 claim할 수 있다.
        """
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, q.CANCELLED, cancel_requested=False, **fields)
            self._release_dedup(job_id)
        return self.tick()

    def is_cancel_requested(self, job_id: str) -> bool:
        """worker control 폴링용 — 이 잡에 취소가 요청됐는지."""
        job = self.jobs.get(job_id)
        return bool(job and job.cancel_requested)

    def reopen(self, job: Job) -> list:
        """취소된 티켓을 재작업으로 재-enqueue(§10.4) → tick.

        호출부(status_watcher)가 gate.claim으로 dedup 재확보한 뒤 부른다. 잡 슬롯을
        새 실행으로 초기화(queued)하고 스케줄링에 다시 태운다.
        """
        with self._lock:
            self.jobs.reopen(job)
        return self.tick()

    def next_for_user(self, user: str) -> Optional[Job]:
        """그 user에게 dispatch된(running) 잡 1개 반환(worker GET /next 용).

        per-user cap=1 전제라 최대 1개. 없으면 None. 재폴링 시 같은 잡을 멱등
        반환(worker가 --resume/session_id로 이어감).
        """
        with self._lock:
            for j in self.jobs.list_jobs():
                if j.user == user and j.status == q.RUNNING:
                    return j
        return None

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict:
        """현재 활성 잡으로부터 락/카운트 상태를 재계산.

        활성 = running + cancelling. cancelling 잡은 worker가 아직 abort/롤백 중이라
        레포를 붙들고 있으므로 락·cap 점유로 센다(§10.3) — 회신(cancelled) 전까지는
        같은 레포에 다른 잡이 들어오지 못한다.
        """
        active = [j for j in self.jobs.list_jobs() if j.status in q.ACTIVE_STATUSES]
        locked_repos: set = set()
        global_lock = False
        for j in active:
            if j.target_repos:
                locked_repos.update(j.target_repos)
            else:
                # 미해석 잡이 활성 = 전역 직렬 점유.
                global_lock = True
        return {
            "running": active,
            "running_count": len(active),
            "locked_repos": locked_repos,
            "global_lock": global_lock,
            "per_user_running": Counter(j.user for j in active),
        }

    def _eligible_now(self, job: Job) -> bool:
        """상태 기준 적격(시각 조건 포함) — cap/락은 별도 판단."""
        if job.status == q.QUEUED:
            return True
        if job.status == q.INTERRUPTED:
            if not job.reset_at:
                return True
            dt = _parse_iso(job.reset_at)
            if dt is None:
                return True  # 파싱 불가 → 즉시 재적격
            return self._now() >= dt
        return False

    def _pick_eligible(self, snap: dict) -> Optional[Job]:
        """스냅샷 기준으로 dispatch 가능한 다음 잡 1개 선택(결정적 순서)."""
        for job in self.jobs.list_jobs():  # 삽입 순서 = 결정적
            if not self._eligible_now(job):
                continue
            if snap["per_user_running"].get(job.user, 0) >= self.per_user_cap:
                continue
            if not job.target_repos:
                # 전역 직렬: running이 하나도 없어야 단독 실행 가능.
                if snap["running_count"] > 0:
                    continue
            else:
                if any(repo in snap["locked_repos"] for repo in job.target_repos):
                    continue
            return job
        return None

    def _dispatch(self, job: Job) -> None:
        """잡을 running으로 표시(레포 점유). attempts 증가, reset_at 클리어."""
        self.jobs.set_status(job.ticket, q.RUNNING, attempts=job.attempts + 1)
        self.jobs.clear_fields(job.ticket, "reset_at")


# 명시적 별칭(문서/가독성).
RepoLockScheduler = Scheduler
