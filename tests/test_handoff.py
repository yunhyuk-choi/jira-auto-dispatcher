"""담당자 변경 = 핸드오프(체크포인트, 롤백X) + 이관 단위테스트(Increment 1).

재배정 ≠ 취소를 검증한다:
    - 취소 = abort + 롤백(WIP 폐기, 기존 §10.4).
    - 핸드오프 = checkpoint(WIP 커밋·push로 보존) + 소유권 이관.

라이브 Jira/claude/git/네트워크 없음(FakeProc·주입 stub·격리 state).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app import agent_runner as ar
from app import queue as q
from app import scheduler as sched
from app.dispatch import Dispatcher
from app.gate import DedupGate
from app.poller import Poller
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from tests.conftest import make_config


# =========================================================================
# 스케줄러 — reassign_or_handoff / confirm_handed_off
# =========================================================================


# 자원 스로틀이 절대 걸리지 않는 프로브(정확성/재배정 계층 격리 — 잡 수 cap 없음).
_AMPLE = lambda: {"mem_available_mb": float(1 << 20), "loadavg_1min": 0.0, "ncpu": 8}


def _wire_sched(per_user=1, gate=None):
    # per_user 인자는 잡 수 cap 제거로 no-op(하위 호환). 큐잉은 전역 레포락으로 재현한다.
    gate = gate if gate is not None else DedupGate()
    sch = Scheduler(make_config(), JobQueue(), gate=gate, resource_probe=_AMPLE)
    return sch, gate


def _job(ticket, user, repos):
    return Job(ticket=ticket, user=user, target_repos=list(repos))


def test_reassign_queued_redispatches_to_y(isolated_state):
    """큐 대기(WIP 없음) 잡의 담당자 변경 → Y로 재-소유하고 재-dispatch(취소 아님).

    잡 수 cap이 없어졌으므로 큐잉은 **전역 레포락**(같은 레포 직렬)으로 재현한다:
    PROJ-2는 PROJ-1과 같은 repoA라 대기 → 재배정으로 Y 재-소유 → PROJ-1 완료로 레포 해제 시 dispatch.
    """
    sch, gate = _wire_sched()
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))       # running(u1, repoA)
    sch.enqueue(_job("PROJ-2", "u1", ["repoA"]))       # queued(같은 레포 = 레포락)
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    signal = sch.reassign_or_handoff("PROJ-2", "u2", enabled=True, autonomy_mode="A")
    assert signal == sched.REASSIGN_REDISPATCH
    j = sch.jobs.get("PROJ-2")
    assert j.user == "u2"                                # Y로 이관
    assert j.status == q.QUEUED                          # repoA 아직 잠김 → 대기(재-소유만)
    assert j.continue_from_wip is False                  # 큐 대기분 = WIP 없음
    assert j.autonomy_mode == "A"                        # Y 모드 반영
    assert j.meta.get("handed_off_from") == "u1"         # 이관 흔적

    sch.on_complete("PROJ-1", q.DONE)                    # 레포락 해제 → Y로 재-dispatch
    assert sch.jobs.get("PROJ-2").status == q.RUNNING
    assert sch.jobs.get("PROJ-2").user == "u2"


def test_reassign_queued_redispatch_routes_via_hook_not_tick(isolated_state):
    """REDISPATCH 시 ``on_redispatch`` 훅이 주어지면 구 tick 대신 그 훅으로 재-dispatch 위임.

    프랙탈 배포: 재-소유된 슬롯을 센트럴 세션으로 라우팅하려고 poller 가 훅을 주입한다.
    훅이 있으면 스케줄러가 tick 으로 running 을 만들지 않는다(실행자 없어 스턱나지 않게).
    """
    sch, gate = _wire_sched()
    # repoA 는 비어 있고 PROJ-2 는 queued — 훅이 없었다면(레거시) tick 이 즉시 running 으로 올린다.
    sch.jobs.enqueue(_job("PROJ-2", "u1", ["repoA"]))
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    calls = []
    signal = sch.reassign_or_handoff(
        "PROJ-2", "u2", enabled=True, autonomy_mode="A",
        on_redispatch=lambda t: calls.append(t))
    assert signal == sched.REASSIGN_REDISPATCH
    assert calls == ["PROJ-2"]                            # 훅으로 라우팅됨
    j = sch.jobs.get("PROJ-2")
    assert j.user == "u2"                                 # Y로 재-소유
    assert j.status == q.QUEUED                           # ⚠️ 구 tick 미실행(running 안 됨)


def test_reassign_running_hands_off_then_continues_to_y(isolated_state):
    """실행 중(WIP 존재) 잡의 담당자 변경 → 핸드오프(롤백X) → Y로 continue 이관."""
    sch, gate = _wire_sched(per_user=5)
    gate.claim("PROJ-1")                                 # 폴러가 잡은 상태 시뮬
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    # 1) 재배정 요청 → 핸드오프(checkpoint) 신호. 즉시 dispatch 없음(회신 대기).
    signal = sch.reassign_or_handoff("PROJ-1", "u2", enabled=True, autonomy_mode="B")
    assert signal == sched.REASSIGN_HANDOFF_REQUESTED
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.HANDING_OFF                      # 회신 대기(레포락 유지)
    assert j.control_action == "handoff"                 # worker control이 handoff 반환
    assert j.user == "u1"                                # 아직 X 소유(회신 전)
    assert gate.is_claimed("PROJ-1") is True             # dedup 유지(이관 예정)

    # 2) worker가 checkpoint 후 handed_off 회신 → Y로 continue 이관 + 재-dispatch.
    sch.report("PROJ-1", "handed_off", branch="auto/PROJ-1")
    j = sch.jobs.get("PROJ-1")
    assert j.user == "u2"                                # Y로 이관됨
    assert j.status == q.RUNNING                          # continue 잡 재-dispatch
    assert j.continue_from_wip is True                    # 브랜치 선행 WIP 힌트
    assert j.branch == "auto/PROJ-1"                      # 같은 브랜치(WIP 보존)
    assert j.meta.get("handed_off_from") == "u1"
    assert gate.is_claimed("PROJ-1") is True              # 이관이므로 dedup 유지


def test_reassign_running_y_not_enabled_parks(isolated_state):
    """Y 미가용(비활성/미등록) → checkpoint로 보존하되 park(미dispatch, dedup 해제)."""
    sch, gate = _wire_sched(per_user=5)
    gate.claim("PROJ-1")
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))

    signal = sch.reassign_or_handoff("PROJ-1", "u2", enabled=False)
    assert signal == sched.REASSIGN_HANDOFF_REQUESTED
    assert sch.jobs.get("PROJ-1").status == q.HANDING_OFF

    # handed_off 회신 → Y 미가용이라 park: handed_off 종결 + dedup 해제, 재-dispatch 없음.
    sch.report("PROJ-1", "handed_off", branch="auto/PROJ-1")
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.HANDED_OFF                       # 종결(WIP 보존·이관 대기)
    assert j.user == "u1"                                 # 이관 안 됨(park)
    assert gate.is_claimed("PROJ-1") is False             # dedup 해제(재트리거 가능)


def test_reassign_queued_y_not_enabled_parks(isolated_state):
    """큐 대기분 + Y 미가용 → 드롭(cancelled) + dedup 해제 → park."""
    sch, gate = _wire_sched(per_user=1)
    gate.claim("PROJ-1")
    gate.claim("PROJ-2")
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))         # running
    sch.enqueue(_job("PROJ-2", "u1", ["repoA"]))         # queued(같은 레포)

    signal = sch.reassign_or_handoff("PROJ-2", "u2", enabled=False)
    assert signal == sched.REASSIGN_PARKED
    assert sch.jobs.get("PROJ-2").status == q.CANCELLED   # 드롭(WIP 없음)
    assert gate.is_claimed("PROJ-2") is False             # dedup 해제


def test_reassign_same_owner_is_noop(isolated_state):
    """같은 소유자(Y==X) 중복 트리거 → REASSIGN_SAME_OWNER, 상태 불변."""
    sch, _ = _wire_sched(per_user=5)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    signal = sch.reassign_or_handoff("PROJ-1", "u1", enabled=True)
    assert signal == sched.REASSIGN_SAME_OWNER
    assert sch.jobs.get("PROJ-1").status == q.RUNNING     # 불변
    assert sch.jobs.get("PROJ-1").control_action == "none"


def test_reassign_no_job_returns_sentinel(isolated_state):
    """추적 잡 없음 → REASSIGN_NO_JOB(호출부가 일반 신규 dispatch)."""
    sch, _ = _wire_sched()
    assert sch.reassign_or_handoff("NOPE", "u2") == sched.REASSIGN_NO_JOB


def test_reassign_terminal_job_returns_no_job(isolated_state):
    """이미 종결(done)된 잡 → REASSIGN_NO_JOB(재오픈은 status_watcher 담당)."""
    sch, _ = _wire_sched(per_user=5)
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    sch.on_complete("PROJ-1", q.DONE)
    assert sch.reassign_or_handoff("PROJ-1", "u2") == sched.REASSIGN_NO_JOB


# =========================================================================
# agent_runner — 핸드오프 신호 → STATUS_HANDOFF (롤백 아님) + 프롬프트 힌트
# =========================================================================


class FakeProc:
    """Popen 대역 — stdout 이터러블 + terminate/kill/wait 기록."""

    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def test_consume_handoff_returns_handed_off():
    """control 체크가 'handoff'를 주면 subprocess 종료 후 STATUS_HANDOFF 반환."""
    lines = [
        '{"type":"system","subtype":"init","session_id":"s1"}\n',
        '{"type":"assistant","message":"working"}\n',
        '{"type":"assistant","message":"more"}\n',
    ]
    proc = FakeProc(lines)
    calls = {"n": 0}

    def control_check():
        calls["n"] += 1
        return "handoff" if calls["n"] >= 2 else "none"

    res = ar._consume(proc, [], cancel_check=control_check)
    assert res.status == ar.STATUS_HANDOFF
    assert proc.terminated is True          # subprocess 종료됨
    assert res.session_id == "s1"           # 신호 전까지 파싱분 보존


def test_normalize_control_accepts_bool_and_string():
    """제어 체크 반환 정규화 — bool(하위호환)과 액션 문자열 모두 흡수."""
    assert ar._normalize_control("handoff") == "handoff"
    assert ar._normalize_control(ar.STATUS_HANDOFF) == "handoff"
    assert ar._normalize_control(True) == "cancel"
    assert ar._normalize_control("cancel") == "cancel"
    assert ar._normalize_control(False) == "none"
    assert ar._normalize_control(None) == "none"


def test_build_prompt_continue_from_wip_hint():
    """continue_from_wip=True면 '이전 담당자 WIP 리뷰·이어서 완성' 힌트가 프롬프트에 들어간다."""
    job = {"ticket": "PROJ-1", "autonomy_mode": "B", "branch": "auto/PROJ-1",
           "continue_from_wip": True, "target_repos": ["repoA"]}
    prompt = ar.build_prompt(job, None)
    assert "선행 작업(WIP)" in prompt
    assert "auto/PROJ-1" in prompt
    # 힌트가 없을 땐 등장하지 않는다.
    job2 = dict(job, continue_from_wip=False)
    assert "선행 작업(WIP)" not in ar.build_prompt(job2, None)


# =========================================================================
# 폴러/웹훅 — 담당자 변경 감지 라우팅(재배정 ≠ 취소)
# =========================================================================

_FIXED_NOW = datetime(2026, 8, 10, 0, 0, 0, tzinfo=timezone.utc)


class FakeJira:
    """poll_once/trigger용 페이크 — 주어진 이슈를 두 쿼리·get_issue에 돌려준다."""

    def __init__(self, issues):
        self._issues = issues

    def search_jql(self, jql, fields=None, max_results=50):
        return {"issues": self._issues, "total": len(self._issues)}

    def get_issue(self, key, fields=None):
        for it in self._issues:
            if it.get("key") == key:
                return it
        return {}


def _issue(key, account_id, status="진행 중"):
    return {
        "key": key,
        "fields": {
            "assignee": {"accountId": account_id},
            "status": {"name": status},
            "created": "2026-08-10T10:00:00.000+0900",
            "components": [],
            "labels": [],
        },
    }


class _FakeCentralSink:
    """CentralSession 대역 — inject_event 호출 기록."""

    def __init__(self, ok=True):
        self.events = []
        self._ok = ok

    def inject_event(self, job):
        self.events.append(job)
        return self._ok


def _wire_poller(issues, *, central_sink=None):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=True))
    reg.upsert(UserRecord(username="u3", jira_account_id="a3", enabled=False))  # 비활성
    gate = DedupGate()
    cfg = make_config(concurrency_per_worker=5)
    cfg.run.repo_resolution = "static"
    if central_sink is not None:
        # 프랙탈 활성 — 지속 stream-json 세션 전제.
        cfg.run.fractal_central = True
        cfg.run.persistent_session = True
        cfg.run.output_format = "stream-json"
        cfg.run.input_format = "stream-json"
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp, clock=lambda: _FIXED_NOW,
                    central_sink=central_sink)
    return reg, sch, gate, poller


def test_poll_once_reassignment_routes_to_handoff(isolated_state):
    """실행 중 잡(X=u1)의 티켓 담당자가 Y=u2로 바뀌면 poll_once가 핸드오프로 라우팅."""
    reg, sch, gate, poller = _wire_poller([_issue("PROJ-1", "a2", status="진행 중")])
    gate.claim("PROJ-1")
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))         # running(u1)
    assert sch.jobs.get("PROJ-1").status == q.RUNNING

    poller.poll_once()                                    # 담당자 a2(u2) 감지
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.HANDING_OFF                      # 핸드오프 요청됨(취소 아님)
    assert j.control_action == "handoff"
    assert j.user == "u1"                                 # 회신 전까진 X 소유


def test_poll_once_reassignment_queued_routes_to_central_when_fractal(isolated_state):
    """프랙탈 활성 + 큐 대기 잡의 담당자 변경(X=u1→Y=u2): REDISPATCH 를 구 tick 이 아니라
    정상 폴링 티켓과 동일한 센트럴 세션 seam(inject_event)으로 라우팅 + fractal 표식."""
    sink = _FakeCentralSink()
    reg, sch, gate, poller = _wire_poller(
        [_issue("PROJ-2", "a2", status="진행 중")], central_sink=sink)
    gate.claim("PROJ-2")
    sch.jobs.enqueue(_job("PROJ-2", "u1", ["repoA"]))     # queued(u1) — WIP 없음
    assert sch.jobs.get("PROJ-2").status == q.QUEUED

    poller.poll_once()                                    # 담당자 a2(u2) 감지 → REDISPATCH
    # 센트럴 세션으로 주입됨(구 tick 아님).
    assert len(sink.events) == 1 and sink.events[0].ticket == "PROJ-2"
    j = sch.jobs.get("PROJ-2")
    assert j.user == "u2"                                 # Y로 재-소유
    assert j.status == q.QUEUED                           # running 안 만듦(스턱 방지)
    assert j.is_fractal is True                           # 스케줄러 디스패치 제외
    assert sch.tick() == []                               # 프랙탈 잡은 tick 이 건너뛴다


def test_poll_once_same_owner_no_reassign(isolated_state):
    """담당자가 그대로(X==Y)면 재배정하지 않고 dedup 흡수(기존 동작 보존)."""
    reg, sch, gate, poller = _wire_poller([_issue("PROJ-1", "a1", status="진행 중")])
    gate.claim("PROJ-1")
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    poller.poll_once()
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.RUNNING                          # 불변(같은 소유자)
    assert j.control_action == "none"


def test_trigger_ticket_reassignment_when_claimed(isolated_state):
    """웹훅 경로: claim 걸린(라이브 잡) 티켓의 담당자 변경 → 핸드오프 라우팅, True 반환.

    ⚠️ 티켓이 '진행 중'(match.statuses 밖)이어도 재배정은 감지한다(상태 게이트 무관).
    """
    reg, sch, gate, poller = _wire_poller([_issue("PROJ-1", "a2", status="진행 중")])
    gate.claim("PROJ-1")
    sch.enqueue(_job("PROJ-1", "u1", ["repoA"]))
    assert poller.trigger_ticket("PROJ-1") is True        # 재배정 처리됨
    j = sch.jobs.get("PROJ-1")
    assert j.status == q.HANDING_OFF
    assert j.control_action == "handoff"


def test_trigger_ticket_status_mismatch_still_releases_when_no_job(isolated_state):
    """라이브 잡 없이 match 밖 상태면 신규 트리거 안 함 + claim 되돌림(기존 동작 보존)."""
    reg, sch, gate, poller = _wire_poller([_issue("PROJ-9", "a1", status="진행 중")])
    assert poller.trigger_ticket("PROJ-9") is False
    assert gate.is_claimed("PROJ-9") is False             # 방금 얻은 claim 되돌림
    assert sch.jobs.get("PROJ-9") is None
