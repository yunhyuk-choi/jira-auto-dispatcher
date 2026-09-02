"""poller 단위테스트 — JQL 구성·매핑·claim·repo 해석·watermark(라이브 금지)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from app import state
from app import queue as q
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.poller import Poller, resolve_target_repos
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config

# 결정적 테스트용 고정 시계(KST 자정) — watermark 초기 시드가 이 값이 되도록 주입.
_KST = timezone(timedelta(hours=9))
_FIXED_NOW = datetime(2026, 8, 10, 0, 0, 0, tzinfo=_KST)


def _fixed_clock():
    return _FIXED_NOW


class FakeJira:
    def __init__(self, issues):
        self._issues = issues
        self.last_jql = None

    def search_jql(self, jql, fields=None, max_results=50):
        self.last_jql = jql
        return {"issues": self._issues, "total": len(self._issues)}


def _issue(key, account_id, status="해야 할 일", created="2026-08-10T10:00:00.000+0900",
           components=None, labels=None):
    return {
        "key": key,
        "fields": {
            "assignee": {"accountId": account_id},
            "status": {"name": status},
            "created": created,
            "components": [{"name": c} for c in (components or [])],
            "labels": labels or [],
        },
    }


class RoutingFakeJira:
    """(A) created 쿼리와 (B) 담당자-변경 쿼리에 서로 다른 이슈를 돌려주는 페이크.

    JQL에 ``CHANGED TO`` 가 있으면 (B) 담당자-변경 결과를, 아니면 (A) created
    결과를 반환한다. 실행된 모든 JQL을 ``jqls`` 에 기록한다(합집합·정렬 검증용).
    """

    def __init__(self, created_issues, assignee_issues):
        self._created = created_issues
        self._assignee = assignee_issues
        self.jqls = []

    def search_jql(self, jql, fields=None, max_results=50):
        self.jqls.append(jql)
        issues = self._assignee if "CHANGED TO" in jql else self._created
        return {"issues": issues, "total": len(issues)}


def _fractalize(cfg):
    """이 배포는 영구 프랙탈-ON — poller 는 항상 센트럴 sink 로 방출한다(fractal-OFF 은퇴).

    ``central_active`` 가 참이 되도록 플래그 + 지속 stream-json 세션 전제를 세운다.
    (레거시 sink 없는 poller 배선은 더 이상 존재하지 않는다.)
    """
    cfg.run.fractal_central = True
    cfg.run.persistent_session = True
    cfg.run.output_format = "stream-json"
    cfg.run.input_format = "stream-json"
    return cfg


def _wire_routing(created_issues, assignee_issues, repo_map=None):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          per_repo={"portal-frontend": "A"}))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=False))  # 비활성
    cfg = _fractalize(make_config(concurrency_per_worker=5, repo_map=repo_map or {}))
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    jira = RoutingFakeJira(created_issues, assignee_issues)
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock,
                    central_sink=_FakeCentralSink())
    return reg, sch, disp, gate, jira, poller


def _wire(issues, repo_map=None, per_user=5):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          per_repo={"portal-frontend": "A"}))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=False))  # 비활성
    sch = Scheduler(make_config(concurrency_per_worker=per_user, repo_map=repo_map or {}), JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    cfg = _fractalize(make_config(concurrency_per_worker=per_user, repo_map=repo_map or {}))
    # 고정 시계 주입 → watermark 최초 시드가 결정적(2026-08-10T00:00, KST)이라
    # 이후 생성분(10:00 등)은 정상적으로 watermark를 전진시킨다.
    # 프랙탈-ON: 방출은 센트럴 sink 주입(record_fractal_job 이 같은 store 에 queued 로 기록).
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp, clock=_fixed_clock,
                    central_sink=_FakeCentralSink())
    return reg, sch, disp, gate, poller


def test_resolve_target_repos_maps_components_and_labels():
    repo_map = {"portal-frontend": "portal-frontend", "be": ["portal-backend"]}
    issue = _issue("PROJ-1", "a1", components=["portal-frontend"], labels=["be", "unknown"])
    assert resolve_target_repos(issue, repo_map) == ["portal-backend", "portal-frontend"]
    assert resolve_target_repos(issue, {}) == []   # 미해석


def test_build_jql_contains_enabled_ids_and_status(isolated_state):
    _, _, _, _, poller = _wire([])
    jql = poller.build_jql()
    assert 'assignee in ("a1")' in jql        # enabled만
    assert '"a2"' not in jql                    # 비활성 제외
    assert 'status in ("해야 할 일")' in jql
    assert jql.endswith("ORDER BY created ASC")


def test_build_jql_none_when_no_enabled(isolated_state):
    reg = Registry()  # 빈 레지스트리
    sch = Scheduler(make_config(), JobQueue())
    poller = Poller(make_config(), FakeJira([]), DedupGate(), reg, Dispatcher(reg, sch))
    assert poller.build_jql() is None


def test_poll_once_dispatches_enabled_and_skips_disabled(isolated_state):
    repo_map = {"portal-frontend": "portal-frontend"}
    reg, sch, disp, gate, poller = _wire(
        [_issue("PROJ-1", "a1", components=["portal-frontend"]),
         _issue("PROJ-2", "a2"),                     # 비활성 유저 → skip
         _issue("PROJ-3", "unknown-acc")],           # 미등록 → skip
        repo_map=repo_map)
    n = poller.poll_once()
    assert n == 1
    j = sch.jobs.get("PROJ-1")
    assert j is not None and j.user == "u1"
    assert j.target_repos == ["portal-frontend"]
    assert j.autonomy_mode == "A"                    # per_repo 오버라이드 반영
    assert j.branch == "auto/PROJ-1"
    assert sch.jobs.get("PROJ-2") is None
    # 미매핑 티켓은 claim 되돌림(재트리거 가능)
    assert gate.is_claimed("PROJ-2") is False
    assert gate.is_claimed("PROJ-3") is False


def test_poll_once_dedup_on_second_run(isolated_state):
    _, sch, _, _, poller = _wire([_issue("PROJ-1", "a1")])
    assert poller.poll_once() == 1
    assert poller.poll_once() == 0                    # 이미 claim → 중복 흡수


def test_poll_once_advances_watermark(isolated_state):
    _, _, _, _, poller = _wire([_issue("PROJ-1", "a1", created="2026-08-10T10:00:00.000+0900")])
    poller.poll_once()
    assert poller.watermark == "2026-08-10T10:00:00.000+0900"
    assert state.load_watermark() == poller.watermark


def test_resolve_user_disabled_returns_none(isolated_state):
    reg, _, _, _, poller = _wire([])
    assert poller.resolve_user(_issue("PROJ-1", "a1")).username == "u1"
    assert poller.resolve_user(_issue("PROJ-2", "a2")) is None   # 비활성


# --- watermark 최초 초기화(하드닝: 기존 To-Do stampede 방지) ---


def test_watermark_initialized_to_now_when_absent(isolated_state):
    """최초 실행(watermark 부재) 시 주입 clock의 now로 초기화하고 영속한다."""
    assert state.load_watermark() is None  # 사전 상태: 없음
    reg = Registry()
    sch = Scheduler(make_config(), JobQueue())
    poller = Poller(make_config(), FakeJira([]), DedupGate(), reg,
                    Dispatcher(reg, sch), clock=_fixed_clock)
    # now(주입 clock)의 ISO8601로 시드되고 state에도 저장된다.
    assert poller.watermark == _FIXED_NOW.isoformat()
    assert state.load_watermark() == _FIXED_NOW.isoformat()


def test_watermark_now_seed_gates_out_older_todo(isolated_state):
    """now 시드 이후, build_jql에 'created > now' 하한 절이 들어가 stampede를 막는다."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    poller = Poller(make_config(), FakeJira([]), DedupGate(), reg,
                    Dispatcher(reg, sch), clock=_fixed_clock)
    jql = poller.build_jql()
    assert 'created > "2026-08-10 00:00"' in jql


def test_watermark_existing_is_not_overwritten(isolated_state):
    """이미 watermark가 있으면 now로 덮어쓰지 않는다(진행 커서 보존)."""
    state.save_watermark("2026-01-01T00:00:00+09:00")
    reg = Registry()
    sch = Scheduler(make_config(), JobQueue())
    poller = Poller(make_config(), FakeJira([]), DedupGate(), reg,
                    Dispatcher(reg, sch), clock=_fixed_clock)
    assert poller.watermark == "2026-01-01T00:00:00+09:00"
    assert state.load_watermark() == "2026-01-01T00:00:00+09:00"


def test_watermark_default_now_uses_resume_timezone(isolated_state):
    """clock 미주입이면 resume.timezone 기준 now로 시드된다(파싱 가능한 ISO)."""
    from types import SimpleNamespace

    cfg = make_config()
    cfg.resume = SimpleNamespace(timezone="Asia/Seoul")
    reg = Registry()
    sch = Scheduler(cfg, JobQueue())
    poller = Poller(cfg, FakeJira([]), DedupGate(), reg, Dispatcher(reg, sch))
    assert poller.watermark is not None
    # ISO8601로 파싱 가능해야 한다(JQL 변환 _jql_time의 전제).
    datetime.fromisoformat(poller.watermark)
    assert state.load_watermark() == poller.watermark


# --- (B) 담당자-변경 트리거축 -------------------------------------------------


def test_build_assignee_change_jql_uses_changed_to_not_updated(isolated_state):
    """(B) JQL은 changelog의 'assignee CHANGED TO (...)' + now 시드 AFTER를 쓴다.

    상태/댓글 변경엔 트리거되지 않도록 **updated 기준 필터를 쓰지 않는다.**
    """
    _, _, _, _, _, poller = _wire_routing([], [])
    jql = poller.build_assignee_change_jql()
    assert 'assignee in ("a1")' in jql              # enabled만
    assert '"a2"' not in jql                         # 비활성 제외
    assert 'status in ("해야 할 일")' in jql
    assert 'assignee CHANGED TO ("a1")' in jql       # 담당자-변경 이벤트축
    assert 'AFTER "2026-08-10 00:00"' in jql         # assignee_watermark now 시드
    assert "updated >" not in jql                    # updated 필터 없음(상태/댓글 무시)
    assert jql.endswith("ORDER BY updated ASC")


def test_assignee_watermark_seeded_to_now_and_persisted(isolated_state):
    """최초 실행 시 assignee_watermark가 now(주입 clock)로 시드·영속된다."""
    assert state.load_assignee_watermark() is None   # 사전 상태: 없음
    _, _, _, _, _, poller = _wire_routing([], [])
    assert poller.assignee_watermark == _FIXED_NOW.isoformat()
    assert state.load_assignee_watermark() == _FIXED_NOW.isoformat()


def test_assignee_watermark_advances_to_now_each_poll(isolated_state):
    """매 폴 사이클마다 assignee_watermark가 폴 시각 now로 전진·영속된다."""
    _, _, _, _, _, poller = _wire_routing([], [])
    assert poller.assignee_watermark == _FIXED_NOW.isoformat()   # now 시드
    later = datetime(2026, 8, 10, 12, 0, 0, tzinfo=_KST)
    poller._clock = lambda: later                                # 다음 폴 시각
    poller.poll_once()
    assert poller.assignee_watermark == later.isoformat()        # now로 전진
    assert state.load_assignee_watermark() == later.isoformat()


def test_poll_once_unions_created_and_assignee_change_and_dedups(isolated_state):
    """(A)신규 ∪ (B)담당자-변경을 티켓 키로 합집합·dedup해 각 1회만 디스패치."""
    # PROJ-1: (A)에만, PROJ-2: (B)에만(담당자-변경으로만 트리거), PROJ-3: 양쪽 모두.
    created = [_issue("PROJ-1", "a1"), _issue("PROJ-3", "a1")]
    assignee = [_issue("PROJ-2", "a1"), _issue("PROJ-3", "a1")]
    _, sch, _, gate, jira, poller = _wire_routing(created, assignee)

    n = poller.poll_once()
    assert n == 3                                    # PROJ-1,2,3 각 1회(PROJ-3 합쳐짐)
    assert sch.jobs.get("PROJ-1") is not None
    assert sch.jobs.get("PROJ-2") is not None        # (B)로만 트리거된 티켓도 잡힘
    assert sch.jobs.get("PROJ-3") is not None
    # 두 축 쿼리가 모두 실행됐다(전진 트리거 = A OR B).
    assert any("ORDER BY created ASC" in j for j in jira.jqls)
    assert any("CHANGED TO" in j for j in jira.jqls)
    # 재폴 시 dedup 게이트가 막는다 — PROJ-3가 양쪽에 다시 걸려도 재디스패치 X.
    assert poller.poll_once() == 0


# --- LLM 레포 해석(central 플래너) 배선 ---------------------------------------

_REPO_MAP_MD = """| 슬러그 | 원격 | 역할 |
|---|---|---|
| `portal-frontend` | http://git/pf.git | FE |
| `portal-backend` | http://git/pb.git | BE |
"""


def _wire_llm(issues, *, loader=None, runner=None, per_user=5, repo_map=None):
    """LLM 리졸버 주입 배선(라이브 claude/git 없이)."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = _fractalize(make_config(concurrency_per_worker=per_user, repo_map=repo_map or {}))
    # run.repo_resolution 기본이 'llm' 임을 명시(make_config엔 없음 → getattr 폴백).
    cfg.run.repo_resolution = "llm"
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp, clock=_fixed_clock,
                    repo_map_loader=loader, llm_runner=runner,
                    central_sink=_FakeCentralSink())
    return reg, sch, disp, gate, poller


def test_poll_once_llm_fills_target_repos(isolated_state):
    """fake runner가 슬러그 배열을 주면 job.target_repos가 그 값으로 채워진다."""
    def runner(cmd, timeout):
        return '["portal-frontend","portal-backend"]'

    _, sch, _, _, poller = _wire_llm(
        [_issue("PROJ-1", "a1")],
        loader=lambda: _REPO_MAP_MD, runner=runner)
    assert poller.poll_once() == 1
    j = sch.jobs.get("PROJ-1")
    assert j.target_repos == ["portal-frontend", "portal-backend"]


def test_poll_once_llm_failure_falls_back_to_static(isolated_state):
    """runner 실패 시 정적 config.repo_map 폴백으로 target_repos를 채운다."""
    def boom(cmd, timeout):
        raise RuntimeError("claude down")

    repo_map = {"portal-frontend": "portal-frontend"}
    _, sch, _, _, poller = _wire_llm(
        [_issue("PROJ-1", "a1", components=["portal-frontend"])],
        loader=lambda: _REPO_MAP_MD, runner=boom, repo_map=repo_map)
    assert poller.poll_once() == 1
    j = sch.jobs.get("PROJ-1")
    assert j.target_repos == ["portal-frontend"]   # 정적 폴백


def test_poll_once_idle_makes_no_llm_call(isolated_state):
    """신규 티켓이 없으면 REPO-MAP 로더도 claude runner도 호출하지 않는다(유휴=0)."""
    loader_calls = []
    runner_calls = []

    def loader():
        loader_calls.append(1)
        return _REPO_MAP_MD

    def runner(cmd, timeout):
        runner_calls.append(1)
        return "[]"

    _, _, _, _, poller = _wire_llm([], loader=loader, runner=runner)
    assert poller.poll_once() == 0
    assert loader_calls == []   # 빈 폴 → REPO-MAP pull/read 안 함
    assert runner_calls == []   # 빈 폴 → claude 호출 안 함


def test_poll_once_static_mode_skips_llm(isolated_state):
    """repo_resolution='static'이면 로더/runner를 부르지 않고 정적 룩업만 한다."""
    loader_calls = []

    def loader():
        loader_calls.append(1)
        return _REPO_MAP_MD

    repo_map = {"portal-frontend": "portal-frontend"}
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = _fractalize(make_config(concurrency_per_worker=5, repo_map=repo_map))
    cfg.run.repo_resolution = "static"
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    poller = Poller(cfg, FakeJira([_issue("PROJ-1", "a1", components=["portal-frontend"])]),
                    DedupGate(), reg, disp, clock=_fixed_clock, repo_map_loader=loader,
                    central_sink=_FakeCentralSink())
    assert poller.poll_once() == 1
    assert loader_calls == []   # static 모드 → REPO-MAP 로드 안 함
    assert sch.jobs.get("PROJ-1").target_repos == ["portal-frontend"]


# --- 웹훅 이벤트 구동 단일 티켓 트리거(poller.trigger_ticket) --------------------


class GetIssueFakeJira:
    """trigger_ticket용 페이크 — get_issue(key)로 단일 이슈를 돌려준다(라이브 금지)."""

    def __init__(self, issue, raise_exc=None):
        self._issue = issue
        self._raise = raise_exc
        self.get_calls = []

    def get_issue(self, key, fields=None):
        self.get_calls.append(key)
        if self._raise is not None:
            raise self._raise
        return self._issue

    def search_jql(self, jql, fields=None, max_results=50):
        return {"issues": [], "total": 0}


def _wire_trigger(issue, *, repo_map=None, resolution="static", raise_exc=None):
    """trigger_ticket 배선 — static 모드 기본(LLM claude 경로 회피)."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          per_repo={"portal-frontend": "A"}))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=False))  # 비활성
    cfg = _fractalize(make_config(concurrency_per_worker=5, repo_map=repo_map or {}))
    cfg.run.repo_resolution = resolution
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    jira = GetIssueFakeJira(issue, raise_exc=raise_exc)
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock,
                    central_sink=_FakeCentralSink())
    return reg, sch, disp, gate, jira, poller


def test_trigger_ticket_happy_path_claims_maps_enqueues(isolated_state):
    """정상 경로 — claim → resolve_user → enqueue, True 반환, 디스패처가 잡 보유."""
    repo_map = {"portal-frontend": "portal-frontend"}
    _, sch, _, gate, jira, poller = _wire_trigger(
        _issue("PROJ-1", "a1", components=["portal-frontend"]), repo_map=repo_map)
    assert poller.trigger_ticket("PROJ-1") is True
    assert jira.get_calls == ["PROJ-1"]                 # 페이로드 대신 Jira 재검증
    j = sch.jobs.get("PROJ-1")
    assert j is not None and j.user == "u1"
    assert j.target_repos == ["portal-frontend"]
    assert j.autonomy_mode == "A"                        # per_repo 오버라이드 반영
    assert j.branch == "auto/PROJ-1"
    assert gate.is_claimed("PROJ-1") is True


def test_trigger_ticket_dedup_when_already_claimed(isolated_state):
    """이미 claim된 티켓(폴러/다른 웹훅 선점)이면 False + 재-enqueue 없음."""
    _, sch, _, gate, _, poller = _wire_trigger(_issue("PROJ-1", "a1"))
    assert gate.claim("PROJ-1") is True                 # 선점
    assert poller.trigger_ticket("PROJ-1") is False
    assert sch.jobs.get("PROJ-1") is None               # 디스패치 안 됨


def test_trigger_ticket_unmapped_user_releases_and_false(isolated_state):
    """미등록/비활성 담당자면 gate.release 후 False(재트리거 가능하도록 claim 되돌림)."""
    _, sch, _, gate, _, poller = _wire_trigger(_issue("PROJ-1", "a2"))  # a2=비활성
    assert poller.trigger_ticket("PROJ-1") is False
    assert gate.is_claimed("PROJ-1") is False           # claim 되돌림
    assert sch.jobs.get("PROJ-1") is None


def test_trigger_ticket_status_mismatch_no_claim(isolated_state):
    """트리거 상태(match.statuses) 밖이면 claim하지 않고 False."""
    _, sch, _, gate, jira, poller = _wire_trigger(_issue("PROJ-1", "a1", status="완료"))
    assert poller.trigger_ticket("PROJ-1") is False
    assert jira.get_calls == ["PROJ-1"]                 # 조회는 했으나
    assert gate.is_claimed("PROJ-1") is False           # claim 안 함
    assert sch.jobs.get("PROJ-1") is None


def test_trigger_ticket_not_found_returns_false(isolated_state):
    """이슈 조회 결과가 비면(없음) False + claim 없음."""
    _, _, _, gate, _, poller = _wire_trigger({})        # 빈 응답
    assert poller.trigger_ticket("PROJ-404") is False
    assert gate.is_claimed("PROJ-404") is False


# --- 취소 즉시화 + 라벨 기반 추적제외(reconcile) — 웹훅/폴러 -----------------


def _wire_trigger_gated(issue, *, repo_map=None, resolution="static"):
    """trigger_ticket 배선(scheduler에 gate 주입) — cancel_job의 dedup 해제까지 검증."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = make_config(concurrency_per_worker=5, repo_map=repo_map or {})
    cfg.run.repo_resolution = resolution
    gate = DedupGate()
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    jira = GetIssueFakeJira(issue)
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock)
    return reg, sch, disp, gate, jira, poller


def test_trigger_ticket_cancel_status_cancels_running_job(isolated_state):
    """① 웹훅으로 취소상태 티켓 → 실행 중 잡을 cancel_job(즉시 취소 수렴)."""
    _, sch, disp, gate, _, poller = _wire_trigger_gated(_issue("PROJ-1", "a1", status="취소됨"))
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING
    assert poller.trigger_ticket("PROJ-1") is False       # 디스패치 아님(취소 수렴)
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING   # 실행중 → worker 위임


def test_trigger_ticket_optout_label_cancels_running_job(isolated_state):
    """② 웹훅으로 opt-out 라벨 티켓(실행 중) → cancel_job."""
    _, sch, disp, gate, _, poller = _wire_trigger_gated(
        _issue("PROJ-1", "a1", labels=["자동화_추적_해제"]))
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING
    assert poller.trigger_ticket("PROJ-1") is False
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING


def test_trigger_ticket_optout_label_no_new_start(isolated_state):
    """③ opt-out 라벨 티켓은 신규 착수하지 않는다(추적 잡 없으면 no-op)."""
    _, sch, _, gate, _, poller = _wire_trigger(_issue("PROJ-1", "a1", labels=["자동화_추적_해제"]))
    assert poller.trigger_ticket("PROJ-1") is False
    assert sch.jobs.get("PROJ-1") is None                  # 디스패치 안 됨
    assert gate.is_claimed("PROJ-1") is False              # claim도 안 함


def test_trigger_ticket_label_removed_eligible_redispatches(isolated_state):
    """⑤ 라벨 제거(=라벨 없음) + 적격 상태면 웹훅 경로로 재착수한다."""
    repo_map = {"portal-frontend": "portal-frontend"}
    _, sch, _, gate, _, poller = _wire_trigger(
        _issue("PROJ-1", "a1", components=["portal-frontend"]), repo_map=repo_map)
    assert poller.trigger_ticket("PROJ-1") is True         # opt-out 라벨 없음 → 신규 착수
    assert sch.jobs.get("PROJ-1") is not None
    assert gate.is_claimed("PROJ-1") is True


def test_build_jql_excludes_optout_labels(isolated_state):
    """④ build_jql에 opt-out 제외 절(무라벨 포함) 포함."""
    _, _, _, _, poller = _wire([])
    jql = poller.build_jql()
    assert '(labels is EMPTY OR labels not in ("자동화_추적_해제"))' in jql


def test_build_assignee_change_jql_excludes_optout_labels(isolated_state):
    """④ 담당자-변경 JQL에도 opt-out 제외 절 포함."""
    _, _, _, _, _, poller = _wire_routing([], [])
    jql = poller.build_assignee_change_jql()
    assert '(labels is EMPTY OR labels not in ("자동화_추적_해제"))' in jql


def test_poll_once_optout_label_not_dispatched(isolated_state):
    """③ 폴러 방어 게이트 — opt-out 라벨 티켓은 poll_once에서도 신규 착수 안 됨."""
    reg, sch, disp, gate, poller = _wire([_issue("PROJ-1", "a1", labels=["자동화_추적_해제"])])
    assert poller.poll_once() == 0
    assert sch.jobs.get("PROJ-1") is None
    assert gate.is_claimed("PROJ-1") is False


# ===========================================================================
# 프랙탈 P2 — 센트럴 라이브 세션 주입 seam(poller._emit) 게이팅 (기본 OFF)
# ===========================================================================


class _FakeCentralSink:
    """CentralSession 대역 — inject_event 호출을 기록(반환값 구성 가능)."""

    def __init__(self, ok=True):
        self.events = []
        self._ok = ok

    def inject_event(self, job):
        self.events.append(job)
        return self._ok


def _wire_central(issues, *, central_on, sink_ok=True, repo_map=None):
    """poller + 센트럴 sink 배선(static 해석 모드로 고정 — llm 격리)."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = make_config(concurrency_per_worker=5, repo_map=repo_map or {})
    # static 해석(테스트 격리) + P2 플래그 토글 + 지속(stream-json) 세션 전제.
    cfg.run.repo_resolution = "static"
    cfg.run.fractal_central = central_on
    cfg.run.persistent_session = True
    cfg.run.output_format = "stream-json"
    cfg.run.input_format = "stream-json"
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    sink = _FakeCentralSink(ok=sink_ok)
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp,
                    clock=_fixed_clock, central_sink=sink)
    return reg, sch, disp, gate, sink, poller


def test_emit_fractal_on_injects_instead_of_enqueue(isolated_state):
    """플래그 ON이면 해석된 티켓을 (구 경로 enqueue 대신) 센트럴 세션에 이벤트로 주입하고,
    **관측성 뼈대(A.1)로 JobQueue 에 queued 레코드를 남긴다**(대시보드 가시성).

    구 경로 dispatch 는 타지 않는다 — 레코드는 meta.fractal 표식이 붙어 스케줄러가
    디스패치하지 않는다(이중 실행 방지). poll_once 는 tick 하지 않으므로 상태는 queued 유지.
    """
    reg, sch, disp, gate, sink, poller = _wire_central(
        [_issue("PROJ-1", "a1")], central_on=True)
    n = poller.poll_once()
    assert n == 1
    # ⚠️ 센트럴 주입됨.
    assert len(sink.events) == 1
    assert sink.events[0].ticket == "PROJ-1"
    assert sink.events[0].user == "u1"
    # 관측성 뼈대: 프랙탈 잡이 store 에 queued 로 기록됐다(대시보드에 뜬다).
    job = sch.jobs.get("PROJ-1")
    assert job is not None
    assert job.status == "queued"
    assert job.user == "u1"
    assert job.is_fractal is True
    # 구 경로 스케줄러는 이 프랙탈 잡을 디스패치하지 않는다(이중 실행 방지).
    assert sch.tick() == []
    assert sch.jobs.get("PROJ-1").status == "queued"


def test_emit_fractal_on_inject_failure_raises_no_legacy_fallback(isolated_state):
    """P3: 주입 실패는 레거시 enqueue 로 폴백하지 않고 CentralInjectFailed 를 올린다.

    프랙탈이 유일 경로이므로 구 경로(스케줄러 enqueue)로 방출하지 않는다(이중-체인 근본
    제거). 실패 시 dedup claim 을 되돌려 다음 폴에서 재트리거 가능하게 한다(잡 유실 없음).
    """
    from app.poller import CentralInjectFailed

    reg, sch, disp, gate, sink, poller = _wire_central(
        [_issue("PROJ-1", "a1")], central_on=True, sink_ok=False)
    with pytest.raises(CentralInjectFailed):
        poller.poll_once()
    assert len(sink.events) == 1               # 주입 시도는 했다
    assert sch.jobs.get("PROJ-1") is None      # ⚠️ 레거시 enqueue 폴백 없음
    # dedup claim 이 되돌려져 재트리거(재-claim) 가능 — 잡 유실 없음.
    assert gate.claim("PROJ-1") is True


# --- 축0: Jira 자격 생존 확인(빈 결과 위장 벗기기) -----------------------------
#
# ★ 실측된 Jira Cloud 동작:
#       GET  /rest/api/3/myself      → 401
#       POST /rest/api/3/search/jql  → 200 {"issues": [], "isLast": true}
#   자격이 틀려도 검색은 성공한 척한다. 그래서 폴러는 "매칭 티켓 없음"과 "토큰 만료"를
#   응답 형태로 구별할 수 없고, 토큰이 회수되면 **영원히 조용히** 돈다.


class AuthProbeJira:
    """검색은 지정한 이슈를, ``myself`` 는 지정한 예외를 내는 대역(호출 횟수 기록)."""

    def __init__(self, issues=None, auth_error=None):
        self._issues = list(issues or [])
        self._auth_error = auth_error
        self.myself_calls = 0

    def search_jql(self, jql, fields=None, max_results=50):
        return {"issues": self._issues, "total": len(self._issues)}

    def myself(self):
        self.myself_calls += 1
        if self._auth_error:
            raise self._auth_error
        return {"displayName": "봇 계정"}


class _SettableClock:
    """테스트가 직접 시각을 밀어 주는 시계(``clock.now = ...``)."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def _wire_auth(jira, *, clock=None):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = _fractalize(make_config())
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    poller = Poller(cfg, jira, DedupGate(), reg, disp, clock=clock or _fixed_clock,
                    central_sink=_FakeCentralSink())
    noted: list = []
    poller.set_auth_reporter(lambda ok, detail: noted.append((ok, detail)))
    return poller, noted


def test_empty_poll_probes_auth_and_reports_failure(isolated_state):
    """★ 빈 폴이 '할 일 없음'인지 '자격 만료'인지 가른다 — 만료면 드러낸다."""
    from app.jira_client import JiraError

    jira = AuthProbeJira(auth_error=JiraError("GET → HTTP 401", status_code=401))
    poller, noted = _wire_auth(jira)
    assert poller.poll_once() == 0
    assert jira.myself_calls == 1
    assert noted == [(False, "HTTP 401")]


def test_empty_poll_reports_ok_when_credentials_are_alive(isolated_state):
    jira = AuthProbeJira()
    poller, noted = _wire_auth(jira)
    poller.poll_once()
    assert noted == [(True, "/myself 확인")]


def test_a_non_empty_poll_is_its_own_proof_of_auth(isolated_state):
    """티켓이 돌아온 폴은 그 자체가 자격 증거 — 추가 요청을 하지 않는다."""
    jira = AuthProbeJira(issues=[_issue("PROJ-1", "a1")])
    poller, noted = _wire_auth(jira)
    assert poller.poll_once() == 1
    assert jira.myself_calls == 0
    assert noted == [(True, "검색 결과로 확인(티켓 수신)")]


def test_auth_probe_respects_the_recheck_interval(isolated_state):
    """매 폴마다 때리지 않는다 — jira.auth_recheck_sec 이 지나야 다시 확인한다."""
    clock = _SettableClock(_FIXED_NOW)
    jira = AuthProbeJira()
    poller, _ = _wire_auth(jira, clock=clock)
    poller.config.jira.auth_recheck_sec = 1800
    poller.poll_once()                      # 최초 — 확인한다
    assert jira.myself_calls == 1
    clock.now = _FIXED_NOW + timedelta(seconds=120)
    poller.poll_once()                      # +2분 — 주기 안이라 건너뛴다
    assert jira.myself_calls == 1
    clock.now = _FIXED_NOW + timedelta(seconds=3600)
    poller.poll_once()                      # +1시간 — 주기가 지나 다시 확인
    assert jira.myself_calls == 2


def test_auth_probe_can_be_switched_off(isolated_state):
    jira = AuthProbeJira()
    poller, noted = _wire_auth(jira)
    poller.config.jira.auth_recheck_sec = 0
    poller.poll_once()
    assert jira.myself_calls == 0 and noted == []


def test_auth_probe_failure_never_breaks_the_poll_loop(isolated_state):
    """진단 호출이 터져도 폴링은 계속된다(이건 전제 조건이 아니라 진단이다)."""
    class _Boom(AuthProbeJira):
        def myself(self):
            self.myself_calls += 1
            raise RuntimeError("소켓 끊김")

    jira = _Boom(issues=[])
    poller, noted = _wire_auth(jira)
    assert poller.poll_once() == 0
    assert noted == [(False, "RuntimeError")]


def test_auth_probe_is_skipped_for_clients_without_myself(isolated_state):
    """``myself`` 가 없는 클라이언트면 조용히 건너뛴다(없는 근거로 실패를 만들지 않는다)."""
    jira = FakeJira([])
    poller, noted = _wire_auth(jira)
    assert poller.poll_once() == 0
    assert noted == []


# --- 축0(2): 감시 프로젝트 실재 확인(빈 결과의 두 번째 위장) --------------------
#
# ★ 실측: 없는 프로젝트 키로도 JQL 은 200 + 빈 목록을 준다. 같은 사이트에서
#   GET /rest/api/3/project/HAN 만 404 다. 설치 시점의 doctor 는 이걸 잡지만, 프로젝트는
#   **운영 중에** 삭제·개명되고 감시 계정의 '찾아보기' 권한도 회수된다 — 그때부터 폴러는
#   다시 조용해지고 아무도 모른다.


class ProjectProbeJira(AuthProbeJira):
    """``get_project`` 까지 갖춘 대역 — 지정한 키만 404 를 낸다(호출 기록)."""

    def __init__(self, issues=None, auth_error=None, missing=(), boom=()):
        super().__init__(issues=issues, auth_error=auth_error)
        self._missing = set(missing)
        self._boom = set(boom)
        self.project_calls: list = []

    def get_project(self, key):
        from app.jira_client import JiraError

        self.project_calls.append(key)
        if key in self._missing:
            raise JiraError("GET → HTTP 404", status_code=404)
        if key in self._boom:
            raise JiraError("GET → HTTP 503", status_code=503)
        return {"key": key}


def _wire_projects(jira, *, clock=None):
    poller, noted = _wire_auth(jira, clock=clock)
    seen: list = []
    poller.set_project_reporter(
        lambda missing, checked, undetermined: seen.append((missing, checked, undetermined)))
    return poller, noted, seen


def test_empty_poll_detects_a_project_that_disappeared(isolated_state):
    """★ 자격은 멀쩡한데 프로젝트가 없다 — 그 둘을 갈라서 보고한다."""
    jira = ProjectProbeJira(missing={"PROJ"})
    poller, noted, seen = _wire_projects(jira)
    assert poller.poll_once() == 0
    assert noted == [(True, "/myself 확인")]           # 자격은 살아 있다
    assert jira.project_calls == ["PROJ"]
    assert seen == [(["PROJ"], [], [])]


def test_project_probe_reports_pass_when_projects_are_alive(isolated_state):
    jira = ProjectProbeJira()
    poller, _, seen = _wire_projects(jira)
    poller.poll_once()
    assert seen == [([], ["PROJ"], [])]


def test_project_probe_runs_even_when_tickets_came_back(isolated_state):
    """감시 프로젝트가 여럿이면 한쪽이 티켓을 주는 동안 다른 쪽이 사라져도 폴이 비지 않는다.

    빈 폴로 한정하면 그 경우를 영원히 못 잡는다 — 그래서 폴이 비었는지와 무관하게
    주기마다 확인한다(주기 게이트가 이미 비용을 잡는다). 자격은 티켓 수신으로 증명되므로
    ``/myself`` 는 부르지 않는다.
    """
    jira = ProjectProbeJira(issues=[_issue("PROJ-1", "a1")], missing={"PROJ"})
    poller, noted, seen = _wire_projects(jira)
    assert poller.poll_once() == 1
    assert jira.myself_calls == 0
    assert noted == [(True, "검색 결과로 확인(티켓 수신)")]
    assert seen == [(["PROJ"], [], [])]


def test_project_probe_is_skipped_while_credentials_are_rejected(isolated_state):
    """자격이 거부되는 동안에는 프로젝트를 묻지 않는다 — 401/403 은 '없다'와 구별되지 않는다."""
    from app.jira_client import JiraError

    jira = ProjectProbeJira(auth_error=JiraError("GET → HTTP 401", status_code=401))
    poller, noted, seen = _wire_projects(jira)
    poller.poll_once()
    assert noted == [(False, "HTTP 401")]
    assert jira.project_calls == [] and seen == []


def test_project_probe_respects_the_recheck_interval(isolated_state):
    """매 폴마다 때리지 않는다 — 자격 확인과 **같은 주기 노브**를 쓴다."""
    clock = _SettableClock(_FIXED_NOW)
    jira = ProjectProbeJira()
    poller, _, _ = _wire_projects(jira, clock=clock)
    poller.config.jira.auth_recheck_sec = 1800
    poller.poll_once()
    assert jira.project_calls == ["PROJ"]
    clock.now = _FIXED_NOW + timedelta(seconds=120)
    poller.poll_once()                      # 주기 안 — 건너뛴다
    assert jira.project_calls == ["PROJ"]
    clock.now = _FIXED_NOW + timedelta(seconds=3600)
    poller.poll_once()                      # 주기 경과 — 다시 확인
    assert jira.project_calls == ["PROJ", "PROJ"]


def test_project_probe_can_be_switched_off_with_the_same_knob(isolated_state):
    jira = ProjectProbeJira()
    poller, _, seen = _wire_projects(jira)
    poller.config.jira.auth_recheck_sec = 0
    poller.poll_once()
    assert jira.project_calls == [] and seen == []


def test_a_non_404_failure_is_undetermined_not_a_missing_project(isolated_state):
    """5xx·네트워크는 **판정 보류** — 없는 근거로 실패를 만들지 않는다(부팅 진단과 같은 규율).

    전부 보류면 아무 것도 보고하지 않는다(직전 관측을 흔들지 않는다).
    """
    jira = ProjectProbeJira(boom={"PROJ"})
    poller, _, seen = _wire_projects(jira)
    poller.poll_once()
    assert jira.project_calls == ["PROJ"] and seen == []


def test_project_probe_is_skipped_for_clients_without_get_project(isolated_state):
    """``get_project`` 가 없는 클라이언트면 조용히 건너뛴다(옛 대역 하위호환)."""
    jira = AuthProbeJira()
    poller, _, seen = _wire_projects(jira)
    poller.poll_once()
    assert seen == []
