"""프랙탈 잡 관측성 — 대시보드 가시성 뼈대(A)·track.py(B)·크로스프로세스 안전(C).

라이브 claude/docker/웹훅은 절대 호출하지 않는다(recorder/store/subprocess 대역). 검증:
    A. 라이프사이클 계측: fractal 경로가 JobQueue(같은 store)에 queued 생성·running 전이·
       done 마킹. worker_dispatch 의 running/failed(error·timeout·refused) 매핑. 구 경로
       이중생성 안 함. cancelled 상태 protect.
    B. track.py 가 레코드를 갱신(주입 store 로 격리 + 실제 state 경로).
    C. 동시 write 가 유실 없이 병합(read_modify_write_jobs 경계 — 스레드 시뮬레이션;
       리눅스에서는 flock 이 이를 프로세스 간으로 확장).
"""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

import gchat
import track
import worker_dispatch as wd
from app import state
from app.queue import Job, JobQueue


# --- 대역 -------------------------------------------------------------------


class _Recorder:
    """run_dispatch 에 주입하는 recorder 대역 — 호출을 순서대로 캡처(state 미접근)."""

    def __init__(self):
        self.calls = []

    def __call__(self, ticket, *, status=None, **fields):
        self.calls.append({"ticket": ticket, "status": status, **fields})


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _cfg():
    return SimpleNamespace(run=SimpleNamespace(claude_bin="claude"))


def _runner(completed):
    def _run(cmd, timeout_sec=0):
        return completed
    return _run


def _rec_of(rec_calls, key):
    return [c for c in rec_calls if c.get("status") == key]


# ===========================================================================
# A. state.record_job_event — 크로스프로세스 안전 라이프사이클 API
# ===========================================================================


def test_record_creates_queued_then_transitions(isolated_state):
    state.record_job_event("T-1", status="running", user="u", log_summary="시작")
    jobs = {j["ticket"]: j for j in state.load_jobs([])}
    assert jobs["T-1"]["status"] == "running"
    assert jobs["T-1"]["user"] == "u"
    assert jobs["T-1"]["log_summary"] == "시작"
    assert jobs["T-1"]["updated_at"]  # 스탬프됨


def test_record_no_create_when_missing_and_flag_off(isolated_state):
    out = state.record_job_event("NOPE", status="running", create_if_missing=False)
    assert out is None
    assert state.load_jobs([]) == []


def test_record_protects_cancel_states(isolated_state):
    """진행 중 취소/이관 상태는 관측성 계측이 덮어쓰지 않는다(cancelled 가시성 보존)."""
    state.record_job_event("T-2", status="cancelling")
    # 뒤늦은 running/done 마킹이 와도 취소 신호를 되돌리지 않는다.
    state.record_job_event("T-2", status="running", log_summary="late-run")
    state.record_job_event("T-2", status="done", log_summary="late-done")
    rec = {j["ticket"]: j for j in state.load_jobs([])}["T-2"]
    assert rec["status"] == "cancelling"          # 상태는 보존
    assert rec["log_summary"] == "late-done"      # 비상태 필드는 여전히 갱신


def test_record_meta_merges_and_routes_unknown_keys(isolated_state):
    state.record_job_event("T-3", meta={"repos": {"a": {"event": "start"}}})
    state.record_job_event("T-3", meta={"repos": {"b": {"event": "done"}}},
                           some_extra="x")
    rec = {j["ticket"]: j for j in state.load_jobs([])}["T-3"]
    assert rec["meta"]["repos"] == {"a": {"event": "start"}, "b": {"event": "done"}}
    assert rec["meta"]["some_extra"] == "x"       # 미지의 키는 meta 로


# ===========================================================================
# A. JobQueue ↔ 외부 프로세스(같은 store) — 가시성 + 무클로버
# ===========================================================================


def test_jobqueue_reads_external_updates(isolated_state):
    """대시보드 가시성: 외부 프로세스가 찍은 running 을 JobQueue.list_jobs/get 가 본다."""
    jq = JobQueue()
    jq.enqueue(Job(ticket="A", user="u", status="queued"))
    # worker_dispatch(별개 프로세스)가 running 을 디스크에 기록했다고 가정.
    state.record_job_event("A", status="running", log_summary="exec")
    assert jq.get("A").status == "running"
    assert any(j.ticket == "A" and j.status == "running" for j in jq.list_jobs())


def test_jobqueue_does_not_clobber_external_job(isolated_state):
    """central 이 다른 티켓을 갱신해도 외부 프로세스가 만든 잡을 유실시키지 않는다."""
    jq = JobQueue()
    jq.enqueue(Job(ticket="A", user="u", status="queued"))
    state.record_job_event("B", status="running", user="v")  # 외부가 만든 B
    jq.set_status("A", "running")                             # central 은 A 만 건드림
    jobs = {j.ticket: j for j in jq.list_jobs()}
    assert jobs["B"].status == "running"   # 외부 B 보존(무클로버)
    assert jobs["A"].status == "running"


# ===========================================================================
# A. worker_dispatch — running 전이 + 결과(ok 유지 / error·timeout·refused → failed)
# ===========================================================================


def test_worker_dispatch_records_running_then_keeps_running_on_ok(isolated_state):
    rec = _Recorder()
    wd.run_dispatch("u", "HAN-1", "i", session_id=None, resume=False,
                    config=_cfg(), runner=_runner(_FakeCompleted(stdout="MR: https://gl/x/-/merge_requests/7", returncode=0)),
                    recorder=rec)
    # 1) exec 직전 running, 2) 결과 ok → 상태변경 없음(status=None) + mr_url/log_summary.
    # (running 과 결과 사이에 진행중-전이 기록이 끼므로 결과 레코드는 mr_url 키로 찾는다.)
    result_rec = next(c for c in rec.calls if "mr_url" in c)
    assert rec.calls[0]["status"] == "running"
    assert result_rec["status"] is None
    assert result_rec["mr_url"] == "https://gl/x/-/merge_requests/7"
    assert result_rec["log_summary"]


def test_worker_dispatch_records_failed_on_error(isolated_state):
    rec = _Recorder()
    wd.run_dispatch("u", "HAN-2", "i", session_id=None, resume=False,
                    config=_cfg(), runner=_runner(_FakeCompleted(stdout="boom", stderr="e", returncode=2)),
                    recorder=rec)
    assert rec.calls[0]["status"] == "running"
    assert rec.calls[-1]["status"] == "failed"
    assert rec.calls[-1]["log_summary"].startswith("[error]")


def test_worker_dispatch_records_failed_on_timeout(isolated_state):
    rec = _Recorder()

    def _to(cmd, timeout_sec=0):
        raise subprocess.TimeoutExpired(cmd, timeout_sec)

    wd.run_dispatch("u", "HAN-3", "i", session_id=None, resume=False,
                    config=_cfg(), runner=_to, timeout_sec=5, recorder=rec)
    assert rec.calls[0]["status"] == "running"
    assert rec.calls[-1]["status"] == "failed"
    assert "timeout" in rec.calls[-1]["log_summary"]


def test_worker_dispatch_records_failed_on_refused_without_running(isolated_state):
    """ROLE=worker 거부는 exec 전 반환 — running 없이 failed 만 기록(self-exec 백스톱)."""
    rec = _Recorder()
    result = wd.run_dispatch("u", "HAN-4", "i", session_id=None, resume=False,
                             config=_cfg(), runner=_runner(_FakeCompleted(returncode=0)),
                             env={"ROLE": "worker"}, recorder=rec)
    assert result["status"] == "refused"
    assert len(rec.calls) == 1
    assert rec.calls[0]["status"] == "failed"


def test_worker_dispatch_no_recorder_is_pure(isolated_state):
    """recorder 미주입이면 상태를 일절 기록하지 않는다(기존 동작 회귀 방지)."""
    wd.run_dispatch("u", "HAN-5", "i", session_id=None, resume=False,
                    config=_cfg(), runner=_runner(_FakeCompleted(stdout="ok", returncode=0)))
    assert state.load_jobs([]) == []


def test_worker_dispatch_mr_parse_none_when_absent(isolated_state):
    rec = _Recorder()
    wd.run_dispatch("u", "HAN-6", "i", session_id=None, resume=False,
                    config=_cfg(), runner=_runner(_FakeCompleted(stdout="작업 완료, MR 없음", returncode=0)),
                    recorder=rec)
    # 결과 레코드는 mr_url 키를 실어 보내는 유일한 레코드다(진행중-전이 기록엔 없음).
    result_rec = next(c for c in rec.calls if "mr_url" in c)
    assert result_rec["mr_url"] is None


# ===========================================================================
# A. gchat — 완료 게이트에서 done 마킹
# ===========================================================================


def _gcfg(enabled=True):
    return SimpleNamespace(
        notify=SimpleNamespace(enabled=enabled, webhook_ref="svc/w"),
        secrets=SimpleNamespace(base_dir="/s"),
    )


def test_gchat_main_marks_done_on_send(monkeypatch, isolated_state):
    monkeypatch.setattr(gchat, "_load_config", lambda p: _gcfg(True))
    monkeypatch.setattr(gchat, "send_report", lambda config, report, **kw: True)
    rc = gchat.main(["--ticket", "HAN-7", "--report", "완료 본문"])
    assert rc == 0
    rec = {j["ticket"]: j for j in state.load_jobs([])}["HAN-7"]
    assert rec["status"] == "done"


def test_gchat_main_second_call_keeps_done(monkeypatch, isolated_state):
    monkeypatch.setattr(gchat, "_load_config", lambda p: _gcfg(True))
    monkeypatch.setattr(gchat, "send_report", lambda config, report, **kw: True)
    gchat.main(["--ticket", "HAN-8", "--report", "본문"])
    gchat.main(["--ticket", "HAN-8", "--report", "본문(재호출)"])  # dedup skip → done 유지
    rec = {j["ticket"]: j for j in state.load_jobs([])}["HAN-8"]
    assert rec["status"] == "done"


# ===========================================================================
# B. track.py — 세만틱 이벤트 기록(주입 store 격리 + 실제 state)
# ===========================================================================


def test_track_record_event_with_injected_store():
    captured = {}

    def _store(ticket, **kw):
        captured.update(ticket=ticket, **kw)
        return {"ticket": ticket}

    track.record_event("HAN-1", event="repo_covered", user="u", repo="repoA",
                       status="running", mr="https://gl/x/-/merge_requests/3",
                       detail="repoA 커버", branch="auto/HAN-1", store=_store)
    assert captured["ticket"] == "HAN-1"
    assert captured["status"] == "running"
    assert captured["user"] == "u"
    assert captured["mr_url"] == "https://gl/x/-/merge_requests/3"
    assert captured["branch"] == "auto/HAN-1"
    assert captured["log_summary"] == "repoA 커버"
    meta = captured["meta"]
    assert meta["last_event"] == "repo_covered"
    assert meta["repos"]["repoA"]["event"] == "repo_covered"
    assert meta["repos"]["repoA"]["detail"] == "repoA 커버"


def test_track_record_event_real_state_creates_and_normalizes(isolated_state):
    # 실제 state 경로: 없으면 생성 + 한글 상태 정규화(진행중 → running).
    track.record_event("HAN-2", event="triaged", status="진행중", detail="영향 R1,R2")
    rec = {j["ticket"]: j for j in state.load_jobs([])}["HAN-2"]
    assert rec["status"] == "running"
    assert rec["log_summary"] == "영향 R1,R2"
    assert rec["meta"]["last_event"] == "triaged"


def test_track_main_cli(isolated_state):
    rc = track.main(["--ticket", "HAN-3", "--event", "repo_covered", "--repo", "repoB",
                     "--mr", "https://gl/y/-/merge_requests/1"])
    assert rc == 0
    rec = {j["ticket"]: j for j in state.load_jobs([])}["HAN-3"]
    assert rec["mr_url"] == "https://gl/y/-/merge_requests/1"
    assert rec["meta"]["repos"]["repoB"]["event"] == "repo_covered"


# ===========================================================================
# C. 크로스프로세스 안전 — 동시 write 유실 방지(read_modify_write 경계)
# ===========================================================================


def test_concurrent_updates_same_ticket_no_lost_update(isolated_state):
    """두 스레드가 같은 티켓에 서로 다른 meta 키를 반복 기록 — 둘 다 살아남는다(무유실).

    read_modify_write_jobs 가 load→수정→save 를 원자 경계로 직렬화하므로, 각 writer 가
    최신을 읽고 병합해 서로 덮어쓰지 않는다. 리눅스에서는 flock 이 이를 프로세스 간으로
    확장한다(테스트는 스레드로 시뮬레이션; 인프로세스 _io_lock 이 경계를 보장).
    """
    state.record_job_event("T", status="queued")

    def _work(key, n):
        for i in range(n):
            state.record_job_event("T", meta={key: i})

    a = threading.Thread(target=_work, args=("a", 60))
    b = threading.Thread(target=_work, args=("b", 60))
    a.start(); b.start(); a.join(); b.join()
    rec = {j["ticket"]: j for j in state.load_jobs([])}["T"]
    assert rec["meta"]["a"] == 59
    assert rec["meta"]["b"] == 59   # 서로 덮어쓰지 않고 둘 다 최종값 도달


def test_concurrent_distinct_tickets_all_present(isolated_state):
    """두 스레드가 서로 다른 티켓들을 동시에 생성 — 하나도 유실되지 않는다."""

    def _work(lo, hi):
        for i in range(lo, hi):
            state.record_job_event(f"T-{i}", status="running")

    a = threading.Thread(target=_work, args=(0, 50))
    b = threading.Thread(target=_work, args=(50, 100))
    a.start(); b.start(); a.join(); b.join()
    tickets = {j["ticket"] for j in state.load_jobs([])}
    assert tickets == {f"T-{i}" for i in range(100)}


def test_jobqueue_and_external_concurrent_no_loss(isolated_state):
    """JobQueue(central 스레드)와 외부 record_job_event 가 동시에 서로 다른 티켓을 써도
    유실 없음(같은 flock/_io 경계 공유)."""
    jq = JobQueue()

    def _central():
        for i in range(40):
            jq.enqueue(Job(ticket=f"C-{i}", user="u", status="queued"))

    def _external():
        for i in range(40):
            state.record_job_event(f"E-{i}", status="running")

    a = threading.Thread(target=_central)
    b = threading.Thread(target=_external)
    a.start(); b.start(); a.join(); b.join()
    tickets = {j.ticket for j in jq.list_jobs()}
    assert {f"C-{i}" for i in range(40)} <= tickets
    assert {f"E-{i}" for i in range(40)} <= tickets
