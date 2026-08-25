"""담당자 변경 = 핸드오프(체크포인트, 롤백X) + 이관 단위테스트(Increment 1).

재배정 ≠ 취소를 검증한다:
    - 취소 = abort + 롤백(WIP 폐기, 기존 §10.4).
    - 핸드오프 = checkpoint(WIP 커밋·push로 보존) + 소유권 이관.

라이브 Jira/claude/git/네트워크 없음(FakeProc·주입 stub·격리 state).
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app import agent_runner as ar
from app import queue as q
from app import scheduler as sched
from app import worker as w
from app.dispatch import DISPATCHER_KEY, Dispatcher, dispatch_bp
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
# dispatch control 엔드포인트 — action 필드
# =========================================================================


def _wire_disp():
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    gate = DedupGate()
    sch = Scheduler(make_config(concurrency_per_worker=5), JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    return reg, gate, sch, disp


def test_control_returns_handoff_action(isolated_state):
    """핸드오프 요청 시 control이 {"cancel": False, "action": "handoff"}를 반환한다."""
    _, gate, sch, disp = _wire_disp()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    # 정상: none
    assert disp.control("u1", "PROJ-1") == {"cancel": False, "action": "none"}
    # 핸드오프 요청 → action=handoff (cancel은 False — 구 worker 오취소 방지).
    sch.reassign_or_handoff("PROJ-1", "u2", enabled=True)
    assert disp.control("u1", "PROJ-1") == {"cancel": False, "action": "handoff"}


def test_http_control_route_handoff(isolated_state):
    """HTTP control 라우트도 action=handoff를 그대로 노출한다."""
    from flask import Flask

    _, gate, sch, disp = _wire_disp()
    gate.claim("PROJ-1")
    disp.enqueue("u1", Job(ticket="PROJ-1", target_repos=["repoA"]))
    sch.reassign_or_handoff("PROJ-1", "u2", enabled=True)

    app = Flask(__name__)
    app.config[DISPATCHER_KEY] = disp
    app.register_blueprint(dispatch_bp)
    client = app.test_client()
    r = client.get("/dispatch/u1/PROJ-1/control")
    assert r.status_code == 200
    assert r.get_json() == {"cancel": False, "action": "handoff"}


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
# worker — checkpoint_job(커밋·push, 롤백X) + 핸드오프 후처리
# =========================================================================


def _cfg():
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json", workspace_dir="/ws"),
        secrets=SimpleNamespace(base_dir=""),
    )


def _token_cfg(tmp_path, user="u1"):
    """per-user 토큰(gitlab-token)을 갖춘 cfg — #6 자격증명 위생 테스트용."""
    (tmp_path / user).mkdir(exist_ok=True)
    (tmp_path / user / "gitlab-token").write_text("TKN123", encoding="utf-8")
    return SimpleNamespace(
        resume=SimpleNamespace(reset_buffer_sec=0),
        run=SimpleNamespace(orchestrator_repo="/app/orch", claude_bin="claude",
                            output_format="stream-json", workspace_dir="/ws"),
        secrets=SimpleNamespace(base_dir=str(tmp_path)),
    )


def _creds_u1():
    return ar.UserCreds(user="u1", gitlab_token_ref="u1/gitlab-token")


def test_checkpoint_job_commits_and_pushes(tmp_path):
    """checkpoint_job은 롤백 대신 add/commit/push로 WIP를 보존한다(주입 git_run).

    #6: push 는 ambient `origin` 이 아니라 **per-user 토큰 URL** 로 나간다."""
    seen = []

    def git_run(args, cwd=None):
        seen.append(list(args))
        if args[:2] == ["remote", "get-url"]:
            return SimpleNamespace(returncode=0,
                                   stdout="http://server.example/g/repoA.git\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    notes = []

    def write_note(cwd, ticket, branch):
        notes.append((ticket, branch))
        return "runs/PROJ-1/HANDOFF.md"

    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    cp = w.checkpoint_job(job, _creds_u1(), _token_cfg(tmp_path),
                          git_run=git_run, write_note=write_note)
    assert cp["checkpointed"] is True
    assert cp["committed"] is True
    assert cp["pushed"] is True
    assert cp["branch"] == "auto/PROJ-1"
    # 롤백(branch -D)이 아니라 commit + push를 한다.
    assert ["add", "-A"] in seen
    assert any(a[:2] == ["commit", "-m"] for a in seen)
    # #6: ambient `push origin` 금지 — per-user 토큰 URL 로 push.
    assert not any(a[:2] == ["push", "origin"] for a in seen)
    push_calls = [a for a in seen if a[:1] == ["push"]]
    assert push_calls, "push 호출이 있어야 한다"
    assert all("oauth2:TKN123@" in a[1] for a in push_calls)  # 명시 토큰 URL
    assert all(a[-1] == "auto/PROJ-1" for a in push_calls)
    assert not any(a[:2] == ["branch", "-D"] for a in seen)   # 롤백 아님
    assert notes == [("PROJ-1", "auto/PROJ-1")]               # 이관 저널 노트 기록


def test_checkpoint_job_skips_commit_when_nothing_to_commit(tmp_path):
    """커밋할 변경이 없으면(commit nonzero) committed=False, push는 여전히 시도(토큰 URL)."""
    def git_run(args, cwd=None):
        if args[:2] == ["remote", "get-url"]:
            return SimpleNamespace(returncode=0,
                                   stdout="http://server.example/g/repoA.git", stderr="")
        rc = 1 if args[:1] == ["commit"] else 0   # commit만 '변경 없음'
        return SimpleNamespace(returncode=rc, stdout="", stderr="")

    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    cp = w.checkpoint_job(job, _creds_u1(), _token_cfg(tmp_path),
                          git_run=git_run, write_note=lambda *a, **k: None)
    assert cp["committed"] is False
    assert cp["pushed"] is True                    # 선행 커밋 push는 시도됨
    assert cp["checkpointed"] is True              # push만 돼도 보존됨


def test_checkpoint_job_skips_push_without_user_token(tmp_path):
    """#6: per-user 토큰이 없으면 push 를 **skip**(ambient `origin` 폴백 금지)."""
    seen = []

    def git_run(args, cwd=None):
        seen.append(list(args))
        if args[:2] == ["remote", "get-url"]:
            return SimpleNamespace(returncode=0,
                                   stdout="http://server.example/g/repoA.git", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # 토큰 ref 미설정 → 토큰 미해결.
    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    cfg = SimpleNamespace(run=SimpleNamespace(workspace_dir="/ws"),
                          secrets=SimpleNamespace(base_dir=str(tmp_path)))
    cp = w.checkpoint_job(job, ar.UserCreds(user="u1"), cfg,
                          git_run=git_run, write_note=lambda *a, **k: None)
    assert cp["pushed"] is False
    assert not any(a[:1] == ["push"] for a in seen)   # ambient 폴백 없이 push 자체를 안 한다


class HandoffHTTP:
    """next 1회 + control(handoff) 응답 + post 기록(핸드오프 실행경로 검증용)."""

    def __init__(self, job, action="handoff"):
        self.job = job
        self.action = action
        self._next_served = False
        self.posts = []
        self.control_polls = 0

    def get(self, url, headers=None):
        if url.endswith("/next"):
            if not self._next_served:
                self._next_served = True
                return _Resp(200, self.job)
            return _Resp(204)
        if url.endswith("/control"):
            self.control_polls += 1
            return _Resp(200, {"cancel": False, "action": self.action})
        return _Resp(204)

    def post(self, url, json=None, headers=None):
        self.posts.append((url, json))
        return _Resp(200, {})


class _Resp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


_ENV = {"DISPATCH_USER": "u1", "CENTRAL_URL": "http://central:8787"}
_NOW = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)


def _statuses(http):
    return [p[1].get("status") for p in http.posts]


def test_worker_handoff_triggers_checkpoint_not_rollback():
    """실행 중 핸드오프 신호 → checkpoint 수행(롤백 아님) + handed_off 회신."""
    job = {"ticket": "PROJ-1", "branch": "auto/PROJ-1", "target_repos": ["repoA"]}
    http = HandoffHTTP(job, action="handoff")
    cp_calls = {}
    rb_calls = {"n": 0}

    def run(job_, creds, cfg, cancel_check=None, **k):
        # agent_runner 흉내: control이 handoff면 STATUS_HANDOFF 반환.
        if cancel_check and ar._normalize_control(cancel_check()) == "handoff":
            return ar.AgentResult(status=ar.STATUS_HANDOFF, session_id="s1",
                                  log_summary="checkpointed")
        return ar.AgentResult(status=ar.STATUS_DONE)

    def checkpoint(job_, creds, cfg):
        cp_calls["job"] = job_
        return {"checkpointed": True, "committed": True, "pushed": True,
                "branch": "auto/PROJ-1"}

    def rollback(*a, **k):
        rb_calls["n"] += 1
        return {"rolledback": True}

    w.worker_loop(_cfg(), env=dict(_ENV), http=http, run=run,
                  resume=lambda *a, **k: None, sleep=lambda s: None, now=_NOW,
                  rollback=rollback, checkpoint=checkpoint, max_iterations=1)

    assert _statuses(http) == ["진행중", "handed_off"]   # 취소가 아니라 이관 회신
    assert http.control_polls >= 1                      # control 폴링으로 감지
    assert cp_calls["job"]["ticket"] == "PROJ-1"        # checkpoint 수행
    assert rb_calls["n"] == 0                            # ⚠️ 롤백은 절대 하지 않음
    payload = http.posts[-1][1]
    assert payload["branch"] == "auto/PROJ-1"
    assert payload["audit_refs"] == {"committed": True, "pushed": True}


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


def _wire_poller(issues):
    reg = Registry()
    reg.upsert(UserRecord(username="u1", jira_account_id="a1", enabled=True))
    reg.upsert(UserRecord(username="u2", jira_account_id="a2", enabled=True))
    reg.upsert(UserRecord(username="u3", jira_account_id="a3", enabled=False))  # 비활성
    gate = DedupGate()
    cfg = make_config(concurrency_per_worker=5)
    cfg.run.repo_resolution = "static"
    sch = Scheduler(cfg, JobQueue(), gate=gate)
    disp = Dispatcher(reg, sch)
    poller = Poller(cfg, FakeJira(issues), gate, reg, disp, clock=lambda: _FIXED_NOW)
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
