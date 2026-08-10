"""spawner 단위테스트 — docker SDK client mock(라이브 Docker 호출 없음).

검증: ensure_worker가 올바른 image/name/network/restart/mem_limit/비-root/env/
volumes로 containers.run을 호출한다 / bypass settings.json 내용 생성 / 시크릿
값이 로그·env에 노출되지 않는다(Jira/GitLab은 파일경로로) / stop/remove/status.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.registry import Registry, SecretsRef, UserRecord
from app.spawner import (
    DEFAULT_RUN_AS,
    SETTINGS_PATH_IN_CONTAINER,
    Spawner,
    render_settings,
    render_settings_json,
)


def _cfg(base_dir: str):
    return SimpleNamespace(
        spawn=SimpleNamespace(
            image="jira-auto-dispatcher:latest",
            network="jad-net",
            central_url="http://central:8787",
            mem_limit="4g",
            docker_host="unix:///var/run/docker.sock",
            run_as="1000:1000",
        ),
        secrets=SimpleNamespace(base_dir=base_dir),
        worker_shared_secret="s3cr3t",
    )


def _user():
    return UserRecord(
        username="yh.choi",
        jira_account_id="acc",
        jira_email="yh@x",
        permission_level="bypass",
        secrets_ref=SecretsRef(
            jira_token="yh.choi/jira-token",
            gitlab_token="yh.choi/gitlab-token",
            claude_oauth_token="yh.choi/claude-oauth-token",
        ),
    )


def _write(base: str, rel: str, value: str) -> None:
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value)


def _client_absent():
    """컨테이너 부재를 흉내내는 mock docker client(get → 예외)."""
    client = MagicMock()
    client.containers.get.side_effect = Exception("not found")
    client.containers.run.return_value = SimpleNamespace(id="cid-123")
    return client


# --- settings.json 번역 ---


def test_render_settings_bypass_content():
    s = render_settings("bypass")
    assert s["permissions"]["defaultMode"] == "bypassPermissions"
    assert s["skipDangerousModePermissionPrompt"] is True
    assert s["skipAutoPermissionPrompt"] is True
    assert s["skipWorkflowUsageWarning"] is True
    # 기본값도 bypass.
    assert render_settings() == s
    # JSON 문자열도 동일 내용.
    assert json.loads(render_settings_json("bypass")) == s


def test_render_settings_unsupported_is_todo():
    with pytest.raises(NotImplementedError):
        render_settings("sandbox")
    with pytest.raises(NotImplementedError):
        render_settings("allowlist")


# --- ensure_worker run() 인자 ---


def test_ensure_worker_run_args(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    _write(base, "yh.choi/claude-oauth-token", "CLAUDE-XYZ")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = _client_absent()
    sp = Spawner(_cfg(base), reg, client=client)

    cid = sp.ensure_worker(u)
    assert cid == "cid-123"

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == "jira-auto-dispatcher:latest"
    assert kwargs["name"] == "jad-worker-yh.choi"
    assert kwargs["network"] == "jad-net"
    assert kwargs["mem_limit"] == "4g"
    assert kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert kwargs["user"] == "1000:1000"
    assert kwargs["detach"] is True

    env = kwargs["environment"]
    assert env["ROLE"] == "worker"
    assert env["DISPATCH_USER"] == "yh.choi"
    assert env["CENTRAL_URL"] == "http://central:8787"
    assert env["WORKER_SHARED_SECRET"] == "s3cr3t"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "CLAUDE-XYZ"
    assert env["SECRETS_DIR"] == "/run/secrets"
    assert env["JIRA_TOKEN_FILE"] == "/run/secrets/yh.choi/jira-token"
    assert env["GITLAB_TOKEN_FILE"] == "/run/secrets/yh.choi/gitlab-token"
    assert env["JIRA_EMAIL"] == "yh@x"

    vols = kwargs["volumes"]
    assert vols["jad-yh.choi"] == {"bind": "/home/app/.claude", "mode": "rw"}
    # 사전 인가 settings.json은 read-only 바인드.
    settings_binds = [v for v in vols.values() if v["bind"] == SETTINGS_PATH_IN_CONTAINER]
    assert settings_binds and settings_binds[0]["mode"] == "ro"
    # per-user 시크릿 디렉토리는 read-only.
    secret_binds = [v for v in vols.values() if v["bind"] == "/run/secrets/yh.choi"]
    assert secret_binds and secret_binds[0]["mode"] == "ro"

    # 레지스트리 상태 갱신.
    assert reg.get("yh.choi").container.status == "running"
    assert reg.get("yh.choi").container.name == "jad-worker-yh.choi"


def test_ensure_worker_default_run_as_when_unset(tmp_path, isolated_state):
    cfg = _cfg(str(tmp_path / "s"))
    cfg.spawn.run_as = ""  # 미설정 → 기본 비-root
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = _client_absent()
    Spawner(cfg, reg, client=client).ensure_worker(u)
    assert client.containers.run.call_args.kwargs["user"] == DEFAULT_RUN_AS


def test_ensure_worker_writes_bypass_settings_file(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    Spawner(_cfg(base), reg, client=_client_absent()).ensure_worker(u)
    path = os.path.join(base, "yh.choi", "claude-settings.json")
    assert os.path.exists(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "bypassPermissions"


def test_jira_gitlab_secrets_passed_as_path_not_value(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    _write(base, "yh.choi/jira-token", "JIRA-SECRET-VAL")
    _write(base, "yh.choi/gitlab-token", "GL-SECRET-VAL")
    _write(base, "yh.choi/claude-oauth-token", "CLAUDE-XYZ")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    env = sp.build_env(_user())
    # Jira/GitLab 토큰 "값"은 어떤 env에도 실리지 않는다(파일경로만).
    joined = "\n".join(str(v) for v in env.values())
    assert "JIRA-SECRET-VAL" not in joined
    assert "GL-SECRET-VAL" not in joined
    assert env["JIRA_TOKEN_FILE"].endswith("yh.choi/jira-token")
    assert env["GITLAB_TOKEN_FILE"].endswith("yh.choi/gitlab-token")
    # Claude setup-token은 값으로 주입(계약).
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "CLAUDE-XYZ"


def test_ensure_worker_reuses_running_container(tmp_path, isolated_state):
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = MagicMock()
    existing = MagicMock()
    existing.status = "running"
    existing.id = "old-id"
    client.containers.get.return_value = existing
    client.containers.get.side_effect = None
    cid = Spawner(_cfg(str(tmp_path / "s")), reg, client=client).ensure_worker(u)
    assert cid == "old-id"
    client.containers.run.assert_not_called()
    existing.start.assert_not_called()


def test_ensure_worker_starts_stopped_container(tmp_path, isolated_state):
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = MagicMock()
    existing = MagicMock()
    existing.status = "exited"
    existing.id = "old-id"
    client.containers.get.return_value = existing
    client.containers.get.side_effect = None
    Spawner(_cfg(str(tmp_path / "s")), reg, client=client).ensure_worker(u)
    existing.start.assert_called_once()
    client.containers.run.assert_not_called()


# --- stop / remove / status ---


def test_stop_worker(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    client = MagicMock()
    cont = MagicMock()
    client.containers.get.return_value = cont
    client.containers.get.side_effect = None
    Spawner(_cfg(str(tmp_path / "s")), reg, client=client).stop_worker("yh.choi")
    cont.stop.assert_called_once()
    assert reg.get("yh.choi").container.status == "stopped"


def test_remove_worker(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    client = MagicMock()
    cont = MagicMock()
    client.containers.get.return_value = cont
    client.containers.get.side_effect = None
    Spawner(_cfg(str(tmp_path / "s")), reg, client=client).remove_worker("yh.choi")
    cont.remove.assert_called_once_with(force=True)
    assert reg.get("yh.choi").container.status == "absent"


def test_worker_status_absent(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    sp = Spawner(_cfg(str(tmp_path / "s")), reg, client=_client_absent())
    assert sp.worker_status("yh.choi") == "absent"
    assert reg.get("yh.choi").container.status == "absent"


def test_worker_status_running_and_stopped(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    client = MagicMock()
    client.containers.get.side_effect = None
    client.containers.get.return_value = SimpleNamespace(status="running")
    sp = Spawner(_cfg(str(tmp_path / "s")), reg, client=client)
    assert sp.worker_status("yh.choi") == "running"
    client.containers.get.return_value = SimpleNamespace(status="exited")
    assert sp.worker_status("yh.choi") == "stopped"


def test_status_update_noop_for_unregistered(tmp_path, isolated_state):
    # 미등록 사용자여도 예외 없이 상태 문자열만 반환.
    sp = Spawner(_cfg(str(tmp_path / "s")), Registry(), client=_client_absent())
    assert sp.worker_status("nobody") == "absent"


def test_module_level_ensure_worker(tmp_path, isolated_state):
    from app import spawner as sp_mod

    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = _client_absent()
    cid = sp_mod.ensure_worker(u, _cfg(base), registry=reg, client=client)
    assert cid == "cid-123"
    client.containers.run.assert_called_once()
