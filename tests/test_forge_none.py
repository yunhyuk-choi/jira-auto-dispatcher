"""``forge.kind: none`` — **"forge 없음"이 1급 값인가**(3차 리허설 C2).

리허설 실측: 스키마가 ``forge.kind`` 를 ``gitlab|github`` 로만 받고 ``forge.token_ref`` 를
사실상 무조건 필수로 만들어서(``RequiredIf(forge.kind, truthy=True)`` — kind 는 기본값이
있어 **항상 truthy**), 순수 git 원격을 쓰는 배포는 `doctor` 가 exit 0 에 도달할 수 없었다.
그 결과 설치자가 **``gitlab`` 을 의미 없이 채우고 더미 토큰 파일을 만들었다** — 거짓 설정이
config 에 남는다. 거짓말을 강요하는 스키마는 그 자체로 결함이다.

이 파일이 강제하는 것:
    1. ``none`` 이 스키마·설정 로더·어댑터 전체에서 받아들여진다.
    2. 그 경우 ``forge.token_ref`` 가 **필수가 아니고**, 렌더된 config 에도 남지 않는다.
    3. 무엇이 꺼지는지 **명시적으로** 보고된다(조용한 SKIP 금지).
    4. 기존 배포(``gitlab``)는 아무것도 달라지지 않는다.
"""

from __future__ import annotations

import pytest

from app import forge
from app import setup_doctor as D
from app import setup_schema as S
from app import setup_validate as V

_BASE = {
    "consent": {"full_permissions": True, "accepted_at": "2026-08-25T09:00:00+09:00"},
    "deploy": {"profile": "cloud_vm", "secrets_base_dir": "/run/secrets"},
    "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
             "trigger_statuses": ["To Do"],
             "watcher_token_file": "service/jira-token",
             "watcher_email": "bot@acme.example"},
    "notifier": {"provider": "none"},
    "webhook": {"enabled": True, "secret_ref": "service/jira-webhook"},
    "run": {"dlc_meta_repo_url": "ssh://git@git.internal.example/acme/dlc-meta.git"},
}


def _answers(**forge_section) -> dict:
    return {**_BASE, "forge": forge_section}


# ---------------------------------------------------------------------------
# 스키마 · 검증
# ---------------------------------------------------------------------------


def test_none_is_a_declared_forge_kind():
    assert forge.KIND_NONE in S.FORGE_KINDS
    assert forge.KIND_NONE not in S.FORGE_KINDS_WITH_API
    assert S.FORGE_KIND_NONE == forge.KIND_NONE      # 두 곳이 갈라지지 않는다


def test_a_forgeless_deployment_needs_no_token_reference():
    """C2 의 본체 — 이게 실패하면 설치자는 더미 토큰 파일을 만들게 된다."""
    result = V.validate_answers(_answers(kind="none"))
    assert result.ok, [f.to_dict() for f in result.errors]


def test_a_real_forge_still_requires_its_token_reference():
    """하위호환 — 기존 배포의 게이트는 그대로다(느슨해지지 않았다)."""
    result = V.validate_answers(_answers(kind="gitlab"))
    assert not result.ok
    assert [(f.key, f.code) for f in result.errors] == [
        ("forge.token_ref", V.CODE_MISSING_REQUIRED_IF)]
    assert V.validate_answers(_answers(kind="gitlab",
                                       token_ref="service/forge-token")).ok


def test_a_forgeless_deployment_does_not_keep_a_token_reference_it_never_uses():
    """렌더될 값에 남으면 '쓰지 않는 참조'가 config 에 박힌다(거짓 설정)."""
    result = V.validate_answers(_answers(kind="none"))
    assert result.explicit["forge.token_ref"] == ""


def test_an_explicit_token_reference_is_not_erased(tmp_path):
    """명시로 적었으면 그 사람의 선택이다 — 파생 규칙이 답을 지우지 않는다."""
    result = V.validate_answers(_answers(kind="none", token_ref="service/forge-token"))
    assert result.explicit["forge.token_ref"] == "service/forge-token"


# ---------------------------------------------------------------------------
# 어댑터 — 모르는 종류로 흘러 기본 forge 행세를 하지 않는다
# ---------------------------------------------------------------------------


def test_the_adapter_does_not_pretend_to_be_gitlab():
    """``.get(k, 기본)`` 폴백에 걸리면 forge 없는 배포가 'GitLab/MR' 로 표시된다."""
    assert forge.label(forge.KIND_NONE) != forge.label(forge.KIND_GITLAB)
    assert forge.change_abbr(forge.KIND_NONE) not in ("MR", "PR")
    assert forge.supports_change_requests(forge.KIND_NONE) is False
    assert forge.supports_change_requests(forge.KIND_GITLAB) is True
    assert forge.supports_change_requests(None) is True      # 기본은 오늘의 동작


def test_no_token_is_injected_into_a_plain_git_remote():
    """forge 가 없으면 토큰 인증이라는 개념도 없다 — URL 을 건드리지 않는다."""
    url = "https://git.internal.example/acme/repo.git"
    assert forge.with_token(url, "tok", kind=forge.KIND_NONE) == url
    assert forge.with_token(url, "tok", kind=forge.KIND_GITLAB) != url


def test_closing_a_change_request_is_a_no_op_without_a_forge():
    assert forge.close_change_request(
        "https://git.internal.example/a/b/-/merge_requests/1", "tok",
        kind=forge.KIND_NONE) is False


def test_searching_for_a_change_url_still_works_without_a_forge():
    """레포별로 남의 forge 링크가 로그에 섞일 수 있다 — 통합 폴백으로 떨어진다."""
    found = forge.search_change_url(
        "보세요 https://github.com/o/r/pull/7 입니다", kind=forge.KIND_NONE)
    assert found == "https://github.com/o/r/pull/7"


# ---------------------------------------------------------------------------
# 진단 — 무엇이 꺼지는지 **말한다**
# ---------------------------------------------------------------------------


class _Cfg:
    """doctor 가 보는 최소 설정 대역."""

    class forge:                       # noqa: N801 — 설정 객체 모양을 흉내낸다
        kind = "none"
        base_url = ""
        token_ref = ""

    class run:                         # noqa: N801
        forge_token_ref = ""
        repo_resolver_gitlab_token_ref = ""


def test_doctor_skips_the_forge_check_and_says_what_is_lost():
    result = D.check_forge_token(_Cfg())
    assert result.status == D.STATUS_SKIP
    assert result.ok                              # SKIP 은 게이트를 막지 않는다 → exit 0
    assert "forge.kind 가 none" in result.message
    # 조용한 SKIP 금지 — 꺼지는 기능이 실제로 열거된다.
    for line in forge.DEGRADED_WITHOUT_FORGE:
        assert line in result.message


def test_the_degradation_list_is_not_empty_boilerplate():
    assert len(forge.DEGRADED_WITHOUT_FORGE) >= 3
    assert all(len(item) > 20 for item in forge.DEGRADED_WITHOUT_FORGE)


# ---------------------------------------------------------------------------
# 프롬프트 — 만들 수 없는 것을 만들라고 하지 않는다
# ---------------------------------------------------------------------------


def test_the_agent_prompt_says_there_is_no_change_request_api():
    from app import agent_runner

    class Cfg(_Cfg):
        pass

    prompt = agent_runner.build_prompt({"ticket": "ACME-1", "autonomy_mode": "A"}, Cfg())
    assert "forge 가 없다" in prompt
    assert "push" in prompt


def test_a_real_forge_prompt_is_unchanged():
    from app import agent_runner

    class Cfg(_Cfg):
        class forge:                   # noqa: N801
            kind = "gitlab"
            base_url = ""
            token_ref = "service/forge-token"

    prompt = agent_runner.build_prompt({"ticket": "ACME-1", "autonomy_mode": "A"}, Cfg())
    assert "MR" in prompt
    assert "forge 가 없다" not in prompt


# ---------------------------------------------------------------------------
# per-user 온보딩 — 발급할 수 없는 토큰을 요구하지 않는다
# ---------------------------------------------------------------------------


def test_joining_a_forgeless_deployment_does_not_demand_a_personal_pat():
    from app import user_schema as U

    answers = {"username": "alice", "jira_email": "alice@acme.example",
               "jira_token": "tok", "jira_account_id": "5b10a2844c20165700ede21g",
               "claude_setup_token": "tok", U.CONSENT_KEY: True,
               U.CONSENT_AT_KEY: "2026-08-25T09:00:00+09:00"}
    assert not U.validate_user_answers(answers, _Cfg()).errors

    class Gitlab(_Cfg):
        class forge:                   # noqa: N801
            kind = "gitlab"
            base_url = ""
            token_ref = "service/forge-token"

    # 반대로 forge 가 있는 배포에서는 여전히 필수다(느슨해지지 않았다).
    assert "forge_token" in [f.key for f in U.validate_user_answers(answers,
                                                                    Gitlab()).errors]


def test_the_onboarding_guide_says_there_is_nothing_to_issue():
    from app import user_schema as U

    guide = U.forge_guide(_Cfg())
    assert guide["kind"] == "none"
    assert "forge 가 없습니다" in guide["path"]
    assert guide["url"] == ""          # 없는 발급 페이지로 사람을 보내지 않는다


@pytest.mark.parametrize("kind", S.FORGE_KINDS)
def test_every_declared_kind_survives_the_config_loader(kind, tmp_path):
    """스키마가 허용하는 값은 로더도 받아야 한다(한쪽만 바꾸면 부팅에서 터진다)."""
    from app import config as C

    assert C._build_forge({"kind": kind}, {}).kind == kind
