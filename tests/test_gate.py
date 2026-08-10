"""dedup 게이트 단위테스트(멱등·영속·release)."""

from __future__ import annotations

from app.gate import DedupGate


def test_claim_idempotent(isolated_state):
    g = DedupGate()
    assert g.claim("HAN-1") is True
    assert g.claim("HAN-1") is False   # 두 번째는 흡수
    assert g.is_claimed("HAN-1") is True


def test_release_allows_reclaim(isolated_state):
    g = DedupGate()
    g.claim("HAN-2")
    g.release("HAN-2")
    assert g.is_claimed("HAN-2") is False
    assert g.claim("HAN-2") is True     # release 후 재claim 가능


def test_persistence_across_instances(isolated_state):
    g1 = DedupGate()
    g1.claim("HAN-3")
    g2 = DedupGate()                    # 재시작 시뮬(상태 재로드)
    assert g2.is_claimed("HAN-3") is True
    assert g2.claim("HAN-3") is False
