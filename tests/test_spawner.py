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


def _cfg(base_dir: str, notify=None, config_path: str = ""):
    ns = SimpleNamespace(
        spawn=SimpleNamespace(
            image="jira-auto-dispatcher:latest",
            network="jad-net",
            central_url="http://central:8787",
            mem_limit="4g",
            docker_host="unix:///var/run/docker.sock",
            run_as="1000:1000",
            workspace_volume="jad-workspace",
        ),
        run=SimpleNamespace(workspace_dir="/app/workspace"),
        secrets=SimpleNamespace(base_dir=base_dir),
        worker_shared_secret="s3cr3t",
        # central 이 로드한 config.yaml 원문 위치(워커 주입 소스). 비면 기본 경로.
        config_path=config_path,
    )
    if notify is not None:
        ns.notify = notify
    return ns


def _cfg_with_config(tmp_path, base_dir: str, notify=None, text: str = "role: central\n"):
    """config 원문 파일까지 갖춘 설정(주입 페이로드 검증용)."""
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8", newline="\n")
    return _cfg(base_dir, notify=notify, config_path=str(path))


def _injected_secrets(env: dict) -> dict:
    """worker env 의 주입 페이로드를 풀어 ``{ref: 내용}`` 으로."""
    from app import inject

    return inject.decode_secrets(env[inject.ENV_SECRETS])


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
    # 사전 인가 settings.json은 파일 바인드하지 않는다(명명 볼륨 하위 파일 경로에
    # 바인드하면 runc가 거부). 주입 → worker 부팅 시 복사로 대체.
    settings_binds = [v for v in vols.values() if v["bind"] == SETTINGS_PATH_IN_CONTAINER]
    assert settings_binds == []
    # per-user 시크릿은 더 이상 마운트가 아니라 **주입**이다(호스트 경로 개념 제거).
    assert not any(v["bind"] == "/run/secrets/testuser" for v in vols.values())
    assert "testuser/claude-settings.json" in _injected_secrets(env)
    # 시크릿이 사는 곳은 uid 소유 tmpfs(RAM 전용).
    assert kwargs["tmpfs"] == {"/run/secrets":
                               "rw,noexec,nosuid,nodev,size=8m,mode=0700,uid=1000,gid=1000"}

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


def test_ensure_worker_injects_bypass_settings_instead_of_host_file(tmp_path, isolated_state):
    """사전 인가 settings.json 은 **호스트에 쓰지 않고** 주입 페이로드로 넘어간다.

    호스트 파일로 스테이징하던 시절엔 그 경로를 워커 바인드 source 로 쓰려고
    host_deploy_dir 이 필요했다. 이제는 매 spawn 마다 렌더해 주입하므로 디스크에
    남는 것도, 호스트 경로도 없다.
    """
    base = str(tmp_path / "secrets")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    client = _client_absent()
    Spawner(_cfg(base), reg, client=client).ensure_worker(u)

    # central 디스크에는 쓰지 않는다.
    assert not os.path.exists(os.path.join(base, "testuser", "claude-settings.json"))
    env = client.containers.run.call_args.kwargs["environment"]
    data = json.loads(_injected_secrets(env)["testuser/claude-settings.json"])
    assert data["permissions"]["defaultMode"] == "bypassPermissions"


def test_jira_gitlab_token_pointers_stay_paths_not_plain_env_values(tmp_path, isolated_state):
    """토큰 **포인터**(*_FILE/*_REF)에는 값이 실리지 않는다 — 값은 주입 채널에만.

    ⚠️ 계약 변화(정직한 기술): 예전엔 Jira/forge 토큰 값이 컨테이너 스펙에 아예 실리지
    않았다(호스트 디렉토리를 ro 바인드했으므로). bind 를 없애면서 값은 전용 주입 env
    (JAD_INJECT_SECRETS, base64)로 이동했다. 노출 수준은 이미 전부터 env 로 넘기던
    CLAUDE_CODE_OAUTH_TOKEN·WORKER_SHARED_SECRET 과 같다(docker API 접근자 = 호스트
    root 동치라 어차피 호스트 secrets/ 를 읽을 수 있다). 여기서 지키는 것은
    **평문이 일반 env 키로 새지 않는다**는 것과 아래 격리 불변식이다.
    """
    from app import inject

    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "JIRA-SECRET-VAL")
    _write(base, "testuser/gitlab-token", "GL-SECRET-VAL")
    _write(base, "testuser/claude-oauth-token", "CLAUDE-XYZ")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    env = sp.build_env(_user())

    # 주입 채널을 뺀 어떤 env 값에도 Jira/forge 토큰 평문이 없다.
    joined = "\n".join(str(v) for k, v in env.items() if k != inject.ENV_SECRETS)
    assert "JIRA-SECRET-VAL" not in joined
    assert "GL-SECRET-VAL" not in joined
    # 주입 채널 자체도 평문이 아니라 base64 페이로드다.
    assert "JIRA-SECRET-VAL" not in env[inject.ENV_SECRETS]
    # 그리고 그 안에서 값이 정확히 왕복한다(워커가 파일로 되살릴 내용).
    files = _injected_secrets(env)
    assert files["testuser/jira-token"] == "JIRA-SECRET-VAL"
    assert files["testuser/gitlab-token"] == "GL-SECRET-VAL"
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


# --- build_volumes / 주입: bind 마운트 없음(host_deploy_dir 개념 제거) ---


def test_build_volumes_has_no_bind_mounts_at_all(tmp_path, isolated_state):
    """⚠️ **회귀 방지 핵심**: 워커 볼륨은 named 볼륨 2개뿐이고 bind 가 하나도 없다.

    bind 가 하나라도 생기면 그 source 를 호스트 docker 데몬이 해석하므로(sibling
    container) 다시 "호스트 배포 절대경로"라는 설치 항목이 필요해지고, 틀리면 워커가
    **에러 없이 뜬 채** 빈 디렉토리를 마운트하는 조용한 실패로 돌아간다. 그래서 여기서
    "bind source 로 보이는 경로가 없다"를 기계적으로 못 박는다.
    """
    base = str(tmp_path / "secrets")
    vols = Spawner(_cfg(base), Registry(), client=_client_absent()).build_volumes(_user())
    assert set(vols) == {"jad-testuser", "jad-workspace"}
    for src in vols:
        assert "/" not in src and os.sep not in src, f"bind 마운트 발견: {src}"
    assert vols["jad-testuser"] == {"bind": "/home/app/.claude", "mode": "rw"}
    assert vols["jad-workspace"] == {"bind": "/app/workspace", "mode": "rw"}


def test_build_volumes_no_settings_file_bind(tmp_path, isolated_state):
    """사전 인가 settings.json 을 명명 볼륨 하위 파일 경로에 바인드하면 runc 가 거부한다."""
    base = str(tmp_path / "secrets")
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    vols = sp.build_volumes(_user())
    assert all(v["bind"] != SETTINGS_PATH_IN_CONTAINER for v in vols.values())
    assert not any(str(src).endswith("claude-settings.json") for src in vols)


def test_build_volumes_mounts_shared_workspace(tmp_path, isolated_state):
    """공유 워크스페이스 named 볼륨을 run.workspace_dir 에 rw로 마운트한다(설계 §4)."""
    base = str(tmp_path / "secrets")
    vols = Spawner(_cfg(base), Registry(), client=_client_absent()).build_volumes(_user())
    assert vols["jad-workspace"] == {"bind": "/app/workspace", "mode": "rw"}


def test_build_volumes_workspace_volume_name_configurable(tmp_path, isolated_state):
    """볼륨명·bind 경로가 config에서 온다(기본 폴백은 DEFAULT_WORKSPACE_VOLUME)."""
    base = str(tmp_path / "secrets")
    cfg = _cfg(base)
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
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    assert sp.build_volumes(_user()) == sp.build_volumes(_user(), "ignored/path")


# --- 주입: config 원문 ---


def test_injects_config_text_verbatim(tmp_path, isolated_state):
    """worker 는 /app/config/config.yaml 을 읽는다 — 그 원문을 그대로 주입한다.

    해석된 설정을 다시 직렬화하지 않는 이유: ``${SECRETS_DIR}`` 같은 토큰은 워커가
    자기 env 로 치환해야 bind 마운트 시절과 같은 의미가 된다.
    """
    from app import inject

    base = str(tmp_path / "secrets")
    text = "role: central\nspawn: { image: x }\n"
    cfg = _cfg_with_config(tmp_path, base, text=text)
    env = Spawner(cfg, Registry(), client=_client_absent()).build_env(_user())
    assert inject.decode_config(env[inject.ENV_CONFIG]) == text


def test_missing_config_file_warns_and_omits_payload(tmp_path, isolated_state, caplog):
    """config 원문을 못 읽으면 **경고를 남기고** 주입 키를 생략한다(조용한 빈 값 금지)."""
    import logging

    from app import inject

    base = str(tmp_path / "secrets")
    cfg = _cfg(base, config_path=str(tmp_path / "does-not-exist.yaml"))
    with caplog.at_level(logging.WARNING, logger="jad.spawner"):
        env = Spawner(cfg, Registry(), client=_client_absent()).build_env(_user())
    assert inject.ENV_CONFIG not in env
    assert any("config" in r.getMessage() for r in caplog.records)


# --- 주입: per-user 격리(이 리포에서 가장 중요한 불변식) ---


def test_injection_contains_only_this_users_secrets(tmp_path, isolated_state):
    """⚠️ **격리**: 워커 A 의 주입 페이로드에 워커 B 의 시크릿이 값도 경로도 없다."""
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "MINE")
    _write(base, "other/jira-token", "THEIRS-DO-NOT-LEAK")
    _write(base, "service/jira-token", "CENTRAL-WATCHER-TOKEN")

    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    env = sp.build_env(_user())
    files = _injected_secrets(env)

    assert all(ref.startswith("testuser/") for ref in files), files.keys()
    blob = "\n".join(files.values())
    assert "THEIRS-DO-NOT-LEAK" not in blob
    assert "CENTRAL-WATCHER-TOKEN" not in blob
    # 페이로드 전체(base64 이전/이후) 어디에도 남의 사용자 이름 경로가 없다.
    assert "other/jira-token" not in str(files)


def test_injection_drops_refs_outside_this_user(tmp_path, isolated_state, caplog):
    """레지스트리가 남의 경로를 가리켜도 그 값은 실리지 않는다(경고 + 제외)."""
    import logging

    base = str(tmp_path / "secrets")
    _write(base, "other/jira-token", "THEIRS-DO-NOT-LEAK")
    user = UserRecord(
        username="testuser",
        jira_account_id="acc",
        jira_email="yh@x",
        secrets_ref=SecretsRef(jira_token="other/jira-token"),
    )
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    with caplog.at_level(logging.WARNING, logger="jad.spawner"):
        files = sp.collect_injected_secrets(user)
    assert "other/jira-token" not in files
    assert "THEIRS-DO-NOT-LEAK" not in "\n".join(files.values())
    assert any("이 사용자" in r.getMessage() for r in caplog.records)


def test_injection_rejects_path_traversal_ref(tmp_path, isolated_state):
    """``..`` 로 시크릿 루트를 벗어나려는 참조는 담지 않는다(심층 방어)."""
    base = str(tmp_path / "secrets")
    user = UserRecord(
        username="testuser",
        jira_account_id="acc",
        jira_email="yh@x",
        secrets_ref=SecretsRef(jira_token="testuser/../service/jira-token"),
    )
    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    files = sp.collect_injected_secrets(user)
    assert all(".." not in ref for ref in files)


def test_two_users_get_disjoint_payloads(tmp_path, isolated_state):
    """두 사용자의 주입 페이로드는 (공용 웹훅을 빼면) 교집합이 없다."""
    base = str(tmp_path / "secrets")
    _write(base, "alice/jira-token", "ALICE-TOKEN")
    _write(base, "bob/jira-token", "BOB-TOKEN")

    def _u(name):
        return UserRecord(username=name, jira_account_id="a", jira_email="e",
                          secrets_ref=SecretsRef(jira_token=f"{name}/jira-token"))

    sp = Spawner(_cfg(base), Registry(), client=_client_absent())
    a = sp.collect_injected_secrets(_u("alice"))
    b = sp.collect_injected_secrets(_u("bob"))
    assert set(a) & set(b) == set()
    assert "BOB-TOKEN" not in "\n".join(a.values())
    assert "ALICE-TOKEN" not in "\n".join(b.values())


# --- 주입: 완료 알림 웹훅(최소권한 — 파일 하나만) ---


def test_injects_webhook_file_when_notify_configured(tmp_path, isolated_state):
    """notify(enabled+webhook_ref) 면 웹훅 **파일 하나**를 주입한다.

    예전엔 host_deploy_dir 이 있을 때만 바인드해서, 없으면 알림이 조용히 스킵됐다.
    주입에는 그 조건이 없다 — 설정만 돼 있으면 항상 간다.
    """
    base = str(tmp_path / "secrets")
    _write(base, "service/google-chat-webhook", "https://chat.example/hook")
    _write(base, "service/jira-token", "CENTRAL-WATCHER-TOKEN")
    cfg = _cfg(base, notify=_notify())
    env = Spawner(cfg, Registry(), client=_client_absent()).build_env(_user())
    files = _injected_secrets(env)
    assert files["service/google-chat-webhook"] == "https://chat.example/hook"
    # ⚠️ 최소권한: service/ 의 다른 파일(central watcher Jira 토큰)은 절대 안 간다.
    assert "service/jira-token" not in files
    assert "CENTRAL-WATCHER-TOKEN" not in "\n".join(files.values())


def test_no_webhook_when_notify_disabled(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    _write(base, "service/google-chat-webhook", "https://chat.example/hook")
    cfg = _cfg(base, notify=_notify(enabled=False))
    files = _injected_secrets(
        Spawner(cfg, Registry(), client=_client_absent()).build_env(_user()))
    assert not any("google-chat-webhook" in ref for ref in files)


def test_no_webhook_when_ref_empty(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    cfg = _cfg(base, notify=_notify(webhook_ref=""))
    files = _injected_secrets(
        Spawner(cfg, Registry(), client=_client_absent()).build_env(_user()))
    assert not any("google-chat-webhook" in ref for ref in files)


# --- 시크릿 tmpfs(비-root 소유 — run_as 파생) ---


def test_secrets_tmpfs_uid_derives_from_run_as(tmp_path, isolated_state):
    """tmpfs 소유 uid 는 ``spawn.run_as`` 에서 파생된다 — 둘이 어긋날 수 없다.

    주입 파일을 **쓰는 주체와 읽는 주체가 같은 uid** 여야 워커가 조용히 시크릿을
    못 읽는 사고가 안 난다.
    """
    base = str(tmp_path / "secrets")
    cfg = _cfg(base)
    cfg.spawn.run_as = "1001:1002"
    spec = Spawner(cfg, Registry(), client=_client_absent()).build_spec(_user())
    assert spec["tmpfs"]["/run/secrets"].endswith("mode=0700,uid=1001,gid=1002")
    assert spec["user"] == "1001:1002"


def test_secrets_tmpfs_falls_back_when_run_as_not_numeric(tmp_path, isolated_state):
    """이름 형식 run_as 는 uid 로 못 옮기므로 mode=0777 로 둔다(컨테이너 전용 tmpfs)."""
    base = str(tmp_path / "secrets")
    cfg = _cfg(base)
    cfg.spawn.run_as = "app"
    spec = Spawner(cfg, Registry(), client=_client_absent()).build_spec(_user())
    opts = spec["tmpfs"]["/run/secrets"]
    assert "mode=0777" in opts and "uid=" not in opts
    # 파일 자체는 여전히 0600 으로 기록된다(app/inject.py).
    from app import inject

    assert inject.SECRET_FILE_MODE == 0o600


def test_secrets_tmpfs_is_hardened(tmp_path, isolated_state):
    base = str(tmp_path / "secrets")
    opts = Spawner(_cfg(base), Registry(), client=_client_absent()).build_spec(_user())["tmpfs"]
    assert opts["/run/secrets"].startswith("rw,noexec,nosuid,nodev,size=")


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


# --- 주입 페이로드 드리프트 → stale 판정(bind 시절의 "파일이라 자동 반영"을 대체) ---


class FakeDockerWithEnv(FakeDockerForReconcile):
    """컨테이너에 **구워진 env** 까지 흉내내는 대역(주입 드리프트 판정용)."""

    def __init__(self, baked_env: dict, **kw):
        super().__init__(**kw)
        self.baked_env = baked_env

    def _get(self, name):
        c = super()._get(name)
        c.attrs = {"Config": {"Env": [f"{k}={v}" for k, v in self.baked_env.items()]}}
        return c


def test_reconcile_recreates_when_injected_payload_changed(tmp_path, isolated_state):
    """이미지는 그대로인데 config·시크릿이 바뀌었으면 stale 로 보고 재생성한다.

    bind 마운트 시절엔 파일이라 워커가 알아서 최신을 읽었다. 주입은 컨테이너 생성
    시점에 고정되므로, 이 판정이 없으면 워커가 **옛 자격증명으로 조용히** 돈다.
    """
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "OLD")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    sp_old = Spawner(_cfg(base), reg, client=_client_absent())
    baked = sp_old.build_injection(u)

    _write(base, "testuser/jira-token", "ROTATED")  # 토큰 교체
    fake = FakeDockerWithEnv(baked, container_img_id="img-1", current_img_id="img-1")
    sp = Spawner(_cfg(base), reg, client=fake)

    summary = sp.reconcile_workers([u], has_active_job=lambda name: False)
    assert summary["recreated"] == ["testuser"]
    assert fake.removed == [True]


def test_reconcile_skips_when_injected_payload_unchanged(tmp_path, isolated_state):
    """페이로드가 그대로면 no-op — 매 부팅마다 워커를 흔들지 않는다."""
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "SAME")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    baked = Spawner(_cfg(base), reg, client=_client_absent()).build_injection(u)
    fake = FakeDockerWithEnv(baked, container_img_id="img-1", current_img_id="img-1")

    summary = Spawner(_cfg(base), reg, client=fake).reconcile_workers(
        [u], has_active_job=lambda name: False)
    assert summary["skipped"] == ["testuser"]
    assert fake.removed == []


def test_reconcile_defers_payload_drift_while_job_active(tmp_path, isolated_state):
    """페이로드 드리프트도 이미지 stale 과 같은 드레인 규율을 따른다(in-flight 잡 보호)."""
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "OLD")
    reg = Registry()
    u = _user()
    reg.upsert(u)
    baked = Spawner(_cfg(base), reg, client=_client_absent()).build_injection(u)
    _write(base, "testuser/jira-token", "ROTATED")
    fake = FakeDockerWithEnv(baked, container_img_id="img-1", current_img_id="img-1")
    sp = Spawner(_cfg(base), reg, client=fake)

    summary = sp.reconcile_workers([u], has_active_job=lambda name: True)
    assert summary["deferred"] == ["testuser"]
    assert fake.removed == []
    assert "testuser" in sp._pending_recreate
