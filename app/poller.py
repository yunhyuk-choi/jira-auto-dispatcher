"""High-watermark JQL 폴러.

역할:
    poll_interval_sec 마다 Jira에 JQL을 던져 "지정 사용자에게 새로 할당된
    트리거 상태" 티켓을 찾아, 각각을 dedup 게이트로 넘긴다(gate.claim()).
    high-watermark(마지막 updated 커서)를 state에 영속해 중복/누락을 줄인다.

구현 Phase: **Phase 4** (폴러 + 웹훅).

흐름:
    1. watermark 로드
    2. JQL: project=HAN AND assignee in (<account_id...>)
       AND status in (<match.statuses>) AND updated >= <watermark>
    3. 각 이슈에 대해 gate.claim(key) → True면 queue.enqueue(Job)
    4. watermark 전진 후 영속

참고:
    - 백그라운드 스레드로 상시 구동(main.py가 기동).
    - 웹훅과 동일하게 반드시 gate를 통과(직접 큐잉 금지).
"""

from __future__ import annotations


class Poller:
    """high-watermark JQL 폴러(스텁)."""

    def __init__(self, config, jira_client, gate, job_queue) -> None:
        """의존성 주입(설정·Jira 클라이언트·게이트·큐).

        TODO(Phase 4): 참조 보관 + watermark 초기 로드.
        """
        self.config = config
        self.jira = jira_client
        self.gate = gate
        self.queue = job_queue
        self._stop = None  # threading.Event (Phase 4)

    def build_jql(self) -> str:
        """트리거 조건 + watermark로 JQL 문자열 구성.

        TODO(Phase 4): assignee/status/updated 절 조립.
        """
        raise NotImplementedError("TODO(Phase 4): build_jql")

    def poll_once(self) -> int:
        """1회 폴링 — 새 티켓을 claim/enqueue하고 처리 건수 반환.

        TODO(Phase 4): search_jql → 페이지네이션 → claim → enqueue → watermark.
        """
        raise NotImplementedError("TODO(Phase 4): poll_once")

    def run_forever(self) -> None:
        """poll_interval_sec 간격 루프(백그라운드 스레드 진입점).

        TODO(Phase 4): stop 이벤트까지 poll_once 반복 + 예외 격리.
        """
        raise NotImplementedError("TODO(Phase 4): run_forever")

    def stop(self) -> None:
        """루프 정지 신호.

        TODO(Phase 4): stop 이벤트 set.
        """
        raise NotImplementedError("TODO(Phase 4): stop")
