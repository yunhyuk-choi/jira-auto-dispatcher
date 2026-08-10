"""High-watermark JQL 폴러(중앙 전용).

역할:
    poll_interval_sec 마다 Jira에 JQL을 던져 "등록 사용자들에게 새로 할당된
    트리거 상태" 티켓을 찾아, dedup 게이트를 통과시킨 뒤(gate.claim), 담당자
    account_id를 레지스트리로 사용자에 매핑하고(enabled 사용자만) 그 사용자
    큐에 디스패치한다(dispatch.enqueue). high-watermark(마지막 updated 커서)를
    state에 영속해 중복/누락을 줄인다.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다).

구현 Phase: **Phase 4** (폴러 + 웹훅).

흐름:
    1. watermark 로드
    2. JQL: project in (<등록 프로젝트>) AND assignee in (<등록 account_id...>)
       AND status in (<match.statuses>) AND updated >= <watermark>
    3. 각 이슈에 대해:
         a. gate.claim(key) → False면 skip(중복)
         b. registry.find_by_account_id(담당자) → 미등록/비활성이면 skip + 로그
            (게이트는 claim 됐으므로 필요 시 release로 되돌릴지 정책 결정)
         c. dispatch.enqueue(user, Job(ticket=key, ...))
    4. watermark 전진 후 영속

참고:
    - 백그라운드 스레드로 상시 구동(main.py의 central 분기가 기동).
    - 웹훅과 동일하게 반드시 gate를 통과한 뒤 매핑/디스패치(직접 큐잉 금지).
    - 매핑 실패/미등록/비활성 사용자면 enqueue하지 않고 로그만 남긴다.
"""

from __future__ import annotations


class Poller:
    """high-watermark JQL 폴러(스텁)."""

    def __init__(self, config, jira_client, gate, registry, dispatcher) -> None:
        """의존성 주입(설정·Jira 클라이언트·게이트·레지스트리·디스패처).

        TODO(Phase 4): 참조 보관 + watermark 초기 로드.
        """
        self.config = config
        self.jira = jira_client
        self.gate = gate
        self.registry = registry
        self.dispatcher = dispatcher
        self._stop = None  # threading.Event (Phase 4)

    def build_jql(self) -> str:
        """트리거 조건 + watermark로 JQL 문자열 구성.

        TODO(Phase 4): 등록 사용자 account_id 집합 + status + updated 절 조립.
        (registry.list_users의 enabled 사용자 account_id로 assignee in (...))
        """
        raise NotImplementedError("TODO(Phase 4): build_jql")

    def resolve_user(self, issue) -> object:
        """이슈 담당자 account_id를 등록 사용자로 매핑(없거나 비활성이면 None).

        TODO(Phase 4): assignee.accountId 추출 → registry.find_by_account_id →
        enabled 확인. 미등록/비활성이면 None(호출부가 skip+로그).
        """
        raise NotImplementedError("TODO(Phase 4): resolve_user")

    def poll_once(self) -> int:
        """1회 폴링 — claim → 사용자 매핑 → 디스패치하고 처리 건수 반환.

        TODO(Phase 4): search_jql → 페이지네이션 → gate.claim → resolve_user →
        dispatch.enqueue(user, job) → watermark 전진. 미매핑은 skip+로그.
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
