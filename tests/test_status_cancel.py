"""취소 실행경로 — agent_runner abort + worker 롤백/회신 단위테스트(§10.4).

라이브 claude/git/네트워크 없음(FakeProc·주입 stub).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import agent_runner as ar
from app import worker as w


# --- agent_runner: 취소 시 subprocess terminate + cancelled 반환 --------------


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


# --- rollback_job: best-effort(브랜치 없음 스킵 / 있으면 삭제 + MR 닫기) ------


def _rb_cfg():
    return SimpleNamespace(run=SimpleNamespace(workspace_dir="/ws"), secrets=SimpleNamespace(base_dir=""))


def test_rollback_skips_when_branch_absent():
    calls = []

    def git_run(args, cwd=None):
        calls.append(list(args))
        return SimpleNamespace(returncode=1, stdout="", stderr="")  # rev-parse → 없음

    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    rb = w.rollback_job(job, ar.UserCreds(user="u1"), _rb_cfg(),
                        git_run=git_run, mr_closer=lambda *a, **k: False)
    assert rb["rolledback"] is False
    assert rb["branch_deleted"] is False
    # 존재 확인만 하고 실제 삭제(branch -D)는 호출하지 않는다.
    assert not any(a[:2] == ["branch", "-D"] for a in calls)


def test_rollback_deletes_branch_and_closes_mr():
    seen = []

    def git_run(args, cwd=None):
        seen.append(list(args))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    closed = {}

    def mr_closer(mr_url, creds, config):
        closed["url"] = mr_url
        return True

    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"],
           "mr_url": "https://gitlab.example.com/g/p/-/merge_requests/3"}
    rb = w.rollback_job(job, ar.UserCreds(user="u1"), _rb_cfg(),
                        git_run=git_run, mr_closer=mr_closer)
    assert rb["rolledback"] is True
    assert rb["branch_deleted"] is True
    assert rb["mr_closed"] is True
    assert closed["url"].endswith("merge_requests/3")
    assert ["branch", "-D", "auto/PROJ-1"] in seen           # 로컬 삭제
    assert ["push", "origin", "--delete", "auto/PROJ-1"] in seen  # 원격 삭제


# --- worker 루프: 실행 중 취소 → 롤백 + cancelled 회신 -----------------------


class FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


class CtrlHTTP:
    """next 1회 + control(cancel=true) 응답 + post 기록."""

    def __init__(self, job, control_cancel=True):
        self.job = job
        self.control_cancel = control_cancel
        self._next_served = False
        self.posts = []
        self.control_polls = 0

    def get(self, url, headers=None):
        if url.endswith("/next"):
            if not self._next_served:
                self._next_served = True
                return FakeResp(200, self.job)
            return FakeResp(204)
        if url.endswith("/control"):
            self.control_polls += 1
            return FakeResp(200, {"cancel": self.control_cancel})
        return FakeResp(204)

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return FakeResp(200, {})


def _cfg():
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json", workspace_dir="/ws"),
        secrets=SimpleNamespace(base_dir=""),
    )


_ENV = {"DISPATCH_USER": "u1", "CENTRAL_URL": "http://central:8787"}
_NOW = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)


def _statuses(http):
    return [p[1].get("status") for p in http.posts]


def test_worker_cancel_triggers_rollback_and_reports_cancelled():
    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    http = CtrlHTTP(job, control_cancel=True)
    rb_calls = {}

    def run(job_, creds, cfg, cancel_check=None, **k):
        # agent_runner를 흉내: cancel_check가 취소를 보고하면 cancelled 반환.
        if cancel_check and cancel_check():
            return ar.AgentResult(status=ar.STATUS_CANCELLED, session_id="s1",
                                  log_summary="aborted")
        return ar.AgentResult(status=ar.STATUS_DONE)

    def rollback(job_, creds, cfg):
        rb_calls["job"] = job_
        return {"rolledback": True, "branch_deleted": True, "mr_closed": False}

    w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run,
                  resume=lambda *a, **k: None, sleep=lambda s: None, now=_NOW,
                  rollback=rollback, max_iterations=1)

    assert _statuses(http) == ["진행중", "cancelled"]
    assert http.control_polls >= 1              # control 폴링으로 취소 감지
    assert rb_calls["job"]["ticket"] == "PROJ-1"  # 롤백 수행
    cancel_payload = http.posts[-1][1]
    assert cancel_payload["rolledback"] is True
    assert cancel_payload["branch"] == "auto/PROJ-1"


def test_worker_no_cancel_runs_to_done():
    job = {"ticket": "PROJ-2", "branch": "auto/PROJ-2", "target_repos": ["repoA"]}
    http = CtrlHTTP(job, control_cancel=False)
    rb_calls = {"n": 0}

    def run(job_, creds, cfg, cancel_check=None, **k):
        if cancel_check:
            cancel_check()                     # 폴링해도 취소 아님
        return ar.AgentResult(status=ar.STATUS_DONE, session_id="s2")

    def rollback(*a, **k):
        rb_calls["n"] += 1
        return {"rolledback": False}

    w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run,
                  resume=lambda *a, **k: None, sleep=lambda s: None, now=_NOW,
                  rollback=rollback, max_iterations=1)

    assert _statuses(http) == ["진행중", "완료"]
    assert rb_calls["n"] == 0                   # 정상 완료 → 롤백 없음
