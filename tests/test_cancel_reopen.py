"""취소/재오픈 — 큐/스케줄러/디스패치 단위테스트(RECURSIVE-DISPATCH §10).

검증: 상태 이름 구분(완료≠취소됨), 큐대기 취소=드롭, 실행중 취소=cancelling+락유지,
확정 회신 시 락+dedup 해제, 재오픈 재-enqueue, dispatch control 채널, 필드 영속.
라이브 호출 없음.
"""

from __future__ import annotations

import pytest

from app import queue as q
from app.dispatch import DISPATCHER_KEY, Dispatcher, dispatch_bp
from app.gate import DedupGate
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config


def _sch(gate=None, per_user=5):
    return Scheduler(make_config(concurrency_per_worker=per_user), JobQueue(), gate=gate)


# --- 상태 이름 구분 ----------------------------------------------------------


def test_status_name_distinguishes_done_from_cancelled():
    # ⚠️ HAN에서 완료·취소됨은 statusCategory가 같다 → 이름으로만 구분.
    assert q.normalize_status("완료") == q.DONE
    assert q.normalize_status("취소됨") == q.CANCELLED
    assert q.DONE != q.CANCELLED
    # 둘 다 종결(레포락 해제)이지만 서로 다른 상태.
    assert q.DONE in q.TERMINAL_STATUSES
    assert q.CANCELLED in q.TERMINAL_STATUSES
    # cancelling은 활성(레포락 점유).
    assert q.CANCELLING in q.ACTIVE_STATUSES
    assert q.RUNNING in q.ACTIVE_STATUSES


def test_job_cancel_requested_roundtrips():
    j = Job(ticket="PROJ-1", cancel_requested=True)
    assert Job.from_dict(j.to_dict()).cancel_requested is True


# --- 큐 대기 취소 = 드롭 -----------------------------------------------------


def test_cancel_queued_job_drops_and_releases_dedup(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u2", target_repos=["repoA"]))  # 같은 레포 대기
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    sch.cancel_job("PROJ-2")  # 큐 대기 → 즉시 드롭
    assert sch.jobs.get("PROJ-2").status == q.CANCELLED
    assert gate.is_claimed("PROJ-2") is False          # dedup 해제
    assert sch.jobs.get("PROJ-1").status == q.RUNNING  # 무관 잡 영향 없음


# --- 실행 중 취소 = cancelling + 락 유지 → 확정 ------------------------------


def test_cancel_running_marks_cancelling_and_holds_lock(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u2", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    sch.cancel_job("PROJ-1")  # 실행 중 → worker에 위임(회신 대기)
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.CANCELLING
    assert j.cancel_requested is True
    # 레포 락 유지(cancelling이 점유) → PROJ-2 아직 대기.
    assert sch.jobs.get("PROJ-2").status == q.QUEUED
    # dedup은 회신 전까지 유지.
    assert gate.is_claimed("PROJ-1") is True


def test_confirm_cancelled_releases_lock_and_dedup(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.enqueue(Job(ticket="PROJ-2", user="u2", target_repos=["repoA"]))
    sch.cancel_job("PROJ-1")  # cancelling

    # worker의 cancelled 회신(한글 상태 + rolledback 부가 필드).
    sch.report("PROJ-1", "취소됨", rolledback=True)
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED
    assert gate.is_claimed("PROJ-1") is False              # dedup 해제(재오픈 대비)
    assert sch.jobs.get("PROJ-1").meta.get("rolledback") is True
    # 락 해제 → 대기하던 PROJ-2 dispatch.
    assert sch.jobs.get("PROJ-2").status == q.RUNNING


def test_cancel_terminal_job_is_noop(isolated_state):
    sch = _sch()
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.on_complete("PROJ-1", q.DONE)
    assert sch.cancel_job("PROJ-1") == []             # 이미 종결 → no-op
    assert sch.jobs.get("PROJ-1").status == q.DONE


# --- 재오픈 ------------------------------------------------------------------


def test_reopen_reenqueues_same_ticket(isolated_state):
    gate = DedupGate()
    gate.claim("PROJ-1")
    sch = _sch(gate=gate)
    sch.enqueue(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"], session_id="old"))
    sch.cancel_job("PROJ-1")
    sch.report("PROJ-1", "취소됨")               # cancelled + dedup 해제
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED

    sch.reopen(Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.RUNNING                 # 같은 티켓 재-dispatch
    assert j.cancel_requested is False
    assert j.session_id is None                  # 새 실행으로 초기화


# --- dispatch control 채널 --------------------------------------------------


def _wire_disp(worker_secret=""):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch, worker_secret=worker_secret)
    return reg, gate, sch, disp


def test_control_reports_cancel_flag(isolated_state):
    _, _, sch, disp = _wire_disp()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    assert disp.control("u1", "PROJ-1") == {"cancel": False}
    sch.cancel_job("PROJ-1")                       # running → cancelling + flag
    assert disp.control("u1", "PROJ-1") == {"cancel": True}
    with pytest.raises(PermissionError):
        disp.control("other", "PROJ-1")
    with pytest.raises(KeyError):
        disp.control("u1", "NOPE")


def _client(disp):
    from flask import Flask

    app = Flask(__name__)
    app.config[DISPATCHER_KEY] = disp
    app.register_blueprint(dispatch_bp)
    return app.test_client()


def test_http_control_route(isolated_state):
    _, _, sch, disp = _wire_disp(worker_secret="secret")
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    client = _client(disp)

    assert client.get("/dispatch/u1/PROJ-1/control").status_code == 401  # 인증 없음
    r = client.get("/dispatch/u1/PROJ-1/control", headers={"X-Worker-Secret": "secret"})
    assert r.status_code == 200 and r.get_json() == {"cancel": False}

    sch.cancel_job("PROJ-1")
    r = client.get("/dispatch/u1/PROJ-1/control", headers={"X-Worker-Secret": "secret"})
    assert r.get_json() == {"cancel": True}

    r = client.get("/dispatch/u1/NOPE/control", headers={"X-Worker-Secret": "secret"})
    assert r.status_code == 404
