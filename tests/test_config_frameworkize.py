"""프레임워크화 설정 단위테스트 — 신규 섹션(forge/notifier/deploy/consent) + **하위호환**.

이 파일의 존재 이유는 하나다: 특정 조직에 묶인 값을 설정으로 빼면서 **기존 배포가
깨지지 않는다**는 것을 기계적으로 고정한다. 그래서 회귀 기준선(:data:`_LEGACY_YAML`)은
신규 키를 하나도 모르는 옛 config.yaml 그대로다.

기본 규칙(모듈 docstring in app/config.py):
    명시 신규 키 > 명시 레거시 키 > (deploy 는) 프로파일 파생 > 코드 기본값, 그 위에 env.
"""

from __future__ import annotations

import pytest

from app import config as C


def _write(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text, encoding="utf-8")
    return str(p)


# 신규 키를 하나도 모르는 **기존 배포**의 config.yaml(회귀 기준선).
_LEGACY_YAML = """
role: central
jira:
  base_url: https://x.atlassian.net/
  project: PROJ
  watcher_token_file: service/jira-token
match:
  statuses: ["해야 할 일"]
  cancel_statuses: ["취소됨"]
  optout_labels: ["자동화_추적_해제"]
spawn:
  docker_host: tcp://socket-proxy:2375
  host_deploy_dir: /home/deploy/jad     # 제거된 키 — 남아 있어도 무시된다(하위호환)
  workspace_volume: legacy-ws
secrets: { base_dir: "${SECRETS_DIR}" }
notify:
  enabled: true
  webhook_ref: service/google-chat-webhook
  notify_cancelled: false
run:
  workspace_dir: /app/workspace
  dataspace_docs_repo: /app/workspace/dataspace_docs
  dataspace_docs_repo_url: https://gitlab.example.com/g/dataspace-docs.git
  repo_resolver_gitlab_token_ref: service/gitlab-central
"""


def test_legacy_config_still_loads_identically(tmp_path, monkeypatch):
    """신규 키를 하나도 안 쓴 기존 config.yaml 이 오늘과 동일한 유효값을 만든다."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, _LEGACY_YAML))

    # 레거시 리더가 보던 필드는 그대로다.
    assert cfg.match.statuses == ["해야 할 일"]
    assert cfg.match.cancel_statuses == ["취소됨"]
    assert cfg.match.optout_labels == ["자동화_추적_해제"]
    assert cfg.spawn.docker_host == "tcp://socket-proxy:2375"
    assert cfg.spawn.workspace_volume == "legacy-ws"
    assert cfg.secrets.base_dir == "/run/secrets"
    assert cfg.notify.enabled is True
    assert cfg.notify.webhook_ref == "service/google-chat-webhook"
    assert cfg.notify.notify_cancelled is False
    assert cfg.run.dataspace_docs_repo == "/app/workspace/dataspace_docs"
    assert cfg.run.dataspace_docs_repo_url.endswith("dataspace-docs.git")

    # 그리고 신규 표현이 같은 값으로 채워져 있다(미러).
    assert cfg.jira.trigger_statuses == ["해야 할 일"]
    assert cfg.jira.cancel_statuses == ["취소됨"]
    assert cfg.notifier.provider == "google_chat"   # enabled: true 의 레거시 의미
    assert cfg.notifier.notify_cancelled is False
    assert cfg.deploy.docker_host == "tcp://socket-proxy:2375"
    assert cfg.deploy.workspace_volume == "legacy-ws"
    assert cfg.deploy.secrets_base_dir == "/run/secrets"
    assert cfg.run.docs_repo == "/app/workspace/dataspace_docs"
    assert cfg.run.docs_repo_url.endswith("dataspace-docs.git")
    assert cfg.forge.token_ref == "service/gitlab-central"
    assert cfg.forge.kind == "gitlab"


def test_legacy_notify_disabled_maps_to_provider_none(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
notify: { enabled: false, webhook_ref: service/gchat }
"""))
    assert cfg.notifier.provider == "none"
    assert cfg.notifier.enabled is False
    assert cfg.notify.enabled is False
    assert cfg.notifier.webhook_ref == "service/gchat"   # 참조는 보존


def test_new_keys_win_over_legacy_keys(tmp_path, monkeypatch):
    """신규 키와 레거시 키가 둘 다 있으면 **신규가 이긴다**."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  trigger_statuses: ["To Do"]
  cancel_statuses: ["Cancelled"]
  optout_labels: []
match:
  statuses: ["해야 할 일"]
  cancel_statuses: ["취소됨"]
  optout_labels: ["자동화_추적_해제"]
notify: { enabled: true, webhook_ref: service/old }
notifier: { provider: slack, webhook_ref: service/new }
spawn: { docker_host: tcp://legacy:2375, workspace_volume: legacy-ws }
deploy: { profile: local, docker_host: "unix:///var/run/docker.sock" }
secrets: { base_dir: "${SECRETS_DIR}" }
run:
  workspace_dir: /app/workspace
  dataspace_docs_repo: /legacy/docs
  docs_repo: /new/docs
  dataspace_docs_repo_url: https://legacy/x.git
  docs_repo_url: https://new/x.git
"""))
    assert cfg.jira.trigger_statuses == ["To Do"]
    assert cfg.match.statuses == ["To Do"]               # 레거시 미러도 신규 값
    assert cfg.jira.optout_labels == []                  # 명시 빈 리스트 존중(기능 끔)
    assert cfg.match.optout_labels == []
    assert cfg.notifier.provider == "slack"
    assert cfg.notifier.webhook_ref == "service/new"
    assert cfg.notify.webhook_ref == "service/new"
    assert cfg.deploy.docker_host == "unix:///var/run/docker.sock"
    assert cfg.spawn.docker_host == "unix:///var/run/docker.sock"
    # deploy 가 workspace_volume 을 명시하지 않았으므로 레거시 spawn 값이 남는다.
    assert cfg.deploy.workspace_volume == "legacy-ws"
    assert cfg.run.docs_repo == "/new/docs"
    assert cfg.run.docs_repo_url == "https://new/x.git"
    assert cfg.run.dataspace_docs_repo == "/new/docs"    # 미러도 신규 값으로 수렴


def test_deploy_profile_derives_values_when_not_given(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "")
    monkeypatch.delenv("HOST_DEPLOY_DIR", raising=False)
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
deploy: { profile: cloud_vm }
"""))
    # 프로파일 파생이 spawn/secrets 로 흘러들어간다(레거시 리더 무변경).
    assert cfg.deploy.docker_host == "tcp://socket-proxy:2375"
    assert cfg.spawn.docker_host == "tcp://socket-proxy:2375"
    assert cfg.secrets.base_dir == "/run/secrets"
    assert cfg.deploy.workspace_volume == "jad-workspace"

    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
deploy: { profile: local, secrets_base_dir: /tmp/jad-secrets }
"""))
    assert cfg2.spawn.docker_host == "unix:///var/run/docker.sock"
    assert cfg2.secrets.base_dir == "/tmp/jad-secrets"


def test_legacy_keys_beat_profile_defaults(tmp_path, monkeypatch):
    """기존 배포는 deploy 섹션 없이 레거시 키만 갖는다 — 프로파일 기본이 그걸 덮으면 안 된다."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
spawn: { docker_host: tcp://custom:2375 }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    # profile 미지정 → local(파생 기본 unix://...)이지만 명시 레거시 값이 이긴다.
    assert cfg.deploy.profile == "local"
    assert cfg.deploy.docker_host == "tcp://custom:2375"
    assert cfg.spawn.docker_host == "tcp://custom:2375"


def test_env_overrides_converge_into_deploy(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
deploy: { profile: cloud_vm }
"""))
    assert cfg.spawn.docker_host == cfg.deploy.docker_host   # 역방향 수렴
    assert cfg.deploy.secrets_base_dir == cfg.secrets.base_dir


def test_removed_host_deploy_dir_key_does_not_break_load(tmp_path, monkeypatch):
    """제거된 키가 deploy 섹션에 남아 있어도 로드가 깨지지 않는다(하위호환)."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
deploy: { profile: cloud_vm, host_deploy_dir: "/srv/jad" }
"""))
    assert cfg.deploy.profile == "cloud_vm"
    assert not hasattr(cfg.deploy, "host_deploy_dir")


def test_notify_enabled_env_override_maps_to_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    body = """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
notifier: { provider: none, webhook_ref: service/w }
"""
    monkeypatch.setenv("NOTIFY_ENABLED", "1")
    cfg = C.load_config(_write(tmp_path, body))
    assert cfg.notifier.provider == "google_chat"   # 레거시 토글의 의미
    assert cfg.notify.enabled is True

    monkeypatch.setenv("NOTIFY_ENABLED", "0")
    cfg2 = C.load_config(_write(tmp_path, body.replace("provider: none", "provider: slack")))
    assert cfg2.notifier.provider == "none"
    assert cfg2.notify.enabled is False

    monkeypatch.delenv("NOTIFY_ENABLED")
    monkeypatch.setenv("GOOGLE_CHAT_WEBHOOK_REF", "service/env-ref")
    cfg3 = C.load_config(_write(tmp_path, body))
    assert cfg3.notifier.webhook_ref == "service/env-ref"
    assert cfg3.notify.webhook_ref == "service/env-ref"


def test_forge_token_ref_bridges_legacy_resolver_ref(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # 신규 키만 준 설정에서도 레거시 리더(main·poller·dlc_meta_writer)가 읽는 필드가 채워진다.
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
forge: { kind: github, base_url: "https://ghe.example.com/", token_ref: service/forge }
"""))
    assert cfg.forge.kind == "github"
    assert cfg.forge.base_url == "https://ghe.example.com"   # 끝 슬래시 제거
    assert cfg.run.repo_resolver_gitlab_token_ref == "service/forge"


def test_jira_instance_specific_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira:
  base_url: https://x
  project: PROJ
  watcher_token_file: t
  custom_fields: { start_date: customfield_99, due_date: duedate }
  done_transition_id: "31"
  done_transition_names: ["Done"]
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.jira.custom_fields["start_date"] == "customfield_99"
    assert cfg.jira.done_transition_id == "31"
    assert cfg.jira.done_transition_names == ["Done"]

    # 미지정이면 비어 있다 → 소비자는 jira_client 모듈 상수로 폴백한다(하위호환).
    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg2.jira.custom_fields == {}
    assert cfg2.jira.done_transition_id == ""
    assert cfg2.jira.done_transition_names == ["완료", "Done"]


def test_docs_repo_is_optional_and_url_defaults_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { workspace_dir: /app/workspace }
"""))
    # 경로는 파생되지만 URL 이 없으므로 프로비저닝은 skip 된다(app/repos.py).
    assert cfg.run.docs_repo == "/app/workspace/docs"
    assert cfg.run.docs_repo_url == ""
    assert cfg.run.dataspace_docs_repo_url == ""


def test_explicit_legacy_docs_path_is_respected_not_rederived(tmp_path, monkeypatch):
    """경로를 명시한 기존 배포는 새 파생 이름(docs)으로 바뀌지 않는다."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
run: { workspace_dir: /app/workspace, dataspace_docs_repo: /app/workspace/dataspace_docs }
"""))
    assert cfg.run.docs_repo == "/app/workspace/dataspace_docs"
    assert cfg.run.dataspace_docs_repo == "/app/workspace/dataspace_docs"


def test_consent_parsed_but_never_fatal(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    # 동의 키가 아예 없어도(기존 배포) 부팅은 성공한다 — 경고만.
    cfg = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
"""))
    assert cfg.consent.full_permissions is False
    assert cfg.consent.accepted_at == ""

    cfg2 = C.load_config(_write(tmp_path, """
role: central
jira: { base_url: https://x, project: PROJ, watcher_token_file: t }
secrets: { base_dir: "${SECRETS_DIR}" }
consent: { full_permissions: true, accepted_at: "2026-08-25T09:00:00+09:00" }
"""))
    assert cfg2.consent.full_permissions is True
    assert cfg2.consent.accepted_at == "2026-08-25T09:00:00+09:00"


@pytest.mark.parametrize("section,body", [
    ("forge", "forge: { kind: bitbucket }"),
    ("notifier", "notifier: { provider: telegram }"),
    ("deploy", "deploy: { profile: kubernetes }"),
])
def test_unknown_enum_values_fail_fast(tmp_path, monkeypatch, section, body):
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    text = (
        "role: central\n"
        "jira: { base_url: https://x, project: PROJ, watcher_token_file: t }\n"
        'secrets: { base_dir: "${SECRETS_DIR}" }\n'
        + body + "\n"
    )
    with pytest.raises(C.ConfigError) as exc:
        C.load_config(_write(tmp_path, text))
    assert section in str(exc.value)


def test_shipped_example_config_loads(monkeypatch):
    """배포되는 예시 파일이 실제로 로드된다(예시와 파서가 갈라지지 않게)."""
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config("config/config.example.yaml")
    assert cfg.role == "central"
    assert cfg.deploy.profile in ("local", "cloud_vm", "onprem_server")
    assert cfg.forge.kind == "gitlab"
    assert cfg.notifier.provider == "none"          # 예시 기본은 알림 끔
    assert cfg.run.docs_repo_url == ""              # 예시 기본은 설계문서 레포 미사용
    assert cfg.jira.trigger_statuses                # 트리거 상태는 비어 있으면 안 된다
