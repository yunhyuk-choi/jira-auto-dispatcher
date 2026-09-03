"""forge 어댑터 단위테스트 — GitLab/GitHub 분기와 **하위호환**을 기계적으로 고정한다.

이 파일이 지키는 것은 두 가지다:

1. **GitHub 에서 인증이 실제로 성립하는가.** 토큰 URL 의 자격 사용자명이 forge 마다
   다르다(GitLab ``oauth2`` / GitHub ``x-access-token``). GitLab 형식을 GitHub 에 쓰면
   인증이 실패하므로, "GitHub URL 에는 GitHub 형식이 박힌다"를 회귀로 못박는다.
2. **기존 GitLab 배포가 그대로 돈다.** 옛 이름(env·레지스트리 키·creds 필드·폼 필드)을
   계속 읽고, 옛 이름으로도 계속 방출한다.

라이브 git/네트워크는 호출하지 않는다.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import config as C
from app import forge as F
from app import repos as R
from app.agent_runner import UserCreds, build_env, extract_mr_url


# --- 헬퍼 -------------------------------------------------------------------


def _cfg(kind="gitlab", base_dir=""):
    """forge 종류/시크릿 루트만 갖춘 최소 config 유사 객체."""
    return SimpleNamespace(
        forge=SimpleNamespace(kind=kind, base_url="", token_ref=""),
        secrets=SimpleNamespace(base_dir=base_dir),
    )


def _write(base: str, rel: str, value: str) -> None:
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value)


# --- 종류 판정 ---------------------------------------------------------------


def test_infer_kind_from_url_only_when_decisive():
    assert F.infer_kind_from_url("https://github.com/o/r.git") == "github"
    assert F.infer_kind_from_url("https://github.corp.example.com/o/r.git") == "github"
    assert F.infer_kind_from_url("https://gitlab.example.com/g/p.git") == "gitlab"
    assert F.infer_kind_from_url("git@github.com:o/r.git") == "github"
    # 중립 호스트는 추측하지 않는다(잘못 찍으면 인증이 깨진다).
    assert F.infer_kind_from_url("https://git.corp.example.com/o/r.git") is None
    assert F.infer_kind_from_url("") is None
    # 임베디드 자격정보가 있어도 호스트를 옳게 뽑는다.
    assert F.infer_kind_from_url("https://oauth2:TOK@github.com/o/r.git") == "github"


def test_kind_for_precedence_url_beats_config():
    """한 배포가 여러 forge 를 섞어 쓴다 — URL 이 밝히면 그 URL 에 한해 URL 이 이긴다."""
    cfg = _cfg("gitlab")
    assert F.kind_for(url="https://github.com/o/r.git", config=cfg) == "github"
    assert F.kind_for(url="https://gitlab.example.com/g/p.git",
                      config=_cfg("github")) == "gitlab"
    # 중립 호스트면 명시 kind → 설정 → 기본값 순.
    neutral = "https://git.corp.example.com/o/r.git"
    assert F.kind_for(url=neutral, kind="github", config=cfg) == "github"
    assert F.kind_for(url=neutral, config=_cfg("github")) == "github"
    assert F.kind_for(url=neutral) == F.DEFAULT_KIND == "gitlab"


def test_resolve_kind_defaults_to_gitlab_for_legacy_config():
    # forge 섹션이 없는 옛 설정 객체 → 그 시절 동작(gitlab)을 유지한다.
    assert F.resolve_kind(SimpleNamespace()) == "gitlab"
    assert F.resolve_kind(_cfg("github")) == "github"
    # 모르는 값도 조용히 기본으로 수렴(부팅 검증은 app.config 가 별도로 한다).
    assert F.resolve_kind(_cfg("bitbucket")) == "gitlab"


def test_terminology_is_forge_specific():
    assert (F.change_abbr("gitlab"), F.change_abbr("github")) == ("MR", "PR")
    assert F.change_term("github") == "Pull Request"
    assert (F.label("gitlab"), F.label("github")) == ("GitLab", "GitHub")


# --- 토큰 URL 주입(핵심 회귀) ------------------------------------------------


def test_github_token_url_uses_x_access_token_not_oauth2():
    """⚠️ 핵심: GitHub 은 ``x-access-token:<token>@`` 이어야 인증된다."""
    url = F.with_token("https://github.com/o/r.git", "TKN")
    assert url == "https://x-access-token:TKN@github.com/o/r.git"
    assert "oauth2:" not in url


def test_gitlab_token_url_unchanged_from_before():
    """기존 GitLab 배포의 형식은 한 글자도 바뀌지 않는다."""
    assert (F.with_token("https://gitlab.example.com/g/p.git", "TKN")
            == "https://oauth2:TKN@gitlab.example.com/g/p.git")
    # 포트·http 스킴 보존, 기존 자격정보 제거 후 재주입.
    assert (F.with_token("http://old:pw@gitlab.example.com:30000/g/p.git", "T")
            == "http://oauth2:T@gitlab.example.com:30000/g/p.git")


def test_neutral_host_uses_configured_kind():
    """GHE 처럼 호스트가 중립이면 설정이 형식을 정한다."""
    ghe = "https://git.corp.example.com/o/r.git"
    assert F.with_token(ghe, "T", config=_cfg("github")).startswith(
        "https://x-access-token:T@")
    assert F.with_token(ghe, "T", config=_cfg("gitlab")).startswith("https://oauth2:T@")
    # 설정을 안 주면 기존 동작(gitlab).
    assert F.with_token(ghe, "T").startswith("https://oauth2:T@")


def test_with_token_leaves_schemeless_url_alone():
    assert F.with_token("host/a.git", "T") == "host/a.git"
    assert F.with_token("", "T") == ""


@pytest.mark.parametrize("url", [
    "file:///c/workspace/dlc-meta",
    "file://C:/workspace/dlc-meta",
    "ssh://git@gitlab.example.com/g/p.git",
    "git://example.com/p.git",
])
def test_with_token_only_injects_into_http_urls(url):
    """토큰 인증은 http(s) 에서만 의미가 있다 — 그 외 스킴은 **원문 그대로** 돌려준다.

    예전에는 스킴이 있기만 하면 주입해서 ``file://oauth2:<token>@/c/…`` 라는 어디에도
    없는 URL 을 만들었다(실측). 깨진 URL 로 실패하는 것보다, 토큰이 엉뚱한 문자열에
    실려 로그·에러 메시지로 새는 쪽이 더 나쁘다.
    """
    injected = F.with_token(url, "SECRET-TOKEN")
    assert injected == url
    assert "SECRET-TOKEN" not in injected


def test_repos_with_token_delegates_to_forge():
    """app.repos 의 주입 지점도 같은 분기를 탄다(호출부가 두 갈래로 갈리지 않게)."""
    assert (R._with_token("https://github.com/o/r.git", "T")
            == "https://x-access-token:T@github.com/o/r.git")
    assert (R._with_token("https://gitlab.example.com/g/p.git", "T")
            == "https://oauth2:T@gitlab.example.com/g/p.git")
    assert (R._with_token("https://git.corp.example.com/o/r.git", "T",
                          forge_kind="github")
            == "https://x-access-token:T@git.corp.example.com/o/r.git")


# --- 마스킹(토큰 유출 방어) --------------------------------------------------


def test_mask_hides_github_credential_label_too():
    """x-access-token 형식도 oauth2 와 동일하게 가려져야 한다(유출 회귀 방지)."""
    text = "fatal: https://x-access-token:SUPERSECRET@github.com/o/r.git denied"
    masked = R._mask(text, "SUPERSECRET")
    assert "SUPERSECRET" not in masked
    assert "x-access-token:***@" in masked
    # 토큰 값을 모르는 경우(값이 달라졌거나 못 넘긴 경우)에도 패턴만으로 가린다.
    blind = R._mask("https://x-access-token:OTHER@github.com/o/r.git", None)
    assert "OTHER" not in blind and "x-access-token:***@" in blind
    # GitLab 형식은 종전대로.
    assert R._mask("http://oauth2:T0KEN@h", "T0KEN") == "http://oauth2:***@h"


def test_strip_token_removes_github_credentials():
    assert (R._strip_token("https://x-access-token:TKN@github.com/o/r.git")
            == "https://github.com/o/r.git")


def test_provision_one_uses_github_format_and_never_logs_token(tmp_path):
    """provision_one → clone 인자 URL 이 GitHub 형식이고, 실패 메시지엔 토큰이 없다."""
    seen: list = []

    class Runner:
        def __call__(self, cmd, **kw):
            seen.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    path = str(tmp_path / "repo")
    url = "https://github.com/o/r.git"
    assert R.provision_one(path, url, "GHTOKEN", runner=Runner()) == "cloned"
    clone = next(c for c in seen if c[:2] == ["git", "clone"])
    assert clone[2] == "https://x-access-token:GHTOKEN@github.com/o/r.git"
    # remote 에는 토큰 없는 clean URL 만 저장한다.
    set_url = next(c for c in seen if "set-url" in c)
    assert set_url[-1] == url and "GHTOKEN" not in " ".join(set_url)


def test_provision_one_failure_message_masks_github_token(tmp_path):
    class FailRunner:
        def __call__(self, cmd, **kw):
            # git 이 인자 URL 을 그대로 되뱉는 최악의 경우를 모사.
            return SimpleNamespace(returncode=128, stdout="",
                                   stderr=f"fatal: {' '.join(cmd)}")

    res = R.provision_one(str(tmp_path / "r"), "https://github.com/o/r.git",
                          "GHTOKEN", runner=FailRunner())
    assert res.startswith("err:")
    assert "GHTOKEN" not in res
    assert "x-access-token:***@" in res


def test_ensure_repos_passes_configured_kind_for_neutral_hosts(tmp_path):
    """중립 호스트 레포도 config.forge.kind 대로 자격 사용자명이 붙는다."""
    seen: list = []

    class Runner:
        def __call__(self, cmd, **kw):
            seen.append(list(cmd))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

    cfg = SimpleNamespace(
        forge=SimpleNamespace(kind="github", base_url="", token_ref=""),
        run=SimpleNamespace(
            orchestrator_repo=str(tmp_path / "orch"),
            orchestrator_repo_url="https://git.corp.example.com/o/orch.git",
            dlc_meta_repo="", dlc_meta_repo_url="",
            docs_repo="", docs_repo_url="",
        ),
    )
    res = R.ensure_repos(cfg, "GHTOKEN", runner=Runner())
    assert res["orchestrator"] == "cloned"
    clone = next(c for c in seen if c[:2] == ["git", "clone"])
    assert clone[2].startswith("https://x-access-token:GHTOKEN@git.corp.example.com/")


def test_ensure_repos_no_token_status_is_forge_neutral(tmp_path):
    res = R.ensure_repos(SimpleNamespace(run=SimpleNamespace()), None)
    assert set(res.values()) == {"skipped: no forge token"}


# --- 변경요청 URL 추출 -------------------------------------------------------


def test_change_url_prefers_native_pattern_then_falls_back():
    text = ("brief: https://gitlab.example.com/g/p/-/merge_requests/7 and "
            "https://github.com/o/r/pull/12")
    assert F.search_change_url(text, "github") == "https://github.com/o/r/pull/12"
    assert (F.search_change_url(text, "gitlab")
            == "https://gitlab.example.com/g/p/-/merge_requests/7")
    # 네이티브가 없으면 통합 폴백(레포마다 forge 가 다를 수 있다).
    only_gl = "see https://gitlab.example.com/g/p/-/merge_requests/9"
    assert F.search_change_url(only_gl, "github").endswith("/merge_requests/9")
    assert F.search_change_url("no links here", "github") is None


def test_extract_mr_url_forge_branch_and_legacy_signature():
    event = {"type": "assistant",
             "text": ("https://gitlab.example.com/g/p/-/merge_requests/3 "
                      "https://github.com/o/r/pull/4")}
    assert extract_mr_url(event, "github") == "https://github.com/o/r/pull/4"
    # kind 를 안 주면 종전과 동일(통합 패턴, 앞선 매치).
    assert extract_mr_url(event).endswith("/merge_requests/3")
    # 구조화 필드가 있으면 그게 최우선(패턴보다 먼저).
    assert extract_mr_url({"mr_url": "https://x/y/pull/1"}, "gitlab") == "https://x/y/pull/1"


# --- UserCreds 이름 이행(하위호환) -------------------------------------------


def test_user_creds_mirrors_legacy_and_new_names():
    legacy = UserCreds(user="u", gitlab_token_ref="u/gitlab-token",
                       google_chat_user_id="123")
    assert legacy.forge_token_ref == "u/gitlab-token"
    assert legacy.notify_user_id == "123"

    modern = UserCreds(user="u", forge_token_ref="u/forge-token", notify_user_id="U9")
    assert modern.gitlab_token_ref == "u/forge-token"
    assert modern.google_chat_user_id == "U9"


def test_user_creds_from_env_reads_new_and_legacy_env():
    modern = UserCreds.from_env("u", env={"FORGE_TOKEN_REF": "u/forge-token",
                                          "DISPATCH_NOTIFY_USER_ID": "U9"})
    assert modern.forge_token_ref == "u/forge-token" and modern.notify_user_id == "U9"

    legacy = UserCreds.from_env("u", env={"GITLAB_TOKEN_REF": "u/gitlab-token",
                                          "DISPATCH_GOOGLE_CHAT_USER_ID": "123"})
    assert legacy.forge_token_ref == "u/gitlab-token"
    assert legacy.gitlab_token_ref == "u/gitlab-token"
    assert legacy.notify_user_id == "123"

    # 둘 다 있으면 신규 이름이 이긴다.
    both = UserCreds.from_env("u", env={"FORGE_TOKEN_REF": "new",
                                        "GITLAB_TOKEN_REF": "old"})
    assert both.forge_token_ref == "new"


def test_user_creds_from_registry_record_reads_both_key_names():
    legacy_rec = SimpleNamespace(
        username="u", identity=None, jira_email="",
        secrets_ref=SimpleNamespace(jira_token="", gitlab_token="u/gitlab-token",
                                    claude_oauth_token=""),
        google_chat_user_id="123",
    )
    creds = UserCreds.from_registry_record(legacy_rec)
    assert creds.forge_token_ref == "u/gitlab-token" and creds.notify_user_id == "123"


# --- build_env: 토큰 주입 이름 + GitHub 조건부 재주입 -------------------------


def test_build_env_emits_forge_and_legacy_token_names(tmp_path):
    base = str(tmp_path / "secrets")
    _write(base, "u/forge-token", "FORGE-VAL")
    creds = UserCreds(user="u", forge_token_ref="u/forge-token")
    env, secrets = build_env({}, creds, _cfg("gitlab", base), base_env={})
    assert env["FORGE_TOKEN"] == "FORGE-VAL"
    assert env["GITLAB_TOKEN"] == "FORGE-VAL"          # 레거시 이름도 같은 값
    assert env["FORGE_TOKEN_FILE"].endswith("u/forge-token")
    assert env["GITLAB_TOKEN_FILE"] == env["FORGE_TOKEN_FILE"]
    assert "FORGE-VAL" in secrets                       # 로그 마스킹 대상에 등록
    # forge 가 GitLab 이면 GitHub 계열 이름은 절대 남지 않는다.
    assert "GITHUB_TOKEN" not in env and "GH_TOKEN" not in env


def test_build_env_strips_inherited_github_tokens_even_on_github_forge(tmp_path):
    """상속된 central GitHub 토큰은 제거되고, 그 자리엔 **사용자 자신의** 토큰만 남는다."""
    base = str(tmp_path / "secrets")
    _write(base, "u/forge-token", "USER-GH-PAT")
    creds = UserCreds(user="u", forge_token_ref="u/forge-token")
    inherited = {"GITHUB_TOKEN": "CENTRAL-SECRET", "GH_ENTERPRISE_TOKEN": "CENTRAL-2"}
    env, _ = build_env({}, creds, _cfg("github", base), base_env=inherited)
    assert env["GITHUB_TOKEN"] == "USER-GH-PAT"
    assert env["GH_TOKEN"] == "USER-GH-PAT"
    assert "GH_ENTERPRISE_TOKEN" not in env
    assert "CENTRAL-SECRET" not in "\n".join(env.values())


def test_build_env_github_forge_without_user_token_leaves_nothing(tmp_path):
    """토큰이 없으면 상속분만 지우고 아무것도 넣지 않는다(빈 자리 > 남의 토큰)."""
    env, _ = build_env({}, UserCreds(user="u"), _cfg("github", str(tmp_path)),
                       base_env={"GITHUB_TOKEN": "CENTRAL-SECRET"})
    assert "GITHUB_TOKEN" not in env


def test_build_env_reads_legacy_creds_field(tmp_path):
    base = str(tmp_path / "secrets")
    _write(base, "u/gitlab-token", "GL-VAL")
    creds = UserCreds(user="u", gitlab_token_ref="u/gitlab-token")
    env, _ = build_env({}, creds, _cfg("gitlab", base), base_env={})
    assert env["FORGE_TOKEN"] == "GL-VAL" and env["GITLAB_TOKEN"] == "GL-VAL"


# --- config: central forge 토큰 참조 접근자 ----------------------------------


def test_central_forge_token_ref_picks_first_filled_representation():
    # 레거시 속성만 있는 옛 config 유사 객체.
    legacy = SimpleNamespace(run=SimpleNamespace(repo_resolver_gitlab_token_ref="s/gl"))
    assert C.central_forge_token_ref(legacy) == "s/gl"
    # forge 섹션만 있는 경우.
    only_forge = SimpleNamespace(forge=SimpleNamespace(token_ref="s/forge"))
    assert C.central_forge_token_ref(only_forge) == "s/forge"
    # 아무것도 없으면 "".
    assert C.central_forge_token_ref(SimpleNamespace()) == ""


def test_loaded_config_syncs_all_three_token_ref_spellings(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    body = ('role: central\n'
            'jira: { base_url: https://x, project: P, watcher_token_file: t }\n'
            'secrets: { base_dir: "${SECRETS_DIR}" }\n')
    p = tmp_path / "c.yaml"

    # 신규 키만 준 설정 → 레거시 속성도 채워진다(기존 리더 무변경).
    p.write_text(body + 'forge: { kind: github, token_ref: service/forge }\n',
                 encoding="utf-8")
    cfg = C.load_config(str(p))
    assert cfg.run.forge_token_ref == "service/forge"
    assert cfg.run.repo_resolver_gitlab_token_ref == "service/forge"
    assert C.central_forge_token_ref(cfg) == "service/forge"

    # 레거시 키만 준 설정 → 중립 표현들이 채워진다.
    p.write_text(body + 'run: { repo_resolver_gitlab_token_ref: service/gl }\n',
                 encoding="utf-8")
    cfg2 = C.load_config(str(p))
    assert cfg2.forge.token_ref == "service/gl"
    assert cfg2.run.forge_token_ref == "service/gl"
    assert C.central_forge_token_ref(cfg2) == "service/gl"


def test_notifier_env_accepts_neutral_and_legacy_names(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    body = ('role: central\n'
            'jira: { base_url: https://x, project: P, watcher_token_file: t }\n'
            'secrets: { base_dir: "${SECRETS_DIR}" }\n'
            'notifier: { provider: none }\n')
    p = tmp_path / "c.yaml"
    p.write_text(body, encoding="utf-8")

    monkeypatch.setenv("NOTIFIER_WEBHOOK_REF", "service/neutral")
    monkeypatch.setenv("NOTIFIER_PROVIDER", "slack")
    cfg = C.load_config(str(p))
    assert cfg.notifier.provider == "slack"
    assert cfg.notifier.webhook_ref == "service/neutral"
    assert cfg.notify.webhook_ref == "service/neutral"   # 레거시 미러

    # 중립 이름이 우선하되 옛 이름도 계속 받는다.
    monkeypatch.delenv("NOTIFIER_WEBHOOK_REF")
    monkeypatch.delenv("NOTIFIER_PROVIDER")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_REF", "service/legacy")
    monkeypatch.setenv("NOTIFY_ENABLED", "1")
    cfg2 = C.load_config(str(p))
    assert cfg2.notifier.provider == "google_chat"
    assert cfg2.notifier.webhook_ref == "service/legacy"

    # 알 수 없는 provider env 는 무시(조용한 채널 변경 방지).
    monkeypatch.delenv("NOTIFY_ENABLED")
    monkeypatch.setenv("NOTIFIER_PROVIDER", "carrier-pigeon")
    assert C.load_config(str(p)).notifier.provider == "none"


# --- 변경요청 닫기 forge 라우팅 ----------------------------------------------
#
# ⚠️ 이 프리미티브(app/forge.close_change_request)의 파이썬 호출자는 현재 없다 — 취소
# 롤백을 하던 워커 폴링 루프가 프랙탈 센트럴 세션으로 대체되며 함께 은퇴했다. 그래도
# **GitLab MR API 와 GitHub PR API 의 모양 차이**는 여기 말고 기록된 곳이 없으므로,
# 어댑터와 함께 그 계약을 지킨다(옛 tests/test_forge.py 의 worker 섹션에서 이관).


class _FakeHTTP:
    """requests 모듈 대역 — put/patch 호출을 기록하고 상태코드를 돌려준다."""

    def __init__(self, status_code=200):
        self.status_code = status_code
        self.calls: list = []

    def put(self, url, **kw):
        self.calls.append(("put", url, kw))
        return SimpleNamespace(status_code=self.status_code)

    def patch(self, url, **kw):
        self.calls.append(("patch", url, kw))
        return SimpleNamespace(status_code=self.status_code)


def test_close_change_request_routes_to_github_pr_api():
    http = _FakeHTTP()
    ok = F.close_change_request("https://github.com/owner/repo/pull/7", "GH-PAT",
                                config=_cfg("github"), http=http)
    assert ok is True
    verb, url, kw = http.calls[0]
    assert verb == "patch"      # ⚠️ GitLab 의 PUT 이 아니라 GitHub 의 PATCH
    assert url == "https://api.github.com/repos/owner/repo/pulls/7"
    assert kw["json"] == {"state": "closed"}
    assert kw["headers"]["Authorization"] == "Bearer GH-PAT"


def test_close_change_request_routes_to_gitlab_mr_api():
    http = _FakeHTTP()
    ok = F.close_change_request("https://gitlab.example.com/g/p/-/merge_requests/7", "GL-PAT",
                                config=_cfg("gitlab"), http=http)
    assert ok is True
    verb, url, kw = http.calls[0]
    assert verb == "put"
    assert url.endswith("/api/v4/projects/g%2Fp/merge_requests/7")
    assert kw["params"] == {"state_event": "close"}
    assert kw["headers"]["PRIVATE-TOKEN"] == "GL-PAT"


def test_close_change_request_prefers_the_url_over_config_kind():
    """이미 만들어진 링크를 되돌리는 일이라 **링크의 모양**이 config 보다 믿을 만하다."""
    http = _FakeHTTP()
    # config 는 gitlab 인데 URL 은 GitHub PR → GitHub API 로 가야 한다.
    assert F.close_change_request("https://github.com/o/r/pull/3", "PAT",
                                  config=_cfg("gitlab"), http=http) is True
    assert http.calls[0][0] == "patch"


@pytest.mark.parametrize("bad", ["https://github.com/owner/repo", "not-a-url", ""])
def test_close_change_request_is_best_effort_on_bad_urls(bad):
    http = _FakeHTTP()
    assert F.close_change_request(bad, "PAT", config=_cfg("github"), http=http) is False
    assert http.calls == []


def test_close_change_request_without_token_never_calls_out():
    http = _FakeHTTP()
    assert F.close_change_request("https://github.com/o/r/pull/3", "",
                                  config=_cfg("github"), http=http) is False
    assert http.calls == []


# --- 프롬프트/알림 용어 ------------------------------------------------------


def test_prompt_uses_pr_wording_on_github():
    from app.agent_runner import build_prompt

    job = {"ticket": "PROJ-1", "autonomy_mode": "A", "branch": "auto/PROJ-1"}
    gh = build_prompt(job, _cfg("github"))
    assert "PR" in gh and "MR" not in gh
    gl = build_prompt(job, _cfg("gitlab"))
    assert "MR" in gl

    job_b = dict(job, autonomy_mode="B")
    assert "원격(GitHub)" in build_prompt(job_b, _cfg("github"))
    assert "원격(GitLab)" in build_prompt(job_b, _cfg("gitlab"))


# ⚠️ 알림 **메시지 조립**의 MR/PR 용어 테스트는 제거됐다 — 그 조립기
# (app/notify.build_message)가 워커 잡-종료 통지자와 함께 은퇴했기 때문이다. 프랙탈
# 경로의 알림 본문은 에이전트가 쓴 완료-리포트 그대로이고(notify_report.py), 용어를
# 고르는 자리는 위 프롬프트 테스트가 지킨다.


# --- base URL 판정 — ⚠️ **토큰이 나갈 곳을 정하는 일이다** --------------------
#
# forge.base_url 이 비면 예전에는 요청이 곧장 SaaS(gitlab.com)로 나갔다. 사내 GitLab 을
# 쓰는 팀이 그 값을 안 적으면 **사내 PAT 가 gitlab.com 으로 전송**됐다 — 진단 실패보다
# 그쪽이 훨씬 나쁘다. 아래 테스트가 그 회귀를 못박는다.


def _cfg_urls(kind="gitlab", base_url="", dlc_meta="", docs="", orchestrator=""):
    """forge 설정 + 레포 URL 만 갖춘 최소 config 유사 객체."""
    return SimpleNamespace(
        forge=SimpleNamespace(kind=kind, base_url=base_url, token_ref=""),
        run=SimpleNamespace(dlc_meta_repo_url=dlc_meta, docs_repo_url=docs,
                            orchestrator_repo_url=orchestrator),
    )


@pytest.mark.parametrize("url,expected", [
    ("https://gitlab.example.com/g/r.git", "https://gitlab.example.com"),
    ("https://git.corp.example.com:8443/g/r.git", "https://git.corp.example.com:8443"),
    ("https://oauth2:tok@gitlab.example.com/g/r.git", "https://gitlab.example.com"),
    ("http://gitlab.internal/g/r.git", "http://gitlab.internal"),
    ("git@gitlab.example.com:g/r.git", ""),        # scp 형식 — 스킴을 지어내지 않는다
    ("ssh://git@gitlab.example.com/g/r.git", ""),  # ssh 는 API base URL 이 아니다
    ("", ""),
])
def test_base_url_from_url(url, expected):
    assert F.base_url_from_url(url) == expected


def test_explicit_base_url_wins():
    r = F.resolve_base_url(_cfg_urls(base_url="https://gitlab.corp.example.com/"))
    assert r.base_url == "https://gitlab.corp.example.com"
    assert r.source == F.SOURCE_CONFIG and r.usable


def test_base_url_is_derived_from_the_dlc_meta_repo_url():
    r = F.resolve_base_url(_cfg_urls(dlc_meta="https://gitlab.corp.example.com/g/m.git"))
    assert r.base_url == "https://gitlab.corp.example.com"
    assert r.source == F.SOURCE_DERIVED and r.origin == "run.dlc_meta_repo_url"


def test_neutral_host_is_accepted_as_evidence():
    """git.corp.example.com 은 forge 종류를 안 밝히지만, 토큰이 이미 그리로 나간다."""
    r = F.resolve_base_url(_cfg_urls(dlc_meta="https://git.corp.example.com/g/m.git"))
    assert r.base_url == "https://git.corp.example.com" and r.source == F.SOURCE_DERIVED


def test_saas_host_is_confirmed_not_derived():
    """⚠️ github.com 을 base_url 로 채우면 오히려 틀린다(API 는 api.github.com)."""
    r = F.resolve_base_url(_cfg_urls(kind="github",
                                     dlc_meta="https://github.com/acme/m.git"))
    assert r.base_url == "" and r.source == F.SOURCE_SAAS and r.usable

    r2 = F.resolve_base_url(_cfg_urls(dlc_meta="https://gitlab.com/acme/m.git"))
    assert r2.base_url == "" and r2.source == F.SOURCE_SAAS and r2.usable


def test_a_url_belonging_to_another_forge_is_ignored():
    """한 배포가 여러 forge 를 섞어 쓴다 — github.com 레포가 gitlab 토큰의 근거일 리 없다."""
    r = F.resolve_base_url(_cfg_urls(kind="gitlab",
                                     dlc_meta="https://github.com/acme/m.git",
                                     docs="https://gitlab.corp.example.com/g/d.git"))
    assert r.base_url == "https://gitlab.corp.example.com"
    assert r.origin == "run.docs_repo_url"


def test_no_repo_url_at_all_is_not_usable():
    """근거가 없으면 SaaS 로 떨어지지 않는다 — 부르는 쪽이 SKIP 해야 한다."""
    r = F.resolve_base_url(_cfg_urls())
    assert r.source == F.SOURCE_NONE and not r.usable and r.base_url == ""


def test_ssh_only_self_hosted_url_is_unresolved_not_saas():
    r = F.resolve_base_url(_cfg_urls(dlc_meta="git@gitlab.corp.example.com:g/m.git"))
    assert r.source == F.SOURCE_UNRESOLVED and not r.usable
    assert r.host == "gitlab.corp.example.com"


def test_orchestrator_repo_url_is_not_evidence():
    """공개 프레임워크 레포는 고정 리터럴(github.com)이라 조직의 forge 증거가 아니다.

    이걸 근거로 삼으면 GitHub Enterprise 배포가 'SaaS 확인됨'으로 오판된다.
    """
    r = F.resolve_base_url(_cfg_urls(
        kind="github", orchestrator="https://github.com/yunhyuk-choi/ai-dlc-orchestrator.git"))
    assert r.source == F.SOURCE_NONE and not r.usable


def test_loader_fills_base_url_from_repo_url(tmp_path, monkeypatch):
    """실제 파서가 이 판정을 설정에 반영한다(+근거를 남긴다)."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config_from_dict({
        "role": "central",
        "jira": {"base_url": "https://x", "project": "P", "watcher_token_file": "t"},
        "secrets": {"base_dir": "${SECRETS_DIR}"},
        "forge": {"kind": "gitlab"},
        "run": {"dlc_meta_repo_url": "https://gitlab.corp.example.com/g/m.git"},
    })
    assert cfg.forge.base_url == "https://gitlab.corp.example.com"
    assert cfg.forge.base_url_source == F.SOURCE_DERIVED
    assert cfg.forge.base_url_origin == "run.dlc_meta_repo_url"


def test_loader_leaves_saas_base_url_empty(monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config_from_dict({
        "role": "central",
        "jira": {"base_url": "https://x", "project": "P", "watcher_token_file": "t"},
        "secrets": {"base_dir": "${SECRETS_DIR}"},
        "forge": {"kind": "github"},
        "run": {"dlc_meta_repo_url": "https://github.com/acme/m.git"},
    })
    assert cfg.forge.base_url == "" and cfg.forge.base_url_source == F.SOURCE_SAAS
