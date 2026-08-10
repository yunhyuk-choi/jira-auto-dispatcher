"""워커 — 중앙 폴링 → 에이전트 실행 → 상태 회신 + 한도 감지/재개(워커 전용).

역할:
    사용자별 동적 컨테이너(ROLE=worker DISPATCH_USER=<user>)로 뜨는 실행체.
    Jira를 직접 보지 않고 CENTRAL_URL을 HTTP 폴링해 자기 잡을 받아, 그 사용자
    정체성으로 오케스트레이터(`claude -p`, agent_runner)를 자율 실행하고, 상태/
    로그를 중앙에 회신한다. 토큰 한도(Max 롤링)를 감지하면 interrupted+reset_at
    으로 회신하고, reset_at까지 대기 후 ``--resume``으로 재개한다.

역할 소속: **worker**.

구현 Phase: **Phase 5** (워커 + 에이전트 실행).

환경(스포너가 주입):
    ROLE=worker
    DISPATCH_USER=<username>                # 이 워커가 대리하는 사용자
    CENTRAL_URL=http://central:8787         # 잡 수신/회신 대상
    WORKER_SHARED_SECRET=<secret>           # dispatch HTTP 인증(X-Worker-Secret)
    CLAUDE_CODE_OAUTH_TOKEN=<setup-token>   # 사용자 Claude 인증(Max)
    (+ agent_runner.UserCreds.from_env 가 읽는 정체성/시크릿 참조 env)

central↔worker HTTP 프로토콜:
    GET  {CENTRAL_URL}/dispatch/<DISPATCH_USER>/next
        → 다음 잡(JSON) 또는 204(대기).
    POST {CENTRAL_URL}/dispatch/<DISPATCH_USER>/<ticket>/status
        → {status, log_summary?, reset_at?, branch?, session_id?, mr_url?, audit_refs?}

⚠️ 보안:
    --dangerously-skip-permissions = 도구권한 자율 에이전트 = RCE 표면.
    사내망·신뢰 환경 한정. 동시성=1(concurrency_per_worker)로 폭주 방지.
    시크릿(X-Worker-Secret·토큰)은 로그로 내보내지 않는다.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import agent_runner
from app.agent_runner import (  # 계승 재노출(단일 원천)
    ANSI_ESCAPE,
    UserCreds,
    resume_job,
    run_job,
)

log = logging.getLogger("jad.worker")

# 기본 폴링 주기(초). env WORKER_POLL_INTERVAL_SEC로 오버라이드.
DEFAULT_POLL_INTERVAL_SEC = 5

# 취소 제어 채널 폴링 주기(초). env WORKER_CONTROL_POLL_SEC로 오버라이드(§10.4).
DEFAULT_CONTROL_POLL_SEC = 3

# 재개 대기 상한(초) — 한 번의 sleep이 지나치게 길지 않도록 클램프.
MAX_RESUME_WAIT_SEC = 6 * 60 * 60

# 채널 F로 보낼 상태 문자열(central의 STATUS_ALIASES가 정규화).
_STATUS_TO_CHANNEL_F = {
    agent_runner.STATUS_DONE: "완료",
    agent_runner.STATUS_FAILED: "failed",
    agent_runner.STATUS_INTERRUPTED: "interrupted",
    agent_runner.STATUS_CANCELLED: "cancelled",
}


class LimitReached(Exception):
    """토큰 한도 도달 신호(하위호환) — reset_at을 실어 interrupted 전이에 사용."""

    def __init__(self, reset_at: Optional[str] = None) -> None:
        super().__init__("token limit reached")
        self.reset_at = reset_at


# --- HTTP 클라이언트 --------------------------------------------------------


def _default_http():
    """기본 HTTP 클라이언트(requests.Session). 지연 import로 테스트 격리."""
    import requests  # noqa: PLC0415

    return requests.Session()


def _poll_interval(env: dict) -> int:
    try:
        return max(1, int(env.get("WORKER_POLL_INTERVAL_SEC", DEFAULT_POLL_INTERVAL_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_POLL_INTERVAL_SEC


def _control_poll_interval(env: dict) -> int:
    try:
        return max(0, int(env.get("WORKER_CONTROL_POLL_SEC", DEFAULT_CONTROL_POLL_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_CONTROL_POLL_SEC


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --- 루프 컴포넌트 ----------------------------------------------------------


def _fetch_next(http, central: str, user: str, headers: dict) -> Optional[dict]:
    """GET /dispatch/<user>/next → 잡 dict 또는 None(204/빈응답)."""
    url = f"{central}/dispatch/{user}/next"
    resp = http.get(url, headers=headers)
    status = getattr(resp, "status_code", None)
    if status == 204 or status is None:
        return None
    if status != 200:
        log.warning("next 폴링 비정상 응답: %s", status)
        return None
    try:
        data = resp.json()
    except (ValueError, TypeError):
        return None
    return data or None


def _post_status(http, central: str, user: str, ticket: str, headers: dict, payload: dict) -> None:
    """POST /dispatch/<user>/<ticket>/status — 채널 F 회신."""
    url = f"{central}/dispatch/{user}/{ticket}/status"
    http.post(url, json=payload, headers=headers)


def _fetch_control(http, central: str, user: str, ticket: str, headers: dict) -> bool:
    """GET /dispatch/<user>/<ticket>/control → 취소 요청 여부(§10.4).

    비정상 응답/파싱 실패는 보수적으로 False(취소 아님)로 본다.
    """
    url = f"{central}/dispatch/{user}/{ticket}/control"
    resp = http.get(url, headers=headers)
    if getattr(resp, "status_code", None) != 200:
        return False
    try:
        data = resp.json()
    except (ValueError, TypeError):
        return False
    return bool((data or {}).get("cancel"))


def _make_cancel_check(
    control_poll: Callable,
    http,
    central: str,
    user: str,
    ticket: str,
    headers: dict,
    *,
    now: Callable,
    interval_sec: int,
) -> Callable[[], bool]:
    """실행 중 agent_runner가 호출할 취소 폴링 클로저 생성(시간 스로틀).

    최소 interval_sec 간격으로만 실제 control을 폴링한다(이벤트 폭주 시 과호출 방지).
    한 번 취소가 감지되면 이후엔 즉시 True(래치).
    """
    st = {"last": None, "cancel": False}

    def check() -> bool:
        if st["cancel"]:
            return True
        t = now()
        if st["last"] is not None and (t - st["last"]).total_seconds() < interval_sec:
            return False
        st["last"] = t
        try:
            c = control_poll(http, central, user, ticket, headers)
        except Exception:  # noqa: BLE001 — 폴링 실패는 취소 아님으로 보수 처리
            return False
        st["cancel"] = bool(c)
        return st["cancel"]

    return check


# --- 롤백(취소 시 가역 산출 되돌리기, best-effort) --------------------------


def _default_git_run(args: list, cwd: Optional[str] = None):
    """기본 git 실행기(subprocess.run). 주입 가능(테스트 격리)."""
    import subprocess  # noqa: PLC0415

    return subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=cwd or None,
        capture_output=True,
        text=True,
    )


def _branch_exists(git_run: Callable, cwd: Optional[str], branch: str) -> bool:
    """로컬에 branch가 있는지(best-effort). 실패 시 False."""
    try:
        r = git_run(["rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=cwd)
    except Exception:  # noqa: BLE001
        return False
    return getattr(r, "returncode", 1) == 0


def _default_mr_closer(mr_url: str, creds: UserCreds, config: Any) -> bool:
    """GitLab MR을 닫는다(state_event=close). per-user 토큰. best-effort → bool."""
    import urllib.parse  # noqa: PLC0415

    import requests  # noqa: PLC0415

    from app.config import read_secret  # noqa: PLC0415

    # mr_url 예: https://gitlab.example.com/group/proj/-/merge_requests/7
    marker = "/-/merge_requests/"
    if marker not in mr_url:
        return False
    left, iid = mr_url.split(marker, 1)
    iid = iid.strip("/").split("/")[0]
    scheme_host, _, project_path = left.partition("://")[2].partition("/")
    if not project_path:
        return False
    base = left.split("/", 3)  # [scheme:, '', host, project_path]
    host = base[2] if len(base) >= 3 else ""
    api = f"https://{host}/api/v4/projects/{urllib.parse.quote_plus(project_path)}/merge_requests/{iid}"
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    token = read_secret(base_dir, getattr(creds, "gitlab_token_ref", "")) or ""
    if not token:
        return False
    resp = requests.put(api, params={"state_event": "close"},
                        headers={"PRIVATE-TOKEN": token}, timeout=30)
    return getattr(resp, "status_code", 500) < 400


def rollback_job(
    job: dict,
    creds: UserCreds,
    config: Any,
    *,
    git_run: Optional[Callable] = None,
    mr_closer: Optional[Callable] = None,
) -> dict:
    """취소된 잡의 가역 산출을 되돌린다(§10.4) — 로컬/원격 브랜치 삭제 + MR 닫기.

    모두 **best-effort**다. 브랜치/MR이 아직 없으면 스킵(되돌릴 것 없음). git_run·
    mr_closer는 주입 가능(순수 테스트). per-user 자격증명(creds)은 worker만 보유한다.

    Returns: {rolledback, branch_deleted, mr_closed, branch, mr_url}.
    """
    ticket = str((job or {}).get("ticket") or "")
    branch = (job or {}).get("branch") or (f"auto/{ticket}" if ticket else None)
    target_repos = list((job or {}).get("target_repos") or [])
    workspace = getattr(getattr(config, "run", None), "workspace_dir", "") or ""
    git_run = git_run or _default_git_run

    branch_deleted = False
    if branch:
        for repo in target_repos:
            cwd = os.path.join(workspace, repo) if workspace else repo
            if not _branch_exists(git_run, cwd, branch):
                continue  # 로컬에 없으면 스킵(아직 산출 없음)
            try:
                r = git_run(["branch", "-D", branch], cwd=cwd)
                if getattr(r, "returncode", 1) == 0:
                    branch_deleted = True
            except Exception:  # noqa: BLE001
                pass
            # 원격도 best-effort 삭제(없으면 조용히 실패).
            try:
                git_run(["push", "origin", "--delete", branch], cwd=cwd)
            except Exception:  # noqa: BLE001
                pass

    mr_url = (job or {}).get("mr_url") or ((job or {}).get("audit_refs") or {}).get("mr_url")
    mr_closed = False
    if mr_url:
        closer = mr_closer or _default_mr_closer
        try:
            mr_closed = bool(closer(mr_url, creds, config))
        except Exception:  # noqa: BLE001
            mr_closed = False

    return {
        "rolledback": bool(branch_deleted or mr_closed),
        "branch_deleted": branch_deleted,
        "mr_closed": mr_closed,
        "branch": branch,
        "mr_url": mr_url,
    }


def _wait_until(reset_at: Optional[str], *, sleep: Callable, now: Callable,
                buffer_sec: int, stop_event) -> None:
    """reset_at(+버퍼)까지 대기. 파싱 불가/과거면 즉시 통과."""
    dt = _parse_iso(reset_at)
    if dt is None:
        return
    delta = (dt - now()).total_seconds() + max(0, buffer_sec)
    if delta <= 0:
        return
    remaining = min(delta, MAX_RESUME_WAIT_SEC)
    if stop_event is not None and stop_event.is_set():
        return
    sleep(remaining)


def _process_job(
    job: dict,
    config: Any,
    creds: UserCreds,
    *,
    http,
    central: str,
    user: str,
    headers: dict,
    run: Callable,
    resume: Callable,
    sleep: Callable,
    now: Callable,
    stop_event,
    rollback: Callable,
    control_poll: Callable,
    control_poll_sec: int,
) -> None:
    """단일 잡 처리 — 진행중 보고 → 실행(취소 폴링) → (한도면 재개 반복) → 최종 회신.

    실행 중 취소 신호(control)가 오면 agent_runner가 abort하고 STATUS_CANCELLED로
    돌려준다. 그러면 worker가 **롤백**(가역 산출 되돌리기)을 수행한 뒤 cancelled로
    회신한다(§10.4).
    """
    ticket = str(job.get("ticket") or "")
    branch = job.get("branch") or (f"auto/{ticket}" if ticket else None)
    buffer_sec = int(getattr(getattr(config, "resume", None), "reset_buffer_sec", 120) or 0)

    # 즉시 진행중 보고(착수).
    _post_status(http, central, user, ticket, headers, {"status": "진행중", "branch": branch})

    cancel_check = _make_cancel_check(
        control_poll, http, central, user, ticket, headers,
        now=now, interval_sec=control_poll_sec,
    )

    result = run(job, creds, config, cancel_check=cancel_check)
    if result.status == agent_runner.STATUS_CANCELLED:
        _handle_cancel(job, config, creds, result, http=http, central=central,
                       user=user, ticket=ticket, headers=headers, branch=branch,
                       rollback=rollback)
        return

    # 한도(interrupted)면 reset_at까지 대기 후 재개를 반복.
    while result.status == agent_runner.STATUS_INTERRUPTED:
        _post_status(
            http, central, user, ticket, headers,
            _channel_f_payload(result, branch),
        )
        _wait_until(result.reset_at, sleep=sleep, now=now,
                    buffer_sec=buffer_sec, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            return
        sid = result.session_id or agent_runner.deterministic_session_id(ticket)
        result = resume(job, sid, creds, config, cancel_check=cancel_check)
        if result.status == agent_runner.STATUS_CANCELLED:
            _handle_cancel(job, config, creds, result, http=http, central=central,
                           user=user, ticket=ticket, headers=headers, branch=branch,
                           rollback=rollback)
            return

    _post_status(http, central, user, ticket, headers, _channel_f_payload(result, branch))


def _handle_cancel(
    job: dict,
    config: Any,
    creds: UserCreds,
    result: agent_runner.AgentResult,
    *,
    http,
    central: str,
    user: str,
    ticket: str,
    headers: dict,
    branch: Optional[str],
    rollback: Callable,
) -> None:
    """취소된 실행 후처리 — 롤백(best-effort) 후 cancelled 회신(§10.4)."""
    try:
        rb = rollback(job, creds, config)
    except Exception:  # noqa: BLE001 — 롤백 실패해도 회신은 해야 락/dedup가 풀린다
        log.exception("롤백 실패(그래도 cancelled 회신)")
        rb = {"rolledback": False}

    payload: dict = {
        "status": _STATUS_TO_CHANNEL_F.get(agent_runner.STATUS_CANCELLED, "cancelled"),
        "branch": branch,
        "rolledback": bool(rb.get("rolledback")),
    }
    if result.session_id:
        payload["session_id"] = result.session_id
    if result.log_summary:
        payload["log_summary"] = result.log_summary
    audit = {k: rb[k] for k in ("branch_deleted", "mr_closed", "mr_url")
             if rb.get(k) is not None}
    if audit:
        payload["audit_refs"] = audit
    _post_status(http, central, user, ticket, headers, payload)


def _channel_f_payload(result: agent_runner.AgentResult, branch: Optional[str]) -> dict:
    """AgentResult → 채널 F POST 본문(상태 문자열 매핑 + None 생략)."""
    payload = result.to_status_payload(branch=branch)
    payload["status"] = _STATUS_TO_CHANNEL_F.get(result.status, result.status)
    return payload


def worker_loop(
    config: Any,
    *,
    env: Optional[dict] = None,
    http=None,
    creds: Optional[UserCreds] = None,
    run: Optional[Callable] = None,
    resume: Optional[Callable] = None,
    sleep: Optional[Callable] = None,
    now: Optional[Callable] = None,
    stop_event=None,
    max_iterations: Optional[int] = None,
    rollback: Optional[Callable] = None,
    control_poll: Optional[Callable] = None,
) -> int:
    """중앙 폴링 루프(worker 프로세스 진입점). 처리 반복 수를 반환(테스트용).

    env DISPATCH_USER / CENTRAL_URL / WORKER_SHARED_SECRET 를 읽어 폴링한다.
    각 반복은 예외 격리되어(한 잡 실패가 루프를 죽이지 않음), 204면 폴링 주기만큼
    대기한다. max_iterations/stop_event로 정지한다. rollback/control_poll은
    취소 플로우(§10.4) 배선점으로 주입 가능(테스트).
    """
    env = env if env is not None else os.environ
    user = (env.get("DISPATCH_USER") or "").strip()
    central = (env.get("CENTRAL_URL") or "").strip().rstrip("/")
    secret = env.get("WORKER_SHARED_SECRET") or ""
    if not user or not central:
        raise RuntimeError("worker는 DISPATCH_USER와 CENTRAL_URL env가 필요합니다")

    http = http if http is not None else _default_http()
    creds = creds if creds is not None else UserCreds.from_env(user, env=env)
    run = run or run_job
    resume = resume or resume_job
    sleep = sleep or time.sleep
    now = now or (lambda: datetime.now(timezone.utc))
    rollback = rollback or rollback_job
    control_poll = control_poll or _fetch_control
    poll = _poll_interval(env)
    control_poll_sec = _control_poll_interval(env)
    headers = {"X-Worker-Secret": secret} if secret else {}

    iterations = 0
    while stop_event is None or not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            job = _fetch_next(http, central, user, headers)
            if job is None:
                sleep(poll)
                continue
            _process_job(
                job, config, creds,
                http=http, central=central, user=user, headers=headers,
                run=run, resume=resume, sleep=sleep, now=now, stop_event=stop_event,
                rollback=rollback, control_poll=control_poll,
                control_poll_sec=control_poll_sec,
            )
        except Exception:  # noqa: BLE001 — 한 잡 실패가 루프를 죽이지 않게 격리
            log.exception("worker 루프 반복 실패(격리)")
            sleep(poll)

    return iterations


# --- 하위호환 클래스 래퍼(문서/기존 인터페이스) ------------------------------


class Worker:
    """worker_loop의 얇은 래퍼(CLAUDE.md가 참조하는 인터페이스)."""

    def __init__(self, config, agent_runner=None) -> None:
        self.config = config
        self.runner = agent_runner
        import threading

        self._stop = threading.Event()

    def run_forever(self, **kw) -> int:
        return worker_loop(self.config, stop_event=self._stop, **kw)

    def stop(self) -> None:
        self._stop.set()


__all__ = [
    "ANSI_ESCAPE",
    "LimitReached",
    "Worker",
    "rollback_job",
    "worker_loop",
]
