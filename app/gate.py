"""원자적 dedup 게이트 — 폴러·웹훅의 수렴점.

역할:
    같은 티켓이 폴러와 웹훅 양쪽에서(또는 폴링 주기가 겹쳐) 중복 트리거되는
    것을 막는 **단일 원자적 관문**. claim(ticket)이 최초 1회만 True를 반환하고,
    이후 동일 티켓은 False. 성공한 claim만 큐에 잡을 넣는다.

구현 Phase: **Phase 3** (dedup 게이트 + 큐).

핵심 설계 불변식(CLAUDE.md 참조):
    - 폴러·웹훅 둘 다 반드시 이 게이트를 통과한다(직접 큐잉 금지).
    - dedup 집합은 state.py로 영속(재시작 후에도 중복 방지).
    - 스레드 안전(락)해야 한다 — 폴러 스레드 + 웹훅 요청 스레드 동시 접근.
"""

from __future__ import annotations


class DedupGate:
    """티켓 단위 원자적 claim 게이트(스텁)."""

    def __init__(self) -> None:
        """락 + 영속된 dedup 집합 로드로 초기화.

        TODO(Phase 3): threading.Lock + state.load(dedup) 복원.
        """
        pass

    def claim(self, ticket: str) -> bool:
        """티켓을 원자적으로 claim. 최초 1회만 True.

        TODO(Phase 3): 락 안에서 집합 검사→추가→영속. 최초만 True 반환.
        """
        raise NotImplementedError("TODO(Phase 3): claim")

    def release(self, ticket: str) -> None:
        """claim 해제(가역성 — 사고 시 재처리 허용).

        TODO(Phase 3): 락 안에서 집합 제거→영속.
        """
        raise NotImplementedError("TODO(Phase 3): release")

    def is_claimed(self, ticket: str) -> bool:
        """이미 claim 됐는지 조회.

        TODO(Phase 3): 락 안에서 멤버십 조회.
        """
        raise NotImplementedError("TODO(Phase 3): is_claimed")
