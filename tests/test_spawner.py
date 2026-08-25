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
    CONFIG_DIR_IN_CONTAINER,
    DEFAULT_RUN_AS,
    DEFAULT_WORKSPACE_VOLUME,
    SETTINGS_PATH_IN_CONTAINER,
    Spawner,
    render_settings,
    render_settings_json,
)


def _cfg(base_dir: str, host_deploy_dir: str = "", notify=None):
    ns = SimpleNamespace(
        spawn=SimpleNamespace(
            image="jira-auto-dispatcher:latest",
            network="jad-net",
            central_url="http://central:8787",
            mem_limit="4g",
            docker_host="unix:///var/run/docker.sock",
            run_as="1000:1000",
            host_deploy_dir=host_deploy_dir,
            workspace_volume="jad-workspace",
        ),
        run=SimpleNamespace(workspace_dir="/app/workspace"),
        secrets=SimpleNamespace(base_dir=base_dir),
        worker_shared_secret="s3cr3t",
    )
    if notify is not None:
        ns.notify = notify
    return ns


def _notify(enabled=True, webhook_ref="service/google-chat-webhook"):
    return SimpleNamespace(enabled=enabled, webhook_ref=webhook_ref)


def _user():
    return UserRecord(
        username="testuser",
        jira_account_id="acc",
        jira_email="yh@x",
        permission_level="bypass",
        secrets_ref=SecretsRef(
            jira_token="testuser/jira-token",
            gitlab_token="testuser/gitlab-token",
            claude_oauth_token="testuser/claude-oauth-token",
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
    _write(base, "testuser/claude-oauth-token", "CLAUDE-XYZ")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = _client_absent()
    sp = Spawner(_cfg(base), reg, client=client)

    cid = sp.ensure_worker(u)
    assert cid == "cid-123"

    kwargs = client.containers.run.call_args.kwargs
    assert kwargs["image"] == "jira-auto-dispatcher:latest"
    assert kwargs["name"] == "jad-worker-testuser"
    assert kwargs["network"] == "jad-net"
    assert kwargs["mem_limit"] == "4g"
    assert kwargs["restart_policy"] == {"Name": "unless-stopped"}
    assert kwargs["user"] == "1000:1000"
    assert kwargs["detach"] is True
    # ⚠️ 좀비 프로세스 방지(결정적 픽스): 워커 컨테이너를 Docker init(tini)로 띄운다.
    # docker-py containers.run(init=True) → HostConfig.Init=true → tini가 PID 1로 고아 reap.
    assert kwargs["init"] is True

    env = kwargs["environment"]
    assert env["ROLE"] == "worker"
    assert env["DISPATCH_USER"] == "testuser"
    assert env["CENTRAL_URL"] == "http://central:8787"
    assert env["WORKER_SHARED_SECRET"] == "s3cr3t"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "CLAUDE-XYZ"
    assert env["SECRETS_DIR"] == "/run/secrets"
    assert env["JIRA_TOKEN_FILE"] == "/run/secrets/testuser/jira-token"
    assert env["GITLAB_TOKEN_FILE"] == "/run/secrets/testuser/gitlab-token"
    assert env["JIRA_EMAIL"] == "yh@x"

    vols = kwargs["volumes"]
    assert vols["jad-testuser"] == {"bind": "/home/app/.claude", "mode": "rw"}
    # 사전 인가 settings.json은 더 이상 파일 바인드하지 않는다(두 번째 spawn 버그 픽스 —
    # 명명 볼륨 하위 파일 경로에 바인드하면 runc가 거부). worker 부팅 시 복사로 대체.
    settings_binds = [v for v in vols.values() if v["bind"] == SETTINGS_PATH_IN_CONTAINER]
    assert settings_binds == []
    # per-user 시크릿 디렉토리는 read-only(claude-settings.json도 여기 포함).
    secret_binds = [v for v in vols.values() if v["bind"] == "/run/secrets/testuser"]
    assert secret_binds and secret_binds[0]["mode"] == "ro"

    # 레지스트리 상태 갱신.
    assert reg.get("testuser").container.status == "running"
    assert reg.get("testuser").container.name == "jad-worker-testuser"


def test_build_spec_sets_init_true_for_zombie_reaping(tmp_path, isolated_state):
    """build_spec는 항상 init=True(Docker tini)를 담아 워커 PID 1의 좀비 reaping을 보장."""
    base = str(tmp_path / "secrets")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    spec = sp.build_spec(_user())
    assert spec["init"] is True


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
    path = os.path.join(base, "testuser", "claude-settings.json")
    assert os.path.exists(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["permissions"]["defaultMode"] == "bypassPermissions"


def test_jira_gitlab_secrets_passed_as_path_not_value(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "JIRA-SECRET-VAL")
    _write(base, "testuser/gitlab-token", "GL-SECRET-VAL")
    _write(base, "testuser/claude-oauth-token", "CLAUDE-XYZ")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    env = sp.build_env(_user())
    # Jira/GitLab 토큰 "값"은 어떤 env에도 실리지 않는다(파일경로만).
    joined = "\n".join(str(v) for v in env.values())
    assert "JIRA-SECRET-VAL" not in joined
    assert "GL-SECRET-VAL" not in joined
    assert env["JIRA_TOKEN_FILE"].endswith("testuser/jira-token")
    assert env["GITLAB_TOKEN_FILE"].endswith("testuser/gitlab-token")
    # Claude setup-token은 값으로 주입(계약).
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "CLAUDE-XYZ"


def test_build_env_injects_worker_safety_ceiling(tmp_path, isolated_state):
    # 스포너는 worker 동시 실행 **안전 상한**(runaway 백스톱, 정책 cap 아님)만 주입한다.
    # 값: run.worker_max_concurrency(>0) → env WORKER_CONCURRENCY. 진짜 스로틀은 central 자원 어드미션.
    base = str(tmp_path / "secrets")
    cfg = _cfg(base)
    cfg.run = SimpleNamespace(workspace_dir="/app/workspace", worker_max_concurrency=64)
    env = Spawner(cfg, Registry(), client=_client_absent()).build_env(_user())
    assert env["WORKER_CONCURRENCY"] == "64"          # 안전 상한 주입
    cfg.run.worker_max_concurrency = 32
    env2 = Spawner(cfg, Registry(), client=_client_absent()).build_env(_user())
    assert env2["WORKER_CONCURRENCY"] == "32"


def test_build_env_omits_worker_concurrency_when_unset(tmp_path, isolated_state):
    # run에 worker_max_concurrency가 없으면(구 config) WORKER_CONCURRENCY를 생략
    # (폴백 = worker가 기본 안전 상한 64로 파생).
    env = Spawner(_cfg(str(tmp_path / "s")), Registry(),
                  client=_client_absent()).build_env(_user())
    assert "WORKER_CONCURRENCY" not in env


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
    Spawner(_cfg(str(tmp_path / "s")), reg, client=client).stop_worker("testuser")
    cont.stop.assert_called_once()
    assert reg.get("testuser").container.status == "stopped"


def test_remove_worker(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    client = MagicMock()
    cont = MagicMock()
    client.containers.get.return_value = cont
    client.containers.get.side_effect = None
    Spawner(_cfg(str(tmp_path / "s")), reg, client=client).remove_worker("testuser")
    cont.remove.assert_called_once_with(force=True)
    assert reg.get("testuser").container.status == "absent"


def test_worker_status_absent(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    sp = Spawner(_cfg(str(tmp_path / "s")), reg, client=_client_absent())
    assert sp.worker_status("testuser") == "absent"
    assert reg.get("testuser").container.status == "absent"


def test_worker_status_running_and_stopped(tmp_path, isolated_state):
    reg = Registry()
    reg.upsert(_user())
    client = MagicMock()
    client.containers.get.side_effect = None
    client.containers.get.return_value = SimpleNamespace(status="running")
    sp = Spawner(_cfg(str(tmp_path / "s")), reg, client=client)
    assert sp.worker_status("testuser") == "running"
    client.containers.get.return_value = SimpleNamespace(status="exited")
    assert sp.worker_status("testuser") == "stopped"


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


# --- build_volumes: 호스트 경로 바인드(sibling container 픽스) ---


def test_build_volumes_uses_host_paths_when_host_deploy_dir_set(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    host = "/home/<deploy-user>/deploy/jira-auto-dispatcher"
    sp = Spawner(_cfg(base, host_deploy_dir=host), Registry(), client=_client_absent())
    settings_path = sp.write_settings("testuser", "bypass")
    vols = sp.build_volumes(_user(), settings_path)

    # config: 호스트 경로 → /app/config (ro) — 크래시 픽스.
    assert vols[host + "/config"] == {"bind": CONFIG_DIR_IN_CONTAINER, "mode": "ro"}
    # per-user 시크릿: 호스트 경로 → /run/secrets/<user> (ro).
    assert vols[host + "/secrets/testuser"] == {"bind": "/run/secrets/testuser", "mode": "ro"}
    # settings.json은 더 이상 파일 바인드하지 않는다(두 번째 spawn 버그 픽스).
    assert host + "/secrets/testuser/claude-settings.json" not in vols
    assert all(v["bind"] != SETTINGS_PATH_IN_CONTAINER for v in vols.values())
    # 명명 볼륨은 호스트 경로 무관 — 그대로.
    assert vols["jad-testuser"] == {"bind": "/home/app/.claude", "mode": "rw"}
    # central 내부 경로(settings_path·base_dir/<user>)는 worker 바인드 source로 쓰이지 않는다.
    assert settings_path not in vols
    assert os.path.join(base, "testuser") not in vols


def test_build_volumes_fallback_warns_and_uses_direct_paths(tmp_path, isolated_state, caplog):
    import logging

    base = str(tmp_path / "secrets")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())  # host_deploy_dir 미설정
    settings_path = sp.write_settings("testuser", "bypass")
    with caplog.at_level(logging.WARNING, logger="jad.spawner"):
        vols = sp.build_volumes(_user(), settings_path)
    # 경고 로그.
    assert any("host_deploy_dir" in r.getMessage() for r in caplog.records)
    # 직접 경로 폴백: base_dir/<user> 를 source로. settings.json은 바인드 안 함.
    assert settings_path not in vols
    assert all(v["bind"] != SETTINGS_PATH_IN_CONTAINER for v in vols.values())
    assert os.path.join(base, "testuser") in vols
    # config 마운트는 폴백에서도 포함.
    config_binds = [v for v in vols.values() if v["bind"] == CONFIG_DIR_IN_CONTAINER]
    assert config_binds and config_binds[0]["mode"] == "ro"


def test_build_volumes_config_mount_always_present(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    # host_deploy_dir 설정·미설정 양쪽 모두 config 마운트가 항상 포함된다.
    for cfg in (_cfg(base), _cfg(base, host_deploy_dir="/host/deploy")):
        sp = Spawner(cfg, Registry(), client=_client_absent())
        settings_path = sp.write_settings("testuser", "bypass")
        vols = sp.build_volumes(_user(), settings_path)
        binds = [v["bind"] for v in vols.values()]
        assert CONFIG_DIR_IN_CONTAINER in binds


def test_build_volumes_no_settings_file_bind(tmp_path, isolated_state):
    """두 번째 spawn 버그 픽스: settings.json을 명명 볼륨 하위 파일 경로에 바인드하면
    runc가 거부한다 → 파일 바인드는 없어야 하고, config·per-user 시크릿 dir·claude
    명명 볼륨·공유 워크스페이스 4개만 남는다(양쪽 host_deploy_dir 모드).
    """
    base = str(tmp_path / "secrets")
    for cfg in (_cfg(base), _cfg(base, host_deploy_dir="/host/deploy")):
        sp = Spawner(cfg, Registry(), client=_client_absent())
        settings_path = sp.write_settings("testuser", "bypass")
        vols = sp.build_volumes(_user(), settings_path)
        # settings.json 파일 바인드가 어느 source·bind로도 존재하지 않는다.
        assert all(v["bind"] != SETTINGS_PATH_IN_CONTAINER for v in vols.values())
        assert not any(
            str(src).endswith("claude-settings.json") for src in vols.keys()
        )
        # 정확히 config·claude 명명 볼륨·per-user 시크릿 dir·공유 워크스페이스 4개만.
        binds = sorted(v["bind"] for v in vols.values())
        assert binds == sorted(
            [CONFIG_DIR_IN_CONTAINER, "/home/app/.claude", "/run/secrets/testuser",
             "/app/workspace"]
        )


def test_build_volumes_mounts_shared_workspace(tmp_path, isolated_state):
    """공유 워크스페이스 named 볼륨을 run.workspace_dir 에 rw로 마운트한다(설계 §4).

    볼륨명은 config.spawn.workspace_volume(기본 jad-workspace)이고, named 볼륨이라
    host_deploy_dir 설정/미설정 양쪽 모두 동일하게 존재한다(호스트 경로 매핑 무관).
    """
    base = str(tmp_path / "secrets")
    for cfg in (_cfg(base), _cfg(base, host_deploy_dir="/host/deploy")):
        vols = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
        assert vols["jad-workspace"] == {"bind": "/app/workspace", "mode": "rw"}


def test_build_volumes_workspace_volume_name_configurable(tmp_path, isolated_state):
    """볼륨명·bind 경로가 config에서 온다(기본 폴백은 DEFAULT_WORKSPACE_VOLUME)."""
    base = str(tmp_path / "secrets")
    cfg = _cfg(base, host_deploy_dir="/host/deploy")
    cfg.spawn.workspace_volume = "custom-ws"
    cfg.run.workspace_dir = "/srv/ws"
    vols = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
    assert vols["custom-ws"] == {"bind": "/srv/ws", "mode": "rw"}
    assert "jad-workspace" not in vols
    # 볼륨명 미설정 시 기본 상수로 폴백.
    cfg.spawn.workspace_volume = ""
    vols2 = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
    assert DEFAULT_WORKSPACE_VOLUME in vols2


def test_build_volumes_settings_path_optional(tmp_path, isolated_state):
    """settings_path 인자는 하위호환(무시)이라 없이도 호출 가능하고 결과가 같다."""
    base = str(tmp_path / "secrets")
    sp = Spawner(_cfg(base, host_deploy_dir="/host/deploy"), Registry(), client=_client_absent())
    assert sp.build_volumes(_user()) == sp.build_volumes(_user(), "ignored/path")


# --- build_volumes: 완료 알림 웹훅 시크릿 마운트(worker notify 버그 픽스) ---


def test_build_volumes_mounts_webhook_when_notify_configured(tmp_path, isolated_state):
    """notify 설정(enabled+webhook_ref)+host_deploy_dir → 웹훅 파일 하나만 worker ro 마운트."""
    base = str(tmp_path / "secrets")
    host = "/home/<deploy-user>/deploy/jira-auto-dispatcher"
    cfg = _cfg(base, host_deploy_dir=host, notify=_notify())
    sp = Spawner(cfg, Registry(), client=_client_absent())
    vols = sp.build_volumes(_user())

    # host 경로 → /run/secrets/service/google-chat-webhook (ro). base_dir 기준 dest.
    assert vols[host + "/secrets/service/google-chat-webhook"] == {
        "bind": "/run/secrets/service/google-chat-webhook",
        "mode": "ro",
    }
    # ⚠️ service 디렉토리 전체는 절대 노출하지 않는다(central watcher jira-token 보호).
    #    마운트 dest도 source도 service '디렉토리'가 아니라 웹훅 '파일'이어야 한다.
    assert "/run/secrets/service" not in [v["bind"] for v in vols.values()]
    assert host + "/secrets/service" not in vols
    assert not any(str(src).endswith("/secrets/service") for src in vols.keys())
    # 기존 4개(config·claude 볼륨·per-user 시크릿·공유 워크스페이스) + 웹훅 1개 = 5개.
    assert len(vols) == 5


def test_build_volumes_no_webhook_when_notify_disabled(tmp_path, isolated_state):
    """notify.enabled=False면 웹훅 마운트를 추가하지 않는다(기존 3개만)."""
    base = str(tmp_path / "secrets")
    host = "/host/deploy"
    cfg = _cfg(base, host_deploy_dir=host, notify=_notify(enabled=False))
    vols = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
    assert not any(str(src).endswith("google-chat-webhook") for src in vols.keys())
    assert len(vols) == 4


def test_build_volumes_no_webhook_when_ref_empty(tmp_path, isolated_state):
    """enabled여도 webhook_ref가 비어 있으면 마운트하지 않는다."""
    base = str(tmp_path / "secrets")
    host = "/host/deploy"
    cfg = _cfg(base, host_deploy_dir=host, notify=_notify(webhook_ref=""))
    vols = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
    assert all("google-chat-webhook" not in str(src) for src in vols.keys())
    assert len(vols) == 4


def test_build_volumes_no_webhook_without_host_deploy_dir(tmp_path, isolated_state):
    """host_deploy_dir 미설정(로컬 폴백)이면 웹훅 마운트를 생략한다(단순 가드)."""
    base = str(tmp_path / "secrets")
    cfg = _cfg(base, notify=_notify())  # host_deploy_dir 미설정
    vols = Spawner(cfg, Registry(), client=_client_absent()).build_volumes(_user())
    assert not any(str(src).endswith("google-chat-webhook") for src in vols.keys())
    assert len(vols) == 4


# --- 작업 C: 배포 시 워커 이미지 reconcile(stale 재생성 / 활성 잡 드레인) -------


class FakeDockerForReconcile:
    """이미지 ID·컨테이너 상태를 통제하는 stateful docker 대역(reconcile 검증).

    - images.get → 현재 이미지 ID(worker_image_id).
    - containers.get → 실행 컨테이너(.image.id = container_img_id). present=False면 부재.
    - containers.run → 재spawn 기록(present=True로 전환).
    - container.remove → present=False로 전환(제거 기록).
    """

    def __init__(self, container_img_id, current_img_id, status="running", present=True):
        self.container_img_id = container_img_id
        self.current_img_id = current_img_id
        self.status = status
        self.present = present
        self.run_calls = []
        self.removed = []
        self.containers = SimpleNamespace(get=self._get, run=self._run)
        self.images = SimpleNamespace(get=self._img_get)
        self.volumes = SimpleNamespace(
            get=lambda n: SimpleNamespace(name=n),
            create=lambda n: SimpleNamespace(name=n),
        )

    def _get(self, name):
        if not self.present:
            raise Exception("not found")
        c = MagicMock()
        c.image = SimpleNamespace(id=self.container_img_id)
        c.status = self.status
        c.id = "cid-existing"
        c.remove = self._remove
        c.stop = lambda **k: None
        return c

    def _remove(self, force=False):
        self.removed.append(force)
        self.present = False

    def _run(self, **kwargs):
        self.run_calls.append(kwargs)
        self.present = True
        return SimpleNamespace(id="cid-new")

    def _img_get(self, tag):
        return SimpleNamespace(id=self.current_img_id)


def test_worker_image_id_returns_current_tag_id(tmp_path, isolated_state):
    fake = FakeDockerForReconcile(container_img_id="x", current_img_id="img-1")
    sp = Spawner(_cfg(str(tmp_path / "s")), Registry(), client=fake)
    assert sp.worker_image_id() == "img-1"


def test_reconcile_recreates_stale_idle_worker(tmp_path, isolated_state):
    """stale 이미지 + 유휴(활성 잡 없음) → 즉시 remove+재spawn."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="img-old", current_img_id="img-new")
    sp = Spawner(_cfg(base), reg, client=fake)

    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary["recreated"] == ["testuser"]
    assert fake.removed == [True]         # 제거 후
    assert len(fake.run_calls) == 1       # 재spawn
    assert "testuser" not in sp._pending_recreate


def test_reconcile_defers_stale_active_then_recreates_on_idle(tmp_path, isolated_state):
    """stale 이미지 + 활성 잡 → 즉시 죽이지 않고 드레인(pending). 잡 종료 후 재생성."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="img-old", current_img_id="img-new")
    sp = Spawner(_cfg(base), reg, client=fake)
    busy = {"v": True}

    summary = sp.reconcile_workers([u], has_active_job=lambda name: busy["v"])
    assert summary["deferred"] == ["testuser"]
    assert fake.run_calls == []           # in-flight 잡을 배포가 죽이지 않는다
    assert fake.removed == []
    assert "testuser" in sp._pending_recreate

    # 아직 활성 → pending 유지(재생성 안 함).
    p1 = sp.reconcile_pending(has_active_job=lambda name: busy["v"])
    assert p1["still_active"] == ["testuser"]
    assert fake.run_calls == []

    # 잡 종료 → 다음 reconcile_pending에서 재생성.
    busy["v"] = False
    p2 = sp.reconcile_pending(has_active_job=lambda name: busy["v"])
    assert p2["recreated"] == ["testuser"]
    assert len(fake.run_calls) == 1
    assert "testuser" not in sp._pending_recreate


def test_reconcile_noop_when_image_current(tmp_path, isolated_state):
    """동일 이미지면 no-op(재생성/제거 없음)."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="img-same", current_img_id="img-same")
    sp = Spawner(_cfg(base), reg, client=fake)
    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary["skipped"] == ["testuser"]
    assert fake.run_calls == [] and fake.removed == []


def test_reconcile_skips_absent_container(tmp_path, isolated_state):
    """enabled인데 컨테이너 부재 = 이미지 stale 조정 대상 아님(skip, 별도 라이프사이클)."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="x", current_img_id="img-new", present=False)
    sp = Spawner(_cfg(base), reg, client=fake)
    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary["skipped"] == ["testuser"]
    assert fake.run_calls == []


def test_reconcile_skips_when_current_image_id_unavailable(tmp_path, isolated_state):
    """현재 이미지 ID 조회 불가 → reconcile 전체 생략(best-effort)."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="x", current_img_id=None)
    sp = Spawner(_cfg(base), reg, client=fake)
    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary == {"recreated": [], "deferred": [], "skipped": [], "errors": []}
    assert fake.run_calls == []


class _ImageNotFound(Exception):
    """docker.errors.ImageNotFound 대역(이름/status_code 기반 판정) — 수정 3."""

    status_code = 404


class _RaisingImgContainer:
    """컨테이너 이미지 inspect(.image 접근)가 ImageNotFound(404)를 던지는 대역.

    rebuild로 옛 이미지 ID가 사라진 상황(실측 로그) 재현. remove/stop은 정상 동작.
    """

    def __init__(self, remove_cb, status="running"):
        self.status = status
        self.id = "cid-existing"
        self._remove_cb = remove_cb

    @property
    def image(self):
        raise _ImageNotFound("no such image")

    def remove(self, force=False):
        self._remove_cb(force)

    def stop(self, **k):
        pass


class FakeDockerImageNotFound(FakeDockerForReconcile):
    """컨테이너 이미지 inspect가 ImageNotFound(404)를 던지는 docker 대역."""

    def _get(self, name):
        if not self.present:
            raise Exception("not found")
        return _RaisingImgContainer(self._remove, self.status)


def test_reconcile_treats_image_not_found_as_stale_and_recreates_idle(tmp_path, isolated_state):
    """워커 이미지 inspect가 ImageNotFound(404)여도 stale로 판정 → (유휴)재생성(수정 3).

    예외로 죽어 재생성이 스킵되던 버그를 방지: inspect 실패=확실한 stale로 취급.
    """
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerImageNotFound(container_img_id="gone", current_img_id="img-new")
    sp = Spawner(_cfg(base), reg, client=fake)

    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary["recreated"] == ["testuser"]   # 404를 stale로 보고 재생성
    assert summary["errors"] == []                # 예외로 죽지 않는다
    assert fake.removed == [True]                 # 제거 후
    assert len(fake.run_calls) == 1               # 재spawn
    assert "testuser" not in sp._pending_recreate


def test_reconcile_defers_image_not_found_when_active(tmp_path, isolated_state):
    """ImageNotFound(stale) + 활성 잡 → 즉시 죽이지 않고 드레인(in-flight 보호)."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerImageNotFound(container_img_id="gone", current_img_id="img-new")
    sp = Spawner(_cfg(base), reg, client=fake)

    summary = sp.reconcile_workers([u], has_active_job=lambda name: True)
    assert summary["deferred"] == ["testuser"]
    assert fake.run_calls == [] and fake.removed == []
    assert "testuser" in sp._pending_recreate


def test_is_image_not_found_matches_name_and_status():
    """_is_image_not_found: 클래스 이름(ImageNotFound/NotFound) 또는 404 status를 포용."""
    from app.spawner import _is_image_not_found

    class ImageNotFound(Exception):
        pass

    class NotFound(Exception):
        pass

    class ApiErr(Exception):
        status_code = 404

    assert _is_image_not_found(_ImageNotFound()) is True   # status_code=404
    assert _is_image_not_found(ImageNotFound()) is True     # 이름 매칭
    assert _is_image_not_found(NotFound()) is True          # 이름 매칭
    assert _is_image_not_found(ApiErr()) is True            # status_code 매칭
    assert _is_image_not_found(RuntimeError("boom")) is False


def test_reconcile_per_user_error_isolated(tmp_path, isolated_state):
    """한 사용자 reconcile 실패가 다른 사용자를 막지 않는다(격리)."""
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    fake = FakeDockerForReconcile(container_img_id="img-old", current_img_id="img-new")
    sp = Spawner(_cfg(base), reg, client=fake)

    # 첫 사용자는 recreate 중 예외를 던지게 해 격리를 확인, 둘째는 정상 재생성.
    bad = SimpleNamespace(username="baduser")

    def has_active(_name):
        return False

    orig_recreate = sp._recreate_worker

    def flaky_recreate(user):
        if getattr(user, "username", "") == "baduser":
            raise RuntimeError("boom")
        return orig_recreate(user)

    sp._recreate_worker = flaky_recreate
    summary = sp.reconcile_workers([bad, u], has_active_job=has_active)
    assert "baduser" in summary["errors"]
    assert "testuser" in summary["recreated"]
