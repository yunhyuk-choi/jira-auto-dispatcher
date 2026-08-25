"""취소 사유(cancel_reason) 단위테스트 — 취소됨(상태) vs 추적해제(라벨) 구별.

핵심 요구: cancelled 잡이 *왜* 취소됐는지 구별한다. status_cancelled(Jira 상태=취소됨)와
untracked_optout(opt-out 라벨)이 **반드시 다른 값**으로 기록돼야 한다(관리 UI 표시용).
재배정 park는 reassigned로 구별한다. 라이브 Jira/네트워크 없음(FakeJira·주입).
"""

from __future__ import annotations

from app import queue as q
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import REASSIGN_PARKED, Scheduler
from app.status_watcher import StatusWatcher
from tests.conftest import make_config
from tests.test_status_watcher import LabelFakeJira, _issue, _wire


# --- 모델 라운드트립 --------------------------------------------------------


def test_cancel_reason_roundtrips():
    j = Job(ticket="PROJ-1", cancel_reason=q.CANCEL_STATUS_CANCELLED)
    assert Job.from_dict(j.to_dict()).cancel_reason == q.CANCEL_STATUS_CANCELLED
    # 기본은 None(취소되지 않은 잡).
    assert Job.from_dict(Job(ticket="PROJ-2").to_dict()).cancel_reason is None


def test_cancel_reasons_are_distinct():
    # 사용자 핵심 요구: 취소됨(상태) ≠ 추적해제(라벨) ≠ 재배정 ≠ 수동.
    reasons = {q.CANCEL_STATUS_CANCELLED, q.CANCEL_UNTRACKED_OPTOUT,
               q.CANCEL_REASSIGNED, q.CANCEL_MANUAL, q.CANCEL_OTHER}
    assert len(reasons) == 5


# --- scheduler.cancel_job 사유 기록 -----------------------------------------


def _sch(gate=None):
    return Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)


def test_cancel_queued_records_reason(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u2", target_repos=["repoA"]))  # 같은 레포 → 큐 대기
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    sch.cancel_job("PROJ-2", reason=q.CANCEL_STATUS_CANCELLED)
    j = sch.jobs.get("PROJ-2")
    assert j.status == q.CANCELLED
    assert j.cancel_reason == q.CANCEL_STATUS_CANCELLED


def test_cancel_running_reason_preserved_through_confirm(isolated_state):
    # 실행 중 취소는 cancelling에서 사유를 선기록하고, worker의 cancelled 회신 확정 시
    # 상태만 종결로 바꾸며 사유는 그대로 보존한다.
    gate = DedupGate()
    gate.claim("PROJ-1")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    sch.cancel_job("PROJ-1", reason=q.CANCEL_UNTRACKED_OPTOUT)  # running → cancelling
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING
    assert sch.jobs.get("PROJ-1").cancel_reason == q.CANCEL_UNTRACKED_OPTOUT

    sch.report("PROJ-1", "취소됨")  # worker 확정 회신
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.CANCELLED
    assert j.cancel_reason == q.CANCEL_UNTRACKED_OPTOUT  # 사유 보존


def test_cancel_without_reason_leaves_none(isolated_state):
    # reason 미지정(기존 호출 시맨틱) → cancel_reason은 None으로 남는다(회귀 방지).
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u2", target_repos=["repoA"]))
    sch.cancel_job("PROJ-2")
    assert sch.jobs.get("PROJ-2").status == q.CANCELLED
    assert sch.jobs.get("PROJ-2").cancel_reason is None


def test_reassign_park_records_reassigned(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u1", target_repos=["repoA"]))  # 큐 대기(WIP 없음)
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    # Y 미가용(enabled=False) → 드롭 + park.
    sig = sch.reassign_or_handoff("PROJ-2", "u2", enabled=False)
    assert sig == REASSIGN_PARKED
    j = sch.jobs.get("PROJ-2")
    assert j.status == q.CANCELLED
    assert j.cancel_reason == q.CANCEL_REASSIGNED


def test_rerun_clears_stale_cancel_reason(isolated_state):
    # 재실행(reopen)으로 되살린 잡은 이전 취소 사유를 남기지 않는다.
    gate = DedupGate()
    gate.claim("PROJ-1")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.cancel_job("PROJ-1", reason=q.CANCEL_STATUS_CANCELLED)
    sch.report("PROJ-1", "취소됨")
    assert sch.jobs.get("PROJ-1").cancel_reason == q.CANCEL_STATUS_CANCELLED

    sch.rerun("PROJ-1")
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.RUNNING
    assert j.cancel_reason is None


# --- status_watcher: 취소됨(상태) vs 추적해제(라벨) 구별 ---------------------


def test_status_cancel_sets_status_cancelled_reason(isolated_state):
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))  # running
    jira.add("취소됨", _issue("PROJ-1"))
    res = watcher.poll_once()
    assert res["cancelled"] == 1
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.CANCELLING  # running → worker 위임(회신 대기)
    assert j.cancel_reason == q.CANCEL_STATUS_CANCELLED


def test_optout_sets_untracked_optout_reason_distinct(isolated_state):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))

    jira = LabelFakeJira([_issue("PROJ-1", status="해야 할 일",
                                 labels=["자동화_추적_해제"])])
    watcher = StatusWatcher(make_config(), jira, gate, reg, disp)
    res = watcher.poll_once()
    assert res["optout"] == 1
    j = sch.jobs.get("PROJ-1")
    assert j.cancel_reason == q.CANCEL_UNTRACKED_OPTOUT
    # 핵심: 취소됨(상태)과 추적해제(라벨) 사유가 반드시 다르다.
    assert q.CANCEL_UNTRACKED_OPTOUT != q.CANCEL_STATUS_CANCELLED
