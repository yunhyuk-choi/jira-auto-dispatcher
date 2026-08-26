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


def test_fractal_central_off_is_ignored_with_a_loud_warning(tmp_path, monkeypatch, caplog):
    """⚠️ fractal-OFF 은퇴: 명시 false(yaml·env)는 **무시**되고 경고만 남는다.

    예전에 OFF 는 "스케줄러 큐 → 워커 HTTP 폴링(구 경로)로 돈다"는 뜻이었지만, 그 폴링
    소비자(app/worker.py)가 프랙탈 경로와 이중 실행(같은 티켓 두 번 → 중복 브랜치·변경
    요청·완료알림)을 일으켜 제거됐다. 소비자가 없는 지금 OFF 는 **아무것도 실행되지
    않음**을 뜻하므로, 옛 설정을 그대로 존중하면 조용한 무실행이 된다.

    부팅 거부 대신 **무시 + 경고**를 택했다 — 이 키는 남의 배포에 이미 적혀 있을 수 있는
    옛 키라 부팅을 막으면 업그레이드가 곧 장애가 되고(설정을 고칠 관리 UI 조차 안 뜬다),
    OFF 로 얻을 동작이 더 이상 존재하지 않아 무시가 의도를 왜곡하지도 않는다.
    """
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    body = """
role: central
jira: { base_url: https://x.atlassian.net/, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
run: {}
"""
    # (1) env 로 끄려 해도 무시된다.
    monkeypatch.setenv("FRACTAL_CENTRAL", "false")
    with caplog.at_level("ERROR", logger="jad.config"):
        cfg = C.load_config(_write(tmp_path, body))
    assert cfg.run.fractal_central is True
    assert "무시" in caplog.text          # 조용히 뒤집지 않는다 — 추적 가능해야 한다

    from app.central_session import central_fractal_enabled
    assert central_fractal_enabled(cfg) is True

    # (2) yaml 로 끄려 해도 무시된다(옛 config.yaml 하위호환).
    monkeypatch.delenv("FRACTAL_CENTRAL", raising=False)
    cfg2 = C.load_config(
        _write(tmp_path, body.replace("run: {}", "run: { fractal_central: false }")))
    assert cfg2.run.fractal_central is True


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


def test_legacy_host_deploy_dir_key_is_ignored_harmlessly(tmp_path, monkeypatch):
    """제거된 host_deploy_dir 이 기존 config.yaml/env 에 남아 있어도 무해하다.

    워커 마운트가 전부 named 볼륨이 되어(나머지는 스폰 시 주입) 호스트 경로가 필요
    없어졌으므로 이 키는 사라졌다. 기존 배포를 깨지 않도록 **조용히 무시**한다
    (설치 검증 `python -m app.setup validate` 는 "선언되지 않은 항목" 경고로 알려 준다).
    """
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    monkeypatch.setenv("HOST_DEPLOY_DIR", "/home/deploy/jad")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
spawn: { host_deploy_dir: "/from/yaml" }
deploy: { host_deploy_dir: "/also/from/yaml" }
"""))
    assert not hasattr(cfg.spawn, "host_deploy_dir")
    assert not hasattr(cfg.deploy, "host_deploy_dir")
    # 나머지 값은 정상 로드된다(부팅이 깨지지 않는다).
    assert cfg.secrets.base_dir == "/run/secrets"


def test_load_config_records_source_path_for_worker_injection(tmp_path, monkeypatch):
    """spawner 가 워커에 주입할 **config 원문** 위치를 설정 자신이 들고 있다."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    path = _write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
""")
    assert C.load_config(path).config_path == path


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


# ---------------------------------------------------------------------------
# 상태·전이의 {id, name} — **id 와 name 을 함께** 보존하되 소비처 타입은 안 바꾼다
# ---------------------------------------------------------------------------


def test_named_refs_keep_names_for_consumers_and_ids_on_the_side(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  trigger_statuses: [{"id": "10000", "name": "해야 할 일"}, "선택 대기"]
  cancel_statuses: [{"id": "10002", "name": "취소됨"}]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    # 소비처(폴러·워처·웹훅)는 예전 그대로 **이름 목록**을 본다.
    assert cfg.jira.trigger_statuses == ["해야 할 일", "선택 대기"]
    assert cfg.match.statuses == ["해야 할 일", "선택 대기"]     # 레거시 미러도 동일
    assert cfg.jira.cancel_statuses == ["취소됨"]
    # id 는 곁에 남아 진단이 짝을 검증할 수 있다.
    assert cfg.jira.status_ids == {"취소됨": "10002", "해야 할 일": "10000"}


def test_plain_string_statuses_still_work(tmp_path, monkeypatch):
    """하위호환 — 옛 설정은 아무것도 안 바꿔도 오늘과 동일하게 읽힌다."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
match: { statuses: ["해야 할 일"], cancel_statuses: ["취소됨"] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.trigger_statuses == ["해야 할 일"]
    assert cfg.jira.status_ids == {}


def test_done_transition_id_comes_from_the_single_named_ref(tmp_path, monkeypatch):
    """discover 가 적어 준 {id, name} 하나면 전이 id 를 따로 관리하지 않아도 된다."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  done_transition_names: [{"id": "41", "name": "완료"}]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.done_transition_id == "41"
    assert cfg.jira.done_transition_names == ["완료"]
    assert cfg.jira.done_transition_ids == {"완료": "41"}


def test_explicit_done_transition_id_wins_over_the_named_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  done_transition_id: "99"
  done_transition_names: [{"id": "41", "name": "완료"}]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.done_transition_id == "99"


def test_ambiguous_named_transition_ids_are_left_to_name_matching(tmp_path, monkeypatch):
    """id 가 여럿이면 어느 것이 '완료'인지 모른다 — 런타임 이름 매칭에 맡긴다."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  done_transition_names: [{"id": "41", "name": "완료"}, {"id": "42", "name": "Done"}]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.done_transition_id == ""
    assert cfg.jira.done_transition_names == ["완료", "Done"]


def test_named_ref_without_a_name_is_dropped(tmp_path, monkeypatch):
    """이름이 없으면 JQL 에도 미러에도 실을 수 없다 — 조용히 버린다(검증기가 먼저 막는다)."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  trigger_statuses: [{"id": "10000"}, "해야 할 일"]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.trigger_statuses == ["해야 할 일"]


# --- 감시 프로젝트 복수화(하위호환) -------------------------------------------


def test_jira_projects_extends_the_primary_project(tmp_path, monkeypatch):
    """``jira.project``(대표) 는 그대로 문자열, ``jira.projects`` 가 나머지를 더한다."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x.atlassian.net
  project: PROJ
  projects: ["TEAM", "OPS"]
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.project == "PROJ"
    assert cfg.jira.projects == ["TEAM", "OPS"]   # 설정 파일이 말한 그대로(대표 제외)
    from app import scope as SC
    assert SC.instance_projects(cfg) == ["PROJ", "TEAM", "OPS"]   # 합집합은 scope 가 만든다


def test_jira_projects_defaults_to_empty_and_drops_malformed_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x.atlassian.net
  project: PROJ
  projects: ["TEAM", "not a key"]
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.projects == ["TEAM"]          # 모양이 아닌 값은 JQL 에 실리지 않는다


def test_jira_project_given_as_a_list_is_absorbed_not_stringified(tmp_path, monkeypatch):
    """옛 설정이 ``project`` 에 목록을 적었어도 "['A', 'B']" 라는 유령 키를 만들지 않는다."""
    monkeypatch.setenv("SECRETS_DIR", "/tmp/jad-secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x.atlassian.net
  project: ["PROJ", "TEAM"]
  watcher_token_file: service/jira-token
match: { statuses: ["해야 할 일"] }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.project == "PROJ"
    assert cfg.jira.projects == ["TEAM"]
