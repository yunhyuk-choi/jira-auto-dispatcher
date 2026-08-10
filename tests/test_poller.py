"""poller 단위테스트 — JQL 구성·매핑·claim·repo 해석·watermark(라이브 금지)."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from app import state
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.poller import Poller, resolve_target_repos
from app.queue import JobQueue
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


def _wire(issues, repo_map=None, per_user=5):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True,
                          per_repo={"portal-frontend": "A"}))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=False))  # 비활성
    sch = Scheduler(make_config(concurrency_per_worker=per_user, repo_map=repo_map or {}), JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    cfg = make_config(concurrency_per_worker=per_user, repo_map=repo_map or {})
    # 고정 시계 주입 → watermark 최초 시드가 결정적(2026-08-10T00:00, KST)이라
    # 이후 생성분(10:00 등)은 정상적으로 watermark를 전진시킨다.
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp, clock=_fixed_clock)
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
