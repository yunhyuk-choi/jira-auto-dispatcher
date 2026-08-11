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
    job = {"ticket": "PROJ-1", "autonomy_mode": "A", "branch": "auto/PROJ-1"}
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
    assert http.posts[0][0] == "http://central:8787/dispatch/u1/PROJ-1/status"


def test_loop_interrupted_then_resume_to_done():
    job = {"ticket": "PROJ-2", "autonomy_mode": "B"}
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


def test_loop_notifies_on_terminal_done():
    """터미널(done) 결과에서 완료 알림(notify)이 호출된다(주입 대역으로 검증)."""
    job = {"ticket": "PROJ-N", "autonomy_mode": "A", "branch": "auto/PROJ-N"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1", mr_url="http://mr/1")
    seen = []
    w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                  run=lambda *a, **k: result, resume=lambda *a, **k: result,
                  sleep=lambda s: None, now=_NOW, max_iterations=2,
                  notify=lambda cfg, res, j, creds: seen.append((res.status, j["ticket"])))
    assert seen == [(ar.STATUS_DONE, "PROJ-N")]


def test_loop_notify_failure_does_not_kill_job():
    """알림 대역이 던져도 잡/루프는 죽지 않고 채널 F 회신은 정상(격리)."""
    job = {"ticket": "PROJ-NF", "autonomy_mode": "B"}
    http = FakeHTTP([FakeResp(200, job), FakeResp(204)])
    result = ar.AgentResult(status=ar.STATUS_DONE, session_id="s1")

    def boom_notify(*a, **k):
        raise RuntimeError("chat down")

    n = w.worker_loop(_cfg(), env=dict(_ENV), http=http,
                      run=lambda *a, **k: result, resume=lambda *a, **k: result,
                      sleep=lambda s: None, now=_NOW, max_iterations=2,
                      notify=boom_notify)
    assert n == 2
    # 알림 실패에도 최종 회신(완료)은 남는다.
    assert _statuses(http) == ["진행중", "완료"]


def test_loop_exception_isolation_does_not_crash():
    job = {"ticket": "PROJ-3"}
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


# --- copy_worker_settings (두 번째 spawn 버그 픽스: 파일 바인드 → 부팅 복사) ---------


def test_copy_worker_settings_copies_when_source_present(tmp_path):
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"permissions": {"defaultMode": "bypassPermissions"}}', encoding="utf-8")
    config_dir = tmp_path / "claude"

    env = {
        "SECRETS_DIR": str(secrets),
        "DISPATCH_USER": "u1",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    dest = main.copy_worker_settings(env=env)
    assert dest == str(config_dir / "settings.json")
    # dest 파일이 실제로 만들어졌고 내용이 소스와 동일(멱등 복사).
    assert (config_dir / "settings.json").read_text(encoding="utf-8") == src.read_text(
        encoding="utf-8"
    )


def test_copy_worker_settings_idempotent_overwrites(tmp_path):
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text("V1", encoding="utf-8")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    # 이미 낡은 dest가 존재해도 최신 소스로 덮어쓴다.
    (config_dir / "settings.json").write_text("OLD", encoding="utf-8")

    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    # 두 번 호출해도 예외 없이 최신 내용으로 수렴(멱등).
    main.copy_worker_settings(env=env)
    src.write_text("V2", encoding="utf-8")
    dest = main.copy_worker_settings(env=env)
    assert (config_dir / "settings.json").read_text(encoding="utf-8") == "V2"
    assert dest == str(config_dir / "settings.json")


def test_copy_worker_settings_missing_source_warns_no_exception(tmp_path, caplog):
    import logging

    secrets = tmp_path / "secrets"  # 존재하지 않는 소스
    config_dir = tmp_path / "claude"
    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    with caplog.at_level(logging.WARNING, logger="jad.main"):
        dest = main.copy_worker_settings(env=env)
    assert dest is None                       # 복사 없음
    assert not (config_dir / "settings.json").exists()
    assert any("소스 없음" in r.getMessage() for r in caplog.records)


def test_copy_worker_settings_missing_env_warns_no_exception(caplog):
    import logging

    with caplog.at_level(logging.WARNING, logger="jad.main"):
        dest = main.copy_worker_settings(env={})   # SECRETS_DIR/DISPATCH_USER 미설정
    assert dest is None
    assert any("SECRETS_DIR" in r.getMessage() for r in caplog.records)


def test_copy_worker_settings_removes_dest_dir_before_copy(tmp_path):
    """하드닝: dest(settings.json)가 과거 실패 바인드 잔재로 **디렉토리**로 남아
    있으면 copyfile이 IsADirectoryError로 죽는다 → 디렉토리를 제거하고 파일로 복사.
    """
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text('{"ok": true}', encoding="utf-8")
    config_dir = tmp_path / "claude"
    config_dir.mkdir()
    # dest가 디렉토리로 잔존(실패 바인드 잔재 재현) — 안에 파일도 하나 둔다.
    dest = config_dir / "settings.json"
    dest.mkdir()
    (dest / "leftover").write_text("stale", encoding="utf-8")

    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1",
           "CLAUDE_CONFIG_DIR": str(config_dir)}
    result = main.copy_worker_settings(env=env)

    assert result == str(dest)
    # 이제 dest는 (디렉토리가 아니라) 파일이고 소스 내용과 동일하다.
    assert dest.is_file()
    assert dest.read_text(encoding="utf-8") == '{"ok": true}'
    # 두 번째 호출도 멱등(이미 파일 → 그대로 덮어쓰기, 예외 없음).
    main.copy_worker_settings(env=env)
    assert dest.is_file()


def test_copy_worker_settings_default_config_dir_and_injection(tmp_path):
    """CLAUDE_CONFIG_DIR 미설정 시 기본 상수 사용 + copyfile/makedirs 주입 검증."""
    secrets = tmp_path / "secrets"
    src = secrets / "u1" / "claude-settings.json"
    src.parent.mkdir(parents=True)
    src.write_text("X", encoding="utf-8")

    made = []
    copied = []
    env = {"SECRETS_DIR": str(secrets), "DISPATCH_USER": "u1"}  # CLAUDE_CONFIG_DIR 없음
    dest = main.copy_worker_settings(
        env=env,
        copyfile=lambda s, d: copied.append((s, d)),
        makedirs=lambda p, exist_ok=False: made.append((p, exist_ok)),
    )
    import os as _os

    expected_dest = _os.path.join(main.DEFAULT_CLAUDE_CONFIG_DIR, "settings.json")
    assert dest == expected_dest
    assert made == [(main.DEFAULT_CLAUDE_CONFIG_DIR, True)]
    assert copied == [(str(src), expected_dest)]
