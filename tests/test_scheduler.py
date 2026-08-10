"""결정적 레포락 스케줄러 단위테스트(RECURSIVE-DISPATCH §4).

검증: 레포락 직렬, 레포 간 병렬, 완료→다음 dispatch, per-user cap,
전역 cap, 미해석=전역 직렬, interrupted+reset_at 재적격.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import queue as q
from app.queue import Job, JobQueue
from app.scheduler import Scheduler
from tests.conftest import make_config


def _job(ticket, user, repos):
    return Job(ticket=ticket, user=user, target_repos=list(repos))


def _status(sch, ticket):
    return sch.jobs.get(ticket).status


def test_same_repo_serial(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))   # 같은 레포 → 직렬
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED


def test_different_repos_parallel(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoB"]))   # 다른 레포 → 병렬
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_complete_dispatches_next_on_same_repo(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)              # 레포락 해제 → 다음 dispatch
    assert _status(sch, "PROJ-1") == q.DONE
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_per_user_concurrency_one(isolated_state):
    # 같은 유저의 두 잡은 서로 다른 레포여도 동시 실행 불가(worker당 1).
    sch = Scheduler(make_config(concurrency_per_worker=1), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u1", ["repoB"]))
    running = [t for t in ("PROJ-1", "PROJ-2") if _status(sch, t) == q.RUNNING]
    assert running == ["PROJ-1"]


def test_global_concurrency_cap(isolated_state):
    sch = Scheduler(make_config(global_concurrency=2, concurrency_per_worker=1), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoB"]))
    sch.enqueue(_job("PROJ-3", "u3", ["repoC"]))   # cap=2 → 대기
    running = sum(1 for t in ("PROJ-1", "PROJ-2", "PROJ-3") if _status(sch, t) == q.RUNNING)
    assert running == 2
    assert _status(sch, "PROJ-3") == q.QUEUED


def test_unresolved_target_is_global_serial(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", []))          # 미해석 → 단독 실행
    sch.enqueue(_job("PROJ-2", "u2", ["repoB"]))   # 전역락에 막힘
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_global_serial_waits_for_others(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))   # 먼저 running
    sch.enqueue(_job("PROJ-2", "u2", []))          # 미해석 → 단독 필요 → 대기
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_interrupted_reset_at_reeligibility(isolated_state):
    now = {"t": datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)}
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(),
                    now_provider=lambda: now["t"])
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert _status(sch, "PROJ-1") == q.RUNNING
    future = (now["t"] + timedelta(hours=1)).isoformat()
    sch.on_interrupt("PROJ-1", reset_at=future)
    assert _status(sch, "PROJ-1") == q.INTERRUPTED
    # reset_at 이전: 재적격 아님
    assert sch.tick() == []
    assert _status(sch, "PROJ-1") == q.INTERRUPTED
    # reset_at 이후: 재-dispatch(resume)
    now["t"] = now["t"] + timedelta(hours=2)
    assert "PROJ-1" in sch.tick()
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert sch.jobs.get("PROJ-1").attempts == 2       # 재개로 attempts 증가
    assert sch.jobs.get("PROJ-1").reset_at is None     # reset_at 클리어


def test_interrupt_frees_repo_for_others(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))       # 같은 레포 대기
    sch.on_interrupt("PROJ-1", reset_at=None)          # reset_at 없음 → 즉시 재적격 풀
    # 락 해제로 PROJ-2가 먼저 dispatch될 수 있음(둘 중 하나 running, 나머지 대기)
    statuses = {t: _status(sch, t) for t in ("PROJ-1", "PROJ-2")}
    assert list(statuses.values()).count(q.RUNNING) == 1


def test_report_router(isolated_state):
    sch = Scheduler(make_config(), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    # 진행중(한글) → running 유지 + 필드 갱신
    sch.report("PROJ-1", "진행중", session_id="s1", mr_url="http://mr")
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.RUNNING and j.session_id == "s1" and j.mr_url == "http://mr"
    # 완료(한글) → done
    sch.report("PROJ-1", "완료")
    assert sch.jobs.get("PROJ-1").status == q.DONE


def test_restart_rebuilds_locks_from_persisted_jobs(isolated_state):
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))       # running + 영속
    # 재시작 시뮬: 새 JobQueue/Scheduler가 jobs.json에서 복원
    sch2 = Scheduler(make_config(concurrency_per_worker=5), JobQueue())
    sch2.enqueue(_job("PROJ-2", "u2", ["repoA"]))       # 같은 레포 → 여전히 막혀야
    assert sch2.jobs.get("PROJ-1").status == q.RUNNING
    assert sch2.jobs.get("PROJ-2").status == q.QUEUED
