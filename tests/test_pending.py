"""Phase 3b-1 — 명시적 id 요청/응답 트랜스포트 토대 단위테스트.

커버:
    - correlation 매칭: 위임 티켓의 terminal 회신이 그 correlation id로 매칭.
    - poll_result/poll_results: terminal 전 pending, 후 결과 payload.
    - **동시 위임 다중 매칭**: 여러 위임이 각각 정확한 id에 매칭(명시적 id의 요점).
    - 재시작 안전: pending/결과가 영속 잡 상태에서 재구성(새 JobQueue/Scheduler/Dispatcher).
    - 하위호환: correlation_id를 안 보내면 티켓 폴백 + 기존 report_status 경로 무변.
"""

from __future__ import annotations

from app import queue as q
from app.dispatch import Dispatcher
from app.pending import PendingRegistry
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config


class _FakeWriter:
    """DlcMetaWriter 대역 — 완료 시 지정 relpath 반환(사이클로그 커밋 흉내)."""

    def __init__(self, relpath="n/cycles/2026-08-20-abc"):
        self.relpath = relpath

    def commit_cycle_log(self, job):
        return self.relpath


def _wire(writer=None):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    disp = Dispatcher(reg, sch, dlc_meta_writer=writer)
    return reg, sch, disp


# --- correlation 기본값 = 티켓(하위호환) ---


def test_correlation_defaults_to_ticket(isolated_state):
    j = Job(ticket="PROJ-1")
    assert j.correlation_id is None        # 미지정
    assert j.corr_id == "PROJ-1"           # 티켓으로 폴백
    # 명시 지정 시 그 값이 우선.
    j2 = Job(ticket="PROJ-2", correlation_id="CID-2")
    assert j2.corr_id == "CID-2"


def test_correlation_id_roundtrips_through_persistence(isolated_state):
    # 구 레코드(correlation_id 키 없음)도 안전 로드.
    assert Job.from_dict({"ticket": "PROJ-9"}).correlation_id is None
    j = Job(ticket="PROJ-1", correlation_id="CID-1")
    assert Job.from_dict(j.to_dict()).correlation_id == "CID-1"


# --- pending 등록 + 폴 ---


def test_poll_pending_before_terminal_then_result_after(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter())
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.register_pending("PROJ-1")   # 기본 correlation = 티켓

    before = disp.poll_result("PROJ-1")
    assert before["status"] == "pending" and before["terminal"] is False
    assert before["result"] is None
    assert "PROJ-1" in disp.pending.pending_ids()

    # 워커 terminal 회신.
    disp.report_status("u1", "PROJ-1", {"status": "완료", "mr_url": "http://mr/1"})

    after = disp.poll_result("PROJ-1")
    assert after["status"] == "done" and after["terminal"] is True
    assert after["result"]["ticket"] == "PROJ-1"
    assert after["result"]["mr_url"] == "http://mr/1"
    assert after["result"]["cycle_log_path"] == "n/cycles/2026-08-20-abc"
    assert "PROJ-1" not in disp.pending.pending_ids()
    assert "PROJ-1" in disp.pending.resolved_ids()


def test_register_pending_unknown_job_raises(isolated_state):
    _, _, disp = _wire()
    try:
        disp.register_pending("NOPE")
        assert False, "미존재 잡 등록이 거부되지 않음"
    except KeyError:
        pass


def test_poll_unknown_correlation(isolated_state):
    _, _, disp = _wire()
    r = disp.poll_result("GHOST")
    assert r["found"] is False and r["status"] == "unknown" and r["result"] is None


# --- 핵심: 동시 위임 다중 매칭(명시적 id의 존재 이유) ---


def test_multiple_concurrent_delegations_each_matched_to_right_id(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/X"))
    # 서로 다른 레포 → 3건 모두 즉시 running(동시 위임).
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoB"]))
    disp.enqueue("u2", Job(ticket="PROJ-3", target_repos=["repoC"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING
    assert sch.jobs.get("PROJ-2").status == q.RUNNING
    assert sch.jobs.get("PROJ-3").status == q.RUNNING

    # 각 위임을 티켓과 **다른** 명시적 correlation id로 등록(id 매칭을 진짜로 검증).
    disp.register_pending("CID-1", ticket="PROJ-1")
    disp.register_pending("CID-2", ticket="PROJ-2")
    disp.register_pending("CID-3", ticket="PROJ-3")

    # 셋 다 pending.
    polled = disp.poll_results(["CID-1", "CID-2", "CID-3"])
    assert all(polled[c]["status"] == "pending" for c in ("CID-1", "CID-2", "CID-3"))

    # 워커들이 **뒤섞인 순서**로, 각자 correlation_id를 되싣어 terminal 회신.
    disp.report_status("u2", "PROJ-3", {"status": "완료", "correlation_id": "CID-3",
                                        "mr_url": "http://mr/3"})
    disp.report_status("u1", "PROJ-1", {"status": "실패", "correlation_id": "CID-1"})
    # PROJ-2는 아직 진행중.

    res = disp.poll_results(["CID-1", "CID-2", "CID-3"])
    # 각 결과가 **정확한 id**에 매칭(교차 없음).
    assert res["CID-3"]["status"] == "done"
    assert res["CID-3"]["result"]["ticket"] == "PROJ-3"
    assert res["CID-3"]["result"]["mr_url"] == "http://mr/3"
    assert res["CID-1"]["status"] == "done"
    assert res["CID-1"]["result"]["ticket"] == "PROJ-1"
    assert res["CID-1"]["job_status"] == q.FAILED
    assert res["CID-1"]["result"]["mr_url"] is None   # 실패엔 mr 없음
    assert res["CID-2"]["status"] == "pending"        # 미완 → 결과 없음
    assert res["CID-2"]["result"] is None

    # 잡에 correlation_id가 명시 영속됐는지(워커 echo 반영).
    assert sch.jobs.get("PROJ-1").correlation_id == "CID-1"
    assert sch.jobs.get("PROJ-3").correlation_id == "CID-3"


# --- 재시작 안전: 영속 잡 상태에서 pending/결과 재구성 ---


def test_restart_safety_pending_reconstructed_from_persisted_state(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/restart"))
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))   # 완료시킬 것
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoB"]))   # pending으로 남길 것
    disp.register_pending("CID-1", ticket="PROJ-1")
    disp.register_pending("CID-2", ticket="PROJ-2")
    disp.report_status("u1", "PROJ-1", {"status": "완료", "correlation_id": "CID-1",
                                        "mr_url": "http://mr/1"})

    # ---- central 재시작 시뮬레이션: jobs.json에서 완전히 새로 조립 ----
    fresh_queue = JobQueue()                        # jobs.json 재로드(진실원)
    fresh_reg = PendingRegistry(fresh_queue)        # in-memory 상태 없이 파생만

    # 완료 위임: terminal + 결과 재구성(cycle_log_path 포함).
    done = fresh_reg.poll_result("CID-1")
    assert done["status"] == "done"
    assert done["result"]["ticket"] == "PROJ-1"
    assert done["result"]["mr_url"] == "http://mr/1"
    assert done["result"]["cycle_log_path"] == "n/cycles/restart"

    # 미완 위임: 여전히 pending으로 파생.
    still = fresh_reg.poll_result("CID-2")
    assert still["status"] == "pending" and still["result"] is None

    # 파생 집합도 재구성.
    assert fresh_reg.pending_ids() == ["CID-2"]
    assert fresh_reg.resolved_ids() == ["CID-1"]


# --- 하위호환: 기존 report_status 경로 무변(회귀) ---


def test_backward_compatible_report_status_without_correlation(isolated_state):
    # 구 워커: correlation_id를 안 보낸다 → 티켓 폴백, 응답/상태 기존과 동일.
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/bc"))
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    disp.enqueue("u1", Job(ticket="PROJ-2", target_repos=["repoA"]))  # 같은 레포 대기

    res = disp.report_status("u1", "PROJ-1", {"status": "완료", "mr_url": "http://mr"})
    # 기존 응답 계약 그대로.
    assert res["ok"] is True
    assert res["dispatched"] == ["PROJ-2"]           # 완료-구동 다음 dispatch
    assert res["cycle_log_path"] == "n/cycles/bc"
    # 잡 상태 전이 기존과 동일.
    assert sch.jobs.get("PROJ-1").status == q.DONE
    assert sch.jobs.get("PROJ-1").mr_url == "http://mr"
    assert sch.jobs.get("PROJ-2").status == q.RUNNING
    # correlation_id는 안 보냈으므로 None(폴백) — 잡 상태 오염 없음.
    assert sch.jobs.get("PROJ-1").correlation_id is None
    # 그래도 티켓으로 폴 가능(등록 안 해도 corr_id=티켓으로 해소).
    assert disp.poll_result("PROJ-1")["status"] == "done"


def test_report_status_response_shape_unchanged_without_writer(isolated_state):
    # 라이터 없음 → cycle_log_path 없음, 응답은 기존 {ok, dispatched}만.
    _, sch, disp = _wire(writer=None)
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    res = disp.report_status("u1", "PROJ-1", {"status": "완료"})
    assert res == {"ok": True, "dispatched": []}
    assert "cycle_log_path" not in res
