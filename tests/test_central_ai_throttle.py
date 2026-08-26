"""central 자기 토큰 레이트/사용량 한도 관리(축1) 테스트 — 쿨다운·pending·드레인.

라이브 claude/git은 절대 호출하지 않는다(runner/loader 주입 + 고정 시계).
검증 포인트:
    - 한도 감지 시 쿨다운을 걸고, 그 사이 티켓은 **유실 없이** pending으로 보관
      (dedup claim 유지·미해석·미디스패치).
    - **결정적 작업**(이미 해석된 잡의 스케줄러 dispatch)은 쿨다운과 무관하게 진행.
    - 회복(쿨다운 만료) 후 드레인이 pending을 해석·디스패치.
    - pending 집합은 state로 영속(재시작 라운드트립).
    - 백오프는 연속 한도에 완만히 증가하고 성공에 리셋.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app import queue as q
from app import state
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.poller import AICooldown, Poller, build_job
from app.queue import JobQueue
from app.registry import Registry, UserRecord
from app.repo_resolver import CentralAIRateLimited
from app.scheduler import Scheduler
from tests.conftest import make_config

_KST = timezone(timedelta(hours=9))
_FIXED_NOW = datetime(2026, 8, 10, 0, 0, 0, tzinfo=_KST)


def _fixed_clock():
    return _FIXED_NOW


class FakeJira:
    """poll_once용 — 모든 search_jql에 같은 이슈 목록을 돌려준다(CHANGED TO 무시)."""

    def __init__(self, issues):
        self._issues = issues

    def search_jql(self, jql, fields=None, max_results=50):
        # (B) 담당자-변경 쿼리엔 빈 결과(중복 유니온 회피 — 신규축만 테스트).
        if "CHANGED TO" in jql:
            return {"issues": [], "total": 0}
        return {"issues": self._issues, "total": len(self._issues)}


class GetIssueFakeJira:
    """trigger_ticket용 — get_issue(key)로 단일 이슈 반환."""

    def __init__(self, issue):
        self._issue = issue
        self.get_calls = []

    def get_issue(self, key, fields=None):
        self.get_calls.append(key)
        return self._issue

    def search_jql(self, jql, fields=None, max_results=50):
        return {"issues": [], "total": 0}


_REPO_MAP_MD = """| 슬러그 | 원격 | 역할 |
|---|---|---|
| `portal-frontend` | http://git/pf.git | FE |
| `portal-backend` | http://git/pb.git | BE |
"""


def _issue(key, account_id="a1", status="해야 할 일",
           created="2026-08-10T10:00:00.000+0900", components=None):
    return {
        "key": key,
        "fields": {
            "assignee": {"accountId": account_id},
            "status": {"name": status},
            "created": created,
            "components": [{"name": c} for c in (components or [])],
            "labels": [],
        },
    }


class _FakeCentralSink:
    """CentralSession 대역 — inject_event 를 성공 처리(프랙탈-ON 배선)."""

    def __init__(self):
        self.events = []

    def inject_event(self, job):
        self.events.append(job)
        return True


def _wire(issues, *, jira=None, loader=None, runner=None, repo_map=None):
    """LLM 리졸버 배선(라이브 없음). jira 미지정 시 FakeJira(issues).

    이 배포는 영구 프랙탈-ON — poller 는 센트럴 sink 로 방출한다(record_fractal_job 이 같은
    JobQueue store 에 queued 로 기록). fractal-OFF(sink 없는) 배선은 은퇴했다.
    """
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = make_config(repo_map=repo_map or {})
    cfg.run.repo_resolution = "llm"
    cfg.run.fractal_central = True
    cfg.run.persistent_session = True
    cfg.run.output_format = "stream-json"
    cfg.run.input_format = "stream-json"
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    jira = jira if jira is not None else FakeJira(issues)
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock,
                    repo_map_loader=loader or (lambda: _REPO_MAP_MD), llm_runner=runner,
                    central_sink=_FakeCentralSink())
    return reg, sch, disp, gate, poller


# --- AICooldown 단위(백오프·reset_at) ----------------------------------------


def test_ai_cooldown_backoff_grows_and_resets():
    cd = AICooldown(default_sec=120, max_sec=900)
    now = 1000.0
    assert cd.is_throttled(now) is False
    # 1회: +120, 2회: +240, 3회: +480, 4회: +960→캡 900.
    assert cd.note_rate_limited(now) == now + 120
    assert cd.note_rate_limited(now) == now + 240
    assert cd.note_rate_limited(now) == now + 480
    assert cd.note_rate_limited(now) == now + 900   # min(960, 900)
    assert cd.is_throttled(now + 899) is True
    assert cd.is_throttled(now + 901) is False
    # 성공 → 성장 리셋 + 해제. 다음 한도는 다시 default(+120)부터.
    cd.note_success()
    assert cd.is_throttled(now) is False
    assert cd.note_rate_limited(now) == now + 120


def test_ai_cooldown_uses_reset_at_when_present():
    cd = AICooldown(default_sec=120, max_sec=900)
    now = 1000.0
    # 미래 reset_at이 있으면 백오프 대신 그 값을 쓴다.
    assert cd.note_rate_limited(now, reset_at=now + 45) == now + 45
    # 과거 reset_at(now 이하)은 무시하고 백오프로 폴백.
    cd.note_success()
    assert cd.note_rate_limited(now, reset_at=now - 10) == now + 120


# --- poll_once: 쿨다운 중이면 pending 보관(유실 없음, claude 미호출) -----------


def test_poll_once_throttled_pends_ticket_no_claude_call(isolated_state):
    runner_calls = []
    loader_calls = []

    def runner(cmd, timeout):
        runner_calls.append(cmd)
        return '["portal-frontend"]'

    def loader():
        loader_calls.append(1)
        return _REPO_MAP_MD

    _, sch, _, gate, poller = _wire([_issue("PROJ-1")], loader=loader, runner=runner)
    # 쿨다운 사전 무장(현재 시각 기준 미래까지).
    poller._ai_cd.note_rate_limited(poller._now_ts())

    n = poller.poll_once()
    assert n == 0                                   # 아무것도 디스패치 안 함
    assert sch.jobs.get("PROJ-1") is None           # 미디스패치
    assert gate.is_claimed("PROJ-1") is True        # 그러나 수신·추적됨(claim 유지)
    assert "PROJ-1" in poller.pending_keys()        # pending으로 보관(유실 없음)
    assert runner_calls == []                        # claude 미호출
    assert loader_calls == []                        # 쿨다운 중이라 REPO-MAP pull도 생략


def test_poll_once_rate_limit_arms_cooldown_and_pends(isolated_state):
    """한도 감지(runner 429) → 쿨다운 무장 + 그 티켓들 pending(유실 없음)."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: API Error 429 rate_limit_error")

    _, sch, _, gate, poller = _wire(
        [_issue("PROJ-1"), _issue("PROJ-2")], runner=limited)

    n = poller.poll_once()
    assert n == 0
    assert poller.is_ai_throttled() is True         # 쿨다운 무장됨
    assert set(poller.pending_keys()) == {"PROJ-1", "PROJ-2"}
    assert gate.is_claimed("PROJ-1") and gate.is_claimed("PROJ-2")
    assert sch.jobs.get("PROJ-1") is None and sch.jobs.get("PROJ-2") is None


# --- trigger_ticket(웹훅): 쿨다운 중 pending -----------------------------------


def test_trigger_ticket_throttled_pends(isolated_state):
    runner_calls = []

    def runner(cmd, timeout):
        runner_calls.append(cmd)
        return '["portal-frontend"]'

    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    cfg = make_config()
    cfg.run.repo_resolution = "llm"
    sch = Scheduler(cfg, JobQueue())
    disp = Dispatcher(reg, sch)
    gate = DedupGate()
    jira = GetIssueFakeJira(_issue("PROJ-1"))
    poller = Poller(cfg, jira, gate, reg, disp, clock=_fixed_clock,
                    repo_map_loader=lambda: _REPO_MAP_MD, llm_runner=runner)
    poller._ai_cd.note_rate_limited(poller._now_ts())   # 쿨다운 무장

    assert poller.trigger_ticket("PROJ-1") is True      # 수신·추적됨
    assert gate.is_claimed("PROJ-1") is True             # claim 유지
    assert "PROJ-1" in poller.pending_keys()             # pending 보관
    assert sch.jobs.get("PROJ-1") is None                # 미디스패치
    assert runner_calls == []                             # claude 미호출


# --- 결정적 작업은 쿨다운과 무관 (scheduler.tick 독립) ------------------------


def test_deterministic_dispatch_proceeds_during_cooldown(isolated_state):
    """이미 해석된 잡의 스케줄러 dispatch는 쿨다운과 무관하게 진행된다."""
    reg, sch, disp, gate, poller = _wire([])
    # central AI 쿨다운을 강하게 무장.
    poller._ai_cd.note_rate_limited(poller._now_ts())
    assert poller.is_ai_throttled() is True

    # target_repos가 이미 채워진 잡을 스케줄러에 직접 enqueue → 즉시 running.
    user = reg.get("u1")
    job = build_job(poller.config, "PROJ-9", _issue("PROJ-9"), user,
                    target_repos=["portal-frontend"])
    dispatched = sch.enqueue(job)
    assert "PROJ-9" in dispatched
    assert sch.jobs.get("PROJ-9").status == q.RUNNING    # 쿨다운 중에도 dispatch됨


# --- 드레인(회복 후 pending 해석·디스패치) ------------------------------------


def test_drain_resolves_and_dispatches_on_recovery(isolated_state):
    """쿨다운 만료 후 드레인이 pending 티켓을 해석·디스패치하고 pending을 비운다."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: usage limit reached")

    _, sch, _, gate, poller = _wire([_issue("PROJ-1")], runner=limited)
    poller.poll_once()                                   # 한도 → pending
    assert "PROJ-1" in poller.pending_keys()
    assert poller.is_ai_throttled() is True

    # 쿨다운 만료 이후로 시계 전진 + 정상 runner로 교체.
    later = _FIXED_NOW + timedelta(seconds=300)
    poller._clock = lambda: later
    poller._llm_runner = lambda cmd, timeout: '["portal-frontend","portal-backend"]'
    assert poller.is_ai_throttled() is False

    drained = poller.drain_pending_resolution()
    assert drained == 1
    assert poller.pending_keys() == []                   # 비워짐
    j = sch.jobs.get("PROJ-1")
    assert j is not None and j.target_repos == ["portal-frontend", "portal-backend"]


def test_drain_is_noop_while_throttled(isolated_state):
    """쿨다운 중에는 드레인이 아무것도 하지 않고 pending을 유지한다."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: 429 overloaded")

    _, sch, _, _, poller = _wire([_issue("PROJ-1")], runner=limited)
    poller.poll_once()
    assert "PROJ-1" in poller.pending_keys()
    assert poller.is_ai_throttled() is True

    assert poller.drain_pending_resolution() == 0        # no-op
    assert "PROJ-1" in poller.pending_keys()             # 유지
    assert sch.jobs.get("PROJ-1") is None


def test_drain_rearms_cooldown_on_repeat_rate_limit(isolated_state):
    """드레인 중 한도가 재발하면 쿨다운을 재-무장하고 나머지 pending을 남긴다."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: quota exceeded")

    _, sch, _, _, poller = _wire([_issue("PROJ-1"), _issue("PROJ-2")], runner=limited)
    poller.poll_once()
    assert set(poller.pending_keys()) == {"PROJ-1", "PROJ-2"}

    # 쿨다운 만료로 전진하되 runner는 여전히 한도.
    later = _FIXED_NOW + timedelta(seconds=300)
    poller._clock = lambda: later
    assert poller.is_ai_throttled() is False

    drained = poller.drain_pending_resolution()
    assert drained == 0
    assert poller.is_ai_throttled() is True              # 재-무장됨
    assert set(poller.pending_keys()) == {"PROJ-1", "PROJ-2"}   # 유지(유실 없음)


def test_drain_drops_ticket_when_user_no_longer_mapped(isolated_state):
    """드레인 시 담당자가 더 이상 enabled가 아니면 claim 되돌리고 pending에서 제거한다."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: rate limit")

    reg, sch, _, gate, poller = _wire([_issue("PROJ-1")], runner=limited)
    poller.poll_once()
    assert "PROJ-1" in poller.pending_keys()
    assert gate.is_claimed("PROJ-1") is True

    # 회복 + 담당자 비활성화.
    later = _FIXED_NOW + timedelta(seconds=300)
    poller._clock = lambda: later
    reg.set_enabled("u1", False)

    assert poller.drain_pending_resolution() == 0
    assert poller.pending_keys() == []                   # 제거
    assert gate.is_claimed("PROJ-1") is False            # claim 되돌림(재트리거 가능)
    assert sch.jobs.get("PROJ-1") is None


# --- pending 영속(재시작 라운드트립) ------------------------------------------


def test_pending_persists_across_restart(isolated_state):
    """pending 집합은 state로 영속되어 새 Poller 인스턴스가 로드한다."""
    def limited(cmd, timeout):
        raise RuntimeError("claude rc=1: 429 rate_limit_error")

    reg, sch, disp, gate, poller = _wire([_issue("PROJ-1")], runner=limited)
    poller.poll_once()
    assert "PROJ-1" in poller.pending_keys()
    # state 파일에 기록됐다.
    assert any(r.get("key") == "PROJ-1" for r in state.load_pending_resolution([]))

    # 새 Poller(재시작) — 같은 state 디렉토리에서 pending을 로드한다.
    poller2 = Poller(poller.config, FakeJira([]), gate, reg, disp, clock=_fixed_clock,
                     repo_map_loader=lambda: _REPO_MAP_MD, llm_runner=None)
    assert "PROJ-1" in poller2.pending_keys()


# --- pending thread-safety(동시 add/drop 항목 손실·경합 없음) -----------------


def test_pending_concurrent_add_drop_no_loss(isolated_state):
    """여러 스레드가 동시에 _add_pending/_drop_pending을 반복해도 항목 손실·예외 없이
    수렴하고, 영속 파일이 최종 메모리 상태와 정확히 일치한다(_pending_lock 검증).

    lock 없는 plain list라면 append와 리스트-컴프리헨션 재대입이 인터리브되며
    lost-update로 항목이 유실된다. 각 스레드가 **서로소** 키 범위를 다루므로 최종
    잔존 집합은 결정적이다(짝수 i drop / 홀수 i 잔존).
    """
    _, _, _, _, poller = _wire([])
    threads_n = 8
    per = 60
    errors: list = []
    start = threading.Barrier(threads_n)

    def worker(tid: int) -> None:
        try:
            start.wait()  # 동시 스타트로 경합 최대화
            for i in range(per):
                poller._add_pending(f"K-{tid}-{i}", _issue(f"K-{tid}-{i}"))
            for i in range(0, per, 2):  # 짝수 인덱스만 제거
                poller._drop_pending(f"K-{tid}-{i}")
        except Exception as exc:  # noqa: BLE001 — 어떤 예외도 실패로 수집
            errors.append(exc)

    ts = [threading.Thread(target=worker, args=(t,)) for t in range(threads_n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    assert not errors, f"동시 접근 중 예외 발생: {errors}"
    expected = {
        f"K-{tid}-{i}"
        for tid in range(threads_n)
        for i in range(per)
        if i % 2 == 1
    }
    # 메모리 상태 — 항목 손실 없음.
    assert set(poller.pending_keys()) == expected
    assert len(poller.pending_keys()) == len(expected)  # 중복도 없음
    # 영속 파일 — 락으로 mutation+persist가 직렬화되어 최종 메모리와 일치.
    persisted = {r.get("key") for r in state.load_pending_resolution([])}
    assert persisted == expected
