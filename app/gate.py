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

import threading

from app import state


class DedupGate:
    """티켓 단위 원자적 claim 게이트."""

    def __init__(self) -> None:
        """락 + 영속된 dedup 집합 로드로 초기화."""
        self._lock = threading.Lock()
        loaded = state.load_dedup([])
        self._claimed: set[str] = set(loaded or [])

    def _persist(self) -> None:
        # 결정적 순서로 저장(디프 안정성).
        state.save_dedup(sorted(self._claimed))

    def claim(self, ticket: str) -> bool:
        """티켓을 원자적으로 claim. 최초 1회만 True.

        이미 claim 된 티켓이면 False(중복 트리거 흡수).
        """
        with self._lock:
            if ticket in self._claimed:
                return False
            self._claimed.add(ticket)
            self._persist()
            return True

    def release(self, ticket: str) -> None:
        """claim 해제(가역성 — 사고/미매핑 시 재처리 허용)."""
        with self._lock:
            if ticket in self._claimed:
                self._claimed.discard(ticket)
                self._persist()

    def is_claimed(self, ticket: str) -> bool:
        """이미 claim 됐는지 조회."""
        with self._lock:
            return ticket in self._claimed
