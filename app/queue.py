"""잡 스토어 + 상태머신(중앙 전용).

역할:
    claim 된 티켓을 잡(Job)으로 만들어 보관하고, 상태를 관리한다. 잡은 소유
    사용자(user)로 태깅되며, dispatch.py가 이 스토어를 사용자별 큐로 인덱싱해
    worker에 HTTP로 넘긴다. 재개 스케줄러가 interrupted 잡을 되살린다.

역할 소속: **central** (dispatch.py의 하부 저장/상태 계층).

구현 Phase: **Phase 3** (dedup 게이트 + 큐).

상태머신:
    queued      대기(claim 직후)
    running     워커 실행 중
    interrupted 토큰 한도 등으로 중단(reset_at 이후 재개 대상)
    done        완료
    failed      복구 불가 실패

참고:
    - 잡은 state.py(JOBS_FILE)로 영속 → 재시작 후 running/interrupted 복원.
    - concurrency=1 전제(자율 에이전트 폭주 방지).
    - 결정적 브랜치(auto/<TICKET>)로 잡 멱등성 보장.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# 잡 상태 상수
QUEUED = "queued"
RUNNING = "running"
INTERRUPTED = "interrupted"
DONE = "done"
FAILED = "failed"


@dataclass
class Job:
    """단일 디스패치 잡."""

    ticket: str = ""
    user: str = ""                     # 소유 사용자(DISPATCH_USER) — per-user 귀속
    status: str = QUEUED
    session_id: Optional[str] = None   # claude --session-id (재개 키)
    branch: Optional[str] = None       # auto/<TICKET>
    reset_at: Optional[str] = None     # interrupted 시 재개 예정 시각
    mr_url: Optional[str] = None       # 완료 시 worker가 회신하는 MR URL
    attempts: int = 0
    meta: dict = field(default_factory=dict)


class JobQueue:
    """영속 잡 스토어 + 상태 전이(스텁)."""

    def __init__(self) -> None:
        """락 + 영속된 잡 목록 로드로 초기화.

        TODO(Phase 3): threading.Lock + state.load(jobs) 복원.
        """
        pass

    def enqueue(self, job: Job) -> None:
        """새 잡을 queued 상태로 등록.

        TODO(Phase 3): 락 안에서 추가→영속.
        """
        raise NotImplementedError("TODO(Phase 3): enqueue")

    def next_queued(self, user: Optional[str] = None) -> Optional[Job]:
        """실행 대기 잡 하나를 반환(user 지정 시 그 사용자 것만, 없으면 None).

        TODO(Phase 3): (user 필터) queued(또는 재개 대상 interrupted) 중 하나.
        """
        raise NotImplementedError("TODO(Phase 3): next_queued")

    def set_status(self, ticket: str, status: str, **fields) -> None:
        """잡 상태/부가 필드 갱신 후 영속.

        TODO(Phase 3): 상태 전이 검증 + 영속.
        """
        raise NotImplementedError("TODO(Phase 3): set_status")

    def get(self, ticket: str) -> Optional[Job]:
        """티켓으로 잡 조회.

        TODO(Phase 3): 락 안에서 조회.
        """
        raise NotImplementedError("TODO(Phase 3): get")

    def list_jobs(self) -> list:
        """전체 잡 목록(관리 UI 현황용).

        TODO(Phase 3): 잡 목록 스냅샷 반환.
        """
        raise NotImplementedError("TODO(Phase 3): list_jobs")
