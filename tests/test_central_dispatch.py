"""central_dispatch 단위테스트 — 프랙탈 방출 seam(주입 성공/실패·관측성 기록·표식).

모든 디스패치 진입점(폴러·상태워처·재배정·rerun)이 공유하는 seam 을 격리 검증한다.
라이브 센트럴 세션/claude 없음(FakeSink). JobQueue/DedupGate 는 실제 인스턴스.
"""

from __future__ import annotations

import pytest

from app import central_dispatch as cd
from app import queue as q
from app.gate import DedupGate
from app.queue import Job, JobQueue


class _FakeSink:
    def __init__(self, ok=True, boom=False):
        self.events = []
        self._ok = ok
        self._boom = boom

    def inject_event(self, job):
        self.events.append(job)
        if self._boom:
            raise RuntimeError("pipe broken")
        return self._ok


def test_emit_success_injects_and_records_new_ticket(isolated_state):
    """주입 성공: 센트럴 세션에 이벤트 주입 + 신규 티켓을 queued+fractal 로 store 기록."""
    jq = JobQueue()
    gate = DedupGate()
    sink = _FakeSink(ok=True)
    job = Job(ticket="P-1", user="u1", target_repos=["repoA"])

    cd.emit_to_central(config=None, central_sink=sink, job_queue=jq, gate=gate, job=job)

    assert [e.ticket for e in sink.events] == ["P-1"]
    rec = jq.get("P-1")
    assert rec is not None and rec.status == q.QUEUED and rec.is_fractal is True


def test_emit_marks_existing_record_without_overwrite(isolated_state):
    """이미 store 에 있는 티켓(재오픈/재배정/rerun 리셋 후): enqueue 로 덮지 않고 fractal 표식만 갱신."""
    jq = JobQueue()
    gate = DedupGate()
    sink = _FakeSink(ok=True)
    # 선행 레코드(예: reopen 이 리셋해 둔 queued 슬롯) — 아직 fractal 표식 없음.
    jq.enqueue(Job(ticket="P-2", user="u1", target_repos=["repoA"], branch="auto/P-2"))
    assert jq.get("P-2").is_fractal is False

    job = jq.get("P-2")
    cd.emit_to_central(config=None, central_sink=sink, job_queue=jq, gate=gate, job=job)

    rec = jq.get("P-2")
    assert rec.is_fractal is True                 # 표식 갱신됨
    assert rec.branch == "auto/P-2"               # 기존 필드 보존(덮어쓰기 없음)


def test_emit_inject_false_releases_claim_and_raises(isolated_state):
    """주입 False: 레거시 폴백 없이 CentralInjectFailed + dedup claim 되돌림(재트리거 가능)."""
    jq = JobQueue()
    gate = DedupGate()
    gate.claim("P-3")
    sink = _FakeSink(ok=False)
    job = Job(ticket="P-3", user="u1", target_repos=["repoA"])

    with pytest.raises(cd.CentralInjectFailed):
        cd.emit_to_central(config=None, central_sink=sink, job_queue=jq, gate=gate, job=job)
    assert gate.is_claimed("P-3") is False        # claim 되돌림
    assert jq.get("P-3") is None                  # 관측성 레코드도 남기지 않음(주입 실패)


def test_emit_inject_exception_releases_claim_and_raises(isolated_state):
    """주입 예외(파이프 깨짐 등): 예외를 CentralInjectFailed 로 감싸 올리고 claim 되돌림."""
    jq = JobQueue()
    gate = DedupGate()
    gate.claim("P-4")
    sink = _FakeSink(boom=True)
    job = Job(ticket="P-4", user="u1", target_repos=["repoA"])

    with pytest.raises(cd.CentralInjectFailed):
        cd.emit_to_central(config=None, central_sink=sink, job_queue=jq, gate=gate, job=job)
    assert gate.is_claimed("P-4") is False


def test_mark_fractal_sets_flag_on_existing(isolated_state):
    """mark_fractal: 기존 레코드에 표식만 붙인다 — 부재면 무해 no-op."""
    jq = JobQueue()
    jq.enqueue(Job(ticket="P-5", user="u1", target_repos=["repoA"]))
    cd.mark_fractal(jq, "P-5")
    assert jq.get("P-5").is_fractal is True
    # 부재 티켓 — 예외 없이 no-op.
    cd.mark_fractal(jq, "P-404")
    assert jq.get("P-404") is None


def test_central_active_false_when_no_sink(isolated_state):
    """central_active: sink 미주입이면 무조건 False(레거시 배포 — 호출부가 구 경로)."""
    assert cd.central_active(config=object(), central_sink=None) is False
