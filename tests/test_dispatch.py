"""dispatch 단위테스트 — enqueue(스케줄러 경유)·완료 상태머신·dlc-meta 커밋·교차유저.

⚠️ 프랙탈 P2: 레거시 worker-facing HTTP 서빙(GET /next·POST /status·GET /control)과
그 폴링 소비자(app/worker.py)는 제거됐다(이중 실행 근본 차단). 남은 인프로세스 표면
(enqueue·report_status·list_jobs·Tier-2 pending)만 검증한다.
"""

from __future__ import annotations

from app import queue as q
from app.dispatch import Dispatcher
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config


def _wire(per_user=5):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(concurrency_per_worker=per_user), JobQueue())
    disp = Dispatcher(reg, sch)
    return reg, sch, disp


def test_enqueue_goes_through_scheduler(isolated_state):
    _, sch, disp = _wire()
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    j = sch.jobs.get("PROJ-1")
    assert j.user == "u1" and j.status == q.RUNNING   # 스케줄러가 즉시 dispatch


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


def test_unknown_job_raises_keyerror(isolated_state):
    _, sch, disp = _wire()
    try:
        disp.report_status("u1", "NOPE", {"status": "완료"})
        assert False, "미존재 잡 보고가 KeyError를 내지 않음"
    except KeyError:
        pass
