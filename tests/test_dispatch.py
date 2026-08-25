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


def test_next_job_exclude_serves_distinct_jobs(isolated_state):
    # per-user 동시(Increment 2): exclude로 이미 처리 중인 잡을 빼고 다른 running 잡을 준다.
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoB"]))   # 다른 레포 → 둘 다 running
    assert disp.next_job("u1").ticket == "PROJ-1"
    assert disp.next_job("u1", exclude={"PROJ-1"}).ticket == "PROJ-2"
    assert disp.next_job("u1", exclude={"PROJ-1", "PROJ-2"}) is None


def test_http_next_exclude_query(isolated_state):
    from flask import Flask
    _, sch, disp = _wire(worker_secret="secret")
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoB"]))
    app = Flask(__name__)
    app.config[DISPATCHER_KEY] = disp
    app.register_blueprint(dispatch_bp)
    client = app.test_client()
    hdr = {"X-Worker-Secret": "secret"}
    r = client.get("/dispatch/u1/next", headers=hdr)
    assert r.get_json()["ticket"] == "PROJ-1"
    # 처리 중(PROJ-1)을 배제하면 다른 running 잡(PROJ-2)을 준다.
    r2 = client.get("/dispatch/u1/next?exclude=PROJ-1", headers=hdr)
    assert r2.get_json()["ticket"] == "PROJ-2"
    # 둘 다 배제 → 204.
    assert client.get("/dispatch/u1/next?exclude=PROJ-1,PROJ-2", headers=hdr).status_code == 204


def test_report_status_terminal_triggers_on_complete(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoA"]))  # 같은 레포 대기
    assert sch.jobs.get("PROJ-2").status == q.QUEUED
    disp.report_status("u1", "PROJ-1", {"status": "완료", "mr_url": "http://mr"})
    assert sch.jobs.get("PROJ-1").status == q.DONE
    assert sch.jobs.get("PROJ-1").mr_url == "http://mr"
    assert sch.jobs.get("PROJ-2").status == q.RUNNING   # 완료-구동 다음 dispatch


class _FakeWriter:
    """DlcMetaWriter 대역 — 완료 시 호출 기록 + 지정 relpath 반환."""

    def __init__(self, relpath="n/cycles/C1"):
        self.relpath = relpath
        self.jobs = []

    def commit_cycle_log(self, job):
        self.jobs.append(job)
        return self.relpath


def test_terminal_report_commits_cycle_log_and_returns_path(isolated_state):
    # 완료(terminal) 회신 → 단일 라이터가 사이클로그 커밋 + relpath를 회신에 실는다.
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    writer = _FakeWriter(relpath="n/cycles/2026-08-19-abc")
    disp = Dispatcher(reg, sch, dlc_meta_writer=writer)
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))

    res = disp.report_status("u1", "PROJ-1", {"status": "완료"})
    assert res["ok"] is True
    assert res["cycle_log_path"] == "n/cycles/2026-08-19-abc"
    assert len(writer.jobs) == 1                     # 완료마다 1회 커밋 시도
    assert writer.jobs[0].ticket == "PROJ-1"


def test_non_terminal_report_does_not_commit(isolated_state):
    # 진행중/중단(비-terminal) 회신은 사이클로그를 커밋하지 않는다.
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    writer = _FakeWriter()
    disp = Dispatcher(reg, sch, dlc_meta_writer=writer)
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))

    res = disp.report_status("u1", "PROJ-1", {"status": "진행중"})
    assert "cycle_log_path" not in res
    assert writer.jobs == []


def test_terminal_report_survives_writer_failure(isolated_state):
    # 커밋이 예외를 던져도 상태 회신은 정상(락/dedup 해제는 스케줄러가 이미 처리).
    class Boom:
        def commit_cycle_log(self, job):
            raise RuntimeError("git down")

    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    disp = Dispatcher(reg, sch, dlc_meta_writer=Boom())
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    res = disp.report_status("u1", "PROJ-1", {"status": "완료"})
    assert res["ok"] is True and "cycle_log_path" not in res
    assert sch.jobs.get("PROJ-1").status == q.DONE


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
