"""agent_runner 단위테스트 — 파싱 유틸·커맨드/프롬프트/env 구성·실행(мock Popen).

라이브 claude는 절대 호출하지 않는다(상위가 통합단계서 검증). subprocess는
FakeProc로 대체해 stream-json 소비 로직만 검증한다.
"""

from __future__ import annotations

from types import SimpleNamespace

from app import agent_runner as ar


# --- 픽스처 유사 헬퍼 --------------------------------------------------------


def _cfg(tmp_path, base_dir=None):
    return SimpleNamespace(
        run=SimpleNamespace(
            claude_bin="claude",
            output_format="stream-json",
            orchestrator_repo=str(tmp_path / "orch"),
        ),
        secrets=SimpleNamespace(base_dir=base_dir or str(tmp_path / "secrets")),
        resume=SimpleNamespace(reset_buffer_sec=120),
    )


class FakeProc:
    """Popen 대역 — stdout 라인 이터러블 + wait()/returncode."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self._rc = returncode

    def wait(self):
        return self._rc


def _factory(lines, returncode=0, capture=None):
    def make(cmd, cwd=None, env=None):
        if capture is not None:
            capture["cmd"] = cmd
            capture["cwd"] = cwd
            capture["env"] = env
        return FakeProc(lines, returncode=returncode)

    return make


# --- parse_stream_event ------------------------------------------------------


def test_parse_stream_event_valid_invalid_and_nonobject():
    assert ar.parse_stream_event('{"type":"system"}') == {"type": "system"}
    assert ar.parse_stream_event("") is None
    assert ar.parse_stream_event("   ") is None
    assert ar.parse_stream_event("not json") is None
    assert ar.parse_stream_event("[1,2,3]") is None  # 객체 아님
    # ANSI 섞여도 파싱
    assert ar.parse_stream_event('\x1b[32m{"a":1}\x1b[0m') == {"a": 1}


# --- extract_session_id ------------------------------------------------------


def test_extract_session_id_toplevel_and_nested_and_aliases():
    assert ar.extract_session_id({"session_id": "abc"}) == "abc"
    assert ar.extract_session_id({"type": "system", "sessionId": "xyz"}) == "xyz"
    # 중첩 컨테이너
    assert ar.extract_session_id({"type": "x", "data": {"session_id": "nested"}}) == "nested"
    assert ar.extract_session_id({"init": {"session": "s9"}}) == "s9"
    assert ar.extract_session_id({"type": "result"}) is None
    assert ar.extract_session_id(None) is None


# --- detect_limit_and_reset --------------------------------------------------


def test_detect_limit_none_when_normal():
    ev = {"type": "result", "subtype": "success", "is_error": False, "result": "done"}
    is_limit, reset = ar.detect_limit_and_reset(ev)
    assert is_limit is False and reset is None


def test_detect_limit_with_epoch_reset_in_text():
    text = "Claude usage limit reached, resets at 1893456000"
    is_limit, reset = ar.detect_limit_and_reset(text)
    assert is_limit is True
    assert reset is not None and reset.startswith("2030-")  # 1893456000 = 2030-01-01Z


def test_detect_limit_with_iso_reset_in_text():
    text = "rate limit; reset at 2099-01-01T00:00:00Z"
    is_limit, reset = ar.detect_limit_and_reset(text)
    assert is_limit is True and reset == "2099-01-01T00:00:00Z"


def test_detect_limit_structured_reset_field_epoch():
    ev = {"type": "result", "is_error": True, "error": "usage limit exceeded",
          "reset_at": 1893456000}
    is_limit, reset = ar.detect_limit_and_reset(ev)
    assert is_limit is True and reset is not None and reset.startswith("2030-")


# --- build_prompt ------------------------------------------------------------


def test_build_prompt_binds_trigger_ticket_as_work_ticket():
    job = {"ticket": "HAN-142", "autonomy_mode": "A", "target_repos": ["portal-frontend"],
           "context_refs": {"dlc_meta": "/app/dlc-meta", "dataspace_docs": "/app/ds"}}
    prompt = ar.build_prompt(job, None)
    assert "HAN-142" in prompt
    assert "새 사이클 티켓을 만들지 말고" in prompt   # 트리거=작업 티켓
    assert "autonomy_mode=A" in prompt
    assert "auto/HAN-142" in prompt
    assert "runs/HAN-142/" in prompt
    assert "portal-frontend" in prompt
    assert "/app/dlc-meta" in prompt


def test_build_prompt_mode_b_wording():
    prompt = ar.build_prompt({"ticket": "HAN-9", "autonomy_mode": "B"}, None)
    assert "autonomy_mode=B" in prompt
    assert "경량 1차" in prompt


# --- build_command -----------------------------------------------------------


def test_build_command_new_flags_and_deterministic_session_id():
    job = {"ticket": "HAN-142", "autonomy_mode": "A"}
    cfg = SimpleNamespace(run=SimpleNamespace(claude_bin="claude", output_format="stream-json"))
    cmd = ar.build_command(job, cfg)
    assert cmd[0] == "claude" and cmd[1] == "-p"
    assert "--output-format" in cmd and "stream-json" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--session-id" in cmd
    sid = cmd[cmd.index("--session-id") + 1]
    assert sid == ar.deterministic_session_id("HAN-142")
    # 결정성: 재구성해도 동일
    cmd2 = ar.build_command(job, cfg)
    assert cmd2[cmd2.index("--session-id") + 1] == sid
    # 프롬프트가 인자에 포함
    assert any("HAN-142" in a for a in cmd)


def test_build_command_resume_uses_resume_and_from_pr():
    job = {"ticket": "HAN-142"}
    cfg = SimpleNamespace(run=SimpleNamespace(claude_bin="claude", output_format="stream-json"))
    cmd = ar.build_command(job, cfg, resume=True, session_id="sess-1", from_pr=5)
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-1"
    assert "--from-pr" in cmd and cmd[cmd.index("--from-pr") + 1] == "5"
    assert "--session-id" not in cmd


# --- build_env ---------------------------------------------------------------


def _write_secret(base, ref, value):
    import os
    path = os.path.join(base, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value)
    return path


def test_build_env_injects_identity_and_tokens_no_github(tmp_path):
    base = str(tmp_path / "secrets")
    _write_secret(base, "users/u1/jira-token", "JIRA-SECRET-VALUE")
    _write_secret(base, "users/u1/gitlab-token", "GITLAB-SECRET-VALUE")
    _write_secret(base, "users/u1/claude-token", "CLAUDE-SECRET-VALUE")

    cfg = _cfg(tmp_path, base_dir=base)
    creds = ar.UserCreds(
        user="u1", git_name="Yun", git_email="yh@x.com", jira_email="yh@x.com",
        jira_token_ref="users/u1/jira-token",
        gitlab_token_ref="users/u1/gitlab-token",
        claude_oauth_token_ref="users/u1/claude-token",
    )
    base_env = {"PATH": "/bin", "GITHUB_TOKEN": "gh-should-be-removed", "GH_TOKEN": "gh2"}
    env, secrets = ar.build_env({"ticket": "HAN-1"}, creds, cfg, base_env=base_env)

    assert env["GIT_AUTHOR_NAME"] == "Yun" and env["GIT_COMMITTER_NAME"] == "Yun"
    assert env["GIT_AUTHOR_EMAIL"] == "yh@x.com" and env["GIT_COMMITTER_EMAIL"] == "yh@x.com"
    assert env["JIRA_EMAIL"] == "yh@x.com"
    assert env["JIRA_API_TOKEN"] == "JIRA-SECRET-VALUE"
    assert env["GITLAB_TOKEN"] == "GITLAB-SECRET-VALUE"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "CLAUDE-SECRET-VALUE"
    # GitHub 계열은 절대 남지 않는다
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env
    # 파일경로도 노출(오케스트레이터 파일 참조용)
    assert env["JIRA_TOKEN_FILE"].endswith("jira-token")
    # 시크릿 값 목록(마스킹용)
    assert set(secrets) == {"JIRA-SECRET-VALUE", "GITLAB-SECRET-VALUE", "CLAUDE-SECRET-VALUE"}


def test_build_env_claude_token_env_fallback(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1", claude_oauth_token_value="ENV-CLAUDE-TOKEN")
    env, secrets = ar.build_env({"ticket": "HAN-1"}, creds, cfg, base_env={})
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ENV-CLAUDE-TOKEN"
    assert "ENV-CLAUDE-TOKEN" in secrets


# --- run_job / resume_job (mock Popen) --------------------------------------


def test_run_job_success_extracts_session_and_mr(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-abc"}\n',
        '{"type":"assistant","message":"working"}\n',
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"created MR https://gitlab.example.com/g/p/-/merge_requests/7"}\n',
    ]
    cap = {}
    res = ar.run_job({"ticket": "HAN-1", "autonomy_mode": "A"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0, capture=cap))
    assert res.status == ar.STATUS_DONE
    assert res.session_id == "sess-abc"
    assert res.mr_url == "https://gitlab.example.com/g/p/-/merge_requests/7"
    assert res.returncode == 0
    # cwd = orchestrator_repo
    assert cap["cwd"] == str(tmp_path / "orch")


def test_run_job_limit_returns_interrupted_with_reset(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-xyz"}\n',
        '{"type":"result","is_error":true,"subtype":"error_max_turns",'
        '"error":"usage limit reached","reset_at":"2099-01-01T00:00:00Z"}\n',
    ]
    res = ar.run_job({"ticket": "HAN-2"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=1))
    assert res.status == ar.STATUS_INTERRUPTED
    assert res.session_id == "sess-xyz"
    assert res.reset_at == "2099-01-01T00:00:00Z"


def test_run_job_failure_when_rc_nonzero_no_limit(tmp_path):
    cfg = _cfg(tmp_path)
    res = ar.run_job({"ticket": "HAN-3"}, ar.UserCreds(user="u1"), cfg,
                     popen_factory=_factory(['{"type":"result","is_error":true,'
                                             '"result":"boom"}\n'], returncode=2))
    assert res.status == ar.STATUS_FAILED and res.returncode == 2


def test_run_job_redacts_secret_from_log_summary(tmp_path):
    base = str(tmp_path / "secrets")
    _write_secret(base, "u1/jira", "SUPER-SECRET-TOKEN-123")
    cfg = _cfg(tmp_path, base_dir=base)
    creds = ar.UserCreds(user="u1", jira_token_ref="u1/jira")
    # 프로세스가 실수로 토큰을 출력해도 요약에서 마스킹돼야 한다
    lines = [
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"leaked SUPER-SECRET-TOKEN-123 oops"}\n',
    ]
    res = ar.run_job({"ticket": "HAN-4"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0))
    assert "SUPER-SECRET-TOKEN-123" not in res.log_summary
    assert "***" in res.log_summary


def test_resume_job_uses_resume_flag_and_keeps_session(tmp_path):
    cfg = _cfg(tmp_path)
    cap = {}
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.resume_job({"ticket": "HAN-5"}, "sess-keep", ar.UserCreds(user="u1"), cfg,
                        popen_factory=_factory(lines, returncode=0, capture=cap))
    assert res.status == ar.STATUS_DONE
    assert res.session_id == "sess-keep"      # 결과에 session_id 없으면 재개 id 유지
    assert "--resume" in cap["cmd"] and "sess-keep" in cap["cmd"]


def test_run_job_spawn_error_is_failed_not_raised(tmp_path):
    cfg = _cfg(tmp_path)

    def boom(cmd, cwd=None, env=None):
        raise FileNotFoundError("claude not found")

    res = ar.run_job({"ticket": "HAN-6"}, ar.UserCreds(user="u1"), cfg, popen_factory=boom)
    assert res.status == ar.STATUS_FAILED
