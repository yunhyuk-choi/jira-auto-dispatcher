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

# 재개 대기 상한(초) — 한 번의 sleep이 지나치게 길지 않도록 클램프.
MAX_RESUME_WAIT_SEC = 6 * 60 * 60

# 채널 F로 보낼 상태 문자열(central의 STATUS_ALIASES가 정규화).
_STATUS_TO_CHANNEL_F = {
    agent_runner.STATUS_DONE: "완료",
    agent_runner.STATUS_FAILED: "failed",
    agent_runner.STATUS_INTERRUPTED: "interrupted",
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
) -> None:
    """단일 잡 처리 — 진행중 보고 → 실행 → (한도면 재개 반복) → 최종 회신."""
    ticket = str(job.get("ticket") or "")
    branch = job.get("branch") or (f"auto/{ticket}" if ticket else None)
    buffer_sec = int(getattr(getattr(config, "resume", None), "reset_buffer_sec", 120) or 0)

    # 즉시 진행중 보고(착수).
    _post_status(http, central, user, ticket, headers, {"status": "진행중", "branch": branch})

    result = run(job, creds, config)

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
        result = resume(job, sid, creds, config)

    _post_status(http, central, user, ticket, headers, _channel_f_payload(result, branch))


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
) -> int:
    """중앙 폴링 루프(worker 프로세스 진입점). 처리 반복 수를 반환(테스트용).

    env DISPATCH_USER / CENTRAL_URL / WORKER_SHARED_SECRET 를 읽어 폴링한다.
    각 반복은 예외 격리되어(한 잡 실패가 루프를 죽이지 않음), 204면 폴링 주기만큼
    대기한다. max_iterations/stop_event로 정지한다.
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
    poll = _poll_interval(env)
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
    "worker_loop",
]
