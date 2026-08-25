"""온보딩 답변 검증기(app/setup_validate.py) 단위테스트.

이 게이트가 하는 약속을 지킨다:
    - 누락·조건부 누락·허용값·타입 위반을 **전부 모아서** 보고한다(첫 오류에서 안 멈춤).
    - ``consent.full_permissions`` 가 참이 아니면 **절대** 통과하지 않는다.
    - 시크릿 **값**이 참조 자리에 오면 막고, 그 값을 출력에 싣지 않는다.
    - 레거시 키로 준 값은 파서(app/config.py)가 읽는 것과 **똑같이** 인정한다.
"""

from __future__ import annotations

import pytest

from app import setup_validate as V

# 통과하는 최소 답변(각 테스트가 필요한 부분만 바꿔 쓴다).
GOOD = {
    "consent": {"full_permissions": True, "accepted_at": "2026-08-25T09:00:00+09:00"},
    "deploy": {"profile": "cloud_vm", "host_deploy_dir": "/srv/jad",
               "secrets_base_dir": "/run/secrets"},
    "forge": {"kind": "gitlab", "token_ref": "service/forge-token"},
    "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
             "trigger_statuses": ["To Do"],
             "watcher_token_file": "service/jira-token",
             "watcher_email": "bot@acme.example"},
    "notifier": {"provider": "none"},
    "webhook": {"enabled": True, "secret_ref": "service/jira-webhook"},
}


def _merged(**sections) -> dict:
    """GOOD 위에 섹션 단위로 덮어쓴 답변을 만든다."""
    out = {k: dict(v) for k, v in GOOD.items()}
    for name, patch in sections.items():
        out.setdefault(name, {})
        out[name].update(patch)
    return out


def _codes(result, key=None) -> set:
    return {f.code for f in result.findings if key is None or f.key == key}


# --- 정상 경로 ---------------------------------------------------------------


def test_minimal_good_answers_pass():
    result = V.validate_answers(GOOD)
    assert result.ok, result.format_text()
    assert result.errors == []


def test_nested_and_dotted_answers_are_equivalent():
    dotted = {"consent.full_permissions": True,
              "consent.accepted_at": "2026-08-25T09:00:00+09:00",
              "deploy.profile": "cloud_vm", "deploy.host_deploy_dir": "/srv/jad",
              "deploy.secrets_base_dir": "/run/secrets",
              "forge.kind": "gitlab", "forge.token_ref": "service/forge-token",
              "jira.base_url": "https://acme.atlassian.net", "jira.project": "ACME",
              "jira.trigger_statuses": ["To Do"],
              "jira.watcher_token_file": "service/jira-token",
              "jira.watcher_email": "bot@acme.example",
              "notifier.provider": "none",
              "webhook.enabled": True, "webhook.secret_ref": "service/jira-webhook"}
    assert V.validate_answers(dotted).ok
    assert V.validate_answers(dotted).explicit == V.validate_answers(GOOD).explicit


def test_flatten_stops_at_declared_map_field():
    """STRING_MAP 값(dict)은 자식 키로 쪼개지 않는다."""
    flat = V.flatten_answers({"jira": {"custom_fields": {"due_date": "duedate"}}})
    assert flat == {"jira.custom_fields": {"due_date": "duedate"}}


# --- 누락·조건부 필수 ---------------------------------------------------------


def test_all_missing_required_reported_at_once():
    """첫 오류에서 멈추지 않는다 — 사람이 왕복을 여러 번 하지 않게."""
    result = V.validate_answers({"consent": {"full_permissions": True,
                                             "accepted_at": "2026-08-25"}})
    missing = {f.key for f in result.errors if f.code == V.CODE_MISSING_REQUIRED}
    assert {"jira.base_url", "jira.project", "jira.watcher_token_file",
            "jira.trigger_statuses", "deploy.secrets_base_dir"} <= missing


def test_required_if_fires_only_when_condition_matches():
    off = V.validate_answers(_merged(notifier={"provider": "none"}))
    assert V.CODE_MISSING_REQUIRED_IF not in _codes(off, "notifier.webhook_ref")

    on = V.validate_answers(_merged(notifier={"provider": "slack"}))
    assert V.CODE_MISSING_REQUIRED_IF in _codes(on, "notifier.webhook_ref")
    assert not on.ok

    fixed = V.validate_answers(_merged(
        notifier={"provider": "slack", "webhook_ref": "service/notifier-webhook"}))
    assert fixed.ok


def test_host_deploy_dir_is_required_on_server_profiles():
    """스키마의 required_if 가 그대로 강제된다(워커 바인드가 조용히 깨지는 조합)."""
    bad = _merged(deploy={"profile": "onprem_server", "host_deploy_dir": ""})
    result = V.validate_answers(bad)
    assert V.CODE_MISSING_REQUIRED_IF in _codes(result, "deploy.host_deploy_dir")

    local_ok = _merged(deploy={"profile": "local", "host_deploy_dir": ""})
    assert V.CODE_MISSING_REQUIRED_IF not in _codes(
        V.validate_answers(local_ok), "deploy.host_deploy_dir")


# --- 허용값·타입 ---------------------------------------------------------------


def test_bad_choice_reported():
    result = V.validate_answers(_merged(deploy={"profile": "vm"}))
    assert V.CODE_BAD_CHOICE in _codes(result, "deploy.profile")


@pytest.mark.parametrize("section,patch,key", [
    ("jira", {"poll_interval_sec": "60"}, "jira.poll_interval_sec"),
    ("jira", {"poll_interval_sec": True}, "jira.poll_interval_sec"),
    ("jira", {"trigger_statuses": "To Do"}, "jira.trigger_statuses"),
    ("jira", {"trigger_statuses": [1, 2]}, "jira.trigger_statuses"),
    ("jira", {"custom_fields": ["due_date"]}, "jira.custom_fields"),
    ("jira", {"custom_fields": {"due_date": 5}}, "jira.custom_fields"),
    ("jira", {"base_url": 123}, "jira.base_url"),
    ("notifier", {"notify_cancelled": "yes"}, "notifier.notify_cancelled"),
])
def test_type_mismatches_reported(section, patch, key):
    result = V.validate_answers(_merged(**{section: patch}))
    assert V.CODE_BAD_TYPE in _codes(result, key)


def test_bool_true_is_not_int_for_int_fields():
    """bool 은 int 의 서브클래스지만 정수 자리에 오면 오설정이다."""
    from app import setup_schema as S

    assert V._type_error(S.get_field("jira.poll_interval_sec"), True) is not None
    assert V._type_error(S.get_field("jira.poll_interval_sec"), 60) is None


# --- 시크릿 규율 ---------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "https://hooks.slack.com/services/T/B/xxxx",   # URL = 값
    "/run/secrets/service/jira-token",             # 절대경로
    "ATATT3xFfGF0abcdefghijklmnop",                # Atlassian 토큰 접두사
    "glpat-abcdefghijklmnopqrst",                  # GitLab PAT 접두사
    "a" * 40,                                      # 긴 단일 덩어리
    "service/jira token",                          # 공백
])
def test_secret_value_in_reference_slot_is_rejected(value):
    result = V.validate_answers(_merged(jira={"watcher_token_file": value}))
    assert V.CODE_SECRET_VALUE in _codes(result, "jira.watcher_token_file")


def test_secret_value_findings_never_leak_the_value():
    secret = "glpat-DO-NOT-LEAK-THIS-TOKEN-VALUE"
    result = V.validate_answers(_merged(jira={"watcher_token_file": secret}))
    blob = result.format_text() + repr(result.to_dict())
    assert secret not in blob


@pytest.mark.parametrize("value", [
    "service/jira-token", "acme/tokens/jira.txt", "jira-token",
])
def test_plain_relative_references_pass(value):
    result = V.validate_answers(_merged(jira={"watcher_token_file": value}))
    assert V.CODE_SECRET_VALUE not in _codes(result, "jira.watcher_token_file")


def test_to_dict_carries_findings_but_not_values():
    payload = V.validate_answers(GOOD).to_dict()
    assert set(payload) == {"ok", "error_count", "warning_count", "findings"}


# --- 동의 게이트 ---------------------------------------------------------------


@pytest.mark.parametrize("consent", [
    {},                                    # 키 자체가 없음
    {"full_permissions": False},           # 명시 거부
    {"full_permissions": "true"},          # 문자열은 동의가 아니다
    {"full_permissions": 1},               # 1 도 아니다
])
def test_consent_gate_blocks_everything_but_real_true(consent):
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["consent"] = consent
    result = V.validate_answers(answers)
    assert not result.ok
    assert V.CODE_CONSENT_REQUIRED in _codes(result, "consent.full_permissions")


def test_consent_true_requires_accepted_at():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["consent"] = {"full_permissions": True}
    result = V.validate_answers(answers)
    assert V.CODE_MISSING_REQUIRED_IF in _codes(result, "consent.accepted_at")


def test_odd_accepted_at_is_only_a_warning():
    result = V.validate_answers(_merged(consent={"accepted_at": "어제쯤"}))
    assert result.ok
    assert V.CODE_BAD_TIMESTAMP in _codes(result, "consent.accepted_at")


# --- 자리표시자·env 토큰 -------------------------------------------------------


def test_example_placeholder_is_an_error():
    result = V.validate_answers(_merged(jira={"project": "<PROJECT_KEY>"}))
    assert V.CODE_PLACEHOLDER in _codes(result, "jira.project")
    assert not result.ok


def test_unsubstituted_env_token_is_a_warning_only():
    result = V.validate_answers(_merged(deploy={"secrets_base_dir": "${SECRETS_DIR}"}))
    assert result.ok
    assert V.CODE_UNSUBSTITUTED_ENV in _codes(result, "deploy.secrets_base_dir")


# --- 레거시 키(파서와 같은 것을 본다) ------------------------------------------


def test_legacy_keys_satisfy_the_new_key_with_a_warning():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["jira"].pop("trigger_statuses")
    answers["match"] = {"statuses": ["할 일"]}
    result = V.validate_answers(answers)
    assert result.ok
    assert result.values["jira.trigger_statuses"] == ["할 일"]
    assert V.CODE_LEGACY_KEY in _codes(result, "jira.trigger_statuses")


def test_legacy_notify_enabled_maps_to_google_chat_like_the_parser():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers.pop("notifier")
    answers["notify"] = {"enabled": True, "webhook_ref": "service/gchat"}
    result = V.validate_answers(answers)
    assert result.values["notifier.provider"] == "google_chat"
    assert result.values["notifier.webhook_ref"] == "service/gchat"
    assert result.ok


def test_new_key_wins_over_legacy_key():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["match"] = {"statuses": ["옛것"]}
    result = V.validate_answers(answers)
    assert result.values["jira.trigger_statuses"] == ["To Do"]


# --- 모르는 키 ------------------------------------------------------------------


def test_unknown_key_inside_closed_section_warns():
    result = V.validate_answers(_merged(forge={"kynd": "gitlab"}))
    assert result.ok  # 경고이지 오류가 아니다
    assert V.CODE_UNKNOWN_KEY in _codes(result, "forge.kynd")


def test_unknown_key_outside_closed_sections_is_silent():
    """완전한 config.yaml 을 그대로 검증해도 경고가 쏟아지지 않아야 한다."""
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["run"] = {"workspace_dir": "/app/workspace", "claude_bin": "claude"}
    answers["server"] = {"host": "0.0.0.0", "port": 8787}
    result = V.validate_answers(answers)
    assert [f for f in result.findings if f.code == V.CODE_UNKNOWN_KEY] == []


# --- 해석 결과(렌더러 계약) -----------------------------------------------------


def test_explicit_holds_only_answered_keys():
    """렌더러는 '답한 것'만 써야 한다 — 기본값까지 쓰면 프로파일 파생이 죽는다."""
    result = V.validate_answers(GOOD)
    assert "deploy.docker_host" not in result.explicit
    assert "deploy.docker_host" in result.values  # 해석 결과에는 기본값이 있다
    assert result.explicit["deploy.profile"] == "cloud_vm"


def test_defaults_are_copied_not_shared():
    """기본값이 리스트여도 결과 간섭이 없어야 한다(mutable default 방어)."""
    a = V.validate_answers(GOOD)
    a.values["jira.cancel_statuses"].append("오염")
    b = V.validate_answers(GOOD)
    assert "오염" not in b.values["jira.cancel_statuses"]


# --- 상태·전이의 {id, name} 형태(NAMED_REF_LIST) --------------------------------
#
# 화면 표시명과 API 의 name/id 가 어긋나는 것이 이 시스템에서 반복된 오설정이다.
# discover 가 id 를 함께 적어 주므로 검증기도 그 형태를 받아야 한다(문자열도 계속).


def test_named_ref_list_accepts_plain_names_and_id_name_pairs():
    ok = V.validate_answers(_merged(jira={
        "trigger_statuses": [{"id": "10000", "name": "해야 할 일"}, "선택 대기"],
        "cancel_statuses": [{"name": "취소됨"}],
        "done_transition_names": [{"id": "41", "name": "완료"}],
    }))
    assert ok.ok, ok.format_text()


@pytest.mark.parametrize("value,reason", [
    ([{"id": "10000"}], "name 이 없다"),
    ([{"id": "10000", "name": ""}], "name 이 비었다"),
    ([{"id": 10000, "name": "해야 할 일"}], "id 가 문자열이 아니다"),
    ([{"name": "해야 할 일", "categoy": "new"}], "모르는 키(오타)"),
    ([123], "문자열도 매핑도 아니다"),
    ("해야 할 일", "목록이 아니다"),
])
def test_named_ref_list_rejects_malformed_entries(value, reason):
    result = V.validate_answers(_merged(jira={"trigger_statuses": value}))
    assert V.CODE_BAD_TYPE in _codes(result, "jira.trigger_statuses"), reason


# --- 웹훅 수신 토큰(조건부 필수) -------------------------------------------------


def test_webhook_secret_ref_is_required_while_the_endpoint_is_on():
    """⚠️ 참조가 없으면 엔드포인트가 503 으로 거부한다 — 설치 시점에 잡는다."""
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["webhook"] = {"enabled": True}
    result = V.validate_answers(answers)
    assert V.CODE_MISSING_REQUIRED_IF in _codes(result, "webhook.secret_ref")
    assert not result.ok


def test_webhook_secret_ref_not_required_when_the_endpoint_is_off():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["webhook"] = {"enabled": False}
    assert V.validate_answers(answers).ok


def test_webhook_default_is_on_so_the_secret_is_asked_for():
    """webhook 섹션을 통째로 빼도 기본값(enabled=true)이라 참조를 묻는다."""
    answers = {k: dict(v) for k, v in GOOD.items() if k != "webhook"}
    result = V.validate_answers(answers)
    assert V.CODE_MISSING_REQUIRED_IF in _codes(result, "webhook.secret_ref")


def test_webhook_secret_value_pasted_into_the_reference_is_refused():
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["webhook"] = {"enabled": True, "secret_ref": "https://hooks.example/abc"}
    result = V.validate_answers(answers)
    assert V.CODE_SECRET_VALUE in _codes(result, "webhook.secret_ref")
    assert "hooks.example" not in result.format_text()   # 값은 출력에 싣지 않는다


# --- 감시 계정 이메일(필수) -------------------------------------------------------


def test_watcher_email_is_required():
    """Basic auth 는 (이메일, 토큰) 쌍이다 — 토큰만 물으면 폴러가 401 로 죽는다."""
    answers = {k: dict(v) for k, v in GOOD.items()}
    answers["jira"].pop("watcher_email")
    result = V.validate_answers(answers)
    assert V.CODE_MISSING_REQUIRED in _codes(result, "jira.watcher_email")
