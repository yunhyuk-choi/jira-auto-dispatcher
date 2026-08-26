"""취소 실행경로 — agent_runner abort(_consume) 단위테스트(§10.4).

⚠️ 프랙탈 P2: 레거시 워커 롤백/회신 경로(app/worker.py::rollback_job·worker_loop)는
제거됐다. 여기서는 취소 신호 → subprocess terminate → STATUS_CANCELLED 로 수렴하는
:func:`app.agent_runner._consume` 만 검증한다(워커 컨테이너 claude 실행의 코어).

라이브 claude/git/네트워크 없음(FakeProc 주입).
"""

from __future__ import annotations

from app import agent_runner as ar


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


def test_consume_cancel_terminates_and_returns_cancelled():
    lines = [
        '{"type":"system","subtype":"init","session_id":"s1"}\n',
        '{"type":"assistant","message":"working"}\n',
        '{"type":"assistant","message":"more"}\n',
    ]
    proc = FakeProc(lines)
    calls = {"n": 0}

    def cancel_check():
        calls["n"] += 1
        return calls["n"] >= 2   # 두 번째 이벤트 시점에 취소 신호

    res = ar._consume(proc, [], cancel_check=cancel_check)
    assert res.status == ar.STATUS_CANCELLED
    assert proc.terminated is True          # subprocess 종료됨
    assert res.session_id == "s1"           # 취소 전까지 파싱분 보존


def test_consume_no_cancel_runs_to_completion():
    lines = ['{"type":"result","subtype":"success","is_error":false,"result":"ok"}\n']
    proc = FakeProc(lines, returncode=0)
    res = ar._consume(proc, [], cancel_check=lambda: False)
    assert res.status == ar.STATUS_DONE
    assert proc.terminated is False
