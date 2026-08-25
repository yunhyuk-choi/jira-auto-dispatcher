"""설정 실측 진단(app/setup_doctor.py) 단위테스트.

⚠️ **네트워크·도커·git 에 절대 실제로 붙지 않는다** — 모든 외부 의존은 대역(fake)으로
주입한다(CI: GitHub Actions ubuntu 에서 그대로 돈다). 그래서 각 검사 함수는 대역을
받도록 설계돼 있다.

특히 다음 두 가지를 집중해서 지킨다:
    - ``host_deploy_dir`` 함정 — 비었을 때/컨테이너 경로일 때 **실패시켜야** 한다.
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
        "deploy": {"profile": "cloud_vm", "host_deploy_dir": "/srv/jad",
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
# host_deploy_dir — 이 리포에서 가장 자주 밟는 함정
# ---------------------------------------------------------------------------


def test_empty_host_deploy_dir_on_server_profile_fails(tmp_path):
    cfg = make_cfg(deploy={"profile": "cloud_vm", "host_deploy_dir": ""})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL
    assert "HOST_DEPLOY_DIR" in r.hint


def test_empty_host_deploy_dir_with_remote_docker_fails(tmp_path):
    """local 프로파일이어도 docker 엔드포인트가 원격이면 폴백 전제가 깨진다."""
    cfg = make_cfg(deploy={"profile": "local", "host_deploy_dir": "",
                           "docker_host": "tcp://socket-proxy:2375",
                           "secrets_base_dir": str(tmp_path)})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL


def test_empty_host_deploy_dir_with_compose_file_warns(tmp_path):
    """compose 로 띄우면 깨지지만 소켓 직결 로컬이면 괜찮다 — 경고가 맞다."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    cfg = make_cfg(deploy={"profile": "local", "host_deploy_dir": "",
                           "docker_host": "unix:///var/run/docker.sock",
                           "secrets_base_dir": str(tmp_path)})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_WARN
    assert "HOST_DEPLOY_DIR=$PWD" in r.hint


def test_empty_host_deploy_dir_pure_local_passes(tmp_path):
    cfg = make_cfg(deploy={"profile": "local", "host_deploy_dir": "",
                           "docker_host": "unix:///var/run/docker.sock",
                           "secrets_base_dir": str(tmp_path)})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_PASS


@pytest.mark.parametrize("value", ["/run/secrets", "/app", "/app/config"])
def test_container_path_as_host_deploy_dir_fails(tmp_path, value):
    cfg = make_cfg(deploy={"host_deploy_dir": value})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL
    assert "컨테이너" in r.message


def test_backslash_host_deploy_dir_fails(tmp_path):
    cfg = make_cfg(deploy={"host_deploy_dir": "C:\\Users\\me\\jad"})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_FAIL


def test_relative_host_deploy_dir_fails(tmp_path):
    cfg = make_cfg(deploy={"host_deploy_dir": "deploy/jad"})
    assert D.check_host_deploy_dir(cfg, project_dir=str(tmp_path)).status == D.STATUS_FAIL


def test_existing_deploy_dir_with_layout_passes(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    cfg = make_cfg(deploy={"host_deploy_dir": str(tmp_path).replace("\\", "/")})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_PASS


def test_existing_dir_without_layout_warns(tmp_path):
    cfg = make_cfg(deploy={"host_deploy_dir": str(tmp_path).replace("\\", "/")})
    r = D.check_host_deploy_dir(cfg, project_dir=str(tmp_path))
    assert r.status == D.STATUS_WARN


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
    """JiraClient 대역 — 지정한 결과/예외를 그대로 낸다."""

    def __init__(self, me=None, page=None, error=None):
        self._me = me or {"displayName": "봇 계정"}
        self._page = page if page is not None else {"issues": [{"key": "ACME-1"}]}
        self._error = error

    def myself(self):
        if self._error:
            raise self._error
        return self._me

    def search_jql_page(self, jql, fields=None, max_results=50, next_page_token=None):
        if self._error:
            raise self._error
        return self._page


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


def _cfg_with_forge_secret(tmp_path, kind="gitlab", token="glpat-TOPSECRETTOKEN"):
    root = str(tmp_path)
    write_secret(root, "service/forge-token", token)
    write_secret(root, "service/jira-token")
    return make_cfg(deploy={"secrets_base_dir": root}, forge={"kind": kind}), root


def test_forge_token_gitlab_success(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path)
    http = FakeHttp()
    r = D.check_forge_token(cfg, project_dir=root, http=http)
    assert r.status == D.STATUS_PASS and "svc-bot" in r.message
    url, headers = http.calls[0]
    assert url.endswith("/api/v4/user") and "PRIVATE-TOKEN" in headers


def test_forge_token_github_uses_bearer_and_saas_endpoint(tmp_path):
    cfg, root = _cfg_with_forge_secret(tmp_path, kind="github")
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
                           only=("config", "host_deploy_dir"))
    assert [r.name for r in results] == ["config", "host_deploy_dir"]


def test_run_checks_rejects_unknown_names():
    with pytest.raises(ValueError):
        D.run_checks(make_cfg(), only=("nope",))


def test_a_crashing_check_does_not_kill_the_run(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise RuntimeError("내부 사고")

    monkeypatch.setattr(D, "check_config", boom)
    results = D.run_checks(make_cfg(), project_dir=str(tmp_path),
                           only=("config", "host_deploy_dir"))
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
