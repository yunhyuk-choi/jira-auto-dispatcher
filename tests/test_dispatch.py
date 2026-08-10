"""dispatch 단위테스트 — enqueue(스케줄러 경유)·next·status·인증·교차유저."""

from __future__ import annotations

from app import queue as q
from app.dispatch import DISPATCHER_KEY, Dispatcher, dispatch_bp
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config


def _wire(worker_secret="", per_user=5):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(concurrency_per_worker=per_user), JobQueue())
    disp = Dispatcher(reg, sch, worker_secret=worker_secret)
    return reg, sch, disp


def test_enqueue_goes_through_scheduler(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    j = sch.jobs.get("PROJ-1")
    assert j.user == "u1" and j.status == q.RUNNING   # 스케줄러가 즉시 dispatch


def test_next_job_returns_running_for_user(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    got = disp.next_job("u1")
    assert got is not None and got.ticket == "PROJ-1"
    assert disp.next_job("other") is None


def test_report_status_terminal_triggers_on_complete(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoA"]))  # 같은 레포 대기
    assert sch.jobs.get("PROJ-2").status == q.QUEUED
    disp.report_status("u1", "PROJ-1", {"status": "완료", "mr_url": "http://mr"})
    assert sch.jobs.get("PROJ-1").status == q.DONE
    assert sch.jobs.get("PROJ-1").mr_url == "http://mr"
    assert sch.jobs.get("PROJ-2").status == q.RUNNING   # 완료-구동 다음 dispatch


def test_report_status_interrupted_records_reset_at(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.report_status("u1", "PROJ-1", {"status": "interrupted", "reset_at": "2099-01-01T00:00:00Z"})
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.INTERRUPTED and j.reset_at == "2099-01-01T00:00:00Z"


def test_cross_user_claim_rejected(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    try:
        disp.report_status("other", "PROJ-1", {"status": "완료"})
        assert False, "교차 유저 보고가 거부되지 않음"
    except PermissionError:
        pass


# --- Flask 라우트/인증 ---


def _app(disp):
    from flask import Flask
    app = Flask(__name__)
    app.config[DISPATCHER_KEY] = disp
    app.register_blueprint(dispatch_bp)
    return app.test_client()


def test_http_next_and_status_with_auth(isolated_state):
    _, sch, disp = _wire(worker_secret="secret")
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    client = _app(disp)

    # 인증 없음 → 401
    assert client.get("/dispatch/u1/next").status_code == 401
    # 인증 OK → 잡 반환
    r = client.get("/dispatch/u1/next", headers={"X-Worker-Secret": "secret"})
    assert r.status_code == 200 and r.get_json()["ticket"] == "PROJ-1"
    # 다른 유저는 204
    assert client.get("/dispatch/nobody/next", headers={"X-Worker-Secret": "secret"}).status_code == 204
    # status 회신
    r = client.post("/dispatch/u1/PROJ-1/status", headers={"X-Worker-Secret": "secret"},
                    json={"status": "완료"})
    assert r.status_code == 200
    assert sch.jobs.get("PROJ-1").status == q.DONE


def test_http_unknown_job_404(isolated_state):
    _, sch, disp = _wire()
    client = _app(disp)
    r = client.post("/dispatch/u1/NOPE/status", json={"status": "완료"})
    assert r.status_code == 404
