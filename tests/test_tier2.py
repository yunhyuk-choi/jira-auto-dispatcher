"""Phase 3b-2 — 파일럿 Tier-2 러너 + 피처 플래그 단위테스트.

커버:
    - **플래그 OFF = 무동작변경**: build_pilot_tier2 → None, 디스패치/회신 전이가 오늘과 동일.
    - build_pilot_tier2 ON: 파일럿 사용자에 바인딩된 tools+runner 조립(자동 실행 안 함).
    - MockTier2Runner: propose→dispatch→collect를 결정적으로 수행, **파일럿만**(타 사용자 무영향).
    - propose_order: interrupted_ready 우선 + ticket id 사전순(결정적).
    - SdkTier2Runner: SDK 미설치 시 Tier2SdkUnavailable(조용한 실패 금지).
    - make_tier2_runner: 알 수 없는 백엔드 거부.

⚠️ 라이브 에이전트 런타임(실제 SDK 에이전트의 실시간 판단)은 CI에서 검증되지 않는다 —
배포 후 오케스트레이터가 검증할 항목(러너 자동기동/실 SDK 배선은 후속 MR).
"""

from __future__ import annotations

from types import SimpleNamespace

from app import queue as q
from app.dispatch import Dispatcher
from app.queue import Job, JobQueue
from app.registry import Registry, UserRecord
from app.scheduler import Scheduler
from app.tier2 import (
    MockTier2Runner,
    SdkTier2Runner,
    Tier2SdkUnavailable,
    build_pilot_tier2,
    make_tier2_runner,
)
from app.tier2_tools import Tier2Tools
from tests.conftest import make_config


class _FakeWriter:
    def __init__(self, relpath="n/cycles/t2"):
        self.relpath = relpath

    def commit_cycle_log(self, job):
        return self.relpath


def _wire(writer=None):
    reg = Registry()
    reg.upsert(UserRecord(username="pilot", jira_account_id="a1", enabled=True))
    reg.upsert(UserRecord(username="other", jira_account_id="a2", enabled=True))
    sch = Scheduler(make_config(), JobQueue())
    disp = Dispatcher(reg, sch, dlc_meta_writer=writer)
    return reg, sch, disp


def _components(disp, sch, pilot=""):
    """build_pilot_tier2용 최소 components dict(cfg.run.tier2_pilot_user 플래그 포함)."""
    cfg = SimpleNamespace(run=SimpleNamespace(tier2_pilot_user=pilot))
    return {"config": cfg, "dispatcher": disp, "scheduler": sch}


# ---------------------------------------------------------------------------
# 플래그 OFF = 무동작변경(핵심 안전 증명)
# ---------------------------------------------------------------------------


def test_flag_off_build_pilot_tier2_returns_none(isolated_state):
    _, sch, disp = _wire()
    # 빈 문자열/미설정 → None(전체 Tier-2 경로 비활성).
    assert build_pilot_tier2(_components(disp, sch, pilot="")) is None
    # run 속성 자체가 없어도(구 config) 안전하게 None.
    assert build_pilot_tier2({"config": SimpleNamespace(), "dispatcher": disp}) is None


def test_flag_off_dispatch_is_byte_for_byte_unchanged(isolated_state):
    """플래그 OFF 상태에서 enqueue→dispatch→완료 회신 전이가 3b-1(오늘)과 동일.

    Tier-2 스캐폴드가 존재해도 플래그가 꺼져 있으면 아무 경로도 그것을 부르지 않으므로
    디스패치 계약(응답 dict·잡 상태 전이·완료-구동 다음 dispatch)이 그대로다.
    """
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/bc"))
    # Tier-2 파일럿 조립 시도(플래그 OFF) → None, components에 아무 것도 안 붙는다.
    assert build_pilot_tier2(_components(disp, sch, pilot="")) is None

    disp.enqueue("pilot", Job(ticket="P-1", target_repos=["repoA"]))
    disp.enqueue("pilot", Job(ticket="P-2", target_repos=["repoA"]))  # 같은 레포 대기

    res = disp.report_status("pilot", "P-1", {"status": "완료", "mr_url": "http://mr"})
    # 기존 응답 계약 그대로(3b-1 test_pending 와 동일한 불변식).
    assert res["ok"] is True
    assert res["dispatched"] == ["P-2"]             # 완료-구동 다음 dispatch
    assert res["cycle_log_path"] == "n/cycles/bc"
    assert sch.jobs.get("P-1").status == q.DONE
    assert sch.jobs.get("P-1").mr_url == "http://mr"
    assert sch.jobs.get("P-2").status == q.RUNNING
    # 위임 마커가 붙지 않았다(Tier-2 미개입) → 순수 폴러 경로, pending 집합 비어 있음.
    assert sch.jobs.get("P-1").correlation_id is None
    assert disp.pending.pending_ids() == []


# ---------------------------------------------------------------------------
# 플래그 ON: 조립(자동 실행 안 함)
# ---------------------------------------------------------------------------


def test_flag_on_builds_tools_and_runner_no_autostart(isolated_state):
    _, sch, disp = _wire()
    built = build_pilot_tier2(_components(disp, sch, pilot="pilot"))
    assert built is not None
    assert built["user"] == "pilot"
    assert built["backend"] == "mock"
    assert isinstance(built["tools"], Tier2Tools)
    assert isinstance(built["runner"], MockTier2Runner)
    # 조립만 — 러너는 아직 아무 것도 실행하지 않았다(잡 없음, pending 없음).
    assert disp.pending.pending_ids() == []


# ---------------------------------------------------------------------------
# MockTier2Runner: propose→dispatch→collect (파일럿만)
# ---------------------------------------------------------------------------


def test_propose_order_deterministic():
    tools = None  # propose_order는 정적 — tools 불요.
    elig = [
        {"ticket": "P-3", "reason": "queued"},
        {"ticket": "P-1", "reason": "interrupted_ready"},
        {"ticket": "P-2", "reason": "queued"},
    ]
    order = [e["ticket"] for e in MockTier2Runner.propose_order(elig)]
    # interrupted_ready 우선, 그 다음 사전순.
    assert order == ["P-1", "P-2", "P-3"]


def test_mock_runner_dispatches_and_collects_pilot_only(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/run"))
    # 큐에 직접 넣어(스케줄러 자동 dispatch 우회) queued 적격 상태를 만든다.
    # 파일럿 2건(서로 다른 레포 → 둘 다 dispatch 가능), 타 사용자 1건.
    sch.jobs.enqueue(Job(ticket="P-1", user="pilot", target_repos=["repoA"], status=q.QUEUED))
    sch.jobs.enqueue(Job(ticket="P-2", user="pilot", target_repos=["repoB"], status=q.QUEUED))
    sch.jobs.enqueue(Job(ticket="O-1", user="other", target_repos=["repoC"], status=q.QUEUED))

    tools = Tier2Tools(disp, "pilot")
    runner = MockTier2Runner(tools)
    report = runner.run_once()

    # 파일럿 티켓만 제안·디스패치(타 사용자 O-1 제외 = 격리).
    assert report["user"] == "pilot"
    assert report["backend"] == "mock"
    assert report["proposed_order"] == ["P-1", "P-2"]
    assert set(report["correlation_ids"]) == {"P-1", "P-2"}
    assert all(d["dispatched"] for d in report["dispatched"])
    assert sch.jobs.get("P-1").status == q.RUNNING
    assert sch.jobs.get("P-2").status == q.RUNNING

    # ⚠️ 타 사용자 잡은 파일럿 러너가 건드리지 않는다: 위임 등록 없음.
    assert "O-1" not in disp.pending.pending_ids()
    # (O-1은 tick으로 파이썬이 dispatch할 수 있으나, 이는 기존 전역 강제이지 파일럿 개입이
    #  아니다 — 파일럿의 pending/collect 집합에는 절대 안 들어온다.)
    assert "O-1" not in report["correlation_ids"]

    # 아직 미완 → drained False, pending에 파일럿 2건.
    assert report["drained"] is False
    assert set(report["pending_ids"]) == {"P-1", "P-2"}

    # 워커 완료 회신 후 collect가 결과를 리뷰로 surfacing.
    disp.report_status("pilot", "P-1", {"status": "완료", "mr_url": "http://mr/1"})
    report2 = runner.run_once()   # 두 번째 드레인 패스(P-1 해소 반영)
    # P-1은 리뷰에 done으로 뜬다(직전 라운드 위임의 완료를 회수)? run_once는 이번 라운드
    # 디스패치분만 collect하므로, 완료 회수는 tools.collect로 직접 확인한다.
    got = tools.collect(["P-1"])
    assert got["P-1"]["status"] == "done"
    assert got["P-1"]["result"]["mr_url"] == "http://mr/1"
    assert got["P-1"]["result"]["cycle_log_path"] == "n/cycles/run"


def test_mock_runner_review_surfaces_terminal_results(isolated_state):
    _, sch, disp = _wire(writer=_FakeWriter(relpath="n/cycles/rev"))
    sch.jobs.enqueue(Job(ticket="P-1", user="pilot", target_repos=["repoA"], status=q.QUEUED))
    tools = Tier2Tools(disp, "pilot")
    runner = MockTier2Runner(tools)
    runner.run_once()                       # dispatch P-1 (running)
    disp.report_status("pilot", "P-1", {"status": "완료", "mr_url": "http://mr/x"})

    # 완료 후 같은 위임을 다시 collect+review(러너 내부 _review 경로).
    reviewed = MockTier2Runner._review(tools.collect(["P-1"]))
    assert reviewed["P-1"]["ok"] is True
    assert reviewed["P-1"]["mr_url"] == "http://mr/x"
    assert reviewed["P-1"]["cycle_log_path"] == "n/cycles/rev"


# ---------------------------------------------------------------------------
# SDK 러너: 미배선 명시 실패 + 팩토리
# ---------------------------------------------------------------------------


def test_sdk_runner_unavailable_raises(isolated_state):
    _, sch, disp = _wire()
    tools = Tier2Tools(disp, "pilot")
    # claude-agent-sdk 미설치(CI 기본) → 생성 시 명확한 예외.
    try:
        SdkTier2Runner(tools)
        assert False, "SDK 미설치인데 SdkTier2Runner가 생성됨"
    except Tier2SdkUnavailable:
        pass


def test_make_tier2_runner_backends(isolated_state):
    _, sch, disp = _wire()
    tools = Tier2Tools(disp, "pilot")
    assert isinstance(make_tier2_runner(tools, backend="mock"), MockTier2Runner)
    # 알 수 없는 백엔드는 거부.
    try:
        make_tier2_runner(tools, backend="nope")
        assert False
    except ValueError:
        pass
