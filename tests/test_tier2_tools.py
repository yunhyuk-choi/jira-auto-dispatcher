"""Phase 3b-2 — Tier-2 자원툴 레이어 단위테스트.

커버:
    - read_state: 3b-0 스냅샷 위임(읽기 전용).
    - eligible_tickets/my_running: **이 파일럿 사용자**만 필터(격리).
    - dispatch: register_pending + tick(파이썬 강제) 조합, 레포락 시 defer.
    - **교차 사용자 방어**: 다른 사용자 티켓 dispatch는 ValueError(격리 불변식).
    - collect/collect_one: 3b-1 명시적 id 결과 회수(각 id 정확 매칭).
"""

from __future__ import annotations

from app import queue as q
from app.dispatch import Dispatcher
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from app.tier2_tools import Tier2Tools
from tests.conftest import make_config


class _FakeWriter:
    def __init__(self, relpath="n/cycles/t2"):
        self.relpath = relpath

    def commit_cycle_log(self, job):
        return self.relpath


def _wire(writer=None):
    reg = Registry()
    reg.upsert(UserRecord(username="pilot", jira_account_id="a1", enabled=True))
    reg.upsert(UserRecord(username="other", jira_account_id="a2", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    disp = Dispatcher(reg, sch, dlc_meta_writer=writer)
    return reg, sch, disp


# --- READ: per-user 필터/격리 ---


def test_eligible_tickets_filters_to_pilot_user(isolated_state):
    _, sch, disp = _wire()
    # pilot 2건, other 1건 enqueue. 서로 다른 레포 → 즉시 running(적격에서 빠짐)이 되지
    # 않도록, 같은 레포로 큐잉해 queued 대기분을 만든다.
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    disp.enqueue("pilot", Job(ticket="P-2", target_repos=["repoA"]))   # 같은 레포 → queued
    disp.enqueue("other", Job(ticket="O-1", target_repos=["repoA"]))   # 같은 레포 → queued

    tools = Tier2Tools(disp, "pilot")
    elig = tools.eligible_tickets()
    tickets = {e["ticket"] for e in elig}
    # queued 대기분(P-2)만 적격에 뜨고 other(O-1)는 애초에 안 보인다. P-1은 running.
    assert "O-1" not in tickets
    assert all(e["user"] == "pilot" for e in elig)


def test_read_state_is_snapshot_passthrough(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    tools = Tier2Tools(disp, "pilot")
    snap = tools.read_state()
    # 3b-0 스냅샷 키 계약 그대로.
    assert "running" in snap and "eligible" in snap and "resource" in snap
    # 순수 위임(부작용 0) — generated_at(호출 시각)·resource(라이브 메모리/로드 프로브)는
    # 호출마다 값이 달라지므로 제외하고, 결정적 스케줄 구조만 동일함을 확인한다.
    _volatile = {"generated_at", "resource"}
    a = {k: v for k, v in snap.items() if k not in _volatile}
    b = {k: v for k, v in sch.state_snapshot().items() if k not in _volatile}
    assert a == b


def test_my_running_filters_to_user(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    disp.enqueue("other", Job(ticket="O-1", target_repos=["repoB"]))
    tools = Tier2Tools(disp, "pilot")
    running = tools.my_running()
    assert [r["ticket"] for r in running] == ["P-1"]


# --- DISPATCH: 제안 + 파이썬 강제 + 교차사용자 방어 ---


def test_dispatch_registers_pending_and_marks_running(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    tools = Tier2Tools(disp, "pilot")
    res = tools.dispatch("P-1")
    assert res["correlation_id"] == "P-1"
    assert res["dispatched"] is True and res["deferred"] is False
    assert res["job_status"] == q.RUNNING
    # pending 등록됨(3b-1) → collect로 회수 가능.
    assert "P-1" in disp.pending.pending_ids()


def test_dispatch_defers_under_repo_lock(isolated_state):
    _, sch, disp = _wire()
    # 같은 레포 2건 → 하나는 running, 하나는 레포락으로 defer.
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    disp.enqueue("pilot", Job(ticket="P-2", target_repos=["repoA"]))
    tools = Tier2Tools(disp, "pilot")
    res = tools.dispatch("P-2")   # repoA는 P-1이 점유
    assert res["deferred"] is True
    assert res["job_status"] != q.RUNNING
    # 그래도 pending 등록은 됨(완료 회수 대상).
    assert "P-2" in disp.pending.pending_ids()


def test_dispatch_cross_user_rejected(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("other", Job(ticket="O-1", target_repos=["repoA"]))
    tools = Tier2Tools(disp, "pilot")
    try:
        tools.dispatch("O-1")
        assert False, "교차 사용자 dispatch가 거부되지 않음"
    except ValueError:
        pass
    # other 잡은 pending으로 등록되지 않았다(격리 — 파일럿이 안 건드림).
    assert "O-1" not in disp.pending.pending_ids()


def test_dispatch_unknown_ticket_raises(isolated_state):
    _, _, disp = _wire()
    tools = Tier2Tools(disp, "pilot")
    try:
        tools.dispatch("GHOST")
        assert False
    except KeyError:
        pass


# --- COLLECT: 명시적 id 결과 회수(3b-1) ---


def test_collect_by_id_after_terminal(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/done"))
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    tools = Tier2Tools(disp, "pilot")
    tools.dispatch("P-1")
    assert tools.collect_one("P-1")["status"] == "pending"

    disp.report_status("pilot", "P-1", {"status": "완료", "mr_url": "http://mr/1"})
    got = tools.collect(["P-1"])
    assert got["P-1"]["status"] == "done"
    assert got["P-1"]["result"]["mr_url"] == "http://mr/1"
    assert got["P-1"]["result"]["cycle_log_path"] == "n/cycles/done"
    # 해소 후 pending에서 빠짐.
    assert tools.pending_ids() == []


def test_pending_ids_filters_to_user(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    disp.enqueue("other", Job(ticket="O-1", target_repos=["repoB"]))
    # 두 사용자 각각 위임 등록.
    disp.register_pending("P-1")
    disp.register_pending("O-1")
    tools = Tier2Tools(disp, "pilot")
    # 파일럿 것만 보인다(격리).
    assert tools.pending_ids() == ["P-1"]
