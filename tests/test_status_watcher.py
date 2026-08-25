"""status_watcher 단위테스트 — 취소 감지→abort, 외부완료(롤백X), 재오픈,
updated 워터마크 전진, 이름 구분(완료≠취소됨). 라이브 Jira 없음(FakeJira)."""

from __future__ import annotations

from app import queue as q
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from app.status_watcher import StatusWatcher
from tests.conftest import make_config


class FakeJira:
    """status 이름으로 이슈를 돌려주는 대역(+ key in (...) 게이팅 존중)."""

    def __init__(self):
        self.by_status: dict = {}
        self.queries: list = []

    def add(self, status, issue):
        self.by_status.setdefault(status, []).append(issue)

    def search_jql(self, jql, fields=None, max_results=50):
        self.queries.append(jql)
        out = []
        for status, items in self.by_status.items():
            if f'"{status}"' not in jql:
                continue
            for it in items:
                key = it["key"]
                if "key in (" in jql and f'"{key}"' not in jql:
                    continue
                out.append(it)
        return {"issues": out, "total": len(out)}


def _issue(key, account_id="a1", status="취소됨",
           updated="2026-08-10T10:00:00.000+0900", labels=None):
    return {
        "key": key,
        "fields": {
            "assignee": {"accountId": account_id},
            "status": {"name": status},
            "updated": updated,
            "components": [],
            "labels": labels or [],
        },
    }


class LabelFakeJira:
    """opt-out 백스톱용 대역 — ``labels in (...)`` JQL에 라벨 매칭 이슈만 반환.

    다른 섹션(취소/외부완료/재오픈) 쿼리엔 응답하지 않아(격리) opt-out만 실증한다.
    """

    def __init__(self, issues):
        self._issues = issues
        self.queries: list = []

    def search_jql(self, jql, fields=None, max_results=50):
        self.queries.append(jql)
        if "labels in (" not in jql:
            return {"issues": [], "total": 0}
        out = []
        for it in self._issues:
            key = it["key"]
            if "key in (" in jql and f'"{key}"' not in jql:
                continue
            labels = (it.get("fields") or {}).get("labels", []) or []
            if any(f'"{l}"' in jql for l in labels):
                out.append(it)
        return {"issues": out, "total": len(out)}


def _wire():
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    jira = FakeJira()
    watcher = StatusWatcher(make_config(), jira, gate, reg, disp)
    return reg, gate, sch, disp, jira, watcher


# --- 취소 감지 → abort -------------------------------------------------------


def test_cancel_aborts_tracked_running_job(isolated_state):
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    jira.add("취소됨", _issue("PROJ-1"))
    res = watcher.poll_once()
    assert res["cancelled"] == 1
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING     # 실행중 → worker 위임
    # updated 워터마크 전진(재처리 축소).
    assert watcher.cancel_watermark == "2026-08-10T10:00:00.000+0900"


def test_cancel_ignores_untracked_ticket(isolated_state):
    _, _, sch, disp, jira, watcher = _wire()
    jira.add("취소됨", _issue("PROJ-999"))       # 중앙이 추적하지 않는 티켓
    res = watcher.poll_once()
    assert res["cancelled"] == 0
    assert sch.jobs.get("PROJ-999") is None


# --- 이름 구분: 완료(정상)는 취소 아님 → 롤백 없이 종료 ----------------------


def test_done_status_is_not_cancel_and_no_rollback(isolated_state):
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))

    jira.add("완료", _issue("PROJ-1", status="완료"))
    res = watcher.poll_once()
    assert res["cancelled"] == 0                 # 완료는 취소로 취급하지 않음
    assert res["done"] == 1
    assert sch.jobs.get("PROJ-1").status == q.DONE  # 정상 종료(롤백 X)


# --- 재오픈: 취소됨 → 해야 할 일 → 재-enqueue -------------------------------


def test_reopen_reenqueues_cancelled_ticket(isolated_state):
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.cancel_job("PROJ-1")               # cancelling
    sch.report("PROJ-1", "취소됨")         # cancelled + dedup 해제
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED
    assert gate.is_claimed("PROJ-1") is False

    jira.add("해야 할 일", _issue("PROJ-1", status="해야 할 일",
                                   updated="2026-08-11T09:00:00.000+0900"))
    res = watcher.poll_once()
    assert res["reopened"] == 1
    assert sch.jobs.get("PROJ-1").status == q.RUNNING    # 같은 티켓 재-dispatch
    assert gate.is_claimed("PROJ-1") is True             # 재-claim
    assert watcher.reopen_watermark == "2026-08-11T09:00:00.000+0900"


def test_reopen_skips_unmapped_assignee(isolated_state):
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.cancel_job("PROJ-1")
    sch.report("PROJ-1", "취소됨")

    # 미등록 담당자로 재오픈된 티켓 → skip.
    jira.add("해야 할 일", _issue("PROJ-1", account_id="unknown", status="해야 할 일"))
    res = watcher.poll_once()
    assert res["reopened"] == 0
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED


def test_poll_once_isolated_sections_all_run(isolated_state):
    # 네 감시 섹션이 한 번의 poll에서 함께 동작함(교차 오염 없음).
    _, gate, sch, disp, jira, watcher = _wire()
    res = watcher.poll_once()
    assert res == {"cancelled": 0, "optout": 0, "done": 0, "reopened": 0}


# --- config 취소상태(하드코딩 대체) -----------------------------------------


def test_cancel_detection_uses_config_cancel_statuses(isolated_state):
    """취소 감지가 하드코딩('취소됨')이 아니라 config.match.cancel_statuses를 쓴다."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    cfg = make_config(concurrency_per_worker=5, cancel_statuses=["폐기됨"])
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    jira = FakeJira()
    watcher = StatusWatcher(cfg, jira, gate, reg, disp)

    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    jira.add("폐기됨", _issue("PROJ-1", status="폐기됨"))
    res = watcher.poll_once()
    assert res["cancelled"] == 1
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING
    # JQL은 status in (...) 형태로 config 값을 반영한다.
    assert any('status in ("폐기됨")' in jq for jq in jira.queries)


# --- opt-out 라벨 백스톱(웹훅 놓쳤을 때) -------------------------------------


def test_optout_backstop_cancels_tracked_running_job(isolated_state):
    """추적 중인 실행 잡의 티켓에 opt-out 라벨이 붙으면 폴링 백스톱이 cancel_job."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    jira = LabelFakeJira([_issue("PROJ-1", status="해야 할 일",
                                 labels=["자동화_추적_해제"])])
    watcher = StatusWatcher(make_config(), jira, gate, reg, disp)
    res = watcher.poll_once()
    assert res["optout"] == 1
    assert sch.jobs.get("PROJ-1").status == q.CANCELLING


def test_optout_backstop_ignores_untracked(isolated_state):
    """추적하지 않는 티켓엔 opt-out 백스톱이 아무 것도 하지 않는다(멱등)."""
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    jira = LabelFakeJira([_issue("PROJ-9", status="해야 할 일",
                                 labels=["자동화_추적_해제"])])
    watcher = StatusWatcher(make_config(), jira, gate, reg, disp)
    res = watcher.poll_once()
    assert res["optout"] == 0


# --- 재개(라벨 제거) 폴링 백스톱 = 재오픈 경로의 opt-out 가드 ----------------


def test_reopen_skips_while_optout_label_present(isolated_state):
    """취소 확정 잡의 티켓이 재오픈 상태라도 opt-out 라벨이 남아 있으면 재-enqueue 안 함."""
    _, gate, sch, disp, jira, watcher = _wire()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", user="u1", target_repos=["repoA"]))
    sch.cancel_job("PROJ-1")
    sch.report("PROJ-1", "취소됨")
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED

    jira.add("해야 할 일", _issue("PROJ-1", status="해야 할 일",
                                   labels=["자동화_추적_해제"],
                                   updated="2026-08-11T09:00:00.000+0900"))
    res = watcher.poll_once()
    assert res["reopened"] == 0                          # 라벨 잔존 → 재개 보류
    assert sch.jobs.get("PROJ-1").status == q.CANCELLED
