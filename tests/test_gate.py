"""dedup 게이트 단위테스트(멱등·영속·release)."""

from __future__ import annotations

from app.gate import DedupGate


def test_claim_idempotent(isolated_state):
    g = DedupGate()
    assert g.claim("PROJ-1") is True
    assert g.claim("PROJ-1") is False   # 두 번째는 흡수
    assert g.is_claimed("PROJ-1") is True


def test_release_allows_reclaim(isolated_state):
    g = DedupGate()
    g.claim("PROJ-2")
    g.release("PROJ-2")
    assert g.is_claimed("PROJ-2") is False
    assert g.claim("PROJ-2") is True     # release 후 재claim 가능


def test_persistence_across_instances(isolated_state):
    g1 = DedupGate()
    g1.claim("PROJ-3")
    g2 = DedupGate()                    # 재시작 시뮬(상태 재로드)
    assert g2.is_claimed("PROJ-3") is True
    assert g2.claim("PROJ-3") is False
