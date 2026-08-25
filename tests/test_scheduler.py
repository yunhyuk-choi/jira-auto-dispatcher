"""자원 인지형 레포락 스케줄러 단위테스트(RECURSIVE-DISPATCH §4).

검증:
    - 레포락 직렬(정확성)·레포 간 병렬·완료→다음 dispatch·미해석=전역 직렬·
      interrupted+reset_at 재적격 (정확성 계층, 자원 무관).
    - **서버 자원 기반 어드미션**(스로틀): 메모리 넉넉→admit / MemAvailable 낮음→큐잉 /
      loadavg/코어 높음→큐잉 / in-flight 예약이 과다 admit 차단 / **잡 수 cap 없음**
      (한 사용자 다수 잡·한 tick 버스트 비-과다커밋).

자원 프로브는 주입(fake)해 결정적으로 검증한다 — 라이브 /proc를 읽지 않는다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import queue as q
from app.queue import Job, JobQueue
from app.scheduler import Scheduler
from tests.conftest import make_config


# --- 프로브 헬퍼 -------------------------------------------------------------

def _probe(mem_mb, load=0.0, ncpu=4):
    """고정 자원 프로브(fake). mem_mb=가용 메모리(MB), load=1분 loadavg."""
    return lambda: {"mem_available_mb": float(mem_mb),
                    "loadavg_1min": float(load), "ncpu": int(ncpu)}


# 사실상 무한 메모리·무부하 → 자원 스로틀이 절대 걸리지 않음(정확성 계층 격리 검증용).
_AMPLE = _probe(1 << 20)


def _sch(probe=_AMPLE, **cfg_kw):
    return Scheduler(make_config(**cfg_kw), JobQueue(), resource_probe=probe)


def _job(ticket, user, repos):
    return Job(ticket=ticket, user=user, target_repos=list(repos))


def _status(sch, ticket):
    return sch.jobs.get(ticket).status


def _running(sch, *tickets):
    return sum(1 for t in tickets if _status(sch, t) == q.RUNNING)


# ===========================================================================
# 정확성 계층 — 전역 레포락(잡 수 cap 아님). 자원 넉넉(_AMPLE)에서 검증.
# ===========================================================================

def test_same_repo_serial(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))   # 같은 레포 → 직렬
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED


def test_different_repos_parallel(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoB"]))   # 다른 레포 → 병렬
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_complete_dispatches_next_on_same_repo(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)              # 레포락 해제 → 다음 dispatch
    assert _status(sch, "PROJ-1") == q.DONE
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_next_for_user_excludes_in_flight_tickets(isolated_state):
    # next_for_user가 exclude로 서로 다른 running 잡을 준다(worker 동시 수령). 잡 수 cap 없음.
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u1", ["repoB"]))
    first = sch.next_for_user("u1")
    assert first.ticket == "PROJ-1"                  # 첫 running(삽입 순서)
    second = sch.next_for_user("u1", exclude={"PROJ-1"})
    assert second.ticket == "PROJ-2"
    assert sch.next_for_user("u1", exclude={"PROJ-1", "PROJ-2"}) is None


def test_unresolved_target_is_global_serial(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", []))          # 미해석 → 단독 실행
    sch.enqueue(_job("PROJ-2", "u2", ["repoB"]))   # 전역락에 막힘
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_global_serial_waits_for_others(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))   # 먼저 running
    sch.enqueue(_job("PROJ-2", "u2", []))          # 미해석 → 단독 필요 → 대기
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)
    assert _status(sch, "PROJ-2") == q.RUNNING


# ===========================================================================
# 자원 기반 어드미션 — 스로틀은 서버 자원(메모리 + 부하). 잡 수 cap 없음.
# ===========================================================================

def test_admits_when_memory_ample(isolated_state):
    # 메모리 넉넉 + 무부하 → 준비된 잡을 즉시 admit.
    sch = _sch(probe=_probe(8192, load=0.0, ncpu=4))
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert _status(sch, "PROJ-1") == q.RUNNING


def test_queues_when_memory_low(isolated_state):
    # MemAvailable < min_free_mem_mb(1536) → 첫 잡부터 큐잉(메모리 압박).
    sch = _sch(probe=_probe(1000, load=0.0, ncpu=4))
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert _status(sch, "PROJ-1") == q.QUEUED
    # 메모리가 회복되면(프로브 교체) 다음 tick에 admit.
    sch._resource_probe = _probe(8192)
    assert "PROJ-1" in sch.tick()
    assert _status(sch, "PROJ-1") == q.RUNNING


def test_queues_when_loadavg_per_core_high(isolated_state):
    # loadavg_1min/ncpu ≥ max_load_per_core(0.9) → 큐잉(부하 압박). 메모리는 넉넉.
    state = {"mem": 1 << 20, "load": 4.0, "ncpu": 4}   # load/core = 1.0 ≥ 0.9
    sch = _sch(probe=lambda: {"mem_available_mb": state["mem"],
                              "loadavg_1min": state["load"], "ncpu": state["ncpu"]})
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert _status(sch, "PROJ-1") == q.QUEUED
    # 부하가 내려가면 admit.
    state["load"] = 1.0                                 # load/core = 0.25 < 0.9
    assert "PROJ-1" in sch.tick()
    assert _status(sch, "PROJ-1") == q.RUNNING


def test_inflight_reservation_prevents_over_admission(isolated_state):
    # 프로브 4GB, 예약 1GB/잡, min_free 1.5GB → 유효가용은 admit마다 1GB씩 감소.
    #   0런닝: 4096-0=4096 admit / 1: 3072 admit / 2: 2048 admit / 3: 1024<1536 정지.
    # 결과: 3개 running, 4번째 큐잉("3개 이미 running → 4번째 큐잉").
    sch = _sch(probe=_probe(4096, load=0.0, ncpu=8))
    for i, repo in enumerate(["repoA", "repoB", "repoC", "repoD"], start=1):
        sch.enqueue(_job(f"PROJ-{i}", "u1", [repo]))    # 모두 다른 레포(레포락 무충돌)
    assert _running(sch, "PROJ-1", "PROJ-2", "PROJ-3", "PROJ-4") == 3
    assert _status(sch, "PROJ-4") == q.QUEUED           # in-flight 예약이 4번째 차단


def test_no_per_user_count_limit_when_resources_allow(isolated_state):
    # 잡 수 cap 없음: 한 사용자가 (서로 다른 레포) 여러 잡을 자원 여유만큼 동시 실행.
    sch = _sch(probe=_probe(1 << 20, load=0.0, ncpu=8))   # 사실상 무한
    for i, repo in enumerate(["r1", "r2", "r3", "r4", "r5"], start=1):
        sch.enqueue(_job(f"J-{i}", "solo", [repo]))
    assert _running(sch, "J-1", "J-2", "J-3", "J-4", "J-5") == 5   # 전부 동시 running


def test_one_tick_does_not_over_commit_a_burst(isolated_state):
    # 5개가 한꺼번에 준비돼도 한 tick은 메모리가 허용하는 N개만 admit, 나머지는 큐잉.
    # 4096MB / (예약 1024, min 1536) → N=3. 큐에만 넣고 tick 한 번.
    sch = _sch(probe=_probe(4096, load=0.0, ncpu=8))
    for i, repo in enumerate(["a", "b", "c", "d", "e"], start=1):
        sch.jobs.enqueue(_job(f"B-{i}", "u1", [repo]))    # tick 없이 큐 적재만
    dispatched = sch.tick()                               # 단 한 번의 tick
    assert len(dispatched) == 3                           # N=3만 admit
    assert _running(sch, "B-1", "B-2", "B-3", "B-4", "B-5") == 3
    assert sum(1 for i in range(1, 6) if _status(sch, f"B-{i}") == q.QUEUED) == 2


def test_freed_memory_admits_queued_next_tick(isolated_state):
    # 완료로 예약이 회수되면 큐 대기분이 다음 tick(on_complete)에 admit된다.
    sch = _sch(probe=_probe(4096, load=0.0, ncpu=8))
    for i, repo in enumerate(["repoA", "repoB", "repoC", "repoD"], start=1):
        sch.enqueue(_job(f"PROJ-{i}", "u1", [repo]))
    assert _status(sch, "PROJ-4") == q.QUEUED            # 예약으로 대기
    sch.on_complete("PROJ-1", q.DONE)                    # 예약 1GB 회수 → 재-tick
    assert _status(sch, "PROJ-4") == q.RUNNING


def test_repo_lock_serializes_same_repo_regardless_of_resources(isolated_state):
    # 자원이 무한이어도 같은 레포는 전역 레포락으로 여전히 직렬(정확성 ≠ 잡 수 cap).
    sch = _sch(probe=_probe(1 << 20, load=0.0, ncpu=8))
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u1", ["repoA"]))         # 같은 레포 → 직렬
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED
    sch.on_complete("PROJ-1", q.DONE)                    # 레포락 해제 → 다음
    assert _status(sch, "PROJ-2") == q.RUNNING


def test_default_probe_fail_open_admits(isolated_state):
    # 프로브 미주입(기본 = 호스트 /proc). 비-Linux/판독 실패 시 무압박 폴백(admit).
    # 이 환경(Windows 등)에서 /proc 부재 → fail-open으로 정상 dispatch되어야 한다.
    sch = Scheduler(make_config(), JobQueue())           # resource_probe 미주입
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert _status(sch, "PROJ-1") == q.RUNNING


# ===========================================================================
# 재개 / 라우터 / rerun / 재시작 — 자원 무관 정확성(ample 프로브).
# ===========================================================================

def test_interrupted_reset_at_reeligibility(isolated_state):
    now = {"t": datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)}
    sch = Scheduler(make_config(), JobQueue(),
                    now_provider=lambda: now["t"], resource_probe=_AMPLE)
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
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))       # 같은 레포 대기
    sch.on_interrupt("PROJ-1", reset_at=None)          # reset_at 없음 → 즉시 재적격 풀
    statuses = {t: _status(sch, t) for t in ("PROJ-1", "PROJ-2")}
    assert list(statuses.values()).count(q.RUNNING) == 1


def test_report_router(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    # 진행중(한글) → running 유지 + 필드 갱신
    sch.report("PROJ-1", "진행중", session_id="s1", mr_url="http://mr")
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.RUNNING and j.session_id == "s1" and j.mr_url == "http://mr"
    # 완료(한글) → done
    sch.report("PROJ-1", "완료")
    assert sch.jobs.get("PROJ-1").status == q.DONE


def test_rerun_resets_terminal_job_and_redispatches(isolated_state):
    """종결(failed) 잡을 사람이 수동 재실행 → queued 리셋 후 재-dispatch(수정 2)."""
    from app.gate import DedupGate

    gate = DedupGate()
    sch = Scheduler(make_config(), JobQueue(), gate=gate, resource_probe=_AMPLE)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.on_complete("PROJ-1", q.FAILED)          # 연결실패 등으로 실패 종결
    assert _status(sch, "PROJ-1") == q.FAILED

    dispatched = sch.rerun("PROJ-1")
    assert "PROJ-1" in dispatched                 # 재-dispatch 시도됨
    assert _status(sch, "PROJ-1") == q.RUNNING
    j = sch.jobs.get("PROJ-1")
    assert j.reset_at is None and j.session_id is None
    assert j.attempts == 1                         # reopen(0) → dispatch(+1)
    assert gate.is_claimed("PROJ-1")               # dedup 재확보


def test_rerun_reclaims_dedup_after_cancel(isolated_state):
    """취소 확정(dedup 해제)된 잡도 rerun이 dedup을 재확보하고 재-dispatch한다."""
    from app.gate import DedupGate

    gate = DedupGate()
    gate.claim("PROJ-1")
    sch = Scheduler(make_config(), JobQueue(), gate=gate, resource_probe=_AMPLE)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.confirm_cancelled("PROJ-1")                # cancelled + dedup 해제
    assert not gate.is_claimed("PROJ-1")

    sch.rerun("PROJ-1")
    assert gate.is_claimed("PROJ-1")               # 재확보
    assert _status(sch, "PROJ-1") == q.RUNNING


def test_rerun_unknown_job_raises(isolated_state):
    import pytest

    sch = _sch()
    with pytest.raises(KeyError):
        sch.rerun("NOPE")


def test_restart_rebuilds_locks_from_persisted_jobs(isolated_state):
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))       # running + 영속
    # 재시작 시뮬: 새 JobQueue/Scheduler가 jobs.json에서 복원
    sch2 = _sch()
    sch2.enqueue(_job("PROJ-2", "u2", ["repoA"]))       # 같은 레포 → 여전히 막혀야
    assert sch2.jobs.get("PROJ-1").status == q.RUNNING
    assert sch2.jobs.get("PROJ-2").status == q.QUEUED


# ===========================================================================
# dispatch 후 훅(on_dispatch) — 미락 참고 레포 최신화 배선점(app.freshen)
# ===========================================================================


def test_on_dispatch_hook_fires_only_on_actual_dispatch(isolated_state):
    """훅은 잡이 실제로 dispatch될 때만, 현재 locked_repos 스냅샷과 함께 호출된다.

    빈 tick(디스패치 없음)에선 호출되지 않는다(유휴=0 — freshen이 빈 폴에 돌지 않음).
    """
    calls = []
    sch = Scheduler(make_config(), JobQueue(), resource_probe=_AMPLE,
                    on_dispatch=lambda locked: calls.append(set(locked)))

    # 잡 없이 tick → 디스패치 없음 → 훅 미호출.
    sch.tick()
    assert calls == []

    # 잡 enqueue → 즉시 dispatch → 훅 1회, 그 잡의 레포가 locked에 포함.
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert len(calls) == 1
    assert "repoA" in calls[0]

    # 같은 레포 잡은 막혀(queued) 디스패치 안 됨 → 훅 추가 호출 없음.
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))
    assert len(calls) == 1


def test_on_dispatch_hook_failure_does_not_break_dispatch(isolated_state):
    """훅이 예외를 던져도 dispatch/스케줄링은 정상 진행된다(best-effort 격리)."""
    def _boom(locked):
        raise RuntimeError("freshen exploded")

    sch = Scheduler(make_config(), JobQueue(), resource_probe=_AMPLE, on_dispatch=_boom)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))          # 예외를 삼키고 dispatch 성공
    assert _status(sch, "PROJ-1") == q.RUNNING


def test_on_dispatch_hook_excludes_locked_via_snapshot(isolated_state):
    """훅에 넘기는 locked_repos는 tick 종료 시점의 활성 잡 레포 합집합이다."""
    seen = []
    sch = Scheduler(make_config(), JobQueue(), resource_probe=_AMPLE,
                    on_dispatch=lambda locked: seen.append(set(locked)))
    sch.enqueue(_job("PROJ-1", "u1", ["repoA", "repoB"]))
    assert seen[-1] == {"repoA", "repoB"}


# ===========================================================================
# Phase 3b-0 — 읽기 전용 상태 접근자(state_snapshot). 무동작변경 검증.
# ===========================================================================

def test_state_snapshot_reflects_running_locked_and_global(isolated_state):
    # 활성 잡·locked_repos·per-user 활성수·global 러닝수를 정확히 반영.
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u1", ["repoB"]))       # 다른 레포 → 병렬(같은 사용자)
    sch.enqueue(_job("PROJ-3", "u2", ["repoA"]))       # repoA 잠김 → 대기
    snap = sch.state_snapshot()
    assert snap["running_count"] == 2
    assert {r["ticket"] for r in snap["running"]} == {"PROJ-1", "PROJ-2"}
    # running 항목은 계약된 필드만 노출
    assert set(snap["running"][0]) == {"ticket", "user", "target_repos", "status"}
    assert snap["per_user_active"] == {"u1": 2}
    assert snap["locked_repos"] == ["repoA", "repoB"]  # 정렬
    assert snap["global_lock"] is False
    # PROJ-3는 repoA 충돌로 대기 → eligible에 잡히지만(상태 queued) dispatch는 파이썬이 막음
    elig = {e["ticket"]: e for e in snap["eligible"]}
    assert "PROJ-3" in elig and elig["PROJ-3"]["reason"] == "queued"
    assert snap["queued_count"] == 1


def test_state_snapshot_global_lock_and_resource_headroom(isolated_state):
    # 미해석(target_repos=[]) 활성 잡 → global_lock True, has_headroom False.
    sch = _sch(probe=_probe(8192, load=0.0, ncpu=4))
    sch.enqueue(_job("PROJ-1", "u1", []))              # 미해석 → 전역 직렬
    snap = sch.state_snapshot()
    assert snap["global_lock"] is True
    res = snap["resource"]
    assert res["mem_available_mb"] == 8192.0
    assert res["ncpu"] == 4
    assert res["load_per_core"] == 0.0
    # effective = 8192 - 1*1024 = 7168 (여유) 이지만 global_lock이라 헤드룸 없음
    assert res["effective_mb"] == 8192.0 - 1024.0
    assert res["mem_pressure"] is False
    assert res["has_headroom"] is False               # global_lock이 헤드룸을 막음


def test_state_snapshot_resource_pressure_flags(isolated_state):
    # 메모리 압박(effective < min_free) → mem_pressure True, has_headroom False.
    sch = _sch(probe=_probe(1000, load=0.0, ncpu=4))
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))       # 압박이라 큐잉됨
    snap = sch.state_snapshot()
    assert snap["running_count"] == 0
    res = snap["resource"]
    assert res["mem_pressure"] is True
    assert res["has_headroom"] is False
    # 부하 압박도 함께 검증
    sch2 = _sch(probe=_probe(1 << 20, load=4.0, ncpu=4))  # load/core=1.0 ≥ 0.9
    sch2.enqueue(_job("PROJ-9", "u1", ["repoZ"]))
    r2 = sch2.state_snapshot()["resource"]
    assert r2["load_pressure"] is True
    assert r2["has_headroom"] is False


def test_state_snapshot_interrupted_eligible_vs_waiting(isolated_state):
    # interrupted+reset_at 미도래=waiting, 도래=eligible(reason=interrupted_ready).
    now = {"t": datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)}
    sch = Scheduler(make_config(), JobQueue(),
                    now_provider=lambda: now["t"], resource_probe=_AMPLE)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    future = (now["t"] + timedelta(hours=1)).isoformat()
    sch.on_interrupt("PROJ-1", reset_at=future)
    snap = sch.state_snapshot()
    assert snap["interrupted_waiting"] == 1
    assert snap["eligible"] == []                      # reset_at 미도래
    # reset_at 도래 후: eligible로 이동
    now["t"] = now["t"] + timedelta(hours=2)
    snap2 = sch.state_snapshot()
    assert snap2["interrupted_waiting"] == 0
    elig = {e["ticket"]: e for e in snap2["eligible"]}
    assert elig["PROJ-1"]["reason"] == "interrupted_ready"


def test_state_snapshot_is_read_only(isolated_state):
    # 스냅샷은 절대 스케줄러 상태를 바꾸지 않는다(read-only/멱등).
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.enqueue(_job("PROJ-2", "u2", ["repoA"]))       # 대기
    before = {j.ticket: (j.status, j.attempts) for j in sch.jobs.list_jobs()}
    # 여러 번 호출해도 상태 불변
    for _ in range(3):
        sch.state_snapshot()
    after = {j.ticket: (j.status, j.attempts) for j in sch.jobs.list_jobs()}
    assert before == after
    assert _status(sch, "PROJ-1") == q.RUNNING
    assert _status(sch, "PROJ-2") == q.QUEUED          # dispatch 안 일어남


def test_state_snapshot_mutating_returned_lists_does_not_leak(isolated_state):
    # 반환 스냅샷의 리스트를 변형해도 내부 잡 상태에 안 샌다(방어 복사).
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    snap = sch.state_snapshot()
    snap["running"][0]["target_repos"].append("HACK")
    snap["locked_repos"].append("HACK")
    assert sch.jobs.get("PROJ-1").target_repos == ["repoA"]   # 원본 불변
    assert sch.state_snapshot()["locked_repos"] == ["repoA"]  # 재조회도 깨끗


def test_state_snapshot_json_serializable(isolated_state):
    import json
    sch = _sch()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    # JSON 직렬화 가능해야 HTTP 엔드포인트로 노출 가능
    json.dumps(sch.state_snapshot())
