"""설정 실측 진단(app/setup_doctor.py) 단위테스트.

⚠️ **네트워크·도커·git 에 절대 실제로 붙지 않는다** — 모든 외부 의존은 대역(fake)으로
주입한다(CI: GitHub Actions ubuntu 에서 그대로 돈다). 그래서 각 검사 함수는 대역을
받도록 설계돼 있다.

특히 다음을 집중해서 지킨다:
    - 마스킹 — 진단 출력에 토큰 값이 절대 실리지 않는다.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from app import config as C
from app import setup_doctor as D
from app.jira_client import JiraError


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    """호스트 env 가 검사 결과를 흔들지 않게 격리한다."""
    for name in ("HOST_DEPLOY_DIR", "SECRETS_DIR", "JIRA_WATCHER_EMAIL",
                 "NOTIFIER_PROVIDER", "NOTIFIER_WEBHOOK_REF", "NOTIFY_ENABLED"):
        monkeypatch.delenv(name, raising=False)


def make_cfg(**over):
    """진단 대상 설정(실제 파서로 만든다 — 대역 config 로는 파서 규칙이 빠진다)."""
    raw = {
        "role": "central",
        "consent": {"full_permissions": True, "accepted_at": "2026-08-25T09:00:00+09:00"},
        "deploy": {"profile": "cloud_vm",
                   "secrets_base_dir": "/run/secrets"},
        "forge": {"kind": "gitlab", "token_ref": "service/forge-token"},
        "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
                 "watcher_token_file": "service/jira-token",
                 "watcher_email": "bot@acme.example",
                 "trigger_statuses": ["To Do"]},
        "notifier": {"provider": "none"},
        "webhook": {"enabled": False},
        "run": {"dlc_meta_repo_url": "https://git.example.com/g/dlc-meta.git"},
    }
    for section, patch in over.items():
        if isinstance(patch, dict) and isinstance(raw.get(section), dict):
            raw[section] = {**raw[section], **patch}
        else:
            raw[section] = patch
    return C.load_config_from_dict(raw)


def write_secret(root, ref, value="s3cret-value", mode=0o600):
    """시크릿 파일 하나 만들기(테스트용)."""
    path = os.path.join(root, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(value)
    if os.name == "posix":
        os.chmod(path, mode)
    return path


# ---------------------------------------------------------------------------
# config · secrets
# ---------------------------------------------------------------------------


def test_config_check_fails_without_consent():
    cfg = make_cfg(consent={"full_permissions": False})
    r = D.check_config(cfg)
    assert r.status == D.STATUS_FAIL and "consent" in r.message


def test_config_check_flags_leftover_placeholders(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("jira:\n  project: <PROJECT_KEY>\n", encoding="utf-8")
    r = D.check_config(make_cfg(), config_path=str(path))
    assert r.status == D.STATUS_FAIL and "자리표시자" in r.message


def test_config_check_ignores_placeholders_inside_comments(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("# 예: /home/<deploy-user>/jad\njira:\n  project: ACME\n",
                    encoding="utf-8")
    assert D.check_config(make_cfg(), config_path=str(path)).status == D.STATUS_PASS


def test_secrets_check_reports_missing_files(tmp_path):
    cfg = make_cfg(deploy={"secrets_base_dir": str(tmp_path)})
    r = D.check_secrets(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL
    assert "jira.watcher_token_file" in r.message


def test_secrets_check_passes_when_all_present(tmp_path):
    root = str(tmp_path)
    write_secret(root, "service/jira-token")
    write_secret(root, "service/forge-token")
    cfg = make_cfg(deploy={"secrets_base_dir": root})
    assert D.check_secrets(cfg, project_dir=root).status == D.STATUS_PASS


def test_missing_webhook_secret_is_only_a_warning(tmp_path):
    """웹훅 시크릿이 없으면 엔드포인트만 503 이고 폴링은 돈다 — 빨간불이면 거짓 경보다."""
    root = str(tmp_path)
    write_secret(root, "service/jira-token")
    write_secret(root, "service/forge-token")
    cfg = make_cfg(deploy={"secrets_base_dir": root},
                   webhook={"enabled": True, "secret_ref": "service/jira-webhook"})
    r = D.check_secrets(cfg, project_dir=root)
    assert r.status == D.STATUS_WARN
    assert "webhook.secret_ref" in r.message


def test_secrets_check_falls_back_to_host_side_directory(tmp_path):
    """호스트에서 돌릴 때 /run/secrets 대신 <배포 디렉토리>/secrets 를 본다."""
    root = tmp_path / "secrets"
    root.mkdir()
    write_secret(str(root), "service/jira-token")
    write_secret(str(root), "service/forge-token")
    cfg = make_cfg()  # secrets_base_dir = /run/secrets (컨테이너 관점)
    r = D.check_secrets(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_PASS and "폴백" in r.message


def test_secrets_check_skips_when_root_is_a_container_path(tmp_path):
    cfg = make_cfg()  # /run/secrets 는 이 머신에 없다
    r = D.check_secrets(cfg, project_dir=str(tmp_path))
    assert r.status in (D.STATUS_SKIP, D.STATUS_FAIL)
    assert "docker compose exec central" in r.hint


def test_secrets_check_never_prints_secret_contents(tmp_path):
    root = str(tmp_path)
    write_secret(root, "service/jira-token", "glpat-SUPERSECRET")
    write_secret(root, "service/forge-token", "glpat-SUPERSECRET")
    cfg = make_cfg(deploy={"secrets_base_dir": root})
    r = D.check_secrets(cfg, project_dir=root)
    assert "SUPERSECRET" not in (r.message + r.hint)


# ---------------------------------------------------------------------------
# Jira 프로브(대역)
# ---------------------------------------------------------------------------


class FakeJira:
    """JiraClient 대역 — 지정한 결과/예외를 그대로 낸다.

    ``missing_projects`` 에 담긴 키는 ``get_project`` 에서 **실측과 같은 모양의** 404
    (본문에 Jira 가 이유를 적어 준)로 실패한다. ``project_error`` 는 그 밖의 실패
    (권한·5xx)를 흉내낸다 — 그건 '없다'의 근거가 아니라 판정 보류다.
    """

    def __init__(self, me=None, page=None, error=None, missing_projects=(),
                 project_error=None):
        self._me = me or {"displayName": "봇 계정"}
        self._page = page if page is not None else {"issues": [{"key": "ACME-1"}]}
        self._error = error
        self._missing = set(missing_projects)
        self._project_error = project_error
        self.project_calls = []

    def myself(self):
        if self._error:
            raise self._error
        return self._me

    def search_jql_page(self, jql, fields=None, max_results=50, next_page_token=None):
        if self._error:
            raise self._error
        return self._page

    def get_project(self, key):
        self.project_calls.append(key)
        if self._project_error:
            raise self._project_error
        if key in self._missing:
            raise JiraError(
                f"GET /rest/api/3/project/{key} → HTTP 404", status_code=404,
                body={"errorMessages": [f"키가 '{key}'인 프로젝트를 찾을 수 없습니다."]},
            )
        return {"key": key, "name": f"{key} 프로젝트"}


def test_jira_auth_success():
    r = D.check_jira_auth(make_cfg(), client=FakeJira())
    assert r.status == D.STATUS_PASS and "봇 계정" in r.message


@pytest.mark.parametrize("code,needle", [
    (401, "인증 실패"), (403, "거부"), (404, "없습니다"),
])
def test_jira_auth_distinguishes_status_codes(code, needle):
    err = JiraError(f"GET → HTTP {code}", status_code=code)
    r = D.check_jira_auth(make_cfg(), client=FakeJira(error=err))
    assert r.status == D.STATUS_FAIL and needle in r.message


def test_jira_auth_404_points_at_cloud_only_support():
    err = JiraError("GET → HTTP 404", status_code=404)
    r = D.check_jira_auth(make_cfg(), client=FakeJira(error=err))
    assert "Cloud" in r.hint


def test_jira_auth_network_failure_is_actionable():
    r = D.check_jira_auth(make_cfg(), client=FakeJira(error=JiraError("연결 끊김")))
    assert r.status == D.STATUS_FAIL and "방화벽" in r.hint


def test_jira_auth_skips_without_credentials(tmp_path):
    cfg = make_cfg(deploy={"secrets_base_dir": str(tmp_path)})
    r = D.check_jira_auth(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_SKIP


def test_jira_search_success():
    r = D.check_jira_search(make_cfg(), client=FakeJira())
    assert r.status == D.STATUS_PASS and "ACME" in r.message


def test_jira_search_400_suggests_wrong_project_key():
    err = JiraError("POST → HTTP 400", status_code=400)
    r = D.check_jira_search(make_cfg(), client=FakeJira(error=err))
    assert r.status == D.STATUS_FAIL and "프로젝트 키" in r.message


class _EmptySearchJira:
    """검색은 200 + 빈 배열, ``myself`` 만 실패하는 대역 — Jira Cloud 실측 동작."""

    def __init__(self, auth_error=None):
        self._auth_error = auth_error
        self.myself_calls = 0

    def myself(self):
        self.myself_calls += 1
        if self._auth_error:
            raise self._auth_error
        return {"displayName": "봇 계정"}

    def search_jql_page(self, jql, fields=None, max_results=50, next_page_token=None):
        return {"issues": [], "isLast": True}


def test_jira_search_does_not_pass_when_credentials_are_rejected():
    """★ 빈 결과 + 자격 거부 = FAIL. 'jira_auth 는 FAIL 인데 jira_search 는 PASS' 금지."""
    jira = _EmptySearchJira(auth_error=JiraError("GET → HTTP 401", status_code=401))
    r = D.check_jira_search(make_cfg(), client=jira)
    assert r.status == D.STATUS_FAIL
    assert "자격" in r.message and "빈 결과" in r.message
    assert jira.myself_calls == 1


def test_jira_search_passes_on_an_empty_but_authenticated_instance():
    """티켓이 정말 없을 뿐이면 PASS — 빈 결과 자체는 죄가 아니다."""
    jira = _EmptySearchJira()
    r = D.check_jira_search(make_cfg(), client=jira)
    assert r.status == D.STATUS_PASS and "표본 0건" in r.message
    assert jira.myself_calls == 1


def test_jira_search_skips_the_extra_probe_when_issues_came_back():
    """이슈가 돌아왔다는 것이 곧 자격 증거다 — 확인 요청을 더 하지 않는다."""
    class _NonEmpty(_EmptySearchJira):
        def search_jql_page(self, jql, fields=None, max_results=50, next_page_token=None):
            return {"issues": [{"key": "ACME-1"}], "isLast": True}

    jira = _NonEmpty(auth_error=JiraError("GET → HTTP 401", status_code=401))
    r = D.check_jira_search(make_cfg(), client=jira)
    assert r.status == D.STATUS_PASS
    assert jira.myself_calls == 0


# --- 없는 프로젝트 = 빈 결과의 두 번째 위장(실측 2026-09) ---------------------


def test_jira_search_fails_when_the_project_does_not_exist():
    """★ 자격은 멀쩡한데 프로젝트 키가 없으면 JQL 은 200 + 빈 목록을 준다 — 초록불 금지.

    실측: ``{"jql": "project = HAN"}`` → 200 ``{"issues": [], "isLast": true}`` 인데
    ``GET /project/HAN`` 은 404 ``키가 'HAN'인 프로젝트를 찾을 수 없습니다``. 이걸 잡지
    않으면 폴러는 영원히 조용히 아무 일도 하지 않고 진단만 PASS 로 남는다.
    """
    jira = FakeJira(page={"issues": [], "isLast": True}, missing_projects={"HAN"})
    r = D.check_jira_search(make_cfg(jira={"project": "HAN"}), client=jira)
    assert r.status == D.STATUS_FAIL
    assert "HAN" in r.message and "없는 프로젝트" in r.message
    # Jira 가 한 말을 그대로 보여 준다(우리 추측이 아니라).
    assert "찾을 수 없습니다" in r.message
    assert jira.project_calls == ["HAN"]


def test_jira_search_checks_every_watched_project_not_just_the_primary():
    """복수 감시 대상 중 **하나만** 틀려도 잡는다 — 표본 1건은 나머지 키를 증명하지 못한다."""
    jira = FakeJira(page={"issues": [{"key": "ACME-1"}]}, missing_projects={"NOPE"})
    cfg = make_cfg(jira={"project": "ACME", "projects": ["NOPE"]})
    r = D.check_jira_search(cfg, client=jira)
    assert r.status == D.STATUS_FAIL
    assert "NOPE" in r.message and "찾을 수 없습니다" in r.message
    # 대표(ACME)는 실재하므로 '없는 프로젝트' 목록에 들지 않는다.
    assert "ACME" not in r.message
    assert jira.project_calls == ["ACME", "NOPE"]


def test_jira_search_passes_and_says_projects_were_verified():
    """실재가 확인되면 그 사실을 보고에 남긴다(무엇을 근거로 초록불인지 보이게)."""
    jira = FakeJira(page={"issues": [], "isLast": True})
    r = D.check_jira_search(make_cfg(), client=jira)
    assert r.status == D.STATUS_PASS
    assert "표본 0건" in r.message and "프로젝트 실재 확인 1개" in r.message


def test_jira_search_does_not_fail_on_an_undetermined_project_probe():
    """404 가 아닌 실패(권한·5xx)는 '없다'의 근거가 아니다 — 정상 배포를 막지 않는다."""
    jira = FakeJira(page={"issues": [], "isLast": True},
                    project_error=JiraError("GET → HTTP 500", status_code=500))
    r = D.check_jira_search(make_cfg(), client=jira)
    assert r.status == D.STATUS_PASS
    assert "확인 보류" not in r.message      # 확인된 것이 하나도 없으면 꼬리말도 없다


def test_jira_search_holds_judgement_when_the_client_cannot_probe():
    """``get_project`` 가 없는 클라이언트(옛 대역)면 판정 보류 — 없는 근거로 FAIL 금지."""
    class _NoProbe:
        def myself(self):
            return {"displayName": "봇 계정"}

        def search_jql_page(self, jql, fields=None, max_results=50, next_page_token=None):
            return {"issues": [], "isLast": True}

    r = D.check_jira_search(make_cfg(), client=_NoProbe())
    assert r.status == D.STATUS_PASS


# --- 404 해석 — Jira 가 한 말이 우리 추측을 이긴다 ----------------------------


def test_404_with_a_jira_message_is_not_blamed_on_server_dc():
    """★ 실측된 오진 차단 — 없는 프로젝트의 404 를 'Cloud 전용'으로 바꾸지 않는다.

    리허설에서 discover 의 statuses 조회가 404 를 받자 "이 시스템은 Jira Cloud 전용입니다
    (Server/DC 미지원)" 라고 보고했다. 같은 사이트에서 인증은 성공했고 base_url 도
    ``.atlassian.net`` 이었으니 완전한 오진이었다 — 진짜 이유는 Jira 가 본문에 적어 준
    "키가 'HAN'인 프로젝트를 찾을 수 없습니다." 였다.
    """
    err = JiraError("GET → HTTP 404", status_code=404,
                    body={"errorMessages": ["키가 'HAN'인 프로젝트를 찾을 수 없습니다."]})
    r = D._jira_failure("statuses", err, "https://acme.atlassian.net")
    assert r.status == D.STATUS_FAIL
    assert "키가 'HAN'인 프로젝트를 찾을 수 없습니다." in r.message
    assert "Server/DC" not in r.message and "Server/DC" not in r.hint
    assert "프로젝트 키" in r.hint


def test_404_without_a_jira_message_still_points_at_cloud_only_support():
    """반대로 Jira 가 아무 말도 안 했으면(경로 자체가 없음) 그때는 Server/DC 를 지목한다."""
    r = D._jira_failure("jira_auth", JiraError("GET → HTTP 404", status_code=404),
                        "https://acme.example.com")
    assert r.status == D.STATUS_FAIL
    assert "Cloud" in r.hint


def test_404_on_a_non_cloud_base_url_adds_the_cloud_possibility():
    """base_url 이 Cloud 로 보이지 않을 때만 Server/DC 를 **부가 가능성**으로 덧붙인다."""
    err = JiraError("GET → HTTP 404", status_code=404,
                    body={"errorMessages": ["Project not found"]})
    cloudish = D._jira_failure("statuses", err, "https://acme.atlassian.net")
    onprem = D._jira_failure("statuses", err, "https://jira.acme.example.com")
    assert "Cloud" not in cloudish.hint
    assert "Cloud" in onprem.hint and "Project not found" in onprem.message


def test_405_is_still_read_as_an_absent_endpoint():
    """405/410 은 엔드포인트 부재의 강한 신호다 — 본문이 있어도 해석을 유지한다."""
    err = JiraError("POST → HTTP 405", status_code=405,
                    body={"errorMessages": ["Method Not Allowed"]})
    r = D._jira_failure("jira_search", err, "https://acme.atlassian.net")
    assert "이 경로가 이 사이트에 없습니다" in r.message
    assert "Method Not Allowed" in r.message
    assert "Cloud" in r.hint


# ---------------------------------------------------------------------------
# forge 토큰 프로브(대역)
# ---------------------------------------------------------------------------


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"username": "svc-bot"}
        self.headers = headers or {}

    def json(self):
        return self._payload


class FakeHttp:
    """requests.Session 대역 — 마지막 호출을 기록한다."""

    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse()
        self.exc = exc
        self.calls = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append((url, headers))
        if self.exc:
            raise self.exc
        return self.response

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json))
        if self.exc:
            raise self.exc
        return self.response


def _cfg_with_forge_secret(tmp_path, kind="gitlab", token="glpat-TOPSECRETTOKEN",
                           dlc_meta_url=None):
    """forge 토큰이 놓인 설정.

    ``dlc_meta_url`` 은 **토큰이 나갈 곳**을 정하는 근거다(forge.base_url 이 비어 있을 때
    로더가 여기서 유도한다 — app/forge.resolve_base_url). 기본은 make_cfg 의 중립 호스트.
    """
    root = str(tmp_path)
    write_secret(root, "service/forge-token", token)
    write_secret(root, "service/jira-token")
    over = {"deploy": {"secrets_base_dir": root}, "forge": {"kind": kind}}
    if dlc_meta_url is not None:
        over["run"] = {"dlc_meta_repo_url": dlc_meta_url}
    return make_cfg(**over), root


def test_forge_token_gitlab_success(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_PASS and "svc-bot" in r.message
    url, headers = http.calls[0]
    assert url.endswith("/api/v4/user") and "PRIVATE-TOKEN" in headers


def test_forge_token_github_uses_bearer_and_saas_endpoint(tmp_path):
    """레포 URL 이 github.com 이면 SaaS 확인 → base_url 없이 api.github.com 을 쓴다.

    (``https://github.com`` 을 base_url 로 채워 버리면 오히려 틀린다 — GitHub SaaS 의
    API 호스트는 ``api.github.com`` 이다.)
    """
    cfg, root = _cfg_with_forge_secret(tmp_path, kind="github",
                                       dlc_meta_url="https://github.com/acme/dlc-meta.git")
    http = FakeHttp(FakeResponse(payload={"login": "svc-bot"},
                                 headers={"x-oauth-scopes": "repo, read:org"}))
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_PASS
    url, headers = http.calls[0]
    assert url == "https://api.github.com/user"
    assert headers["Authorization"].startswith("Bearer ")


def test_forge_token_github_without_repo_scope_warns(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path, kind="github")
    http = FakeHttp(FakeResponse(payload={"login": "svc-bot"},
                                 headers={"x-oauth-scopes": "read:user"}))
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_WARN and "repo" in r.message


@pytest.mark.parametrize("code,status", [(401, D.STATUS_FAIL), (403, D.STATUS_FAIL),
                                         (500, D.STATUS_FAIL)])
def test_forge_token_http_errors(tmp_path, code, status):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    r = D.check_forge_token(cfg, project_dir=root,
                            http=FakeHttp(FakeResponse(status_code=code)))
    assert r.status == status


def test_forge_token_failure_never_leaks_the_token(tmp_path):
    token = "glpat-TOPSECRETTOKEN"
    cfg, root = _cfg_with_forge_secret(tmp_path, token=token)
    http = FakeHttp(exc=RuntimeError(f"boom while using {token}"))
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_FAIL
    assert token not in (r.message + r.hint)


def test_forge_token_skips_without_reference(tmp_path):
    cfg = make_cfg(forge={"token_ref": ""}, run={"repo_resolver_gitlab_token_ref": ""},
                   deploy={"secrets_base_dir": str(tmp_path)})
    assert D.check_forge_token(cfg, project_dir=str(tmp_path)).status == D.STATUS_SKIP


# ---------------------------------------------------------------------------
# dlc-meta 도달성(대역 git)
# ---------------------------------------------------------------------------


def _runner(returncode=0, stdout="", stderr="", exc=None):
    def run(cmd, **kwargs):
        run.cmd = cmd
        run.kwargs = kwargs
        if exc:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


def test_dlc_meta_reachable(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    runner = _runner(stdout="abc\trefs/heads/master\n")
    r = D.check_dlc_meta(cfg, project_dir=root, runner=runner)
    assert r.status == D.STATUS_PASS and "1개" in r.message
    # 프롬프트로 매달리지 않게 env 를 잠근다.
    assert runner.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"


def test_dlc_meta_empty_repo_warns(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    r = D.check_dlc_meta(cfg, project_dir=root, runner=_runner(stdout=""))
    assert r.status == D.STATUS_WARN


def test_dlc_meta_timeout_names_the_private_network_trap(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    runner = _runner(exc=subprocess.TimeoutExpired(cmd="git", timeout=1))
    r = D.check_dlc_meta(cfg, project_dir=root, runner=runner)
    assert r.status == D.STATUS_FAIL
    assert "사설" in r.hint and "VPN" in r.hint


def test_dlc_meta_auth_failure_is_masked(tmp_path):
    token = "glpat-TOPSECRETTOKEN"
    cfg, root = _cfg_with_forge_secret(tmp_path, token=token)
    stderr = (f"fatal: Authentication failed for "
              f"'https://oauth2:{token}@git.example.com/g/dlc-meta.git/'")
    r = D.check_dlc_meta(cfg, project_dir=root, runner=_runner(1, stderr=stderr))
    assert r.status == D.STATUS_FAIL
    assert token not in r.message
    assert "***" in r.message


def test_dlc_meta_skips_without_url(tmp_path):
    cfg = make_cfg(run={"dlc_meta_repo_url": ""},
                   deploy={"secrets_base_dir": str(tmp_path)})
    assert D.check_dlc_meta(cfg, project_dir=str(tmp_path)).status == D.STATUS_SKIP


def test_dlc_meta_skips_when_git_is_absent(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    r = D.check_dlc_meta(cfg, project_dir=root, runner=_runner(exc=FileNotFoundError()))
    assert r.status == D.STATUS_SKIP


# ---------------------------------------------------------------------------
# docker
# ---------------------------------------------------------------------------


def test_docker_ping_success():
    cfg = make_cfg(deploy={"docker_host": "unix:///var/run/docker.sock"})
    r = D.check_docker(cfg, docker_factory=lambda url: SimpleNamespace(
        ping=lambda: True))
    assert r.status == D.STATUS_PASS


def test_docker_failure_on_local_socket_is_a_real_failure():
    cfg = make_cfg(deploy={"docker_host": "unix:///var/run/docker.sock"})

    def factory(url):
        raise RuntimeError("permission denied")

    r = D.check_docker(cfg, docker_factory=factory)
    assert r.status == D.STATUS_FAIL and "docker 그룹" in r.hint


def test_docker_compose_internal_name_is_skipped_on_the_host(monkeypatch):
    """socket-proxy 는 compose 네트워크 안에서만 해석된다 — 호스트 실패는 거짓 경보다."""
    monkeypatch.setattr(D, "in_container", lambda: False)
    cfg = make_cfg(deploy={"docker_host": "tcp://socket-proxy:2375"})

    def factory(url):
        raise RuntimeError("Name or service not known")

    r = D.check_docker(cfg, docker_factory=factory)
    assert r.status == D.STATUS_SKIP
    assert "docker compose exec central" in r.hint


def test_docker_compose_internal_name_fails_inside_container(monkeypatch):
    monkeypatch.setattr(D, "in_container", lambda: True)
    cfg = make_cfg(deploy={"docker_host": "tcp://socket-proxy:2375"})

    def factory(url):
        raise RuntimeError("connection refused")

    assert D.check_docker(cfg, docker_factory=factory).status == D.STATUS_FAIL


def test_docker_sdk_absent_is_skip():
    cfg = make_cfg()

    def factory(url):
        raise ImportError("no docker")

    assert D.check_docker(cfg, docker_factory=factory).status == D.STATUS_SKIP


@pytest.mark.parametrize("host,expected", [
    ("tcp://socket-proxy:2375", True),
    ("tcp://10.0.0.5:2375", False),
    ("tcp://localhost:2375", False),
    ("unix:///var/run/docker.sock", False),
])
def test_compose_internal_detection(host, expected):
    assert D._is_compose_internal(host) is expected


# ---------------------------------------------------------------------------
# 알림
# ---------------------------------------------------------------------------


def test_notifier_off_is_skipped():
    assert D.check_notifier(make_cfg()).status == D.STATUS_SKIP


def test_notifier_missing_webhook_file_fails(tmp_path):
    cfg = make_cfg(deploy={"secrets_base_dir": str(tmp_path)},
                   notifier={"provider": "slack", "webhook_ref": "service/hook"})
    r = D.check_notifier(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL


def test_notifier_present_reference_passes_without_sending(tmp_path):
    root = str(tmp_path)
    write_secret(root, "service/hook", "https://hooks.example.com/abc")
    cfg = make_cfg(deploy={"secrets_base_dir": root},
                   notifier={"provider": "slack", "webhook_ref": "service/hook"})
    r = D.check_notifier(cfg, project_dir=root)
    assert r.status == D.STATUS_PASS and "발송은 하지 않음" in r.message


def test_notifier_non_url_content_fails_without_leaking(tmp_path):
    root = str(tmp_path)
    write_secret(root, "service/hook", "NOT-A-URL-SECRET")
    cfg = make_cfg(deploy={"secrets_base_dir": root},
                   notifier={"provider": "slack", "webhook_ref": "service/hook"})
    r = D.check_notifier(cfg, project_dir=root)
    assert r.status == D.STATUS_FAIL
    assert "NOT-A-URL-SECRET" not in (r.message + r.hint)


def test_notifier_actually_sends_only_with_the_flag(tmp_path):
    root = str(tmp_path)
    write_secret(root, "service/hook", "https://hooks.example.com/abc")
    cfg = make_cfg(deploy={"secrets_base_dir": root},
                   notifier={"provider": "slack", "webhook_ref": "service/hook"})
    http = FakeHttp(FakeResponse(status_code=200))
    assert D.check_notifier(cfg, project_dir=root, http=http).status == D.STATUS_PASS
    assert http.calls == []          # 플래그 없이는 한 통도 안 보낸다
    r = D.check_notifier(cfg, project_dir=root, send=True, http=http)
    assert r.status == D.STATUS_PASS and len(http.calls) == 1


# ---------------------------------------------------------------------------
# 실행기
# ---------------------------------------------------------------------------


def test_run_checks_runs_every_check_and_keeps_going(tmp_path):
    cfg = make_cfg(deploy={"secrets_base_dir": str(tmp_path)})
    results = D.run_checks(cfg, project_dir=str(tmp_path),
                           runner=_runner(stdout="x\trefs/heads/master\n"),
                           http=FakeHttp(),
                           docker_factory=lambda url: SimpleNamespace(ping=lambda: True),
                           jira_client=FakeJira())
    assert [r.name for r in results] == list(D.CHECK_ORDER)


def test_run_checks_subset(tmp_path):
    results = D.run_checks(make_cfg(), project_dir=str(tmp_path),
                           only=("config", "secrets"))
    assert [r.name for r in results] == ["config", "secrets"]


def test_run_checks_rejects_unknown_names():
    with pytest.raises(ValueError):
        D.run_checks(make_cfg(), only=("nope",))


def test_a_crashing_check_does_not_kill_the_run(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("내부 사고")

    monkeypatch.setattr(D, "check_config", boom)
    results = D.run_checks(make_cfg(), project_dir=str(tmp_path),
                           only=("config", "secrets"))
    assert results[0].status == D.STATUS_FAIL and "예외" in results[0].message
    assert len(results) == 2


def test_results_to_dict_ok_flag_ignores_warn_and_skip():
    passing = [D.CheckResult("a", D.STATUS_PASS, "x"),
               D.CheckResult("b", D.STATUS_WARN, "x"),
               D.CheckResult("c", D.STATUS_SKIP, "x")]
    assert D.results_to_dict(passing)["ok"] is True
    failing = passing + [D.CheckResult("d", D.STATUS_FAIL, "x")]
    assert D.results_to_dict(failing)["ok"] is False


def test_format_results_is_readable():
    text = D.format_results([D.CheckResult("a", D.STATUS_FAIL, "깨졌다", "이렇게 고쳐라")])
    assert "[FAIL] a — 깨졌다" in text and "이렇게 고쳐라" in text


# ---------------------------------------------------------------------------
# ⚠️ forge 토큰이 **엉뚱한 곳으로 나가지 않는가** (실제 보안 문제)
# ---------------------------------------------------------------------------


def test_forge_token_derives_base_url_from_the_repo_url(tmp_path):
    """base_url 이 비어도 사내 레포 URL 이 있으면 그 호스트로 간다(gitlab.com 아님)."""
    cfg, root = _cfg_with_forge_secret(
        tmp_path, dlc_meta_url="https://gitlab.corp.example.com/g/dlc-meta.git")
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_PASS
    url, _headers = http.calls[0]
    assert url == "https://gitlab.corp.example.com/api/v4/user"
    assert "gitlab.com" not in url


def test_forge_token_skips_instead_of_sending_the_token_to_saas(tmp_path):
    """⚠️ 갈 곳을 모르면 **요청 자체를 하지 않는다** — 사내 PAT 의 외부 전송 방지."""
    cfg, root = _cfg_with_forge_secret(tmp_path, dlc_meta_url="")
    cfg.run.docs_repo_url = ""
    cfg.forge.base_url = ""
    cfg.forge.base_url_source = ""
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_SKIP
    assert http.calls == []                      # 토큰이 어디로도 나가지 않았다
    assert "forge.base_url" in r.hint


def test_forge_token_skips_when_self_hosted_url_has_no_http_scheme(tmp_path):
    """ssh 로만 적힌 사내 레포 — self-hosted 신호는 있지만 주소를 모른다 → 보내지 않는다."""
    cfg, root = _cfg_with_forge_secret(
        tmp_path, dlc_meta_url="git@gitlab.corp.example.com:g/dlc-meta.git")
    cfg.run.docs_repo_url = ""
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_SKIP
    assert http.calls == []
    assert "gitlab.corp.example.com" in r.message


def test_forge_token_uses_gitlab_saas_when_the_repo_url_says_so(tmp_path):
    cfg, root = _cfg_with_forge_secret(
        tmp_path, dlc_meta_url="https://gitlab.com/acme/dlc-meta.git")
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_PASS
    assert http.calls[0][0] == "https://gitlab.com/api/v4/user"


def test_forge_token_skip_message_never_leaks_the_token(tmp_path):
    token = "glpat-TOPSECRETTOKEN"
    cfg, root = _cfg_with_forge_secret(tmp_path, token=token, dlc_meta_url="")
    cfg.run.docs_repo_url = ""
    cfg.forge.base_url_source = ""
    r = D.check_forge_token(cfg, project_dir=root, http=FakeHttp())
    assert token not in (r.message + r.hint)


# ---------------------------------------------------------------------------
# 은퇴한 검사 — worker 공유 시크릿
# ---------------------------------------------------------------------------


def test_retired_worker_secret_check_is_gone_from_the_catalog():
    """``worker_secret`` 검사는 **은퇴했다** — 아무것도 인증하지 않는 값이었다.

    옛 모델에서 이 시크릿은 워커가 중앙의 dispatch HTTP 를 부를 때 쓰는
    ``X-Worker-Secret`` 값이었다. 그 서빙 표면과 폴링 소비자가 프랙탈 seam(중앙 →
    docker exec 푸시)으로 대체되며 읽는 곳이 사라졌으므로, 설치자에게 **왜 검사하는지
    모르는 항목**을 보여 주지 않는다.
    """
    assert "worker_secret" not in D.CHECK_ORDER
    assert not hasattr(D, "check_worker_secret")
    with pytest.raises(ValueError):
        D.run_checks(make_cfg(), only=("worker_secret",))
