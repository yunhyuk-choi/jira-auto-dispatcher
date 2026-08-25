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
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://your-org.atlassian.net/
  project: PROJ
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { worker_max_concurrency: 32 }
"""))
    assert cfg.role == "central"
    assert cfg.jira.base_url == "https://your-org.atlassian.net"  # trailing / 제거
    assert cfg.jira.project == "PROJ"
    assert cfg.secrets.base_dir == "/tmp/jad-secrets"
    assert cfg.run.worker_max_concurrency == 32  # 명시값 존중(안전 상한, 정책 cap 아님)


def test_admission_defaults(tmp_path, monkeypatch):
    # 자원 기반 어드미션 기본값(잡 수 cap을 대체). 미설정이면 문서화된 기본을 주입한다.
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x.atlassian.net/
  project: PROJ
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""))
    assert cfg.admission.min_free_mem_mb == 1536
    assert cfg.admission.per_job_mem_reserve_mb == 1024
    assert cfg.admission.max_load_per_core == 0.9
    assert cfg.run.worker_max_concurrency == 64   # 안전 상한 기본


def test_admission_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x.atlassian.net/
  project: PROJ
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
admission: { min_free_mem_mb: 2048, per_job_mem_reserve_mb: 1536, max_load_per_core: 0.75 }
"""))
    assert cfg.admission.min_free_mem_mb == 2048
    assert cfg.admission.per_job_mem_reserve_mb == 1536
    assert cfg.admission.max_load_per_core == 0.75


def test_env_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.setenv("WORKER_SHARED_SECRET", "s3cr3t")
    monkeypatch.setenv("CENTRAL_URL", "http://central:9999")
    monkeypatch.setenv("ROLE", "central")
    cfg = C.load_config(_write(tmp_path, """
role: worker
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.role == "central"          # env ROLE 우선
    assert cfg.worker_shared_secret == "s3cr3t"
    assert cfg.spawn.central_url == "http://central:9999"
    assert cfg.secrets.base_dir == "/run/secrets"


def test_tier2_pilot_user_defaults_off(tmp_path, monkeypatch):
    # Phase 3b-2: 기본 OFF — 미설정이면 빈 문자열(전체 Tier-2 경로 비활성).
    monkeypatch.delenv("TIER2_PILOT_USER", raising=False)
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""))
    assert cfg.run.tier2_pilot_user == ""


def test_tier2_pilot_user_yaml_and_env_override(tmp_path, monkeypatch):
    # YAML 값 존중 + env TIER2_PILOT_USER 우선.
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    monkeypatch.delenv("TIER2_PILOT_USER", raising=False)
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { tier2_pilot_user: alice }
"""))
    assert cfg.run.tier2_pilot_user == "alice"

    monkeypatch.setenv("TIER2_PILOT_USER", "bob")
    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { tier2_pilot_user: alice }
"""))
    assert cfg2.run.tier2_pilot_user == "bob"   # env 우선


def test_fractal_worker_defaults_off(tmp_path, monkeypatch):
    # 프랙탈 P1 신경로: 기본 OFF — 미설정이면 False(워커는 오늘 per-ticket 경로).
    monkeypatch.delenv("FRACTAL_WORKER", raising=False)
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""))
    assert cfg.run.fractal_worker is False


def test_fractal_worker_yaml_and_env_override(tmp_path, monkeypatch):
    # YAML 값 존중 + env FRACTAL_WORKER 우선(truthy/falsy).
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    monkeypatch.delenv("FRACTAL_WORKER", raising=False)
    body = """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { fractal_worker: true }
"""
    cfg = C.load_config(_write(tmp_path, body))
    assert cfg.run.fractal_worker is True

    monkeypatch.setenv("FRACTAL_WORKER", "false")
    cfg2 = C.load_config(_write(tmp_path, body))
    assert cfg2.run.fractal_worker is False   # env 우선(끌 수도 있음)


def test_fractal_central_default_on_config_only(tmp_path, monkeypatch):
    # 작업 A: fractal_central 이 **기본 ON 으로 승격**됨 — config.yaml 에 키가 없어도(그리고
    # env override 없이) config 만으로 fractal 신경로가 켜진다. override 없이 배포만으로 ON.
    monkeypatch.delenv("FRACTAL_CENTRAL", raising=False)
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""))
    # 키 부재 → 기본 True(승격). 지속 세션·양방향 stream-json 기본이 충족되므로 게이트도 True.
    assert cfg.run.fractal_central is True
    assert cfg.run.persistent_session is True
    assert cfg.run.output_format == "stream-json"
    assert cfg.run.input_format == "stream-json"

    from app.central_session import central_fractal_enabled
    assert central_fractal_enabled(cfg) is True   # config.yaml 만으로 게이트 ON


def test_fractal_central_env_override_still_off(tmp_path, monkeypatch):
    # 승격 후에도 env FRACTAL_CENTRAL 명시가 여전히 우선 — falsy 로 끌 수 있다.
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    body = """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""
    monkeypatch.setenv("FRACTAL_CENTRAL", "false")
    cfg = C.load_config(_write(tmp_path, body))
    assert cfg.run.fractal_central is False   # env 우선(명시 falsy → OFF)

    from app.central_session import central_fractal_enabled
    assert central_fractal_enabled(cfg) is False

    # 명시 yaml false 도 존중(env 없을 때).
    monkeypatch.delenv("FRACTAL_CENTRAL", raising=False)
    cfg2 = C.load_config(_write(tmp_path, body.replace("run: {}", "run: { fractal_central: false }")))
    assert cfg2.run.fractal_central is False


def test_missing_required_key_fails(tmp_path):
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(_write(tmp_path, """
role: central
jira: { project: PROJ, watcher_token_file: t }
secrets: { base_dir: /tmp }
"""))
    assert "jira.base_url" in str(exc.value)


def test_unresolved_token_fails(tmp_path, monkeypatch):
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert "SECRETS_DIR" in str(exc.value) or "미치환" in str(exc.value)


def test_host_deploy_dir_env_override_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.setenv("HOST_DEPLOY_DIR", "/home/<deploy-user>/deploy/jad")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { host_deploy_dir: "/from/yaml" }
"""))
    # env HOST_DEPLOY_DIR 폴백 우선 — yaml 값을 덮는다.
    assert cfg.spawn.host_deploy_dir == "/home/<deploy-user>/deploy/jad"


def test_host_deploy_dir_unresolved_token_becomes_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.delenv("HOST_DEPLOY_DIR", raising=False)
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { host_deploy_dir: "${HOST_DEPLOY_DIR}" }
"""))
    # env 미설정으로 토큰 미치환 → 빈 값(폴백). broken bind 방지.
    assert cfg.spawn.host_deploy_dir == ""


def test_repo_resolution_defaults_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # 기본: repo_resolution=llm, repo_map_path 빈값(→ 런타임에 workspace 파생).
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.run.repo_resolution == "llm"
    assert cfg.run.repo_map_path == ""
    assert cfg.run.repo_resolver_gitlab_token_ref == ""
    assert cfg.run.repo_resolver_timeout_sec == 60

    # 명시 오버라이드.
    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
run:
  repo_resolution: static
  repo_map_path: /app/workspace/dlc-meta
  repo_resolver_gitlab_token_ref: service/gitlab-central
  repo_resolver_timeout_sec: 90
"""))
    assert cfg2.run.repo_resolution == "static"
    assert cfg2.run.repo_map_path == "/app/workspace/dlc-meta"
    assert cfg2.run.repo_resolver_gitlab_token_ref == "service/gitlab-central"
    assert cfg2.run.repo_resolver_timeout_sec == 90


def test_workspace_paths_derived_when_unset(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # workspace_dir만 주고 3개 레포 경로는 비워둔다 → 공유 워크스페이스 하위로 파생.
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
run:
  workspace_dir: /app/workspace
"""))
    assert cfg.run.orchestrator_repo == "/app/workspace/orchestrator"
    assert cfg.run.dlc_meta_repo == "/app/workspace/dlc-meta"
    assert cfg.run.docs_repo == "/app/workspace/docs"
    # 레거시 속성 미러도 같은 값을 갖는다(옛 리더 하위호환).
    assert cfg.run.dataspace_docs_repo == "/app/workspace/docs"


def test_workspace_paths_explicit_respected(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # 명시값이 있으면 파생하지 않고 존중한다(orchestrator만 명시 → 나머지는 파생).
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
run:
  workspace_dir: /app/workspace
  orchestrator_repo: /custom/orch
"""))
    assert cfg.run.orchestrator_repo == "/custom/orch"
    assert cfg.run.dlc_meta_repo == "/app/workspace/dlc-meta"


def test_workspace_paths_not_derived_without_workspace_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # workspace_dir 미설정이면 파생하지 않는다(빈 값 유지 → 프로비저닝 skip).
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.run.orchestrator_repo == ""
    assert cfg.run.dlc_meta_repo == ""
    assert cfg.run.docs_repo == ""
    assert cfg.run.dataspace_docs_repo == ""


def test_workspace_volume_default_and_override(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.spawn.workspace_volume == "jad-workspace"
    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { workspace_volume: shared-ws }
"""))
    assert cfg2.spawn.workspace_volume == "shared-ws"


def test_match_cancel_and_optout_defaults(tmp_path, monkeypatch):
    """⑥ match.cancel_statuses/optout_labels 키 부재 → 문서화된 기본값 주입."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.match.statuses == ["해야 할 일"]
    assert cfg.match.cancel_statuses == ["취소됨"]
    assert cfg.match.optout_labels == ["자동화_추적_해제"]


def test_match_cancel_and_optout_override(tmp_path, monkeypatch):
    """⑥ 명시값이 있으면 존중하고, 명시 빈 리스트는 기능 비활성으로 존중한다."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
match:
  statuses: ["해야 할 일"]
  cancel_statuses: ["취소됨", "폐기됨"]
  optout_labels: ["no_auto"]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.match.cancel_statuses == ["취소됨", "폐기됨"]
    assert cfg.match.optout_labels == ["no_auto"]

    # 명시 빈 리스트([]) → 기능 비활성(존중).
    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"], optout_labels: [] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg2.match.optout_labels == []
    assert cfg2.match.cancel_statuses == ["취소됨"]   # 부재 → 기본 유지


def test_read_secret(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "token").write_text("abc123\n", encoding="utf-8")
    assert C.read_secret(str(tmp_path), "sub/token") == "abc123"
    assert C.read_secret(str(tmp_path), "sub/missing") is None
