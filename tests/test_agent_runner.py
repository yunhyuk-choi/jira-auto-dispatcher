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
    text = "usage limit; reset at 2099-01-01T00:00:00Z"
    is_limit, reset = ar.detect_limit_and_reset(text)
    assert is_limit is True and reset == "2099-01-01T00:00:00Z"


def test_detect_limit_structured_reset_field_epoch():
    ev = {"type": "result", "is_error": True, "error": "usage limit exceeded",
          "reset_at": 1893456000}
    is_limit, reset = ar.detect_limit_and_reset(ev)
    assert is_limit is True and reset is not None and reset.startswith("2030-")


# --- 패턴 축소(오탐 방지) 회귀: 광의 문구는 이제 한도로 보지 않는다 -----------


def test_detect_limit_false_for_bare_rate_limit_and_broad_phrases():
    # 일반 코드/콘텐츠/HTTP 문맥에 흔한 문구는 이제 한도(usage-limit)로 보지 않는다.
    for text in (
        "rate limit",
        "ratelimit exceeded",
        "the rate-limit middleware returned 429",
        "limit reached for the pagination cursor",
        "limit exceeded on array bounds",
        "429 too many requests",
        "too many requests to the search endpoint",
    ):
        is_limit, _ = ar.detect_limit_and_reset(text)
        assert is_limit is False, text


def test_detect_limit_true_for_usage_and_quota_phrases():
    # "usage/quota/사용량 한도"에 특정된 문구만 한도로 본다(영/한).
    for text in (
        "Claude usage limit reached. reset at 2099-01-01T00:00:00Z",
        "usage limit exceeded",
        "you are out of quota",
        "out of usage",
        "quota exceeded",
        "사용 한도에 도달했습니다",
        "사용량 한도 초과",
        "한도 도달",
        "한도 초과",
    ):
        is_limit, _ = ar.detect_limit_and_reset(text)
        assert is_limit is True, text


# --- build_prompt ------------------------------------------------------------


def test_build_prompt_binds_trigger_ticket_as_work_ticket():
    job = {"ticket": "PROJ-142", "autonomy_mode": "A", "target_repos": ["portal-frontend"],
           "context_refs": {"dlc_meta": "/app/dlc-meta", "dataspace_docs": "/app/ds"}}
    prompt = ar.build_prompt(job, None)
    assert "PROJ-142" in prompt
    assert "새 사이클 티켓을 만들지 말고" in prompt   # 트리거=작업 티켓
    assert "autonomy_mode=A" in prompt
    assert "auto/PROJ-142" in prompt
    assert "runs/PROJ-142/" in prompt
    assert "portal-frontend" in prompt
    assert "/app/dlc-meta" in prompt


def test_build_prompt_mode_a_leaves_mr_link_comment():
    """A 모드: 종료 문구가 MR 링크 코멘트로 남기고 진행 중(리뷰 대기)."""
    prompt = ar.build_prompt({"ticket": "PROJ-142", "autonomy_mode": "A"}, None)
    assert "MR 링크를 코멘트로 남기고" in prompt
    assert "'리뷰 대기'" in prompt
    # A는 여전히 MR 초안 생성.
    assert "MR 초안" in prompt


def test_build_prompt_mode_b_wording():
    prompt = ar.build_prompt({"ticket": "PROJ-9", "autonomy_mode": "B"}, None)
    assert "autonomy_mode=B" in prompt
    assert "경량 1차" in prompt


def test_build_prompt_mode_b_pushes_branch_and_leaves_branch_journal_comment():
    """B 모드 핸드오프 수정: auto/<ticket> 원격 push 지시 + 브랜치/저널 코멘트 지시,
    MR은 만들지 않음(하드코딩 'MR 링크' 문구가 B에 새지 않아야 한다)."""
    prompt = ar.build_prompt({"ticket": "PROJ-9", "autonomy_mode": "B"}, None)
    # 원격(GitLab) push를 반드시 하라는 지시.
    assert "원격" in prompt and "push" in prompt
    assert "auto/PROJ-9" in prompt
    # 종료 시 브랜치명 + runs 저널 위치를 코멘트로 남긴다.
    assert "runs/PROJ-9/" in prompt
    assert "fetch" in prompt  # 로컬에서 fetch해 이어서 완성
    # B는 MR을 만들지 않는다 — 'MR 링크를 코멘트로' 문구가 새지 않아야 한다.
    assert "MR 링크를 코멘트로 남기고" not in prompt
    assert "MR은 만들지 말라" in prompt


# --- build_command -----------------------------------------------------------


def test_build_command_persistent_session_flags_and_deterministic_session_id():
    """지속 세션(기본): -p --input-format stream-json --output-format stream-json
    --verbose --dangerously-skip-permissions --session-id <uuid5>. **프롬프트는 인자에
    넣지 않는다**(stdin으로 주입) — HAN-537 지혈(one-shot 종결)."""
    job = {"ticket": "PROJ-142", "autonomy_mode": "A"}
    cfg = SimpleNamespace(run=SimpleNamespace(claude_bin="claude", output_format="stream-json"))
    cmd = ar.build_command(job, cfg)
    assert cmd[0] == "claude" and cmd[1] == "-p"
    # 양방향 stream-json 입력(살아있는 세션의 핵심).
    assert "--input-format" in cmd
    assert cmd[cmd.index("--input-format") + 1] == "stream-json"
    assert "--output-format" in cmd and "stream-json" in cmd
    # stream-json + --print은 CLI가 --verbose를 강제한다(실배포 확인 버그).
    assert "--verbose" in cmd
    assert "--dangerously-skip-permissions" in cmd
    assert "--session-id" in cmd
    sid = cmd[cmd.index("--session-id") + 1]
    assert sid == ar.deterministic_session_id("PROJ-142")
    # 결정성: 재구성해도 동일
    cmd2 = ar.build_command(job, cfg)
    assert cmd2[cmd2.index("--session-id") + 1] == sid
    # ⚠️ 프롬프트/티켓은 **인자에 없어야 한다**(stdin 주입) — one-shot 종결의 표식.
    assert not any("PROJ-142" in a for a in cmd)
    assert "오케스트레이터" not in " ".join(cmd)


def test_build_command_legacy_oneshot_when_persistent_disabled():
    """persistent_session=False면 레거시 one-shot(프롬프트를 인자로)로 폴백(안전 롤백)."""
    job = {"ticket": "PROJ-142", "autonomy_mode": "A"}
    cfg = SimpleNamespace(run=SimpleNamespace(
        claude_bin="claude", output_format="stream-json", persistent_session=False))
    cmd = ar.build_command(job, cfg)
    assert cmd[1] == "-p"
    assert "--input-format" not in cmd            # 양방향 입력 안 씀
    assert any("PROJ-142" in a for a in cmd)       # 프롬프트가 인자에 포함
    assert "--verbose" in cmd and "--session-id" in cmd


def test_persistent_enabled_requires_stream_json_both_ways():
    mk = lambda **kw: SimpleNamespace(run=SimpleNamespace(**kw))
    assert ar.persistent_enabled(mk(output_format="stream-json")) is True
    assert ar.persistent_enabled(mk(output_format="json")) is False
    assert ar.persistent_enabled(mk(output_format="stream-json", input_format="text")) is False
    assert ar.persistent_enabled(mk(output_format="stream-json", persistent_session=False)) is False


def test_build_command_resume_uses_resume_and_from_pr():
    job = {"ticket": "PROJ-142"}
    cfg = SimpleNamespace(run=SimpleNamespace(claude_bin="claude", output_format="stream-json"))
    cmd = ar.build_command(job, cfg, resume=True, session_id="sess-1", from_pr=5)
    assert "--resume" in cmd and cmd[cmd.index("--resume") + 1] == "sess-1"
    assert "--from-pr" in cmd and cmd[cmd.index("--from-pr") + 1] == "5"
    assert "--session-id" not in cmd
    # resume 경로도 stream-json이면 --verbose를 포함해야 한다.
    assert "--verbose" in cmd


def test_build_command_omits_verbose_when_not_stream_json():
    job = {"ticket": "PROJ-142"}
    cfg = SimpleNamespace(run=SimpleNamespace(claude_bin="claude", output_format="json"))
    cmd = ar.build_command(job, cfg)
    assert "--output-format" in cmd and "json" in cmd
    # stream-json이 아니면 --verbose를 붙이지 않는다(방어적).
    assert "--verbose" not in cmd
    cmd_resume = ar.build_command(job, cfg, resume=True, session_id="s")
    assert "--verbose" not in cmd_resume


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
    env, secrets = ar.build_env({"ticket": "PROJ-1"}, creds, cfg, base_env=base_env)

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
    env, secrets = ar.build_env({"ticket": "PROJ-1"}, creds, cfg, base_env={})
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
    res = ar.run_job({"ticket": "PROJ-1", "autonomy_mode": "A"}, creds, cfg,
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
    res = ar.run_job({"ticket": "PROJ-2"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=1))
    assert res.status == ar.STATUS_INTERRUPTED
    assert res.session_id == "sess-xyz"
    assert res.reset_at == "2099-01-01T00:00:00Z"


def test_run_job_no_false_limit_from_midstream_content(tmp_path):
    """중간 이벤트(assistant/tool result)의 텍스트에 'rate limit'·'limit reached'·
    '429 too many requests'가 있어도 한도로 오판하지 않는다(핵심 회귀).

    최종 result 이벤트는 정상 성공 → interrupted 아님, done.
    """
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-mid"}\n',
        # 오케스트레이터가 작업 중 코드/콘텐츠에 흔한 문구를 흘림 — 오탐 유발 소지.
        '{"type":"assistant","message":"editing the rate limit middleware; '
        'note: limit reached branch"}\n',
        '{"type":"user","message":"tool result: HTTP 429 too many requests from upstream"}\n',
        '{"type":"result","subtype":"success","is_error":false,"result":"done"}\n',
    ]
    res = ar.run_job({"ticket": "PROJ-mid"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0))
    assert res.status == ar.STATUS_DONE          # interrupted 아님
    assert res.reset_at is None
    assert res.session_id == "sess-mid"


def test_run_job_real_usage_limit_result_is_interrupted(tmp_path):
    """진짜 usage-limit result 이벤트면 interrupted + reset_at."""
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-lim"}\n',
        # 중간엔 광의 문구가 섞여도 무해해야 한다.
        '{"type":"assistant","message":"retrying after rate limit backoff"}\n',
        '{"type":"result","is_error":true,'
        '"result":"Claude usage limit reached. reset at 2099-01-01T00:00:00Z"}\n',
    ]
    res = ar.run_job({"ticket": "PROJ-lim"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=1))
    assert res.status == ar.STATUS_INTERRUPTED
    assert res.reset_at == "2099-01-01T00:00:00Z"
    assert res.session_id == "sess-lim"


def test_run_job_generic_error_result_is_failed_not_interrupted(tmp_path):
    """한도가 아닌 일반 에러 result(usage-limit 아님)는 failed."""
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = [
        '{"type":"system","subtype":"init","session_id":"sess-err"}\n',
        # 에러 텍스트에 광의 문구가 있어도 한도가 아니므로 failed여야 한다.
        '{"type":"result","is_error":true,"subtype":"error_during_execution",'
        '"result":"downstream service rate limit / 429 too many requests"}\n',
    ]
    res = ar.run_job({"ticket": "PROJ-err"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=1))
    assert res.status == ar.STATUS_FAILED
    assert res.reset_at is None


def test_run_job_failure_when_rc_nonzero_no_limit(tmp_path):
    cfg = _cfg(tmp_path)
    res = ar.run_job({"ticket": "PROJ-3"}, ar.UserCreds(user="u1"), cfg,
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
    res = ar.run_job({"ticket": "PROJ-4"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0))
    assert "SUPER-SECRET-TOKEN-123" not in res.log_summary
    assert "***" in res.log_summary


def test_resume_job_uses_resume_flag_and_keeps_session(tmp_path):
    cfg = _cfg(tmp_path)
    cap = {}
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.resume_job({"ticket": "PROJ-5"}, "sess-keep", ar.UserCreds(user="u1"), cfg,
                        popen_factory=_factory(lines, returncode=0, capture=cap))
    assert res.status == ar.STATUS_DONE
    assert res.session_id == "sess-keep"      # 결과에 session_id 없으면 재개 id 유지
    assert "--resume" in cap["cmd"] and "sess-keep" in cap["cmd"]


def test_run_job_spawn_error_is_failed_not_raised(tmp_path):
    cfg = _cfg(tmp_path)

    def boom(cmd, cwd=None, env=None):
        raise FileNotFoundError("claude not found")

    res = ar.run_job({"ticket": "PROJ-6"}, ar.UserCreds(user="u1"), cfg, popen_factory=boom)
    assert res.status == ar.STATUS_FAILED


# --- ensure_repos 프로비저닝 훅 ----------------------------------------------


def test_run_job_calls_ensure_repos_with_user_gitlab_token(tmp_path):
    base = str(tmp_path / "secrets")
    _write_secret(base, "u1/gitlab", "GL-USER-TOKEN")
    cfg = _cfg(tmp_path, base_dir=base)
    creds = ar.UserCreds(user="u1", gitlab_token_ref="u1/gitlab")

    seen = {}

    def fake_ensure(config, token, *, fresh=True):
        seen["config"] = config
        seen["token"] = token
        seen["fresh"] = fresh
        return {"orchestrator": "reset", "dlc_meta": "reset", "dataspace_docs": "cloned"}

    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.run_job({"ticket": "PROJ-7"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0),
                     ensure_repos_fn=fake_ensure)
    # ensure_repos가 사용자 GitLab 토큰 값으로 호출됐다(build_env와 동일 소스).
    assert seen["token"] == "GL-USER-TOKEN"
    assert seen["config"] is cfg
    # 신규 잡(run_job)은 fresh=True로 프로비저닝(dirty 워크스페이스 강제 정합).
    assert seen["fresh"] is True
    # 프로비저닝 성공 → 잡은 정상 진행.
    assert res.status == ar.STATUS_DONE


def test_resume_job_provisions_with_fresh_false_to_preserve_worktree(tmp_path):
    # 재개는 진행 중 작업 보존을 위해 fresh=False(파괴적 reset 금지)로 프로비저닝한다.
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    seen = {}

    def fake_ensure(config, token, *, fresh=True):
        seen["fresh"] = fresh
        return {"orchestrator": "pulled", "dlc_meta": "pulled", "dataspace_docs": "pulled"}

    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.resume_job({"ticket": "PROJ-7R"}, "sess-r", creds, cfg,
                        popen_factory=_factory(lines, returncode=0),
                        ensure_repos_fn=fake_ensure)
    assert seen["fresh"] is False  # 재개는 절대 파괴적 reset을 하지 않는다
    assert res.status == ar.STATUS_DONE


def test_run_job_aborts_when_orchestrator_repo_provisioning_fails(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")

    calls = {"popen": 0}

    def never(cmd, cwd=None, env=None):  # claude는 절대 실행되면 안 된다
        calls["popen"] += 1
        raise AssertionError("popen 호출되면 안 됨")

    res = ar.run_job(
        {"ticket": "PROJ-8"}, creds, cfg,
        popen_factory=never,
        ensure_repos_fn=lambda config, token, *, fresh=True: {
            "orchestrator": "err: auth failed"
        },
    )
    assert res.status == ar.STATUS_FAILED
    assert "repos-error" in res.log_summary
    assert calls["popen"] == 0


def test_run_job_proceeds_when_nonorch_repo_fails(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.run_job(
        {"ticket": "PROJ-9"}, creds, cfg,
        popen_factory=_factory(lines, returncode=0),
        ensure_repos_fn=lambda config, token, *, fresh=True: {
            "orchestrator": "cloned", "dlc_meta": "err: pull conflict",
        },
    )
    # orchestrator는 준비됨 → dlc_meta 실패는 비치명적, 잡 진행.
    assert res.status == ar.STATUS_DONE


def test_resume_job_provisions_and_keeps_session_on_fatal(tmp_path):
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")

    def never(cmd, cwd=None, env=None):
        raise AssertionError("popen 호출되면 안 됨")

    res = ar.resume_job(
        {"ticket": "PROJ-10"}, "sess-keep", creds, cfg,
        popen_factory=never,
        ensure_repos_fn=lambda config, token, *, fresh=True: {
            "orchestrator": "err: boom"
        },
    )
    assert res.status == ar.STATUS_FAILED
    assert res.session_id == "sess-keep"  # 치명 실패에도 재개 키 유지


def test_run_job_default_ensure_repos_no_token_is_noop(tmp_path):
    # gitlab 토큰 참조 없음 → 기본 ensure_repos가 전체 skip(라이브 git 미호출) → 진행.
    cfg = _cfg(tmp_path)
    creds = ar.UserCreds(user="u1")
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    res = ar.run_job({"ticket": "PROJ-11"}, creds, cfg,
                     popen_factory=_factory(lines, returncode=0))
    assert res.status == ar.STATUS_DONE


# --- 프로세스 그룹 teardown(좀비/고아 방지, 방어심층 #2) ----------------------
#
# 실 런타임(Linux)에서 claude 를 새 프로세스 그룹 리더로 띄우고(start_new_session),
# 잡 종료 시 그룹 전체를 kill해 git/esbuild 손자가 고아→PID1 reparent→좀비화되는
# 것을 원천 차단한다. 테스트 호스트는 Windows(os.killpg/getpgid/setsid 없음)라
# POSIX 호출을 monkeypatch로 주입해 **Linux 경로의 로직**을 검증한다.


class _GroupProc:
    """pid를 가진 Popen 대역 — terminate/kill/wait 기록(그룹 teardown 검증용)."""

    def __init__(self, pid=4321, wait_raises=False):
        self.pid = pid
        self.stdout = iter([])
        self.returncode = 0
        self.terminated = False
        self.killed = False
        self._wait_raises = wait_raises

    def wait(self, timeout=None):
        if self._wait_raises:
            raise subprocess_timeout()
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def subprocess_timeout():
    import subprocess

    return subprocess.TimeoutExpired(cmd="claude", timeout=5)


def _patch_posix_group(monkeypatch, *, getpgid_ok=True):
    """os.getpgid/os.killpg 를 주입(Windows에도 없던 속성 추가). killpg 호출 기록 반환."""
    import os as _os

    calls = []
    if getpgid_ok:
        monkeypatch.setattr(_os, "getpgid", lambda pid: pid, raising=False)
    else:
        def _boom(pid):
            raise ProcessLookupError("gone")

        monkeypatch.setattr(_os, "getpgid", _boom, raising=False)
    monkeypatch.setattr(_os, "killpg", lambda pgid, sig: calls.append((pgid, sig)), raising=False)
    return calls


def test_terminate_proc_kills_process_group_on_posix(monkeypatch):
    # Linux 경로: 그룹 SIGTERM 이 리더 pgid 로 전송되고, 개별 terminate 폴백은 쓰지 않는다.
    calls = _patch_posix_group(monkeypatch)
    proc = _GroupProc(pid=4321)
    ar._terminate_proc(proc)
    # 그룹 SIGTERM 전송(pgid=pid=4321).
    assert (4321, ar._SIGTERM) in calls
    # 그룹 kill이 성공했으므로 개별 terminate 폴백은 호출되지 않는다.
    assert proc.terminated is False


def test_terminate_proc_sigkill_on_grace_timeout(monkeypatch):
    # SIGTERM 후 유예 초과(wait timeout) → 그룹 SIGKILL 로 승격.
    calls = _patch_posix_group(monkeypatch)
    proc = _GroupProc(pid=999, wait_raises=True)
    ar._terminate_proc(proc)
    sigs = [sig for _pgid, sig in calls]
    assert ar._SIGTERM in sigs
    assert ar._SIGKILL in sigs
    assert all(pgid == 999 for pgid, _sig in calls)


def test_terminate_proc_falls_back_to_individual_without_group(monkeypatch):
    # 그룹 조회 불가(getpgid 실패=이미 사라짐/비POSIX) → 개별 terminate 폴백, killpg 미호출.
    calls = _patch_posix_group(monkeypatch, getpgid_ok=False)
    proc = _GroupProc(pid=555)
    ar._terminate_proc(proc)
    assert calls == []              # 그룹 kill 시도 없음
    assert proc.terminated is True  # 개별 terminate 폴백


def test_terminate_proc_no_pid_uses_individual(monkeypatch):
    # pid 없는 대역(예: 기존 FakeProc) → 그룹 경로 스킵, 개별 terminate. (Windows 안전)
    calls = _patch_posix_group(monkeypatch)

    class _NoPid:
        def __init__(self):
            self.terminated = False

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            self.terminated = True

        def kill(self):
            pass

    proc = _NoPid()
    ar._terminate_proc(proc)
    assert calls == []
    assert proc.terminated is True


def test_consume_normal_completion_sweeps_process_group(monkeypatch):
    # 정상 종료 후 남은 그룹 손자를 SIGKILL로 쓸어담는다(리더는 wait로 참 rc 얻은 뒤).
    calls = _patch_posix_group(monkeypatch)
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    proc = _GroupProc(pid=777)
    proc.stdout = iter(lines)
    res = ar._consume(proc, [])
    assert res.status == ar.STATUS_DONE
    # 정상 완료 후 그룹 SIGKILL sweep(pgid=777).
    assert (777, ar._SIGKILL) in calls


# --- 지속 세션: 프롬프트 인코딩 / 백그라운드 pending 파싱 -----------------------


def test_encode_user_message_is_stream_json_user_line():
    import json
    line = ar._encode_user_message("PROJ-537 티켓을 수행하라")
    assert line.endswith("\n")
    obj = json.loads(line)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert obj["message"]["content"][0]["text"] == "PROJ-537 티켓을 수행하라"


def test_bg_pending_from_event_snapshot_semantics():
    # background_tasks_changed 의 tasks 배열 = 현 시점 pending 전체 스냅샷(전량 치환).
    started = {"type": "system", "subtype": "background_tasks_changed",
               "tasks": [{"task_id": "t1"}, {"task_id": "t2"}]}
    cleared = {"type": "system", "subtype": "background_tasks_changed", "tasks": []}
    other = {"type": "assistant", "message": "hi"}
    assert ar._bg_pending_from_event(started) == {"t1", "t2"}
    assert ar._bg_pending_from_event(cleared) == set()
    assert ar._bg_pending_from_event(other) is None   # 해당 이벤트 아님 = 변화 없음


# --- HAN-537 재현 시나리오: 지속 세션이 "완료 알림 대기"를 넘어 살아남아 커밋한다 ----


class _StdinCap:
    """stdin 대역 — write 캡처 + close 시점을 공유 로그에 기록."""

    def __init__(self, log):
        self._log = log
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def close(self):
        if not self.closed:
            self.closed = True
            self._log.append("STDIN_CLOSED")


class _PersistProc:
    """지속 세션 Popen 대역 — stdin(양방향) + stdout 제너레이터(소비 시점 로깅)."""

    def __init__(self, tagged_lines, log, returncode=0):
        self._log = log
        self.stdin = _StdinCap(log)
        self._rc = returncode
        self.returncode = returncode

        def _gen():
            for tag, line in tagged_lines:
                self._log.append(("LINE", tag))
                yield line

        self.stdout = _gen()

    def wait(self, timeout=None):
        return self._rc

    def poll(self):
        return None


def test_han537_persistent_session_survives_background_await_and_commits():
    """HAN-537 핵심 회귀: 오케스트레이터가 코드 편집을 **백그라운드 서브에이전트**에
    위임하고 "완료 알림을 기다린다"며 턴을 종료(첫 result)해도, 지속 세션 소비자는
    **세션을 닫지 않고**(stdin open 유지) 백그라운드 완료 → 재개 → 커밋/MR(둘째 result)
    까지 소비해 **DONE**에 도달한다.

    과거 one-shot 경로라면 첫 result(=end_turn "대기합니다")에서 프로세스가 종료돼
    커밋 0 + dirty → FAILED 였다. 여기서는 pending 백그라운드 태스크가 남아있는 동안
    (background_tasks_changed.tasks != []) 첫 result를 **종결로 보지 않음**을 증명한다.
    """
    log = []
    mr = "https://gitlab.example.com/g/p/-/merge_requests/537"
    tagged = [
        ("init", '{"type":"system","subtype":"init","session_id":"sess-537"}\n'),
        # 백그라운드 서브에이전트 위임 → pending 1건 등록.
        ("bg_started",
         '{"type":"system","subtype":"background_tasks_changed",'
         '"tasks":[{"task_id":"a1","task_type":"local_agent"}]}\n'),
        # 오케스트레이터가 "백그라운드 작업이 끝날 때까지 대기합니다"며 턴 종료(첫 result).
        ("result1",
         '{"type":"result","subtype":"success","is_error":false,'
         '"result":"백그라운드 작업이 끝날 때까지 대기합니다"}\n'),
        # 백그라운드 완료 → pending 0으로 스냅샷 치환.
        ("bg_cleared",
         '{"type":"system","subtype":"background_tasks_changed","tasks":[]}\n'),
        # 세션이 살아있어 다음 턴으로 재개 → 커밋 + MR.
        ("resume", '{"type":"assistant","message":"커밋하고 MR 생성"}\n'),
        ("result2",
         '{"type":"result","subtype":"success","is_error":false,'
         '"result":"pushed and opened MR ' + mr + '"}\n'),
    ]
    proc = _PersistProc(tagged, log, returncode=0)
    # 백스톱 워치독 비활성(테스트 결정성) — 완료 판정만 검증.
    res = ar._consume(proc, [], initial_prompt="PROJ-537 티켓을 수행하라",
                      session_max_sec=0, session_idle_sec=0)

    # 결과: 둘째 result까지 도달해 DONE + MR 캡처.
    assert res.status == ar.STATUS_DONE
    assert res.mr_url == mr
    assert res.session_id == "sess-537"

    # 프롬프트가 stdin에 stream-json user 메시지로 주입됐다.
    assert proc.stdin.writes, "티켓 프롬프트가 stdin으로 주입되지 않았다"
    import json as _json
    injected = _json.loads(proc.stdin.writes[0])
    assert injected["type"] == "user"
    assert "PROJ-537" in injected["message"]["content"][0]["text"]

    # ⚠️ 핵심 단언: 첫 result(대기)에서 **세션을 닫지 않았다**. stdin close는 둘째
    # result(진짜 완료) 이후에만 일어난다 → one-shot이라면 못 했을 "턴을 넘긴 await".
    assert "STDIN_CLOSED" in log
    close_idx = log.index("STDIN_CLOSED")
    assert log.index(("LINE", "result1")) < close_idx, "첫 result에서 조기 종료됨(HAN-537 회귀)"
    assert log.index(("LINE", "bg_cleared")) < close_idx
    assert log.index(("LINE", "result2")) < close_idx
    # 첫 result가 종결이었다면 bg_cleared/result2 라인은 소비되지 않았을 것.
    assert ("LINE", "result2") in log


def test_persistent_result_with_pending_bg_is_not_terminal_but_cleared_is():
    """단위: pending 백그라운드가 남은 result는 비종결, 비워진 뒤 result는 종결."""
    log = []
    tagged = [
        ("bg", '{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"x"}]}\n'),
        ("r1", '{"type":"result","subtype":"success","is_error":false,"result":"awaiting"}\n'),
        ("clr", '{"type":"system","subtype":"background_tasks_changed","tasks":[]}\n'),
        ("r2", '{"type":"result","subtype":"success","is_error":false,"result":"done"}\n'),
    ]
    proc = _PersistProc(tagged, log, returncode=0)
    res = ar._consume(proc, [], initial_prompt="go", session_max_sec=0, session_idle_sec=0)
    assert res.status == ar.STATUS_DONE
    # r1에서 닫았다면 r2 라인은 소비되지 않았을 것.
    assert ("LINE", "r2") in log
    assert log.index(("LINE", "r2")) < log.index("STDIN_CLOSED")


def test_persistent_limit_result_is_terminal_even_without_bg_clear():
    """지속 세션이라도 usage-limit result는 그 자리에서 종결(INTERRUPTED)."""
    log = []
    tagged = [
        ("bg", '{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"x"}]}\n'),
        ("lim", '{"type":"result","is_error":true,'
                '"result":"Claude usage limit reached. reset at 2099-01-01T00:00:00Z"}\n'),
    ]
    proc = _PersistProc(tagged, log, returncode=1)
    res = ar._consume(proc, [], initial_prompt="go", session_max_sec=0, session_idle_sec=0)
    assert res.status == ar.STATUS_INTERRUPTED
    assert res.reset_at == "2099-01-01T00:00:00Z"
    assert "STDIN_CLOSED" in log       # 한도 → 즉시 stdin 닫아 종료


def test_default_popen_start_new_session_matches_platform(monkeypatch):
    # POSIX(os.setsid 있음)에서만 start_new_session=True 를 넘긴다(Windows는 미전달).
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(ar.subprocess, "Popen", fake_popen)
    ar._default_popen(["claude"], cwd=None, env={})
    if hasattr(ar.os, "setsid"):
        assert captured.get("start_new_session") is True
    else:
        assert "start_new_session" not in captured


# --- 설계 문서 레포 컨텍스트 참조: 신규 키 우선 + 레거시 키 폴백 --------------


def test_build_prompt_uses_new_docs_context_key():
    job = {"ticket": "PROJ-900", "context_refs": {"docs": "/app/workspace/docs"}}
    prompt = ar.build_prompt(job, None)
    assert "docs=/app/workspace/docs" in prompt


def test_build_prompt_falls_back_to_legacy_docs_context_key():
    """옛 central 이 큐에 넣어 둔 잡(레거시 키)도 그대로 읽는다(무중단 배포 하위호환)."""
    job = {"ticket": "PROJ-901", "context_refs": {"dataspace_docs": "/app/workspace/ds"}}
    prompt = ar.build_prompt(job, None)
    assert "docs=/app/workspace/ds" in prompt


def test_build_prompt_prefers_new_key_when_both_present():
    job = {"ticket": "PROJ-902",
           "context_refs": {"docs": "/new", "dataspace_docs": "/legacy"}}
    prompt = ar.build_prompt(job, None)
    assert "docs=/new" in prompt
    assert "/legacy" not in prompt


def test_build_prompt_omits_docs_when_unset():
    """설계 문서 레포는 선택 — 없으면 컨텍스트 줄에 아예 나오지 않는다."""
    job = {"ticket": "PROJ-903", "context_refs": {"dlc_meta": "/app/dlc-meta"}}
    prompt = ar.build_prompt(job, None)
    assert "docs=" not in prompt
    assert "dlc-meta=/app/dlc-meta" in prompt
