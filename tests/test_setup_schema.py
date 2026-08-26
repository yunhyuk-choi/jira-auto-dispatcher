"""온보딩 설정 스키마(app/setup_schema.py) 단위테스트.

스키마는 **선언**이므로 테스트도 선언의 일관성을 지킨다:
    - 키가 중복되지 않고 설명이 비어 있지 않다(온보딩 질문 문구의 원천이므로).
    - ENUM 은 choices 를 갖고 기본값이 그 안에 있다.
    - 조건부 필수는 **실재하는 다른 키**를 가리킨다(오타 방지).
    - 시크릿 규율: 값 시크릿과 참조 시크릿을 동시에 주장하지 않는다.
    - ⚠️ 가장 중요한 것 — 선언한 키 경로가 :mod:`app.config` 에서 **실제로 로드된다**.
      스키마와 파서가 갈라지는 것이 이 프레임워크화에서 가장 위험한 드리프트다.
"""

from __future__ import annotations

import pytest

from app import config as C
from app import setup_schema as S


# --- 선언 일관성 -------------------------------------------------------------


def test_sections_are_the_declared_eight():
    assert [s.name for s in S.SETUP_SCHEMA] == [
        "forge", "notifier", "jira", "webhook", "dlc_meta", "docs_repo",
        "deploy", "consent",
    ]
    # 섹션 전체를 건너뛸 수 있는 것은 웹훅 수신과 설계문서 레포뿐이다.
    # ⚠️ dlc_meta 는 선택이 아니다 — central 이 사이클로그를 쓰고 REPO-MAP 을 읽는 곳이고,
    #    그 URL 이 forge base_url·kind 판정의 근거이기도 하다.
    assert [s.name for s in S.SETUP_SCHEMA if s.optional] == ["webhook", "docs_repo"]


def test_field_keys_unique_and_documented():
    keys = [f.key for f in S.iter_fields()]
    assert len(keys) == len(set(keys)), "키 경로가 중복됐다"
    for f in S.iter_fields():
        assert f.description.strip(), f"{f.key}: 설명이 비어 있다"
        assert f.key.count(".") >= 1, f"{f.key}: 점 표기 경로여야 한다"


def test_enum_fields_have_choices_containing_default():
    for f in S.iter_fields():
        if f.type is S.FieldType.ENUM:
            assert f.choices, f"{f.key}: ENUM 인데 choices 가 없다"
            assert f.default in f.choices, f"{f.key}: 기본값이 choices 밖이다"


def test_required_if_points_at_an_existing_key():
    for f in S.iter_fields():
        if f.required_if is not None:
            assert S.get_field(f.required_if.key) is not None, (
                f"{f.key}: 조건 대상 {f.required_if.key} 가 스키마에 없다"
            )


def test_secret_flags_are_mutually_exclusive_and_refs_only():
    for f in S.iter_fields():
        assert not (f.secret and f.secret_ref), f"{f.key}: 두 시크릿 성격을 동시에 주장한다"
    # 이 시스템은 시크릿 "값"을 config.yaml 에 담지 않는다 — 스키마에도 값 시크릿은 없다.
    assert [f.key for f in S.iter_fields() if f.secret] == []
    # 참조 시크릿은 forge/notifier/jira/webhook 각각 하나씩 있다.
    assert sorted(f.key for f in S.iter_fields() if f.secret_ref) == [
        "forge.token_ref", "jira.watcher_token_file", "notifier.webhook_ref",
        "webhook.secret_ref",
    ]


def test_required_if_matches_semantics():
    truthy = S.get_field("forge.token_ref").required_if
    assert truthy.matches("gitlab") and not truthy.matches("")
    equals = S.get_field("notifier.webhook_ref").required_if
    assert equals.matches("slack") and not equals.matches("none")
    assert "notifier.provider" in equals.describe()


def test_lookup_helpers():
    assert S.get_section("jira").title == "Jira 인스턴스"
    assert S.get_section("nope") is None
    assert S.get_field("deploy.profile").choices == S.DEPLOY_PROFILES
    assert S.get_field("nope.nope") is None


def test_legacy_key_map_covers_renamed_keys():
    m = S.legacy_key_map()
    assert m["match.statuses"] == "jira.trigger_statuses"
    assert m["notify.webhook_ref"] == "notifier.webhook_ref"
    assert m["run.dataspace_docs_repo"] == "run.docs_repo"
    assert m["run.dataspace_docs_repo_url"] == "run.docs_repo_url"
    assert m["run.repo_resolver_gitlab_token_ref"] == "forge.token_ref"
    assert m["spawn.docker_host"] == "deploy.docker_host"
    assert m["secrets.base_dir"] == "deploy.secrets_base_dir"


def test_profile_defaults_cover_every_declared_profile():
    for profile in S.DEPLOY_PROFILES:
        d = S.PROFILE_DEFAULTS[profile]
        assert set(d) == {"docker_host", "secrets_base_dir", "workspace_volume"}
    # ⚠️ **모든** 프로파일이 socket-proxy 경유다(소켓 직결은 호스트 root 동치라
    # 기본값이 될 수 없고, 이 리포가 배포하는 compose 도 socket-proxy 를 선언한다).
    # local 만 예외로 두면 배포되는 compose 와 모순되는 config 가 파생된다(실측 결함).
    for profile in S.DEPLOY_PROFILES:
        assert S.PROFILE_DEFAULTS[profile]["docker_host"].startswith("tcp://"), profile
    # 프로파일이 실제로 갈리는 자리는 시크릿 루트다(로컬 디렉토리 vs 컨테이너 경로).
    assert S.PROFILE_DEFAULTS["local"]["secrets_base_dir"] == ""
    assert S.PROFILE_DEFAULTS["cloud_vm"]["secrets_base_dir"] == "/run/secrets"


def test_profile_derived_values_are_dotted_and_drop_empties():
    """파생값은 점 표기 키로 나오고, 빈 값('의견 없음')은 싣지 않는다."""
    local = S.profile_derived_values("local")
    assert local["deploy.docker_host"] == "tcp://socket-proxy:2375"
    assert local["deploy.workspace_volume"] == "jad-workspace"
    # local 의 secrets_base_dir 은 "" → 렌더에 쓰면 템플릿의 ${SECRETS_DIR} 을 지운다.
    assert "deploy.secrets_base_dir" not in local
    assert S.profile_derived_values("cloud_vm")["deploy.secrets_base_dir"] == "/run/secrets"
    # 대소문자·공백은 흡수하고, 모르는 프로파일은 빈 dict.
    assert S.profile_derived_values("  CLOUD_VM ") == S.profile_derived_values("cloud_vm")
    assert S.profile_derived_values("없는프로파일") == {}
    assert S.profile_derived_values(None) == {}


def test_jira_custom_field_keys_match_todays_constants():
    """논리 키의 '오늘의 기본값'이 jira_client 모듈 상수와 일치해야 한다(드리프트 방지)."""
    from app import jira_client as J

    defaults = {k: v for k, _desc, v in S.JIRA_CUSTOM_FIELD_KEYS}
    assert defaults["start_date"] == J.FIELD_START_DATE
    assert defaults["due_date"] == J.FIELD_DUE_DATE
    assert defaults["actual_start"] == J.FIELD_ACTUAL_START
    assert defaults["actual_end"] == J.FIELD_ACTUAL_END


# --- 스키마 ↔ 파서 드리프트 방지 ---------------------------------------------

# 스키마 키 경로 → 로드된 AppConfig 에서 그 값을 읽는 경로.
# 스키마가 "묻겠다"고 선언한 항목은 반드시 config.py 가 읽어야 한다.
_KEY_TO_ATTR = {
    "forge.kind": "forge.kind",
    "forge.base_url": "forge.base_url",
    "forge.token_ref": "forge.token_ref",
    "notifier.provider": "notifier.provider",
    "notifier.webhook_ref": "notifier.webhook_ref",
    "notifier.notify_interrupted": "notifier.notify_interrupted",
    "notifier.notify_cancelled": "notifier.notify_cancelled",
    "jira.base_url": "jira.base_url",
    "jira.project": "jira.project",
    "jira.projects": "jira.projects",
    "jira.poll_interval_sec": "jira.poll_interval_sec",
    "jira.auth_recheck_sec": "jira.auth_recheck_sec",
    "jira.watcher_token_file": "jira.watcher_token_file",
    "jira.watcher_email": "jira.watcher_email",
    "jira.trigger_statuses": "jira.trigger_statuses",
    "jira.cancel_statuses": "jira.cancel_statuses",
    "jira.optout_labels": "jira.optout_labels",
    "jira.custom_fields": "jira.custom_fields",
    "jira.done_transition_id": "jira.done_transition_id",
    "jira.done_transition_names": "jira.done_transition_names",
    "webhook.enabled": "webhook.enabled",
    "webhook.secret_ref": "webhook.secret_ref",
    "run.dlc_meta_repo_url": "run.dlc_meta_repo_url",
    "run.docs_repo": "run.docs_repo",
    "run.docs_repo_url": "run.docs_repo_url",
    "deploy.profile": "deploy.profile",
    "deploy.docker_host": "deploy.docker_host",
    "deploy.secrets_base_dir": "deploy.secrets_base_dir",
    "deploy.workspace_volume": "deploy.workspace_volume",
    "consent.full_permissions": "consent.full_permissions",
    "consent.accepted_at": "consent.accepted_at",
}


def _get(obj, dotted):
    for part in dotted.split("."):
        obj = getattr(obj, part)
    return obj


def test_every_schema_key_has_a_config_accessor():
    assert sorted(_KEY_TO_ATTR) == sorted(f.key for f in S.iter_fields()), (
        "스키마 필드와 파서 접근 경로 표가 어긋났다 — 한쪽만 바꾸지 말 것"
    )


@pytest.mark.parametrize("key", sorted(_KEY_TO_ATTR))
def test_schema_defaults_are_what_the_parser_produces(key, monkeypatch):
    """아무것도 안 준 최소 설정에서, 파서 기본값 == 스키마가 선언한 기본값.

    (필수 키는 최소 설정에서 값을 줘야 하므로 비교 대상에서 제외한다.)
    """
    monkeypatch.delenv("HOST_DEPLOY_DIR", raising=False)
    monkeypatch.setenv("SECRETS_DIR", "/run/secrets")
    cfg = C.load_config_from_dict({
        "role": "central",
        "jira": {"base_url": "https://x", "project": "PROJ",
                 "watcher_token_file": "t", "trigger_statuses": ["해야 할 일"]},
        "secrets": {"base_dir": "${SECRETS_DIR}"},
    })
    f = S.get_field(key)
    if f.required or f.default is None:
        pytest.skip("필수 항목이거나 선언된 기본값이 없다")
    assert _get(cfg, _KEY_TO_ATTR[key]) == f.default
