"""config 로드/검증/오버라이드 단위테스트."""

from __future__ import annotations

import pytest

from app import config as C


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_missing_file_raises(tmp_path):
    with pytest.raises(C.ConfigError):
        C.load_config(str(tmp_path / "nope.yaml"))


def test_load_and_env_substitution(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "C:/temp")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://interx-jira.atlassian.net/
  project: HAN
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { concurrency_per_worker: 1 }
"""))
    assert cfg.role == "central"
    assert cfg.jira.base_url == "https://interx-jira.atlassian.net"  # trailing / 제거
    assert cfg.jira.project == "HAN"
    assert cfg.secrets.base_dir == "C:/temp"
    assert cfg.run.global_concurrency == 3  # 기본값 주입


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.setenv("WORKER_SHARED_SECRET", "s3cr3t")
    monkeypatch.setenv("CENTRAL_URL", "http://central:9999")
    monkeypatch.setenv("ROLE", "central")
    cfg = C.load_config(_write(tmp_path, """
role: worker
jira: { base_url: https://x, project: HAN, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.role == "central"          # env ROLE 우선
    assert cfg.worker_shared_secret == "s3cr3t"
    assert cfg.spawn.central_url == "http://central:9999"
    assert cfg.secrets.base_dir == "/run/secrets"


def test_missing_required_key_fails(tmp_path):
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(_write(tmp_path, """
role: central
jira: { project: HAN, watcher_token_file: t }
secrets: { base_dir: /tmp }
"""))
    assert "jira.base_url" in str(exc.value)


def test_unresolved_token_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: HAN, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert "SECRETS_DIR" in str(exc.value) or "미치환" in str(exc.value)


def test_host_deploy_dir_env_override_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.setenv("HOST_DEPLOY_DIR", "/home/yhchoi/deploy/jad")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: HAN, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { host_deploy_dir: "/from/yaml" }
"""))
    # env HOST_DEPLOY_DIR 폴백 우선 — yaml 값을 덮는다.
    assert cfg.spawn.host_deploy_dir == "/home/yhchoi/deploy/jad"


def test_host_deploy_dir_unresolved_token_becomes_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.delenv("HOST_DEPLOY_DIR", raising=False)
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: HAN, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { host_deploy_dir: "${HOST_DEPLOY_DIR}" }
"""))
    # env 미설정으로 토큰 미치환 → 빈 값(폴백). broken bind 방지.
    assert cfg.spawn.host_deploy_dir == ""


def test_read_secret(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "token").write_text("abc123\n", encoding="utf-8")
    assert C.read_secret(str(tmp_path), "sub/token") == "abc123"
    assert C.read_secret(str(tmp_path), "sub/missing") is None
