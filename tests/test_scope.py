"""프로젝트 범위(scope) 단위테스트 — 정규화·상속·합집합 JQL·전 경로 게이트.

라이브 Jira/네트워크는 호출하지 않는다(대역만). 이 파일의 핵심 주장 셋:

    1. per-user ``scope.projects`` 가 **실제로 라우팅을 바꾼다**(저장만 되던 잠복 버그).
    2. 그 게이트가 **폴러·웹훅·상태 감시축 전 경로**에 일관되게 걸린다
       (:func:`test_no_ungated_account_mapping` 이 그것을 기계적으로 지킨다).
    3. 합집합이 비면 **JQL 을 던지지 않는다**(예전엔 project 절만 빠져 전 프로젝트를 긁었다).
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

from app import queue as q
from app import scope as SC
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.poller import Poller
from app.queue import Job, JobQueue
from app.registry import Registry, Scope, UserRecord
from app.scheduler import Scheduler
from app.status_watcher import StatusWatcher
from tests.conftest import make_config

_KST = timezone(timedelta(hours=9))
_FIXED_NOW = datetime(2026, 8, 10, 0, 0, 0, tzinfo=_KST)


def _fixed_clock():
    return _FIXED_NOW


def _issue(key, account_id="a1", status="해야 할 일",
           created="2026-08-10T10:00:00.000+0900", project=None):
    """이슈 대역. ``project`` 를 주면 ``fields.project.key`` 로 싣는다(없으면 키 접두사)."""
    fields = {
        "assignee": {"accountId": account_id},
        "status": {"name": status},
        "created": created,
        "updated": created,
        "components": [],
        "labels": [],
    }
    if project is not None:
        fields["project"] = {"key": project}
    return {"key": key, "fields": fields}


class FakeJira:
    def __init__(self, issues=(), issue_by_key=None):
        self._issues = list(issues)
        self._by_key = dict(issue_by_key or {})
        self.jqls: list = []

    def search_jql(self, jql, fields=None, max_results=50):
        self.jqls.append(jql)
        return {"issues": self._issues, "total": len(self._issues)}

    def get_issue(self, key, fields=None):
        return self._by_key.get(key)


# ---------------------------------------------------------------------------
# 정규화 · 인젝션 방어
# ---------------------------------------------------------------------------

def test_normalize_projects_accepts_string_and_list():
    assert SC.normalize_projects("PROJ, TEAM ,") == ["PROJ", "TEAM"]
    assert SC.normalize_projects(["PROJ", " TEAM "]) == ["PROJ", "TEAM"]
    assert SC.normalize_projects(None) == []


def test_normalize_projects_dedups_case_insensitively_keeping_first_spelling():
    assert SC.normalize_projects(["PROJ", "proj", "TEAM"]) == ["PROJ", "TEAM"]


@pytest.mark.parametrize("bad", [
    'PROJ") OR assignee is not EMPTY OR ("x',   # 따옴표+괄호 탈출 시도
    "PROJ OR TEAM",                             # 공백/연산자
    "PROJ-1",                                   # 하이픈(이슈 키지 프로젝트 키가 아니다)
    "1PROJ",                                    # 숫자로 시작
    'PR"OJ',                                    # 따옴표
    "PR\\OJ",                                   # 백슬래시
])
def test_invalid_project_keys_are_rejected_not_escaped(bad):
    assert SC.is_valid_project_key(bad) is False
    assert SC.normalize_projects([bad, "OK"]) == ["OK"]


def test_project_clause_quotes_and_never_carries_structure_chars():
    assert SC.project_clause(["PROJ", "TEAM"]) == 'project in ("PROJ", "TEAM")'
    assert SC.project_clause([]) == ""
    # 인젝션 시도가 섞여도 절에는 남지 않는다(이스케이프가 아니라 거부).
    clause = SC.project_clause(['PROJ") OR ("', "TEAM"])
    assert clause == 'project in ("TEAM")'
    assert "OR" not in clause


# ---------------------------------------------------------------------------
# 인스턴스 기본값 · 상속 · 합집합
# ---------------------------------------------------------------------------

def test_instance_projects_merges_primary_and_extras():
    cfg = make_config(project="PROJ")
    cfg.jira.projects = ["TEAM", "PROJ"]      # 대표 중복은 접힌다
    assert SC.instance_projects(cfg) == ["PROJ", "TEAM"]


def test_instance_projects_tolerates_legacy_config_without_projects_attr():
    cfg = make_config(project="PROJ")          # conftest 대역엔 projects 속성이 없다
    assert SC.instance_projects(cfg) == ["PROJ"]


def test_empty_user_scope_inherits_instance_default_not_unlimited():
    """빈 scope = '제한 없음' 이 아니라 **인스턴스 기본값 상속**(기존 registry 하위호환)."""
    legacy = UserRecord(username="old", jira_account_id="a1", enabled=True)
    assert legacy.scope.projects == []
    assert SC.user_projects(legacy, ["PROJ"]) == ["PROJ"]


def test_explicit_user_scope_wins_over_instance_default():
    user = UserRecord(username="u", jira_account_id="a1", enabled=True,
                      scope=Scope(projects=["TEAM"]))
    assert SC.user_projects(user, ["PROJ"]) == ["TEAM"]


def test_union_projects_covers_every_enabled_user():
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))          # 상속
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=True,
                          scope=Scope(projects=["TEAM"])))
    reg.upsert(UserRecord(username="u3", jira_account_id="a3", enabled=False,
                          scope=Scope(projects=["HIDDEN"])))                            # 비활성
    reg.upsert(UserRecord(username="u4", jira_account_id="", enabled=True,
                          scope=Scope(projects=["NOACCT"])))                            # 매칭 불가
    assert SC.union_projects(reg, make_config(project="PROJ")) == ["PROJ", "TEAM"]


def test_project_key_of_prefers_response_field_then_key_prefix():
    assert SC.project_key_of("TEAM-9", _issue("TEAM-9", project="TEAM")) == "TEAM"
    assert SC.project_key_of("TEAM-9", _issue("TEAM-9")) == "TEAM"
    assert SC.project_key_of("", {"key": "PROJ-1"}) == "PROJ"
    assert SC.project_key_of("nonsense", {}) == ""


def test_in_user_scope_is_false_when_nothing_declares_a_range():
    """범위를 확정할 수 없으면 통과시키지 않는다(조용히 넓어지느니 좁힌다)."""
    cfg = make_config(project="")
    user = UserRecord(username="u", jira_account_id="a1", enabled=True)
    assert SC.in_user_scope("PROJ-1", _issue("PROJ-1"), user, cfg) is False


# ---------------------------------------------------------------------------
# 폴러 — 합집합 JQL · 빈 합집합 차단 · 수신 후 게이트
# ---------------------------------------------------------------------------

def _wire_poller(issues=(), users=None, cfg=None, issue_by_key=None):
    reg = Registry()
    for rec in (users or [UserRecord(username="u1", jira_account_id="a1", enabled=True)]):
        reg.upsert(rec)
    cfg = cfg or make_config(concurrency_per_worker=5)
    gate = DedupGate()
    # 스케줄러에도 같은 게이트를 준다 — park(재배정 드롭) 시 dedup 해제가 실제로 일어나야
    # "나중에 다시 범위에 들어오면 재트리거" 가 성립한다.
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    jira = FakeJira(issues, issue_by_key)
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock)
    return reg, sch, disp, gate, jira, poller


def test_build_jql_uses_union_project_clause(isolated_state):
    users = [
        UserRecord(username="u1", jira_account_id="a1", enabled=True),                  # 상속 PROJ
        UserRecord(username="u2", jira_account_id="a2", enabled=True,
                   scope=Scope(projects=["TEAM"])),
    ]
    _, _, _, _, _, poller = _wire_poller(users=users)
    for jql in (poller.build_jql(), poller.build_assignee_change_jql()):
        assert jql.startswith('project in ("PROJ", "TEAM") AND ')


def test_build_jql_returns_none_when_union_empty(isolated_state, caplog):
    """합집합이 비면 JQL 자체를 만들지 않는다 — 그리고 왜 안 도는지 로그로 드러난다."""
    cfg = make_config(project="")   # 인스턴스 기본값 없음 + 사용자 scope 도 비어 있음
    _, _, _, _, jira, poller = _wire_poller(cfg=cfg)
    with caplog.at_level("WARNING", logger="jad.poller"):
        assert poller.build_jql() is None
        assert poller.build_assignee_change_jql() is None
    # 조용히 아무것도 안 하면 원인 추적이 불가능하다 — 무엇을 고쳐야 하는지까지 말한다.
    assert "감시할 프로젝트가 없어" in caplog.text
    assert "jira.project" in caplog.text
    assert "scope.projects" in caplog.text


def test_poll_once_blocked_when_union_empty_never_queries_jira(isolated_state, caplog):
    cfg = make_config(project="")
    _, _, _, _, jira, poller = _wire_poller(issues=[_issue("ANY-1", "a1")], cfg=cfg)
    with caplog.at_level("WARNING", logger="jad.poller"):
        assert poller.poll_once() == 0
    assert jira.jqls == []          # ⚠️ 전 프로젝트 스크레이핑이 아니라 **무질의**
    assert "감시할 프로젝트가 없어" in caplog.text


def test_poll_once_skips_ticket_outside_the_assignees_scope(isolated_state):
    """합집합 JQL 이 남의 프로젝트 티켓을 물어와도 담당자 범위 밖이면 디스패치하지 않는다."""
    users = [
        UserRecord(username="u1", jira_account_id="a1", enabled=True,
                   scope=Scope(projects=["PROJ"])),
        UserRecord(username="u2", jira_account_id="a2", enabled=True,
                   scope=Scope(projects=["TEAM"])),
    ]
    issues = [_issue("TEAM-1", "a1"), _issue("TEAM-2", "a2")]   # a1 은 TEAM 담당이 아니다
    _, sch, _, gate, _, poller = _wire_poller(issues=issues, users=users)
    assert poller.poll_once() == 1
    assert sch.jobs.get("TEAM-2").user == "u2"
    assert sch.jobs.get("TEAM-1") is None
    # 범위 밖은 claim 을 되돌린다 — 나중에 범위에 들어오면 재트리거될 수 있어야 한다.
    assert gate.is_claimed("TEAM-1") is False


def test_poll_once_still_dispatches_legacy_user_with_empty_scope(isolated_state):
    """scope 가 없던 시절 레코드는 인스턴스 기본 프로젝트를 그대로 받는다(하위호환)."""
    _, sch, _, _, _, poller = _wire_poller(issues=[_issue("PROJ-1", "a1")])
    assert poller.poll_once() == 1
    assert sch.jobs.get("PROJ-1").user == "u1"


def test_trigger_ticket_skips_out_of_scope(isolated_state):
    users = [UserRecord(username="u1", jira_account_id="a1", enabled=True,
                        scope=Scope(projects=["PROJ"]))]
    by_key = {"TEAM-1": _issue("TEAM-1", "a1"), "PROJ-1": _issue("PROJ-1", "a1")}
    _, sch, _, gate, _, poller = _wire_poller(users=users, issue_by_key=by_key)
    assert poller.trigger_ticket("TEAM-1") is False
    assert sch.jobs.get("TEAM-1") is None
    assert gate.is_claimed("TEAM-1") is False
    assert poller.trigger_ticket("PROJ-1") is True   # 범위 안은 그대로 통과


def test_drain_pending_drops_out_of_scope(isolated_state):
    """쿨다운 중 보관된 티켓도 드레인 때 같은 게이트를 통과해야 한다."""
    users = [UserRecord(username="u1", jira_account_id="a1", enabled=True,
                        scope=Scope(projects=["PROJ"]))]
    _, sch, _, gate, _, poller = _wire_poller(users=users)
    poller._llm_runner = lambda *a, **k: ""
    poller._repo_map_loader = lambda: ""
    gate.claim("TEAM-1")
    poller._add_pending("TEAM-1", _issue("TEAM-1", "a1"))
    assert poller.drain_pending_resolution() == 0
    assert poller.pending_keys() == []
    assert gate.is_claimed("TEAM-1") is False
    assert sch.jobs.get("TEAM-1") is None


def _park_or_redispatch(new_user_scope):
    """큐 대기 잡의 담당자를 u1→u2 로 바꿔 보고 결과 잡을 돌려준다(범위만 다르게)."""
    users = [
        UserRecord(username="u1", jira_account_id="a1", enabled=True,
                   scope=Scope(projects=["PROJ", "TEAM"])),
        UserRecord(username="u2", jira_account_id="a2", enabled=True,
                   scope=Scope(projects=new_user_scope)),
    ]
    _, sch, _, gate, _, poller = _wire_poller(users=users)
    gate.claim("TEAM-1")
    # tick 없이 **큐 대기** 상태로만 올린다(실행 중이면 핸드오프 경로라 판정이 흐려진다).
    sch.jobs.enqueue(Job(ticket="TEAM-1", user="u1", target_repos=["repoA"]))
    handled = poller._handle_reassignment("TEAM-1", _issue("TEAM-1", "a2"))
    return handled, sch.jobs.get("TEAM-1"), gate


def test_reassignment_to_out_of_scope_user_is_parked(isolated_state):
    """재배정이 per-user scope 를 우회하는 뒷문이 되지 않는다 — 범위 밖 Y 는 park."""
    handled, job, gate = _park_or_redispatch(["PROJ"])   # u2 는 TEAM 담당이 아니다
    assert handled is True                 # 감지는 한다(잡을 보존해야 하므로)
    assert job.user == "u1"                # Y 로 넘어가지 않았다
    assert job.status == q.CANCELLED and job.cancel_reason == q.CANCEL_REASSIGNED
    assert gate.is_claimed("TEAM-1") is False


def test_reassignment_to_in_scope_user_still_redispatches(isolated_state):
    """대조군 — 범위 안이면 예전처럼 Y 가 그대로 이어받는다(게이트만 달라졌다)."""
    handled, job, _ = _park_or_redispatch(["PROJ", "TEAM"])
    assert handled is True
    assert job.user == "u2"
    assert job.status != q.CANCELLED


# ---------------------------------------------------------------------------
# 상태 감시축(재오픈 = 재-디스패치)
# ---------------------------------------------------------------------------

class _ReopenJira:
    """``해야 할 일`` 재오픈 쿼리에만 응답하는 대역(다른 섹션은 빈 결과)."""

    def __init__(self, issues):
        self._issues = list(issues)
        self.queries: list = []

    def search_jql(self, jql, fields=None, max_results=50):
        self.queries.append(jql)
        if '"해야 할 일"' not in jql:
            return {"issues": [], "total": 0}
        out = [it for it in self._issues
               if "key in (" not in jql or f'"{it["key"]}"' in jql]
        return {"issues": out, "total": len(out)}


def test_reopen_skips_out_of_scope_assignee(isolated_state):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          scope=Scope(projects=["PROJ"])))
    cfg = make_config(concurrency_per_worker=5)
    gate = DedupGate()
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    gate.claim("TEAM-1")
    disp.enqueue("u1", Job(ticket="TEAM-1", user="u1", target_repos=["repoA"]))
    sch.cancel_job("TEAM-1")
    sch.report("TEAM-1", "취소됨")
    assert sch.jobs.get("TEAM-1").status == q.CANCELLED

    jira = _ReopenJira([_issue("TEAM-1", "a1", status="해야 할 일")])
    watcher = StatusWatcher(cfg, jira, gate, reg, disp)
    assert watcher.poll_once()["reopened"] == 0
    assert sch.jobs.get("TEAM-1").status == q.CANCELLED


def test_status_watcher_project_clause_uses_the_same_union(isolated_state):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          scope=Scope(projects=["TEAM"])))
    cfg = make_config()
    sch = Scheduler(cfg, JobQueue())
    watcher = StatusWatcher(cfg, FakeJira(), DedupGate(), reg, Dispatcher(reg, sch))
    assert watcher._project_clause() == 'project in ("TEAM") AND '


# ---------------------------------------------------------------------------
# 전 경로 일관성 — 매핑 지점이 하나뿐임을 기계적으로 지킨다
# ---------------------------------------------------------------------------

def test_no_ungated_account_mapping():
    """``get_by_account_id`` 는 :mod:`app.scope` 밖에서 호출되지 않는다.

    폴러만 고치고 웹훅·워처를 빠뜨리면 그쪽 경로로 범위 밖 티켓이 새어 들어온다 —
    실제로 이 버그가 그렇게 생겼다. 새 호출 경로가 매핑을 직접 하려 들면 여기서 깨진다.
    """
    app_dir = pathlib.Path(__file__).resolve().parent.parent / "app"
    offenders = []
    for path in sorted(app_dir.glob("*.py")):
        if path.name in ("scope.py", "registry.py"):
            continue    # 정의(registry) 와 유일한 게이트(scope)
        for num, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bget_by_account_id\s*\(", line):
                offenders.append(f"{path.name}:{num}")
    assert not offenders, (
        "범위 게이트를 우회하는 담당자 매핑이 있습니다 — app.scope.resolve_user_in_scope "
        "를 쓰세요: " + ", ".join(offenders)
    )


# ---------------------------------------------------------------------------
# 온보딩 — 기본 포함 여부 + 추가 프로젝트
# ---------------------------------------------------------------------------

def test_onboarding_include_default_without_extras_stays_inherited():
    """기본만 받겠다면 빈 목록으로 저장한다 — 기본이 늘어나면 자동으로 따라간다."""
    assert SC.resolve_onboarding_projects(["PROJ"], True, []) == []


def test_onboarding_include_default_with_extras_pins_both():
    assert SC.resolve_onboarding_projects(["PROJ"], True, "TEAM, OPS") == \
        ["PROJ", "TEAM", "OPS"]


def test_onboarding_exclude_default_keeps_only_extras():
    assert SC.resolve_onboarding_projects(["PROJ"], False, ["TEAM"]) == ["TEAM"]


def test_onboarding_empty_choice_is_rejected():
    with pytest.raises(SC.ScopeChoiceError):
        SC.resolve_onboarding_projects(["PROJ"], False, [])
