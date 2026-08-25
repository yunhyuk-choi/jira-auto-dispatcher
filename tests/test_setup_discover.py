"""Jira 인스턴스 조회(app/setup_discover.py) 단위테스트.

⚠️ **Jira 에 절대 실제로 붙지 않는다** — 클라이언트는 전부 대역(fake)으로 주입한다
(CI: ubuntu 에서 네트워크 없이 그대로 돈다).

이 모듈이 존재하는 이유가 곧 테스트가 지켜야 할 계약이다:
    - 커스텀필드는 **확신할 때만** 고른다(정확일치가 유일할 때). 애매하면 후보만 준다 —
      잘못 고른 필드 id 는 검증·진단을 통과한 뒤 운영에서 400 으로 터진다.
    - 설정에 적힌 id·상태 이름이 **이 인스턴스에 실재하는지** 검증한다(다른 조직의
      customfield id 가 예시 값 그대로 남는 것이 이 리포의 실제 사고였다).
    - 상태·전이는 **id 와 name 을 함께** 제안한다.
    - 토큰 값은 어디에도 실리지 않는다.
"""

from __future__ import annotations

import pytest

from app import config as C
from app import setup_discover as D
from app.jira_client import JiraError

# --- 대역 ---------------------------------------------------------------------

FIELDS = [
    {"id": "duedate", "name": "Due date", "custom": False, "schema": {"type": "date"}},
    {"id": "customfield_20001", "name": "시작날짜", "custom": True,
     "schema": {"type": "date"}},
    {"id": "customfield_20002", "name": "실제 시작일", "custom": True,
     "schema": {"type": "date"}},
    {"id": "customfield_20003", "name": "실제 종료 시각", "custom": True,
     "schema": {"type": "date"}},
    {"id": "customfield_20004", "name": "Sprint", "custom": True,
     "schema": {"type": "array"}},
]

PROJECT_STATUSES = [{
    "id": "10001", "name": "작업",
    "statuses": [
        {"id": "10000", "name": "해야 할 일", "statusCategory": {"key": "new"}},
        {"id": "3", "name": "진행 중", "statusCategory": {"key": "indeterminate"}},
        {"id": "10002", "name": "취소됨", "statusCategory": {"key": "done"}},
    ],
}]

TRANSITIONS = [
    {"id": "11", "name": "시작", "to": {"id": "3", "name": "진행 중",
                                       "statusCategory": {"key": "indeterminate"}}},
    {"id": "41", "name": "완료", "to": {"id": "10003", "name": "Done",
                                       "statusCategory": {"key": "done"}}},
]


class FakeJira:
    """조회 메서드만 흉내내는 대역(운영 경로는 건드리지 않는다)."""

    def __init__(self, *, fields=None, statuses=None, transitions=None,
                 labels=None, issues=None, raises=None):
        self.fields = FIELDS if fields is None else fields
        self.statuses = PROJECT_STATUSES if statuses is None else statuses
        self.transitions = TRANSITIONS if transitions is None else transitions
        self.labels = ["backend", "자동화_추적_해제"] if labels is None else labels
        self.issues = [{"key": "ACME-7"}] if issues is None else issues
        self.raises = raises or {}
        self.calls = []

    def _maybe_raise(self, name):
        self.calls.append(name)
        if name in self.raises:
            raise self.raises[name]

    def myself(self):
        self._maybe_raise("myself")
        return {"accountId": "5f0abc", "displayName": "감시봇",
                "emailAddress": "bot@acme.example", "active": True}

    def list_fields(self):
        self._maybe_raise("list_fields")
        return self.fields

    def project_statuses(self, key):
        self._maybe_raise("project_statuses")
        return self.statuses

    def list_transitions(self, key):
        self._maybe_raise("list_transitions")
        return self.transitions

    def list_labels(self):
        self._maybe_raise("list_labels")
        return {"labels": self.labels, "total": len(self.labels), "truncated": False}

    def search_jql_page(self, jql, **kwargs):
        self._maybe_raise("search_jql_page")
        return {"issues": self.issues}


def make_cfg(**jira_over):
    """조회 대상 설정(실제 파서로 만든다 — 대역 config 로는 파서 규칙이 빠진다)."""
    jira = {
        "base_url": "https://acme.atlassian.net", "project": "ACME",
        "watcher_token_file": "service/jira-token",
        "watcher_email": "bot@acme.example",
        "trigger_statuses": ["해야 할 일"], "cancel_statuses": ["취소됨"],
    }
    jira.update(jira_over)
    return C.load_config_from_dict({
        "role": "central",
        "consent": {"full_permissions": True},
        "deploy": {"profile": "local", "secrets_base_dir": "/tmp/jad-secrets"},
        "jira": jira,
    })


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    for name in ("HOST_DEPLOY_DIR", "SECRETS_DIR", "JIRA_WATCHER_EMAIL"):
        monkeypatch.delenv(name, raising=False)


# ---------------------------------------------------------------------------
# 커스텀필드 후보 추리기 — **자동 선택을 아끼는 것**이 계약이다
# ---------------------------------------------------------------------------


def test_unique_exact_name_match_is_selected():
    out = D.match_custom_field(FIELDS, "start_date")
    assert out["selected"] == "customfield_20001"
    assert out["reason"] == D.TIER_EXACT


def test_system_field_matches_by_name_too():
    """duedate 는 시스템 필드라 id 가 인스턴스마다 같지만, 판정 경로는 동일해야 한다."""
    out = D.match_custom_field(FIELDS, "due_date")
    assert out["selected"] == "duedate"
    assert out["candidates"][0]["custom"] is False


def test_partial_match_is_offered_but_never_selected():
    """'실제 종료 시각' 은 '실제 종료' 를 포함하지만 정확일치가 아니다 — 고르지 않는다."""
    out = D.match_custom_field(FIELDS, "actual_end")
    assert out["selected"] is None
    assert out["reason"] == "no_exact_match"
    assert [c["id"] for c in out["candidates"]] == ["customfield_20003"]
    assert out["candidates"][0]["tier"] == D.TIER_PARTIAL


def test_duplicate_exact_names_are_ambiguous_and_not_selected():
    """같은 이름의 필드가 여럿인 인스턴스가 실제로 있다(프로젝트별 커스텀필드)."""
    fields = FIELDS + [{"id": "customfield_30001", "name": "시작날짜", "custom": True}]
    out = D.match_custom_field(fields, "start_date")
    assert out["selected"] is None
    assert out["reason"] == "ambiguous"
    exact = [c for c in out["candidates"] if c["tier"] == D.TIER_EXACT]
    assert [c["id"] for c in exact] == ["customfield_20001", "customfield_30001"]


def test_no_candidate_at_all():
    out = D.match_custom_field([{"id": "customfield_1", "name": "무관한 필드"}],
                               "start_date")
    assert out["selected"] is None and out["candidates"] == []


def test_partial_match_does_not_steal_a_neighbouring_field():
    """'실제 시작일' 이 start_date 의 정확일치로 잡히면 안 된다(사고 시나리오)."""
    out = D.match_custom_field(FIELDS, "start_date")
    exact_ids = [c["id"] for c in out["candidates"] if c["tier"] == D.TIER_EXACT]
    assert exact_ids == ["customfield_20001"]


# ---------------------------------------------------------------------------
# 설정에 적힌 값의 **실재 검증** — 이 조회의 진짜 값어치
# ---------------------------------------------------------------------------


def test_configured_field_id_absent_from_instance_is_flagged():
    """다른 조직의 customfield id 가 남아 있으면 반드시 드러나야 한다."""
    cfg = make_cfg(custom_fields={"start_date": "customfield_10015"})
    section = D.discover_custom_fields(FakeJira(), cfg)
    assert section.status == D.STATUS_OK
    assert "start_date" in section.data["invalid_configured"]
    assert section.data["logical_keys"]["start_date"]["configured"]["exists"] is False
    assert "없습니다" in "\n".join(section.lines)
    assert section.hint


def test_example_default_ids_are_flagged_as_absent_too():
    """설정을 아예 안 준 경우의 '오늘의 기본값'도 남의 인스턴스 값이다."""
    section = D.discover_custom_fields(FakeJira(), make_cfg())
    configured = section.data["logical_keys"]["actual_start"]["configured"]
    assert configured["source"] == "default"
    assert configured["exists"] is False
    assert "actual_start" in section.data["invalid_configured"]


def test_unknown_status_name_is_flagged():
    """상태 이름이 어긋나면 폴러는 조용히 아무것도 못 찾는다 — 여기서 잡아야 한다."""
    cfg = make_cfg(trigger_statuses=["해야 할 일", "존재하지 않는 상태"])
    section = D.discover_statuses(FakeJira(), cfg)
    assert section.data["unknown"] == ["trigger_statuses: 존재하지 않는 상태"]
    assert section.hint


def test_status_name_whitespace_difference_is_resolved_with_the_actual_name():
    """'해야할일' 처럼 공백만 다르게 적어도 실제 이름·id 를 짚어 준다."""
    section = D.discover_statuses(FakeJira(), make_cfg(trigger_statuses=["해야할일"]))
    entry = section.data["configured"]["trigger_statuses"][0]
    assert entry["exists"] is True
    assert entry["actual_name"] == "해야 할 일" and entry["id"] == "10000"
    assert section.data["unknown"] == []


def test_configured_done_transition_id_absent_is_flagged():
    cfg = make_cfg(done_transition_id="999")
    section = D.discover_transitions(FakeJira(), cfg)
    assert section.data["configured"]["exists"] is False
    assert "없" in section.summary and section.hint


# ---------------------------------------------------------------------------
# 상태·전이는 **id 와 name 을 함께** 제안한다
# ---------------------------------------------------------------------------


def test_statuses_carry_both_id_and_name():
    section = D.discover_statuses(FakeJira(), make_cfg())
    entry = section.data["configured"]["trigger_statuses"][0]
    assert entry == {"name": "해야 할 일", "id": "10000", "exists": True,
                     "actual_name": "해야 할 일", "category": "new"}


def test_transitions_carry_id_name_and_target_status():
    section = D.discover_transitions(FakeJira(), make_cfg(), issue_key="ACME-1")
    assert section.data["issue_key"] == "ACME-1" and section.data["sampled"] is False
    assert section.data["transitions"][1] == {
        "id": "41", "name": "완료", "to_id": "10003", "to_name": "Done",
        "to_category": "done"}
    assert section.data["done_selected"]["id"] == "41"


def test_transition_sample_issue_is_picked_when_not_given():
    client = FakeJira()
    section = D.discover_transitions(client, make_cfg())
    assert section.data["sampled"] is True and section.data["issue_key"] == "ACME-7"
    assert "search_jql_page" in client.calls


def test_transition_is_skipped_when_the_project_has_no_issue():
    section = D.discover_transitions(FakeJira(issues=[]), make_cfg())
    assert section.status == D.STATUS_SKIP
    assert "--issue" in section.hint


def test_ambiguous_done_transition_is_not_selected():
    """done 전이가 여럿이고 이름도 안 맞으면 고르지 않는다(런타임과 같은 규율)."""
    transitions = [
        {"id": "51", "name": "보류", "to": {"id": "1", "name": "Parked",
                                           "statusCategory": {"key": "done"}}},
        {"id": "52", "name": "폐기", "to": {"id": "2", "name": "Dropped",
                                           "statusCategory": {"key": "done"}}},
    ]
    section = D.discover_transitions(FakeJira(transitions=transitions), make_cfg(),
                                     issue_key="ACME-1")
    assert section.data["done_selected"] == {}
    assert "모호" in section.hint


def test_single_done_category_transition_is_selected_without_a_name_match():
    transitions = [{"id": "77", "name": "Ship it",
                    "to": {"id": "9", "name": "Shipped",
                           "statusCategory": {"key": "done"}}}]
    section = D.discover_transitions(FakeJira(transitions=transitions), make_cfg(),
                                     issue_key="ACME-1")
    assert section.data["done_selected"]["id"] == "77"
    assert section.data["done_selected"]["reason"] == "single_done_category"


# ---------------------------------------------------------------------------
# 라벨 — 조회보다 **안내**가 중요하다
# ---------------------------------------------------------------------------


def test_labels_section_carries_the_optout_rule_not_just_a_list():
    section = D.discover_labels(FakeJira(), make_cfg())
    assert section.data["guide"] == D.OPTOUT_LABEL_GUIDE
    assert "자동화에서 제외" in "\n".join(section.lines)


def test_absent_optout_label_is_not_an_error():
    """Jira 라벨은 붙이는 순간 생성된다 — 목록에 없어도 정상이다."""
    section = D.discover_labels(FakeJira(labels=["backend"]), make_cfg())
    assert section.status == D.STATUS_OK
    assert section.data["optout_labels"][0]["exists"] is False


# ---------------------------------------------------------------------------
# 계정
# ---------------------------------------------------------------------------


def test_account_reports_the_account_id():
    section = D.discover_account(FakeJira())
    assert section.data["account_id"] == "5f0abc"
    assert "accountId: 5f0abc" in "\n".join(section.lines)


# ---------------------------------------------------------------------------
# 실행기 — 부분 실패가 나머지를 죽이지 않는다
# ---------------------------------------------------------------------------


def test_discover_runs_every_section_in_order():
    result = D.discover(make_cfg(), client=FakeJira())
    assert [s.name for s in result.sections] == list(D.SECTION_ORDER)
    assert result.ok


def test_only_subset_runs_just_that_section():
    result = D.discover(make_cfg(), client=FakeJira(), only=("labels",))
    assert [s.name for s in result.sections] == ["labels"]


def test_unknown_section_name_raises():
    with pytest.raises(D.DiscoveryError):
        D.discover(make_cfg(), client=FakeJira(), only=("nope",))


def test_one_failed_section_does_not_stop_the_others():
    client = FakeJira(raises={"list_fields": JiraError("boom", status_code=403)})
    result = D.discover(make_cfg(), client=client)
    assert result.ok is False
    assert result.get("custom_fields").status == D.STATUS_FAIL
    assert result.get("labels").status == D.STATUS_OK


def test_sections_are_skipped_when_the_client_cannot_be_built(tmp_path):
    """토큰·이메일이 없으면 조회 자체가 불가능하다 — 실패가 아니라 건너뜀이다."""
    cfg = make_cfg()
    cfg.jira.watcher_email = ""
    result = D.discover(cfg, project_dir=str(tmp_path))
    assert result.ok                       # 건너뜀은 게이트를 막지 않는다
    assert all(s.status == D.STATUS_SKIP for s in result.sections)


def test_statuses_skipped_without_a_project_key():
    cfg = make_cfg()
    cfg.jira.project = ""
    section = D.discover_statuses(FakeJira(), cfg)
    assert section.status == D.STATUS_SKIP


# ---------------------------------------------------------------------------
# 제안값 — 확정된 것만, id·name 을 함께
# ---------------------------------------------------------------------------


def test_suggested_answers_only_contain_what_was_determined():
    result = D.discover(make_cfg(), client=FakeJira())
    suggested = result.suggested_answers()
    # actual_end 는 정확일치가 없어 확정하지 않았다 — 제안에 들어가면 안 된다.
    assert "actual_end" not in suggested["jira.custom_fields"]
    assert suggested["jira.custom_fields"]["start_date"] == "customfield_20001"
    assert suggested["jira.trigger_statuses"] == [{"id": "10000", "name": "해야 할 일"}]
    assert suggested["jira.done_transition_names"] == [{"id": "41", "name": "완료"}]


def test_suggested_answers_round_trip_through_validate_and_the_parser():
    """제안값이 검증기·파서를 그대로 통과해야 한다(제안이 곧 답변이 되므로)."""
    from app import setup_validate as V

    result = D.discover(make_cfg(), client=FakeJira())
    answers = {
        "consent.full_permissions": True,
        "consent.accepted_at": "2026-08-25T09:00:00+09:00",
        "deploy.profile": "local", "deploy.secrets_base_dir": "/run/secrets",
        "forge.kind": "gitlab", "forge.token_ref": "service/forge-token",
        "jira.base_url": "https://acme.atlassian.net", "jira.project": "ACME",
        "jira.watcher_token_file": "service/jira-token",
        "jira.watcher_email": "bot@acme.example",
        "notifier.provider": "none",
        "webhook.enabled": True, "webhook.secret_ref": "service/jira-webhook",
        **result.suggested_answers(),
    }
    validated = V.validate_answers(answers)
    assert validated.ok, validated.format_text()

    cfg = C.load_config_from_dict({
        "role": "central",
        "jira": {"base_url": "https://acme.atlassian.net", "project": "ACME",
                 "watcher_token_file": "t",
                 "trigger_statuses": answers["jira.trigger_statuses"],
                 "done_transition_names": answers["jira.done_transition_names"]},
        "secrets": {"base_dir": "/run/secrets"},
    })
    assert cfg.jira.trigger_statuses == ["해야 할 일"]        # 소비처는 이름만 본다
    assert cfg.jira.status_ids == {"해야 할 일": "10000"}      # id 는 곁에 남는다
    assert cfg.jira.done_transition_id == "41"                # 이름에 달린 id 를 쓴다


def test_suggested_block_is_pasteable_yaml():
    """사람이 읽는 출력의 '확정된 값' 블록은 그대로 붙여넣을 수 있어야 한다."""
    import yaml

    result = D.discover(make_cfg(), client=FakeJira())
    text = result.format_text()
    start = text.index("jira:")
    block = "\n".join(ln[2:] for ln in text[start - 2:].split("\n")
                      if ln.startswith("  ") and not ln.strip().startswith("("))
    loaded = yaml.safe_load(block)
    assert loaded["jira"]["trigger_statuses"] == [{"id": "10000", "name": "해야 할 일"}]
    assert loaded["jira"]["custom_fields"]["due_date"] == "duedate"


def test_nothing_determined_says_so_instead_of_guessing():
    client = FakeJira(fields=[], statuses=[], transitions=[])
    result = D.discover(make_cfg(trigger_statuses=[], cancel_statuses=[]),
                        client=client)
    assert result.suggested_answers() == {}
    assert "확정할 수 있는 값이 없었습니다" in result.format_text()


# ---------------------------------------------------------------------------
# 시크릿 규율
# ---------------------------------------------------------------------------


def test_no_token_ever_appears_in_the_output(tmp_path):
    """토큰은 대역 뒤에 있어 출력 경로에 닿지 않는다 — 그 사실을 회귀로 못박는다."""
    import json

    token = "ATATT-SUPERSECRET-TOKEN"
    cfg = make_cfg()
    cfg.jira.watcher_token_file = "service/jira-token"
    result = D.discover(cfg, client=FakeJira())
    blob = result.format_text() + json.dumps(result.to_dict(), ensure_ascii=False)
    assert token not in blob
