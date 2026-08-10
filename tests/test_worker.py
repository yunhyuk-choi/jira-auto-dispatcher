"""worker 단위테스트 — 폴링 루프(mock HTTP): 204 sleep, 잡 실행·회신,
interrupted→resume 경로, 예외 격리, 시크릿 헤더. + main.run_worker 배선.

라이브 네트워크/claude는 호출하지 않는다(run/resume는 스텁 주입).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import agent_runner as ar
from app import main
from app import worker as w


# --- HTTP 대역 ---------------------------------------------------------------


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class FakeHTTP:
    def __init__(self, get_responses):
        self._gets = list(get_responses)
        self.get_calls = []
        self.posts = []

    def get(self, url, headers=None):
        self.get_calls.append((url, headers))
        return self._gets.pop(0) if self._gets else FakeResp(204)

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        return FakeResp(200, {})


def _cfg():
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json"),
        secrets=SimpleNamespace(base_dir=""),
    )


_ENV = {"DISPATCH_USER": "u1", "CENTRAL_URL": "http://central:8787"}
_NOW = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)


def _statuses(http):
    return [p[1].get("status") for p in http.posts]


# --- 폴링 루프 ---------------------------------------------------------------


def test_loop_204_sleeps_and_no_post():
    http = FakeHTTP([FakeResp(204)])
    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: None, resume=lambda *a, **k: None,
                      sleep=slept.append, now=_NOW, max_iterations=1)
    assert n == 1
    assert http.posts == []          # 잡 없으면 회신 없음
    assert slept == [w.DEFAULT_POLL_INTERVAL_SEC]
    assert http.get_calls[0][0] == "http://central:8787/dispatch/u1/next"


def test_loop_runs_job_and_reports_running_then_done():
    job = {"ticket": "HAN-1", "autonomy_mode": "A", "branch": "auto/HAN-1"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1",
                            mr_url="http://mr/1", log_summary="ok")
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2)
    assert n == 2
    assert _statuses(http) == ["진행중", "완료"]
    # 완료 회신에 mr_url/session_id 포함
    done_payload = http.posts[-1][1]
    assert done_payload["mr_url"] == "http://mr/1"
    assert done_payload["session_id"] == "s1"
    # status URL은 ticket 기반
    assert http.posts[0][0] == "http://central:8787/dispatch/u1/HAN-1/status"


def test_loop_interrupted_then_resume_to_done():
    job = {"ticket": "HAN-2", "autonomy_mode": "B"}
    http = FakeHTTP([FakeResp(200, job)])
    calls = {"run": 0, "resume": 0}

    def run(*a, **k):
        calls["run"] += 1
        return ar.AgentResult(status=ar.STATUS_INTERRUPTED, session_id="s2",
                              reset_at="2026-01-01T00:00:00Z")

    def resume(job_, sid, creds, cfg, **k):
        calls["resume"] += 1
        assert sid == "s2"
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s2", mr_url="http://mr/2")

    slept = []
    w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run, resume=resume,
                  sleep=slept.append, now=_NOW, max_iterations=1)
    assert calls == {"run": 1, "resume": 1}
    assert _statuses(http) == ["진행중", "interrupted", "완료"]
    interrupted_payload = http.posts[1][1]
    assert interrupted_payload["reset_at"] == "2026-01-01T00:00:00Z"


def test_loop_exception_isolation_does_not_crash():
    job = {"ticket": "HAN-3"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])

    def boom(*a, **k):
        raise RuntimeError("agent blew up")

    slept = []
    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=boom,
                      resume=lambda *a, **k: None, sleep=slept.append,
                      now=_NOW, max_iterations=2)
    assert n == 2                       # 예외에도 루프 생존
    # 착수(진행중)는 보고됐고, 그 뒤 예외로 중단 → 최종 회신 없음
    assert _statuses(http) == ["진행중"]
    assert len(slept) >= 1              # 예외 후 백오프 sleep


def test_loop_sends_worker_secret_header():
    http = FakeHTTP([FakeResp(204)])
    env = dict(_ENV, WORKER_SHARED_SECRET="topsecret")
    w.worker_loop(_cfg(), env=env, http=http, run=lambda *a, **k: None,
                  resume=lambda *a, **k: None, sleep=lambda s: None,
                  now=_NOW, max_iterations=1)
    _, headers = http.get_calls[0]
    assert headers.get("X-Worker-Secret") == "topsecret"


def test_loop_requires_user_and_central():
    import pytest
    with pytest.raises(RuntimeError):
        w.worker_loop(_cfg(), env={}, http=FakeHTTP([]), max_iterations=1)


# --- main.run_worker 배선 ----------------------------------------------------


def test_run_worker_starts_loop_thread_serve_false():
    seen = {}

    def fake_loop(cfg, stop_event=None):
        seen["cfg"] = cfg
        seen["stop"] = stop_event

    cfg = _cfg()
    t = main.run_worker(config=cfg, serve=False, worker_loop_fn=fake_loop)
    t.join(timeout=2)
    assert seen["cfg"] is cfg
    assert seen["stop"] is not None      # stop 이벤트 주입
    assert not t.is_alive()
