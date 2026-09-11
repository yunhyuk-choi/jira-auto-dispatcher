"""동의 출처(app/setup_consent.py) 단위테스트 — **자기 승인을 막는 것이 계약이다.**

3차 여정 리허설 실측: 온보딩 **서브 에이전트가 ``consent.full_permissions: true`` 를
스스로 세팅**해 게이트를 통과했다. 룰북이 "대신 눌러 주지 마라"라고 하면서 같은 문서에서
"서브에겐 사용자 채널이 없다"고도 했기 때문이다 — 양립 불가한 두 지시 앞에서 서브는
진행 쪽을 택했다. 이 파일은 그 구멍이 **닫혀 있는지**를 양쪽으로 본다:

    - 서브 경로에서 동의를 **만들 수 없다**(주체 게이트 · TTY 게이트).
    - 사람 동의가 전달됐을 때는 **통과한다**(사람 채널 · 상위 중계).
    - 답변 파일의 불리언 하나로는 통과하지 못한다(증서 게이트).
"""

from __future__ import annotations

import json

import pytest

from app import setup_consent as C
from app import setup_schema as S
from app import setup_validate as V


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

SUBAGENT_ENV = {C.ACTOR_ENV: "subagent"}
HUMAN_ENV: dict = {}


def _replies(*answers):
    """스크립트된 입력기(대화형 채널 대역)."""
    queue = list(answers)
    return lambda _prompt="": queue.pop(0)


def _silent(*_args, **_kwargs):
    """고지 출력 대역(테스트 출력을 더럽히지 않는다)."""


def _human_record(**kwargs) -> C.ConsentRecord:
    base = dict(full_permissions=True, accepted_at="2026-09-03T10:00:00+09:00",
                channel=C.CHANNEL_HUMAN, granted_by="installer@acme.example")
    base.update(kwargs)
    return C.ConsentRecord(**base)


# ---------------------------------------------------------------------------
# 주체 게이트 — 서브 에이전트는 동의를 만들 수 없다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", ["subagent", "sub-agent", "SubAgent", "agent",
                                      "sub", "worker"])
def test_declared_subagents_are_recognised(declared):
    assert C.is_subagent({C.ACTOR_ENV: declared}) is True


@pytest.mark.parametrize("declared", ["", "human", "installer", "orchestrator"])
def test_other_actors_are_not_treated_as_subagents(declared):
    """선언이 없거나 다른 값이면 여기서 막지 않는다 — 추측으로 사람을 막지 않는다."""
    assert C.is_subagent({C.ACTOR_ENV: declared}) is False


def test_subagent_cannot_create_consent_interactively_even_with_a_tty():
    """TTY 가 있어도(있는 척해도) 서브 경로면 거부 — 주체 게이트가 먼저다."""
    with pytest.raises(C.ConsentError) as exc:
        C.grant_interactive(granted_by="누군가", isatty=True,
                            prompt=_replies("동의합니다"), show=_silent,
                            env=SUBAGENT_ENV)
    assert "서브 에이전트는 동의를 만들 수 없습니다" in str(exc.value)
    assert "--request" in str(exc.value)          # 무엇을 대신 하라고 말해 준다


def test_subagent_cannot_relay_consent_either():
    """중계도 막는다 — 서브가 "사용자가 그랬다"고 스스로 주장하는 것이 곧 자기 승인이다."""
    with pytest.raises(C.ConsentError) as exc:
        C.grant_relay(granted_by="사용자", statement="풀 퍼미션으로 돌려도 좋습니다",
                      relayed_by="onboarding-sub", env=SUBAGENT_ENV)
    assert "서브 에이전트는 동의를 만들 수 없습니다" in str(exc.value)


def test_headless_session_without_a_tty_cannot_create_human_consent():
    """주체를 선언하지 않은 헤드리스 세션은 **TTY 부재**가 막는다(두 번째 장치)."""
    with pytest.raises(C.ConsentError) as exc:
        C.grant_interactive(granted_by="누군가", isatty=False,
                            prompt=_replies("동의합니다"), show=_silent, env=HUMAN_ENV)
    assert "이 채널에는 사람이 없습니다" in str(exc.value)


# ---------------------------------------------------------------------------
# 사람 채널 — 통과하는 쪽
# ---------------------------------------------------------------------------


def test_human_at_a_terminal_can_consent():
    record = C.grant_interactive(isatty=True,
                                 prompt=_replies("김설치", "동의합니다"),
                                 show=_silent, now=lambda: "2026-09-03T10:00:00+09:00",
                                 env=HUMAN_ENV)
    assert record.full_permissions is True
    assert record.channel == C.CHANNEL_HUMAN
    assert record.granted_by == "김설치"
    assert record.relayed is False


def test_english_confirmation_phrase_also_works():
    """한글 입력이 곤란한 콘솔 대비 — 대소문자는 무시한다."""
    record = C.grant_interactive(granted_by="installer", isatty=True,
                                 prompt=_replies("i agree"), show=_silent,
                                 env=HUMAN_ENV)
    assert record.full_permissions is True


@pytest.mark.parametrize("typed", ["", "y", "yes", "네", "동의", "동의합니다만"])
def test_a_wrong_confirmation_phrase_records_nothing(typed):
    with pytest.raises(C.ConsentError) as exc:
        C.grant_interactive(granted_by="installer", isatty=True,
                            prompt=_replies(typed), show=_silent, env=HUMAN_ENV)
    assert "확인 문구가 일치하지 않아" in str(exc.value)


def test_the_disclosure_is_actually_shown_to_the_human():
    """무엇에 동의하는지 보여주지 않고 받은 동의는 동의가 아니다."""
    shown: list = []
    C.grant_interactive(granted_by="installer", isatty=True,
                        prompt=_replies("동의합니다"), show=shown.append,
                        env=HUMAN_ENV)
    text = "\n".join(shown)
    assert "사람의 매 단계 승인 없이" in text
    assert "SECURITY.md" in text


# ---------------------------------------------------------------------------
# 중계 채널 — 상위가 사람에게서 받아 전달
# ---------------------------------------------------------------------------


def test_orchestrator_can_relay_a_human_consent():
    record = C.grant_relay(granted_by="yh.choi@example.com",
                           statement="풀 퍼미션으로 돌려도 좋습니다. 위험은 이해했습니다.",
                           relayed_by="orchestrator",
                           now=lambda: "2026-09-03T10:00:00+09:00", env=HUMAN_ENV)
    assert record.channel == C.CHANNEL_RELAY
    assert record.relayed is True
    assert record.granted_by == "yh.choi@example.com"
    assert record.relayed_by == "orchestrator"
    assert "orchestrator" in record.describe() and "중계" in record.describe()


@pytest.mark.parametrize("statement", ["", "   ", "yes", "true", "OK", "동의",
                                       "<사용자가 실제로 한 말 그대로>"])
def test_relay_rejects_machine_boilerplate_as_a_users_words(statement):
    """원문 자리에 기계 상투어·자리표시자가 오면 그건 중계가 아니라 자기 승인이다."""
    with pytest.raises(C.ConsentError):
        C.grant_relay(granted_by="사용자", statement=statement,
                      relayed_by="orchestrator", env=HUMAN_ENV)


@pytest.mark.parametrize("who,relay", [("", "orchestrator"), ("사용자", ""),
                                       ("<사용자 이름>", "orchestrator")])
def test_relay_requires_both_identities(who, relay):
    with pytest.raises(C.ConsentError):
        C.grant_relay(granted_by=who, statement="풀 퍼미션으로 돌려도 좋습니다",
                      relayed_by=relay, env=HUMAN_ENV)


# ---------------------------------------------------------------------------
# 동의 요청서 — 서브가 **빈손으로 멈추지 않는다**
# ---------------------------------------------------------------------------


def test_a_subagent_can_always_build_a_consent_request(tmp_path):
    """서브가 할 수 있는 유일한 동의 관련 동작 — 부작용이 없어야 한다."""
    payload = C.consent_request(project_dir=str(tmp_path))
    assert payload["kind"] == "consent_request"
    assert payload["blocked_key"] == "consent.full_permissions"
    assert payload["disclosure"]                      # 무엇에 동의하는지가 실려 있다
    assert "--relay" in payload["relay_command"]      # 상위가 실행할 명령이 실려 있다
    assert not list(tmp_path.iterdir())               # 아무것도 쓰지 않았다


# ---------------------------------------------------------------------------
# 증서 파일 입출력
# ---------------------------------------------------------------------------


def test_record_roundtrips_through_the_file(tmp_path):
    saved = C.save_record(_human_record(), project_dir=str(tmp_path))
    assert saved.endswith(C.RECORD_FILENAME)
    assert C.load_record(project_dir=str(tmp_path)) == _human_record()


def test_no_record_is_not_an_error(tmp_path):
    assert C.load_record(project_dir=str(tmp_path)) is None


@pytest.mark.parametrize("payload", [
    {"full_permissions": True, "channel": "손으로적음"},     # 모르는 채널
    {"full_permissions": "true", "channel": C.CHANNEL_HUMAN},  # 문자열 true
    {"full_permissions": True, "channel": C.CHANNEL_HUMAN, "version": 99},
])
def test_a_hand_made_record_is_refused_loudly(tmp_path, payload):
    """깨진·손으로 만든 증서를 **조용히 없는 셈 치지 않는다**(그러면 다음 오류가 엉뚱해진다)."""
    (tmp_path / C.RECORD_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(C.ConsentError):
        C.load_record(project_dir=str(tmp_path))


def test_a_broken_json_record_is_refused_loudly(tmp_path):
    (tmp_path / C.RECORD_FILENAME).write_text("{ not json", encoding="utf-8")
    with pytest.raises(C.ConsentError):
        C.load_record(project_dir=str(tmp_path))


def test_the_statement_never_reaches_config_yaml():
    """설정 파일은 컨테이너로 마운트된다 — 사람의 원문은 증서에만 둔다."""
    record = C.grant_relay(granted_by="사용자", statement="비밀스러운 사담이 섞인 원문",
                           relayed_by="orchestrator", env=HUMAN_ENV)
    values = record.config_values()
    assert "비밀스러운" not in json.dumps(values, ensure_ascii=False)
    assert values["consent.channel"] == C.CHANNEL_RELAY
    assert values["consent.relayed_by"] == "orchestrator"


# ---------------------------------------------------------------------------
# 증서 게이트(순수 술어)
# ---------------------------------------------------------------------------


def test_answering_true_without_a_record_is_a_problem():
    problem = C.attestation_problem(True, None)
    assert problem is not None and problem.code == C.PROBLEM_MISSING


def test_a_record_that_says_no_beats_an_answer_that_says_yes():
    problem = C.attestation_problem(True, _human_record(full_permissions=False))
    assert problem is not None and problem.code == C.PROBLEM_DENIED


def test_consent_to_an_older_disclosure_does_not_carry_over():
    problem = C.attestation_problem(True, _human_record(disclosure_version="0"))
    assert problem is not None and problem.code == C.PROBLEM_STALE_DISCLOSURE


def test_a_human_record_passes():
    assert C.attestation_problem(True, _human_record()) is None


@pytest.mark.parametrize("answered", [None, False, "true"])
def test_not_consenting_is_someone_elses_check(answered):
    """"동의 안 함"은 여기가 아니라 _check_consent 의 몫 — 메시지가 갈리면 안 된다."""
    assert C.attestation_problem(answered, None) is None


# ---------------------------------------------------------------------------
# 검증기와의 이음매
# ---------------------------------------------------------------------------

_ANSWERS = {
    "consent": {"full_permissions": True, "accepted_at": "2026-08-25T09:00:00+09:00"},
    "deploy": {"profile": "cloud_vm", "secrets_base_dir": "/run/secrets"},
    "forge": {"kind": "gitlab", "token_ref": "service/forge-token"},
    "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
             "trigger_statuses": ["To Do"],
             "watcher_token_file": "service/jira-token",
             "watcher_email": "bot@acme.example"},
    "notifier": {"provider": "none"},
    "webhook": {"enabled": True, "secret_ref": "service/jira-webhook"},
    "run": {"dlc_meta_repo_url": "https://git.example.com/acme/dlc-meta.git"},
}


def test_validate_blocks_a_self_approved_consent():
    """답변 파일에 true 를 적는 것만으로는 통과하지 못한다(C1 의 본체)."""
    result = V.validate_answers(_ANSWERS, require_attestation=True, attestation=None)
    assert not result.ok
    assert [f.code for f in result.errors] == [V.CODE_CONSENT_UNATTESTED]


def test_validate_passes_when_a_human_consented():
    result = V.validate_answers(_ANSWERS, require_attestation=True,
                                attestation=_human_record())
    assert result.ok
    assert not [f for f in result.warnings if f.code == V.CODE_CONSENT_RELAYED]


def test_validate_passes_but_says_so_loudly_when_the_consent_was_relayed():
    relayed = C.grant_relay(granted_by="사용자", statement="풀 퍼미션으로 돌려도 좋습니다",
                            relayed_by="orchestrator", env=HUMAN_ENV)
    result = V.validate_answers(_ANSWERS, require_attestation=True, attestation=relayed)
    assert result.ok                                   # 정당한 경로다 — 막지 않는다
    relayed_warnings = [f for f in result.warnings if f.code == V.CODE_CONSENT_RELAYED]
    assert len(relayed_warnings) == 1                  # 그러나 매번 드러낸다
    assert "주장" in relayed_warnings[0].hint


def test_library_callers_without_attestation_are_unchanged():
    """per-user 온보딩처럼 증서 개념이 없는 소비처는 그대로 돈다(하위호환)."""
    assert V.validate_answers(_ANSWERS).ok


# ---------------------------------------------------------------------------
# 상수 3각 정합(스키마 ↔ 동의 모듈)
# ---------------------------------------------------------------------------


def test_channel_names_do_not_drift_between_schema_and_consent():
    assert S.CONSENT_CHANNELS == C.CHANNELS
    assert S.CONSENT_CHANNEL_CHOICES == ("",) + C.CHANNELS
    field = S.get_field("consent.channel")
    assert field is not None and set(C.CHANNELS) <= set(field.choices)
