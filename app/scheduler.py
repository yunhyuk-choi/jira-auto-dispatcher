"""재개 스케줄러 + 야간 드레인 배치.

역할:
    1) interrupted 잡의 reset_at(파싱된 한도 리셋시각 + reset_buffer_sec)에
       맞춰 재개를 예약한다.
    2) 야간 드레인 창(nightly_drain)에는 백로그를 몰아서 처리한다.
    업무시간(work_hours)에는 실시간 처리를 우선한다.

구현 Phase: **Phase 5** (워커 + 스케줄러).

구현 기반: APScheduler(BackgroundScheduler), timezone=resume.timezone.

참고:
    - 재개는 워커의 run_job(resume=True)로 위임.
    - UI의 "지금 재개" 버튼도 동일 경로를 호출(수동 트리거).
"""

from __future__ import annotations

from typing import Optional


class ResumeScheduler:
    """리셋시각 재개 + 야간 드레인 스케줄러(스텁)."""

    def __init__(self, config, job_queue, worker) -> None:
        """의존성 주입(설정·큐·워커) + APScheduler 준비.

        TODO(Phase 5): BackgroundScheduler(timezone) 생성.
        """
        self.config = config
        self.queue = job_queue
        self.worker = worker
        self._scheduler = None  # apscheduler BackgroundScheduler (Phase 5)

    def start(self) -> None:
        """스케줄러 기동 + 야간 드레인 크론 잡 등록.

        TODO(Phase 5): scheduler.start() + nightly_drain 크론 트리거 등록.
        """
        raise NotImplementedError("TODO(Phase 5): start")

    def schedule_resume(self, ticket: str, reset_at: str) -> None:
        """특정 잡을 reset_at(+buffer)에 재개하도록 일회성 예약.

        TODO(Phase 5): DateTrigger(reset_at + reset_buffer_sec) 등록.
        """
        raise NotImplementedError("TODO(Phase 5): schedule_resume")

    def drain_backlog(self) -> None:
        """야간 드레인 — interrupted/queued 백로그를 순차 재개.

        TODO(Phase 5): 드레인 창 내 잡 순차 처리.
        """
        raise NotImplementedError("TODO(Phase 5): drain_backlog")

    def resume_now(self, ticket: Optional[str] = None) -> None:
        """수동 재개(UI 버튼) — 지정 잡 또는 전체 재개.

        TODO(Phase 5): worker.run_job(resume=True) 호출.
        """
        raise NotImplementedError("TODO(Phase 5): resume_now")

    def shutdown(self) -> None:
        """스케줄러 종료.

        TODO(Phase 5): scheduler.shutdown().
        """
        raise NotImplementedError("TODO(Phase 5): shutdown")
