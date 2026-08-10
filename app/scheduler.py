"""재개 스케줄러 + 야간 드레인 배치(중앙 전용).

역할:
    1) interrupted 잡의 reset_at(파싱된 한도 리셋시각 + reset_buffer_sec)에
       맞춰 재개를 예약한다 — 재개 = 해당 사용자 큐에 잡을 다시 넣는 것
       (dispatch.enqueue). 그 사용자의 worker가 --resume으로 이어간다.
    2) 야간 드레인 창(nightly_drain)에는 백로그(interrupted/queued)를 몰아서
       재-enqueue한다. 업무시간(work_hours)에는 실시간 처리를 우선한다.

역할 소속: **central** (실제 실행은 각 사용자 worker가 한다).

구현 Phase: **Phase 5** (스케줄러).

구현 기반: APScheduler(BackgroundScheduler), timezone=resume.timezone.

참고:
    - 재개는 워커를 직접 부르지 않고 dispatcher를 통해 재-enqueue한다
      (중앙은 잡만 되살리고, 실행은 사용자 worker가 HTTP로 가져간다).
    - UI의 "지금 재개" 버튼도 동일 경로(resume_now → dispatcher 재-enqueue).
"""

from __future__ import annotations

from typing import Optional


class ResumeScheduler:
    """리셋시각 재개 + 야간 드레인 스케줄러(스텁)."""

    def __init__(self, config, dispatcher) -> None:
        """의존성 주입(설정·디스패처) + APScheduler 준비.

        TODO(Phase 5): BackgroundScheduler(timezone) 생성.
        """
        self.config = config
        self.dispatcher = dispatcher
        self._scheduler = None  # apscheduler BackgroundScheduler (Phase 5)

    def start(self) -> None:
        """스케줄러 기동 + 야간 드레인 크론 잡 등록.

        TODO(Phase 5): scheduler.start() + nightly_drain 크론 트리거 등록.
        """
        raise NotImplementedError("TODO(Phase 5): start")

    def schedule_resume(self, user: str, ticket: str, reset_at: str) -> None:
        """특정 잡을 reset_at(+buffer)에 재개(사용자 큐 재-enqueue) 예약.

        TODO(Phase 5): DateTrigger(reset_at + reset_buffer_sec)로
        dispatcher.enqueue(user, resume_job) 예약.
        """
        raise NotImplementedError("TODO(Phase 5): schedule_resume")

    def drain_backlog(self) -> None:
        """야간 드레인 — interrupted/queued 백로그를 순차 재-enqueue.

        TODO(Phase 5): 드레인 창 내 잡을 사용자별로 재-enqueue.
        """
        raise NotImplementedError("TODO(Phase 5): drain_backlog")

    def resume_now(self, user: Optional[str] = None, ticket: Optional[str] = None) -> None:
        """수동 재개(UI 버튼) — 지정 사용자/잡 또는 전체 재-enqueue.

        TODO(Phase 5): dispatcher.enqueue로 재개 대상 재투입.
        """
        raise NotImplementedError("TODO(Phase 5): resume_now")

    def shutdown(self) -> None:
        """스케줄러 종료.

        TODO(Phase 5): scheduler.shutdown().
        """
        raise NotImplementedError("TODO(Phase 5): shutdown")
