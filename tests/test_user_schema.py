"""합류자(per-user) 온보딩 스키마 단위테스트 — app/user_schema.py.

이 모듈이 지키기로 한 약속:
    - **검증기는 하나**: 설치 관문과 같은 :mod:`app.setup_validate` 를 스키마만 갈아
      끼워 쓴다(규칙이 두 벌이 되면 갈라진다).
    - **forge 토큰은 필수**: 없으면 워커가 커밋만 하고 MR/PR 을 못 만드는 반쪽 동작이 된다.
    - **동의는 본인에게**: ``consent_full_permissions`` 가 정확히 true 여야 통과한다.
    - **안내는 설정에서 렌더**: forge 종류가 바뀌면 토큰 안내도 따라 바뀐다.
    - **시크릿 미노출**: 안내 페이로드에도 검증 출력에도 값이 실리지 않는다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import forge
from app import setup_schema as S
from app import setup_validate as V
from app import user_schema as U


def _cfg(kind="gitlab", jira_base="https://acme.atlassian.net", project="PROJ",
         dlc_meta="https://gitlab.acme.example/acme/dlc-meta.git",
         orchestrator="https://github.com/yunhyuk-choi/ai-dlc-orchestrator.git"):
    """관리 UI 안내 렌더에 쓰이는 최소 config 대역(비-시크릿 값만)."""
    return SimpleNamespace(
        forge=SimpleNamespace(kind=kind, base_url="", base_url_source="",
                              base_url_origin=""),
        jira=SimpleNamespace(base_url=jira_base, project=project),
        run=SimpleNamespace(dlc_meta_repo_url=dlc_meta,
                            orchestrator_repo_url=orchestrator, docs_repo_url=""),
    )


GOOD = {
    "username": "tester",
    "jira_account_id": "557058:1a2b",
    "jira_email": "you@example.com",
    "jira_token": "ATATT-token-value",
    "forge_token": "glpat-token-value",
    "claude_setup_token": "claude-token-value",
    U.CONSENT_KEY: True,
}


# ---------------------------------------------------------------------------
# 스키마 선언 자체
# ---------------------------------------------------------------------------


def test_schema_uses_the_shared_datastructures():
    """같은 자료구조를 써야 렌더·검증·안내가 한 벌의 기계로 돈다."""
    assert all(isinstance(sec, S.SchemaSection) for sec in U.USER_SCHEMA)
    assert all(isinstance(f, S.SchemaField) for f in U.iter_fields())


def test_required_keys_include_forge_token_and_consent():
    """forge 토큰과 본인 동의가 **무조건 필수**다(조건부가 아니다)."""
    required = U.required_keys()
    assert "forge_token" in required
    assert U.CONSENT_KEY in required
    for key in ("username", "jira_account_id", "jira_email", "jira_token",
                "claude_setup_token"):
        assert key in required
    # forge_token 은 조건부가 아니다 — 이 배포가 forge 를 안 쓰는 경우가 없기 때문이다.
    assert U.get_field("forge_token").required_if is None


def test_secret_fields_declare_no_example():
    """시크릿은 예시조차 안내에 싣지 않는다(placeholder 로도 새면 안 된다)."""
    guide = U.build_guide(_cfg())
    for section in guide["sections"]:
        for field in section["fields"]:
            if field["secret"]:
                assert field["example"] is None


def test_legacy_field_names_are_declared_not_special_cased():
    """옛 폼 필드 이름은 스키마의 legacy_keys 로 선언돼 있다(코드 분기가 아니라)."""
    legacy = S.legacy_key_map_in(U.USER_SCHEMA)
    assert legacy["gitlab_token"] == "forge_token"
    assert legacy["google_chat_user_id"] == "notify_user_id"


def test_every_forge_kind_has_token_guidance():
    """forge 를 추가하고 안내를 빠뜨리면 그 배포의 합류자는 무엇을 발급할지 모른다."""
    assert set(U.FORGE_TOKEN_GUIDE) == set(S.FORGE_KINDS)
    for kind, guide in U.FORGE_TOKEN_GUIDE.items():
        assert guide["path"] and guide["scope"] and guide["scope_why"]


# ---------------------------------------------------------------------------
# 검증 — 설치 관문과 같은 라이브러리
# ---------------------------------------------------------------------------


def test_good_answers_pass():
    result = U.validate_user_answers(GOOD)
    assert result.ok, result.format_text()


def test_missing_required_are_reported_together():
    """첫 오류에서 멈추지 않는다 — 왕복이 설치 포기의 주된 이유다."""
    result = U.validate_user_answers({"username": "tester", U.CONSENT_KEY: True})
    assert not result.ok
    missing = set(U.missing_keys(result))
    assert {"jira_account_id", "jira_email", "jira_token", "forge_token",
            "claude_setup_token"} <= missing


def test_forge_token_missing_is_an_error():
    answers = dict(GOOD)
    del answers["forge_token"]
    result = U.validate_user_answers(answers)
    assert not result.ok
    assert "forge_token" in U.missing_keys(result)


@pytest.mark.parametrize("value", [None, False, "true", "네", 1])
def test_consent_must_be_exactly_true(value):
    """관대한 해석은 동의를 받지 않는 것과 같다."""
    answers = dict(GOOD)
    if value is None:
        del answers[U.CONSENT_KEY]
    else:
        answers[U.CONSENT_KEY] = value
    result = U.validate_user_answers(answers)
    assert not result.ok
    assert any(f.key == U.CONSENT_KEY for f in result.errors)


def test_secret_values_are_allowed_here_but_never_echoed():
    """이 소비처는 토큰 **값**을 받는다(설치 답변과 다르다) — 대신 출력에 싣지 않는다."""
    result = U.validate_user_answers(GOOD)
    assert result.ok
    dumped = str(result.to_dict())
    for secret in ("ATATT-token-value", "glpat-token-value", "claude-token-value"):
        assert secret not in dumped


def test_setup_schema_still_rejects_secret_values():
    """per-user 예외가 설치 관문으로 새지 않는다(기본값은 여전히 '값 금지')."""
    field = S.SchemaField("x.secret", S.FieldType.STRING, "설명", secret=True)
    section = S.SchemaSection("x", "제목", "설명", (field,))
    result = V.validate_answers({"x": {"secret": "glpat-oops"}},
                                sections=(section,), consent_key="x.none")
    codes = {f.code for f in result.errors}
    assert V.CODE_SECRET_VALUE in codes
    assert "glpat-oops" not in str(result.to_dict())


def test_bad_choice_is_reported_with_field_key():
    """findings[].key 가 폼 입력칸 이름과 같아야 UI 가 칸별로 표시할 수 있다."""
    result = U.validate_user_answers({**GOOD, "autonomy_mode": "Z"})
    assert not result.ok
    bad = [f for f in result.errors if f.code == V.CODE_BAD_CHOICE]
    assert [f.key for f in bad] == ["autonomy_mode"]


def test_legacy_forge_token_field_is_accepted_with_a_warning():
    answers = dict(GOOD)
    answers["gitlab_token"] = answers.pop("forge_token")
    result = U.validate_user_answers(answers)
    assert result.ok
    assert any(f.code == V.CODE_LEGACY_KEY and f.key == "forge_token"
               for f in result.warnings)


def test_unknown_keys_do_not_warn():
    """평평한 폼 필드에는 '섹션 안의 오타'라는 개념이 없다(경고를 끄는 이유)."""
    result = U.validate_user_answers({**GOOD, "future_field": "x"})
    assert result.ok
    assert not [f for f in result.warnings if f.code == V.CODE_UNKNOWN_KEY]


# ---------------------------------------------------------------------------
# 입력 정규화 — 폼은 전부 문자열로 온다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [("on", True), ("true", True), ("1", True),
                                          ("false", False), ("", False), ("off", False)])
def test_coerce_checkbox_strings(raw, expected):
    assert U.coerce_answers({U.CONSENT_KEY: raw})[U.CONSENT_KEY] is expected


def test_coerce_keeps_unknown_boolean_words_as_is():
    """모르는 모양을 억지로 True 로 바꾸지 않는다 — 검증기가 타입 오류로 잡게 둔다."""
    coerced = U.coerce_answers({U.CONSENT_KEY: "네"})
    assert coerced[U.CONSENT_KEY] == "네"
    assert not U.validate_user_answers(coerced).ok


def test_coerce_string_list_from_comma_string():
    assert U.coerce_answers({"scope": "PROJ, PORTAL ,"})["scope"] == ["PROJ", "PORTAL"]


def test_coerce_strips_whitespace_and_keeps_unknown_keys():
    out = U.coerce_answers({"username": "  tester  ", "unknown": " x "})
    assert out["username"] == "tester"
    assert out["unknown"] == " x "      # 선언되지 않은 키는 손대지 않는다


def test_now_iso_is_parseable():
    from datetime import datetime

    assert datetime.fromisoformat(U.now_iso()).tzinfo is not None


# ---------------------------------------------------------------------------
# 안내 렌더 — **설정에서** 유도한다
# ---------------------------------------------------------------------------


def test_guide_follows_forge_kind():
    """forge 를 바꾸면 안내가 따라간다 — 하드코딩이면 안 따라간다."""
    gl = U.build_guide(_cfg(kind="gitlab"))["forge"]
    gh = U.build_guide(_cfg(kind="github",
                            dlc_meta="https://github.com/acme/dlc-meta.git"))["forge"]
    assert gl["kind"] == forge.KIND_GITLAB and gl["scope"] == "api"
    assert gl["change_abbr"] == "MR"
    assert gh["kind"] == forge.KIND_GITHUB and gh["scope"] == "repo"
    assert gh["change_abbr"] == "PR"
    assert "GitLab" not in gh["path"] and "GitHub" not in gl["path"]


def test_guide_token_url_points_at_self_hosted_host():
    """사내 GitLab 을 쓰는 팀에게 gitlab.com 링크를 주지 않는다(토큰이 나갈 곳)."""
    guide = U.build_guide(_cfg(dlc_meta="https://gitlab.acme.example/acme/dlc-meta.git"))
    assert guide["forge"]["url"].startswith("https://gitlab.acme.example/")
    assert "gitlab.com" not in guide["forge"]["url"]


def test_guide_token_url_is_blank_when_host_is_unknown():
    """근거가 없으면 주소를 지어내지 않는다(추측한 링크로 사람을 보내지 않는다)."""
    guide = U.build_guide(_cfg(dlc_meta=""))
    assert guide["forge"]["url"] == ""
    assert guide["forge"]["path"]      # 경로 설명은 그대로 보여 준다


def test_guide_jira_links_follow_the_configured_site():
    guide = U.build_guide(_cfg(jira_base="https://acme.atlassian.net"))
    assert guide["jira"]["myself_url"] == "https://acme.atlassian.net/rest/api/3/myself"
    assert guide["jira"]["project"] == "PROJ"


def test_guide_survives_a_partial_config():
    """일부 섹션만 있는 config(테스트·임베드 조립)에서도 예외를 내지 않는다."""
    guide = U.build_guide(SimpleNamespace(secrets=SimpleNamespace(base_dir="/tmp")))
    assert guide["sections"] and guide["steps"]
    assert guide["jira"]["myself_url"] == ""
    guide_none = U.build_guide(None)
    assert guide_none["sections"]


def test_guide_declares_the_two_step_join():
    """웹 등록만으로는 절반이다 — 로컬 SETTER 합류가 1단이다."""
    steps = U.build_guide(_cfg())["steps"]
    assert [s["id"] for s in steps] == ["local", "web"]
    assert steps[0]["order"] == 1 and "SETTER" in steps[0]["how"]
    assert "ai-dlc-orchestrator" in steps[0]["repo_url"]
    assert steps[1]["order"] == 2


def test_guide_marks_server_derived_fields_as_non_input():
    """동의 시각은 서버가 찍는다 — 폼에 입력칸이 생기면 안 된다."""
    fields = {f["key"]: f
              for sec in U.build_guide(_cfg())["sections"] for f in sec["fields"]}
    assert fields[U.CONSENT_AT_KEY]["input"] is False
    assert fields[U.CONSENT_KEY]["input"] is True


def test_guide_carries_no_secret_material():
    """관리 UI 에는 인증이 없다 — 안내에 시크릿이 실리면 그대로 유출이다."""
    cfg = _cfg()
    cfg.secrets = SimpleNamespace(base_dir="/run/secrets")
    dumped = str(U.build_guide(cfg))
    assert "/run/secrets" not in dumped
    for field in U.iter_fields():
        if field.secret:
            assert field.example is None


def test_guide_carries_the_secret_handling_note():
    """토큰을 붙여넣는 사람에게 보관 규율을 말해 준다(필드가 아니라 폼 전체에 걸린다)."""
    note = U.build_guide(_cfg())["secrets_note"]
    assert "0600" in note and "enabled=false" in note
