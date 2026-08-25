"""worker_dispatch.py 단위테스트 — 워커 위임 신뢰 하네스(프랙탈 P2).

라이브 claude/docker 는 절대 호출하지 않는다(subprocess 실행자를 대역). 검증:
    - 커맨드 구성: --session-id(첫) / --resume(이어), 병렬은 distinct sid.
    - 리포트 캡처: BEGIN/END 마커 추출 + 마커 없을 때 stdout 폴백.
    - JSON 반환(stdout) 계약 + status/returncode.
    - CLI main: mutually-exclusive(--session-id|--resume), stdout=JSON.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

import worker_dispatch as wd
from app import central_session as cs
from app import prompts

# 유효 UUID(호출자가 세션을 핀 고정한 경우 — 그대로 존중되는지 검증용).
_UUID = "123e4567-e89b-12d3-a456-426614174000"


def _cfg():
    return SimpleNamespace(run=SimpleNamespace(claude_bin="claude"))


class _FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def _capturing_runner(completed):
    """cmd 를 캡처하고 지정한 CompletedProcess 를 돌려주는 실행자 대역."""
    box = {}

    def _run(cmd, timeout_sec=0):
        box["cmd"] = cmd
        box["timeout_sec"] = timeout_sec
        return completed

    return _run, box


# --- 커맨드 구성(--session-id 첫 / --resume 이어) ----------------------------


def test_dispatch_first_turn_uses_session_id():
    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    wd.run_dispatch(
        "yhchoi", "HAN-1", "지시", session_id="sid-1", resume=False,
        config=_cfg(), runner=runner,
    )
    cmd = box["cmd"]
    assert cmd[:5] == ["docker", "exec", "jad-worker-yhchoi", "claude", "-p"]
    # #2: --output-format json 으로 단일 최종 result 객체를 받는다.
    assert "--output-format" in cmd and cmd[cmd.index("--output-format") + 1] == "json"
    assert "--session-id" in cmd and "--resume" not in cmd
    # #1: 비UUID "sid-1" → (user,ticket) 파생 UUID 로 대체(claude 요건).
    assert cmd[cmd.index("--session-id") + 1] == cs.worker_session_id("yhchoi", "HAN-1")
    assert cmd[-1] == "지시"


def test_dispatch_continuation_uses_resume_same_sid():
    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    wd.run_dispatch(
        "yhchoi", "HAN-1", "추가 지시", session_id="sid-1", resume=True,
        config=_cfg(), runner=runner,
    )
    cmd = box["cmd"]
    assert "--resume" in cmd and "--session-id" not in cmd
    # 같은 (user,ticket) → 첫 턴과 같은 파생 sid 로 재개(AC2).
    assert cmd[cmd.index("--resume") + 1] == cs.worker_session_id("yhchoi", "HAN-1")
    assert cmd[-1] == "추가 지시"


def test_dispatch_explicit_uuid_is_respected():
    """호출자가 유효 UUID 를 주면(세션 핀 고정) 그대로 존중한다."""
    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    wd.run_dispatch("u", "HAN-1", "i", session_id=_UUID, resume=False, config=_cfg(), runner=runner)
    assert box["cmd"][box["cmd"].index("--session-id") + 1] == _UUID


def test_dispatch_parallel_tickets_use_distinct_sids():
    """AC1 배관: 병렬 티켓은 (user,ticket) 파생으로 자동 distinct sid 를 얻는다."""
    r1, b1 = _capturing_runner(_FakeCompleted(stdout="a", returncode=0))
    r2, b2 = _capturing_runner(_FakeCompleted(stdout="b", returncode=0))
    # sid 미지정(에이전트가 손수 만들지 않는다) — 하네스가 티켓별로 파생.
    wd.run_dispatch("u", "HAN-1", "i1", session_id=None, resume=False, config=_cfg(), runner=r1)
    wd.run_dispatch("u", "HAN-2", "i2", session_id=None, resume=False, config=_cfg(), runner=r2)
    sid1 = b1["cmd"][b1["cmd"].index("--session-id") + 1]
    sid2 = b2["cmd"][b2["cmd"].index("--session-id") + 1]
    assert sid1 == cs.worker_session_id("u", "HAN-1")
    assert sid2 == cs.worker_session_id("u", "HAN-2")
    assert sid1 != sid2


# --- 리포트 캡처(마커 추출 + 폴백) -------------------------------------------


def test_report_extracted_from_markers():
    body = "무슨 작업/레포/MR/테스트 요약"
    stdout = (
        "잡음 라인\n"
        f"{prompts.REPORT_BEGIN} HAN-7===\n{body}\n{prompts.REPORT_END} HAN-7===\n"
        "꼬리 잡음\n"
    )
    runner, _ = _capturing_runner(_FakeCompleted(stdout=stdout, returncode=0))
    result = wd.run_dispatch(
        "u", "HAN-7", "i", session_id="s", resume=False, config=_cfg(), runner=runner,
    )
    assert result["report_extracted"] is True
    assert result["report"] == body
    assert result["status"] == "ok"
    assert result["returncode"] == 0
    # #1: 비UUID "s" → (user,ticket) 파생 UUID 가 반환 JSON 에도 실린다(안정 sid).
    assert result["session_id"] == cs.worker_session_id("u", "HAN-7")
    assert result["mode"] == "session-id"
    assert result["container"] == "jad-worker-u"


def test_report_falls_back_to_full_stdout_when_no_markers():
    runner, _ = _capturing_runner(_FakeCompleted(stdout="  마커 없는 전문 출력  ", returncode=0))
    result = wd.run_dispatch(
        "u", "HAN-8", "i", session_id="s", resume=True, config=_cfg(), runner=runner,
    )
    assert result["report_extracted"] is False
    assert result["report"] == "마커 없는 전문 출력"
    assert result["mode"] == "resume"


def test_report_extracted_from_output_format_json_result():
    """#2: --output-format json 의 단일 result 객체에서 최종 리포트를 결정적으로 뽑는다."""
    body = "무슨 작업/레포/MR/테스트 요약"
    result_text = f"{prompts.REPORT_BEGIN} HAN-7===\n{body}\n{prompts.REPORT_END} HAN-7==="
    stdout = json.dumps({"type": "result", "subtype": "success",
                         "result": result_text, "is_error": False}, ensure_ascii=False)
    runner, _ = _capturing_runner(_FakeCompleted(stdout=stdout, returncode=0))
    result = wd.run_dispatch("u", "HAN-7", "i", session_id=None, resume=False, config=_cfg(), runner=runner)
    assert result["report_extracted"] is True
    assert result["report"] == body


def test_json_result_without_markers_uses_result_text():
    """마커가 없어도 result 텍스트 전체를 리포트로 쓴다(빈 리포트로 조용히 반환 금지)."""
    stdout = json.dumps({"type": "result", "result": "마커 없는 최종 리포트 본문"}, ensure_ascii=False)
    runner, _ = _capturing_runner(_FakeCompleted(stdout=stdout, returncode=0))
    result = wd.run_dispatch("u", "HAN-8", "i", session_id=None, resume=False, config=_cfg(), runner=runner)
    assert result["report_extracted"] is False
    assert result["report"] == "마커 없는 최종 리포트 본문"


def test_empty_result_falls_back_to_full_stdout():
    """#2: result 가 비어도 stdout 전문으로 폴백(빈 리포트로 조용히 반환하지 않는다)."""
    stdout = json.dumps({"type": "result", "result": ""}, ensure_ascii=False)
    runner, _ = _capturing_runner(_FakeCompleted(stdout=stdout, returncode=0))
    result = wd.run_dispatch("u", "HAN-8", "i", session_id=None, resume=False, config=_cfg(), runner=runner)
    assert result["report"]  # 비어 있지 않다
    assert stdout in result["report"] or result["report"] == stdout.strip()


def test_nonzero_returncode_maps_to_error_status():
    runner, _ = _capturing_runner(_FakeCompleted(stdout="", stderr="boom", returncode=2))
    result = wd.run_dispatch(
        "u", "HAN-9", "i", session_id="s", resume=False, config=_cfg(), runner=runner,
    )
    assert result["status"] == "error"
    assert result["returncode"] == 2


# --- #5-실행: ROLE=worker 컨텍스트 거부(self-exec 재귀 백스톱) ------------------


def test_dispatch_refuses_under_role_worker():
    """워커(ROLE=worker)가 worker_dispatch 를 부르면 exec 하지 않고 거부한다 —
    self-exec 재귀·라이브락 방지. runner 는 절대 호출되지 않아야 한다."""
    called = {"n": 0}

    def _runner(cmd, timeout_sec=0):
        called["n"] += 1
        return _FakeCompleted(stdout="should-not-run", returncode=0)

    result = wd.run_dispatch(
        "yhchoi", "HAN-1", "지시", session_id=None, resume=False,
        config=_cfg(), runner=_runner, env={"ROLE": "worker", "DISPATCH_USER": "yhchoi"},
    )
    assert called["n"] == 0  # exec 하지 않는다
    assert result["status"] == "refused"
    assert result["returncode"] is None
    assert "ROLE=central 전용" in result["report"]


def test_dispatch_proceeds_under_role_central():
    """ROLE=central 컨텍스트면 정상 위임한다(거부 백스톱은 worker 에만 건다)."""
    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    result = wd.run_dispatch(
        "u", "HAN-1", "i", session_id=None, resume=False,
        config=_cfg(), runner=runner, env={"ROLE": "central"},
    )
    assert "cmd" in box  # exec 됐다
    assert result["status"] == "ok"


def test_dispatch_proceeds_when_role_unset():
    """ROLE 미설정(테스트/기본)이면 거부하지 않는다 — 기존 동작 회귀 방지."""
    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    result = wd.run_dispatch(
        "u", "HAN-1", "i", session_id=None, resume=False,
        config=_cfg(), runner=runner, env={},
    )
    assert "cmd" in box
    assert result["status"] == "ok"


def test_timeout_maps_to_timeout_status():
    import subprocess

    def _run(cmd, timeout_sec=0):
        raise subprocess.TimeoutExpired(cmd, timeout_sec)

    result = wd.run_dispatch(
        "u", "HAN-10", "i", session_id="s", resume=False, config=_cfg(),
        runner=_run, timeout_sec=5,
    )
    assert result["status"] == "timeout"
    assert result["returncode"] is None


# --- CLI main(JSON stdout, mutually-exclusive) -------------------------------


def test_main_emits_json_on_stdout(monkeypatch, capsys):
    monkeypatch.setattr(wd, "_load_config", lambda p: _cfg())
    monkeypatch.setattr(
        wd, "run_dispatch",
        lambda *a, **k: {"ticket": "HAN-1", "status": "ok", "report": "R", "session_id": k["session_id"]},
    )
    rc = wd.main(["--user", "u", "--ticket", "HAN-1", "--instruction", "i", "--session-id", "s1"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ticket"] == "HAN-1" and out["status"] == "ok"
    assert out["session_id"] == "s1"


def test_main_resume_flag_sets_resume_mode(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(wd, "_load_config", lambda p: _cfg())

    def _fake(user, ticket, instruction, *, session_id, resume, config, timeout_sec=0,
              recorder=None):
        seen.update(session_id=session_id, resume=resume)
        return {"status": "error"}

    monkeypatch.setattr(wd, "run_dispatch", _fake)
    rc = wd.main(["--user", "u", "--ticket", "T", "--instruction", "i", "--resume", "s9"])
    assert seen == {"session_id": "s9", "resume": True}
    assert rc == 1  # status != ok → exit 1


def test_main_without_session_flags_is_first_turn_derived(monkeypatch, capsys):
    """#1: sid 플래그 없이도 유효(첫 턴) — 하네스가 (user,ticket)에서 파생한다."""
    seen = {}
    monkeypatch.setattr(wd, "_load_config", lambda p: _cfg())

    def _fake(user, ticket, instruction, *, session_id, resume, config, timeout_sec=0,
              recorder=None):
        seen.update(session_id=session_id, resume=resume)
        return {"status": "ok", "session_id": "x"}

    monkeypatch.setattr(wd, "run_dispatch", _fake)
    rc = wd.main(["--user", "u", "--ticket", "T", "--instruction", "i"])
    assert rc == 0
    assert seen == {"session_id": None, "resume": False}


def test_main_resume_flag_without_value_is_resume_mode(monkeypatch):
    """--resume 를 값 없이 줘도 이어가기 모드 — sid 는 하네스가 파생한다(#1)."""
    seen = {}
    monkeypatch.setattr(wd, "_load_config", lambda p: _cfg())

    def _fake(user, ticket, instruction, *, session_id, resume, config, timeout_sec=0,
              recorder=None):
        seen.update(session_id=session_id, resume=resume)
        return {"status": "ok"}

    monkeypatch.setattr(wd, "run_dispatch", _fake)
    wd.main(["--user", "u", "--ticket", "T", "--instruction", "i", "--resume"])
    assert seen == {"session_id": None, "resume": True}


def test_main_rejects_both_session_id_and_resume():
    with pytest.raises(SystemExit):
        wd.main(["--user", "u", "--ticket", "T", "--instruction", "i",
                 "--session-id", "a", "--resume", "b"])


# --- 진행중(in-progress) Jira 전이 — 착수 시 해야할일→진행중(런타임 발견·멱등) ---------


class _FakeJira:
    """JiraClient 대역 — get_issue/list_transitions/transition 호출을 스크립트/기록한다."""

    def __init__(self, *, status_category="new", status_name="", transitions=None):
        self._status_category = status_category
        self._status_name = status_name
        self._transitions = transitions or []
        self.transitions_done = []   # (key, id) 기록
        self.done_calls = []         # transition_done 호출(완료 전이 금지 검증)

    def get_issue(self, key, fields=None):
        return {"fields": {"status": {
            "name": self._status_name,
            "statusCategory": {"key": self._status_category},
        }}}

    def list_transitions(self, key):
        return self._transitions

    def transition(self, key, transition_id, fields=None):
        self.transitions_done.append((key, str(transition_id)))

    def transition_done(self, *a, **k):  # 존재하되 호출되면 실패해야 한다(범위 밖)
        self.done_calls.append((a, k))


# 지상검증 실측 유사 전이 목록: **indeterminate 가 둘**(보류 id=2 + 진행 중 id=21) — 이름으로
# 확정하지 않으면 보류로 잘못 갈 수 있는 실제 워크플로우 형태. 재오픈(new)·완료(done) 혼재.
_TRANSITIONS = [
    {"id": "11", "name": "재오픈", "to": {"name": "해야 할 일", "statusCategory": {"key": "new"}}},
    {"id": "2", "name": "보류", "to": {"name": "보류", "statusCategory": {"key": "indeterminate"}}},
    {"id": "21", "name": "진행 시작", "to": {"name": "진행 중", "statusCategory": {"key": "indeterminate"}}},
    {"id": "41", "name": "완료", "to": {"name": "완료", "statusCategory": {"key": "done"}}},
]


def test_transition_called_before_exec_on_dispatch():
    """(a) run_dispatch 착수 시 주입된 전이자가 exec **전에** 불린다."""
    order = []

    def _runner(cmd, timeout_sec=0):
        order.append("exec")
        return _FakeCompleted(stdout="ok", returncode=0)

    def _transitioner(user, ticket, config):
        order.append("transition")
        return {"transitioned": True, "transition_id": "21"}

    result = wd.run_dispatch(
        "u", "HAN-1", "i", session_id=None, resume=False, config=_cfg(),
        runner=_runner, jira_transitioner=_transitioner,
    )
    assert order == ["transition", "exec"]   # 전이가 exec 앞
    assert result["status"] == "ok"          # 디스패치는 정상 진행


def test_picks_in_progress_not_on_hold_when_two_indeterminate():
    """(a-핵심 회귀) indeterminate 가 둘(보류+진행중)일 때 **이름 매칭으로 '진행 중'을 고른다** —
    '보류'(id=2)로 가는 지상검증 결함을 막는다."""
    chosen, reason, candidates = wd._find_in_progress_transition(_TRANSITIONS)
    assert chosen["id"] == "21"            # 진행 중(21), 보류(2) 아님
    assert reason is None
    assert set(candidates) == {"보류", "진행 중"}
    # 실 경로: 현재 new → '진행 중'(21)로 POST, 보류(2) 아님, 완료(41) 아님.
    fake = _FakeJira(status_category="new", status_name="해야 할 일", transitions=_TRANSITIONS)
    res = wd._do_in_progress_transition(fake, "HAN-2")
    assert res["transitioned"] is True
    assert res["transition_id"] == "21"
    assert res["to_status"] == "진행 중"
    assert fake.transitions_done == [("HAN-2", "21")]
    assert fake.done_calls == []           # (e) 완료 전이 안 함


def test_single_indeterminate_without_name_match_is_used():
    """(b) 이름 매칭이 없고 indeterminate 가 정확히 1개면 그걸 쓴다(모호성 없음)."""
    trs = [
        {"id": "11", "name": "재오픈", "to": {"name": "해야 할 일", "statusCategory": {"key": "new"}}},
        {"id": "7", "name": "커스텀 이동", "to": {"name": "검토 대기", "statusCategory": {"key": "indeterminate"}}},
        {"id": "41", "name": "완료", "to": {"name": "완료", "statusCategory": {"key": "done"}}},
    ]
    chosen, reason, _ = wd._find_in_progress_transition(trs)
    assert chosen["id"] == "7"
    assert reason is None
    fake = _FakeJira(status_category="new", status_name="해야 할 일", transitions=trs)
    res = wd._do_in_progress_transition(fake, "HAN-7")
    assert res["transitioned"] is True and res["transition_id"] == "7"


def test_ambiguous_multiple_indeterminate_no_name_match_skips():
    """(c) indeterminate 가 여럿인데 이름 매칭이 없으면 고르지 않고 skip(ambiguous·후보 기록)."""
    trs = [
        {"id": "5", "name": "보류로", "to": {"name": "보류", "statusCategory": {"key": "indeterminate"}}},
        {"id": "6", "name": "검토로", "to": {"name": "검토 대기", "statusCategory": {"key": "indeterminate"}}},
    ]
    chosen, reason, candidates = wd._find_in_progress_transition(trs)
    assert chosen is None
    assert reason == "ambiguous-in-progress"
    assert set(candidates) == {"보류", "검토 대기"}
    fake = _FakeJira(status_category="new", status_name="해야 할 일", transitions=trs)
    res = wd._do_in_progress_transition(fake, "HAN-8")
    assert res["skipped"] is True
    assert res["reason"] == "ambiguous-in-progress"
    assert set(res["candidates"]) == {"보류", "검토 대기"}
    assert fake.transitions_done == []            # 잘못 고르느니 안 옮긴다


def test_on_hold_is_not_treated_as_in_progress():
    """(d) 현재 상태가 '보류'(indeterminate지만 진행중 아님)면 skip 하지 않고 진행중으로 전이한다."""
    fake = _FakeJira(status_category="indeterminate", status_name="보류", transitions=_TRANSITIONS)
    res = wd._do_in_progress_transition(fake, "HAN-9")
    assert res["transitioned"] is True
    assert res["transition_id"] == "21"           # 보류 → 진행 중
    assert fake.transitions_done == [("HAN-9", "21")]


def test_idempotent_skip_when_already_in_progress():
    """(멱등) 현재 status **이름**이 in-progress 집합이면 재전이하지 않는다(--resume 재개 방어).

    '진행 중'은 공백/NFC 변형까지 정규화 매칭된다(카테고리가 아니라 이름 기준)."""
    fake = _FakeJira(status_category="indeterminate", status_name="진행 중", transitions=_TRANSITIONS)
    res = wd._do_in_progress_transition(fake, "HAN-3")
    assert res["skipped"] is True
    assert res["reason"] == "already-in-progress"
    assert fake.transitions_done == []            # POST 안 함


def test_no_in_progress_transition_available_skips():
    """가용 전이에 진행중류(indeterminate)가 없으면 조용히 성공하지 않고 사유를 남긴다."""
    only_done = [{"id": "41", "name": "완료", "to": {"name": "완료", "statusCategory": {"key": "done"}}}]
    fake = _FakeJira(status_category="new", status_name="해야 할 일", transitions=only_done)
    res = wd._do_in_progress_transition(fake, "HAN-4")
    assert res["skipped"] is True
    assert res["reason"] == "no-in-progress-transition"
    assert fake.transitions_done == []


def test_transition_failure_does_not_break_dispatch():
    """(d) 전이가 예외를 던져도 디스패치는 계속(exec 실행)·결과가 meta 에 기록된다."""
    recorded = []

    def _recorder(ticket, *, status=None, **fields):
        recorded.append((ticket, status, fields))

    def _boom(user, ticket, config):
        raise RuntimeError("jira down")

    runner, box = _capturing_runner(_FakeCompleted(stdout="ok", returncode=0))
    result = wd.run_dispatch(
        "u", "HAN-5", "i", session_id=None, resume=False, config=_cfg(),
        runner=runner, jira_transitioner=_boom, recorder=_recorder,
    )
    assert "cmd" in box                # exec 됐다(디스패치 계속)
    assert result["status"] == "ok"
    # 전이 결과가 잡 meta(jira_in_progress)에 실렸고, 예외가 조용히 성공 처리되지 않았다.
    tr = [f for (_t, _s, f) in recorded if "jira_in_progress" in f]
    assert tr and tr[0]["jira_in_progress"]["error"] == "RuntimeError"
    assert tr[0]["jira_in_progress"]["transitioned"] is False


def test_default_transitioner_skips_without_jira_config():
    """기본 전이자는 jira.base_url 이 없으면 네트워크/레지스트리 접근 없이 즉시 skip 한다.
    (기존 테스트들이 _cfg() 로 run_dispatch 를 불러도 라이브 호출이 없음을 보장)."""
    res = wd._default_jira_transitioner("u", "HAN-6", _cfg())
    assert res["skipped"] is True
    assert res["reason"] == "no-jira-config"


def test_done_transition_is_never_used():
    """(e) 이 경로는 완료(done) 전이를 절대 고르지 않는다 — 진행중만."""
    # done 카테고리만 있는 경우에도 진행중 전이로 오인해 POST 하지 않는다.
    only_done = [{"id": "41", "name": "완료", "to": {"name": "완료", "statusCategory": {"key": "done"}}}]
    chosen, reason, _ = wd._find_in_progress_transition(only_done)
    assert chosen is None
    assert reason == "no-in-progress-transition"


def test_status_name_normalization_covers_whitespace_and_case():
    """이름 정규화(NFC·공백제거·casefold)가 변형을 커버한다."""
    n = wd._normalize_status_name
    assert n("진행 중") == n("진행중")
    assert n("In Progress") == n("in progress") == n("INPROGRESS")
    assert n("  진행  중  ") == n("진행중")
    assert n("보류") not in wd._IN_PROGRESS_NORM      # 보류는 in-progress 아님
    assert n("진행 중") in wd._IN_PROGRESS_NORM


# --- config 경로 앵커링(/app) — jira 전이 비결정적 skip 근본 해소 ------------


def test_resolve_config_path_anchors_relative_default_to_app(monkeypatch, tmp_path):
    """센트럴 서브 cwd(/app 아님)에서 --config 없이 실행해도 /app/config/config.yaml 로 앵커.

    라이브 티켓 555 지상검증: cwd=<workspace_dir>/orchestrator 에서 상대 config/config.yaml
    은 존재하지 않는 <cwd>/config/config.yaml 로 해석돼 config 로드 실패 → jira 전이가
    no-jira-config 로 skip 됐다. 이제 cwd 와 무관하게 /app 로 앵커한다."""
    monkeypatch.delenv("JAD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd 를 /app 아닌 곳(orchestrator cwd 모사)으로.
    expected = os.path.join("/app", "config/config.yaml")
    # argparse 기본값(상대 config/config.yaml) — cwd 와 무관하게 /app 앵커.
    assert wd._resolve_config_path("config/config.yaml") == expected
    # 미지정(None)도 동일 기본값으로 앵커.
    assert wd._resolve_config_path(None) == expected


def test_resolve_config_path_respects_absolute(monkeypatch, tmp_path):
    """절대경로 --config 는 그대로 존중(이미 절대경로로 주던 경로 불변)."""
    monkeypatch.delenv("JAD_CONFIG", raising=False)
    abs_cfg = str(tmp_path / "custom.yaml")
    (tmp_path / "custom.yaml").write_text("x", encoding="utf-8")
    assert wd._resolve_config_path(abs_cfg) == abs_cfg


def test_resolve_config_path_falls_back_when_anchored_missing(monkeypatch):
    """상대경로를 /app 앵커했으나 그 경로가 없으면 표준 /app/config/config.yaml 로 폴백."""
    monkeypatch.delenv("JAD_CONFIG", raising=False)
    fallback = os.path.join("/app", "config/config.yaml")
    # 폴백(/app/config/config.yaml)만 존재하는 파일시스템을 모사.
    monkeypatch.setattr(wd.os.path, "exists", lambda p: p == fallback)
    assert wd._resolve_config_path("weird/other.yaml") == fallback


def test_resolve_config_path_env_override_wins(monkeypatch):
    """JAD_CONFIG env(배포 오버라이드/테스트 격리)는 상대/절대 모두에 우선한다(gchat 패턴)."""
    monkeypatch.setenv("JAD_CONFIG", "/etc/jad/config.yaml")
    assert wd._resolve_config_path("config/config.yaml") == "/etc/jad/config.yaml"
    assert wd._resolve_config_path("/abs/other.yaml") == "/etc/jad/config.yaml"


def test_main_loads_config_via_app_anchored_path(monkeypatch, capsys, tmp_path):
    """main 이 상대 --config(기본값)를 /app 앵커해 _load_config 에 넘긴다(cwd 무관).

    회귀 방지: config 가 항상 /app/config/config.yaml 로 로드돼야 jira 전이 등 config
    의존 로직이 결정적으로 동작한다."""
    monkeypatch.delenv("JAD_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # cwd 를 /app 아닌 곳으로.
    seen = {}

    def _fake_load(p):
        seen["path"] = p
        return _cfg()

    monkeypatch.setattr(wd, "_load_config", _fake_load)
    monkeypatch.setattr(wd, "run_dispatch", lambda *a, **k: {"status": "ok"})
    rc = wd.main(["--user", "u", "--ticket", "T", "--instruction", "i"])
    assert rc == 0
    assert seen["path"] == os.path.join("/app", "config/config.yaml")
