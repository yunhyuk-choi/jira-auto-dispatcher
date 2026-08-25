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
    신뢰 네트워크 한정. worker는 central이 dispatch한 잡을 **모두** 동시에 굴린다
    (진짜 스로틀은 central의 **서버 자원 어드미션**이다 — 잡 수 cap 아님). worker는 runaway
    방지용 **안전 상한**(worker_max_concurrency, 기본 64 — 정책 cap 아님)만 둔다. central
    전역 레포락이 동시 잡을 항상 서로 다른 레포로 보장한다.
    시크릿(X-Worker-Secret·토큰)은 로그로 내보내지 않는다.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import agent_runner, forge
from app.agent_runner import (  # 계승 재노출(단일 원천)
    ANSI_ESCAPE,
    UserCreds,
    resume_job,
    run_job,
)
from app.notify import notify_job_end

log = logging.getLogger("jad.worker")

# 기본 폴링 주기(초). env WORKER_POLL_INTERVAL_SEC로 오버라이드.
DEFAULT_POLL_INTERVAL_SEC = 5

# 취소 제어 채널 폴링 주기(초). env WORKER_CONTROL_POLL_SEC로 오버라이드(§10.4).
DEFAULT_CONTROL_POLL_SEC = 3

# worker 동시 실행 **안전 상한**(runaway 방지 백스톱 — 정책 cap이 아님). 기본 64.
# 진짜 스로틀은 central의 서버 자원 어드미션이다. env WORKER_CONCURRENCY /
# config.run.worker_max_concurrency로 오버라이드.
DEFAULT_WORKER_MAX_CONCURRENCY = 64

# 재개 대기 상한(초) — 한 번의 sleep이 지나치게 길지 않도록 클램프.
MAX_RESUME_WAIT_SEC = 6 * 60 * 60

# --- central 연결 복원력(재시도/백오프) 상수 ---
# central 재기동 창(예: DNS "Failed to resolve 'central'")에 워커가 죽거나 잡을
# failed로 오판하지 않도록, central에 하는 HTTP를 지수 백오프로 감싼다.
RETRY_BASE_BACKOFF_SEC = 1     # 첫 재시도 대기(이후 2·4·8…로 증가).
RETRY_MAX_BACKOFF_SEC = 60     # 백오프 상한(cap).
# 다음 잡 폴링(GET /next)의 **유한** 재시도 횟수. 소진되면 예외/무한루프 없이
# None을 반환한다 — 워커 루프는 죽지 않고 정상 폴 간격으로 계속 살아, **다음 폴에서
# 다시 시도**한다. ⚠️ 한 폴 호출을 무한 재시도하지 않는다(단일 호출 무한재시도 금지).
FETCH_RETRY_ATTEMPTS = 6
# 상태 회신(POST /status)의 유한 재시도 횟수. 소진되면 포기(다음 tick 재시도) —
# ⚠️ 연결 실패를 job 실패로 **오판하지 않는다**(회신만 못 했을 뿐 잡은 정상).
STATUS_POST_ATTEMPTS = 6

# 일시적 연결 오류 예외 튜플 캐시(지연 계산). requests 예외 + 표준 예외.
_TRANSIENT_ERRORS: Optional[tuple] = None


def _transient_http_errors() -> tuple:
    """재시도 대상(일시적 연결/타임아웃) 예외 튜플. requests + 표준, 지연 import.

    requests.exceptions.ConnectionError/Timeout(둘 다 OSError 하위) + 표준
    ConnectionError/TimeoutError 를 포함한다. requests 미설치여도 표준 예외로 동작.
    """
    global _TRANSIENT_ERRORS
    if _TRANSIENT_ERRORS is not None:
        return _TRANSIENT_ERRORS
    errs: list = [ConnectionError, TimeoutError]
    try:
        import requests  # noqa: PLC0415

        errs.extend([requests.exceptions.ConnectionError, requests.exceptions.Timeout])
    except Exception:  # noqa: BLE001 — requests 부재 시 표준 예외만
        pass
    _TRANSIENT_ERRORS = tuple(errs)
    return _TRANSIENT_ERRORS


def _backoff_delay(attempt: int) -> int:
    """attempt(0-기반)에 대한 지수 백오프 대기(초), cap 적용. 1·2·4·8…≤cap."""
    return min(RETRY_MAX_BACKOFF_SEC, RETRY_BASE_BACKOFF_SEC * (2 ** max(0, attempt)))

# 채널 F로 보낼 상태 문자열(central의 STATUS_ALIASES가 정규화).
_STATUS_TO_CHANNEL_F = {
    agent_runner.STATUS_DONE: "완료",
    agent_runner.STATUS_FAILED: "failed",
    agent_runner.STATUS_INTERRUPTED: "interrupted",
    agent_runner.STATUS_CANCELLED: "cancelled",
    agent_runner.STATUS_HANDOFF: "handed_off",
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


def _worker_concurrency(env: dict, config: Any) -> int:
    """worker 동시 실행 **안전 상한**을 결정(≥1) — runaway 방지 백스톱, 정책 cap 아님.

    worker는 central이 dispatch한 잡을 모두 동시에 굴린다(진짜 스로틀은 central의 서버
    자원 어드미션). 이 값은 버그로 인한 무한 스레드 폭주만 막는 상한이다.

    우선순위: ``env WORKER_CONCURRENCY`` (스포너 주입) > ``config.run.worker_max_concurrency``
    (>0일 때) > ``DEFAULT_WORKER_MAX_CONCURRENCY`` (64). 파싱 불가/미설정은 조용히 다음
    후보로 폴백한다.
    """
    raw = env.get("WORKER_CONCURRENCY")
    if raw is not None and str(raw).strip() != "":
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            pass
    run = getattr(config, "run", None)
    try:
        wc = int(getattr(run, "worker_max_concurrency", 0) or 0)
        if wc > 0:
            return max(1, wc)
    except (TypeError, ValueError):
        pass
    return DEFAULT_WORKER_MAX_CONCURRENCY


class _LockedDict:
    """터미널 재회신 캐시(ticket→payload)의 **스레드 안전** 최소 래퍼(동시성 ≥2).

    동시 실행되는 여러 잡 스레드가 :func:`_mark_completed` 로 이 캐시를 쓰고,
    worker 루프가 재-fetch 시 읽는다. 개별 dict 연산은 GIL로 원자적이지만,
    루프의 "있으면 재회신"(check-then-act)에서 다른 스레드가 pop하는 레이스를 막기
    위해 모든 접근을 하나의 락으로 감싼다. 단일 잡 경로(concurrency=1)는 이 래퍼를
    쓰지 않고 평범한 dict를 그대로 써 기존 동작을 100% 보존한다.
    """

    def __init__(self) -> None:
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key, default=None):
        with self._lock:
            return self._d.get(key, default)

    def pop(self, key, default=None):
        with self._lock:
            return self._d.pop(key, default)

    def __setitem__(self, key, value) -> None:
        with self._lock:
            self._d[key] = value

    def __contains__(self, key) -> bool:
        with self._lock:
            return key in self._d


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


def _fetch_next(
    http,
    central: str,
    user: str,
    headers: dict,
    *,
    sleep: Optional[Callable] = None,
    stop_event=None,
    exclude: Optional[set] = None,
) -> Optional[dict]:
    """GET /dispatch/<user>/next → 잡 dict 또는 None(204/빈응답/재시도 소진).

    ``exclude`` = 이 worker가 **이미 동시 처리 중인 티켓** 집합(동시성 ≥2). central에
    ``?exclude=T1,T2`` 로 넘겨 그와 **다른** running 잡을 받는다 → 같은 잡을 두 번 집지
    않고 서로 다른 잡을 동시에 실행한다(Increment 2). 비었으면 쿼리를 붙이지 않아
    단일 잡 경로의 URL·동작이 그대로 유지된다.

    ⚠️ 복원력: central 재기동(예: DNS "Failed to resolve 'central'")로 인한 일시적
    연결/타임아웃 오류는 예외를 전파하지 않고 지수 백오프(1·2·4·8…≤cap)로
    **유한**(FETCH_RETRY_ATTEMPTS회) 재시도한다. 소진되면 **None을 반환**한다 —
    한 폴 호출을 무한 재시도하지 않는다(단일 호출 무한재시도 금지). 워커 루프는 죽지
    않고 정상 폴 간격만큼 쉰 뒤 **다음 폴에서 다시 시도**하므로 central 복귀를
    놓치지 않는다("워커가 죽지 않되, 단일 호출을 무한 재시도하지 않는다"). stop_event가
    set되면 즉시 None으로 빠져나온다(정상 종료). 비-연결 예외(버그성)는 그대로 전파해
    상위 루프 격리(except)가 처리한다.
    """
    url = f"{central}/dispatch/{user}/next"
    if exclude:
        # 결정적 순서(sorted)로 콤마 조인. 티켓은 안전한 문자만이라 인코딩 불요.
        url = f"{url}?exclude={','.join(sorted(exclude))}"
    sleep = sleep or time.sleep
    transient = _transient_http_errors()
    resp = None
    for attempt in range(max(1, FETCH_RETRY_ATTEMPTS)):
        if stop_event is not None and stop_event.is_set():
            return None
        try:
            resp = http.get(url, headers=headers)
            break
        except transient as exc:  # noqa: PERF203 — 연결 실패는 유한 재시도(로그엔 타입만)
            if attempt + 1 >= FETCH_RETRY_ATTEMPTS:
                # ⚠️ 유한 재시도 소진 → None(무한재시도 금지). 다음 폴에서 다시 시도.
                log.warning(
                    "next 폴링 연결 실패 — 유한 재시도 소진, 다음 폴에서 재시도(%s)",
                    type(exc).__name__,
                )
                return None
            delay = _backoff_delay(attempt)
            # ⚠️ 시크릿/헤더는 로깅하지 않는다(예외 타입만).
            log.warning("next 폴링 연결 실패 — %ss 후 재시도(%s)", delay, type(exc).__name__)
            sleep(delay)
    if resp is None:
        return None
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


def _post_status_core(
    http,
    central: str,
    user: str,
    ticket: str,
    headers: dict,
    payload: dict,
    *,
    sleep: Optional[Callable] = None,
    stop_event=None,
) -> "tuple[bool, dict]":
    """POST /dispatch/<user>/<ticket>/status — 채널 F 회신. ``(성공여부, 응답본문)`` 반환.

    응답 본문(central의 ``{"ok":True,"dispatched":[...],"cycle_log_path":...}``)을 파싱해
    돌려준다 — 완료 회신이면 central이 커밋한 dlc-meta 사이클로그 상대경로가 실린다
    (워커가 이를 완료 알림에 `사이클로그:` 라인으로 붙인다). 파싱 불가/본문 없음이면
    ``{}``.

    ⚠️ 복원력: central 재기동 창의 일시적 연결/타임아웃은 **유한 재시도**
    (STATUS_POST_ATTEMPTS회, 지수 백오프)한다. 소진되면 예외를 올리지 않고
    ``(False, {})`` 를 반환한다 — 상위 루프는 다음 tick에 다시 회신을 시도한다.
    **연결 실패를 job 실패로 오판하지 않는다**(회신을 못 했을 뿐 잡 결과는 유효).
    비-연결 예외(버그성)는 그대로 전파해 상위 격리가 처리한다.
    """
    url = f"{central}/dispatch/{user}/{ticket}/status"
    sleep = sleep or time.sleep
    transient = _transient_http_errors()
    for attempt in range(max(1, STATUS_POST_ATTEMPTS)):
        if stop_event is not None and stop_event.is_set():
            return False, {}
        try:
            resp = http.post(url, json=payload, headers=headers)
            body: dict = {}
            try:
                parsed = resp.json() if resp is not None else None
                if isinstance(parsed, dict):
                    body = parsed
            except Exception:  # noqa: BLE001 — 본문 파싱 실패는 무해(회신 자체는 성공)
                body = {}
            return True, body
        except transient as exc:  # noqa: PERF203 — 연결 실패는 유한 재시도
            if attempt + 1 >= STATUS_POST_ATTEMPTS:
                # ⚠️ 연결 실패를 job 실패로 오판하지 않는다 — 다음 tick 재시도.
                log.warning(
                    "상태 회신 연결 실패 — 유한 재시도 소진, 다음 tick 재시도(%s)",
                    type(exc).__name__,
                )
                return False, {}
            delay = _backoff_delay(attempt)
            log.warning("상태 회신 연결 실패 — %ss 후 재시도(%s)", delay, type(exc).__name__)
            sleep(delay)
    return False, {}


def _post_status(
    http,
    central: str,
    user: str,
    ticket: str,
    headers: dict,
    payload: dict,
    *,
    sleep: Optional[Callable] = None,
    stop_event=None,
) -> bool:
    """:func:`_post_status_core` 의 bool 전용 래퍼(하위호환 — 본문이 불필요한 회신)."""
    ok, _body = _post_status_core(
        http, central, user, ticket, headers, payload,
        sleep=sleep, stop_event=stop_event,
    )
    return ok


def _fetch_control(http, central: str, user: str, ticket: str, headers: dict) -> str:
    """GET /dispatch/<user>/<ticket>/control → 제어 액션(§10.4 + 핸드오프).

    응답 {"cancel": bool, "action": "none"|"cancel"|"handoff"}. action 필드를 우선
    보고(신 central), 없으면 cancel 불리언으로 폴백(하위호환). 비정상 응답/파싱
    실패는 보수적으로 "none"(신호 없음)으로 본다.
    """
    url = f"{central}/dispatch/{user}/{ticket}/control"
    resp = http.get(url, headers=headers)
    if getattr(resp, "status_code", None) != 200:
        return "none"
    try:
        data = resp.json()
    except (ValueError, TypeError):
        return "none"
    data = data or {}
    action = data.get("action")
    if action in ("cancel", "handoff", "none"):
        return action
    # 하위호환: action 필드가 없는 구 central은 cancel 불리언만 준다.
    return "cancel" if data.get("cancel") else "none"


def _make_control_check(
    control_poll: Callable,
    http,
    central: str,
    user: str,
    ticket: str,
    headers: dict,
    *,
    now: Callable,
    interval_sec: int,
) -> Callable[[], str]:
    """실행 중 agent_runner가 호출할 **제어 액션** 폴링 클로저 생성(시간 스로틀).

    최소 interval_sec 간격으로만 실제 control을 폴링한다(이벤트 폭주 시 과호출 방지).
    반환은 액션 문자열("none"|"cancel"|"handoff"). 한 번 취소/핸드오프가 감지되면
    이후엔 즉시 그 액션을 반환한다(래치). control_poll이 bool을 주는 하위호환도 흡수한다.
    """
    st = {"last": None, "action": "none"}

    def _norm(v) -> str:
        if v == "handoff":
            return "handoff"
        if v is True or v == "cancel":
            return "cancel"
        return "none"

    def check() -> str:
        if st["action"] in ("cancel", "handoff"):
            return st["action"]
        t = now()
        if st["last"] is not None and (t - st["last"]).total_seconds() < interval_sec:
            return "none"
        st["last"] = t
        try:
            a = control_poll(http, central, user, ticket, headers)
        except Exception:  # noqa: BLE001 — 폴링 실패는 신호 없음으로 보수 처리
            return "none"
        st["action"] = _norm(a)
        return st["action"]

    return check


# 하위호환 별칭(기존 이름 참조 대비).
_make_cancel_check = _make_control_check


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


def _user_token_push_url(
    git_run: Callable, cwd: Optional[str], creds: "UserCreds", config: Any
) -> Optional[str]:
    """cwd 레포의 origin 을 **dispatched 유저의 per-user 토큰 URL** 로 변환(#6 자격증명 위생).

    핵심 불변식: 워커의 모든 push 는 ambient ``git push origin`` 이 아니라 **dispatched
    유저($DISPATCH_USER)의 per-user forge 토큰**(``creds.forge_token_ref`` — 옛 이름
    ``gitlab_token_ref``)으로 만든 **명시 토큰 URL** 로 나가야 MR/PR·브랜치 push 귀속이
    정확하다(지상검증: ambient/임베디드 폴백이 비결정적으로 타인에게 귀속됨). origin URL 은
    :func:`app.repos._strip_token` 으로 임베디드 자격정보를 걷어낸 뒤 그 순간의 토큰만
    :func:`app.repos._with_token` 으로 싣는다(remote 에는 저장하지 않는다).

    토큰 URL 의 자격 사용자명은 forge 마다 다르므로(GitLab ``oauth2`` / GitHub
    ``x-access-token``) origin 호스트·``config.forge.kind`` 로 분기한다(:mod:`app.forge`).

    per-user 토큰이나 origin URL 을 못 구하면 **None** 을 반환한다 — 호출부는 ambient 로
    폴백하지 않고 push 를 skip 한다(잘못된 귀속을 만드느니 push 를 하지 않는다). 토큰 값은
    반환 URL 에만 실리고 로그·예외에 남기지 않는다.
    """
    from app.config import read_secret  # noqa: PLC0415
    from app.repos import _strip_token, _with_token  # noqa: PLC0415

    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    token = read_secret(base_dir, _forge_token_ref(creds)) or ""
    if not token:
        return None
    try:
        r = git_run(["remote", "get-url", "origin"], cwd=cwd)
    except Exception:  # noqa: BLE001
        return None
    if getattr(r, "returncode", 1) != 0:
        return None
    url = (getattr(r, "stdout", "") or "").strip()
    if not url:
        return None
    return _with_token(_strip_token(url), token, config=config)


def _forge_token_ref(creds: Any) -> str:
    """creds 의 per-user forge 토큰 참조(정본 ``forge_token_ref``, 레거시 폴백)."""
    return (getattr(creds, "forge_token_ref", "")
            or getattr(creds, "gitlab_token_ref", "") or "")


def _close_gitlab_mr(mr_url: str, token: str) -> bool:
    """GitLab MR 닫기(``state_event=close``). 형태가 안 맞으면 False."""
    import urllib.parse  # noqa: PLC0415

    import requests  # noqa: PLC0415

    # mr_url 예: https://gitlab.example.com/group/proj/-/merge_requests/7
    marker = "/-/merge_requests/"
    if marker not in mr_url:
        return False
    left, iid = mr_url.split(marker, 1)
    iid = iid.strip("/").split("/")[0]
    _scheme_host, _, project_path = left.partition("://")[2].partition("/")
    if not project_path:
        return False
    base = left.split("/", 3)  # [scheme:, '', host, project_path]
    host = base[2] if len(base) >= 3 else ""
    api = f"https://{host}/api/v4/projects/{urllib.parse.quote_plus(project_path)}/merge_requests/{iid}"
    resp = requests.put(api, params={"state_event": "close"},
                        headers={"PRIVATE-TOKEN": token}, timeout=30)
    return getattr(resp, "status_code", 500) < 400


def _close_github_pr(pr_url: str, token: str) -> bool:
    """GitHub PR 닫기(``PATCH /repos/{owner}/{repo}/pulls/{n}`` state=closed).

    GitHub.com 은 ``api.github.com``, GHE 는 ``<host>/api/v3`` 가 API 루트다.
    형태가 안 맞으면 False(호출부가 best-effort 로 흡수).
    """
    import requests  # noqa: PLC0415

    # pr_url 예: https://github.com/owner/repo/pull/7
    marker = "/pull/"
    if marker not in pr_url:
        return False
    left, number = pr_url.split(marker, 1)
    number = number.strip("/").split("/")[0]
    host, _, repo_path = left.partition("://")[2].partition("/")
    if not host or repo_path.count("/") < 1:
        return False
    owner, _, repo = repo_path.partition("/")
    repo = repo.split("/")[0]
    root = "https://api.github.com" if host.lower() == "github.com" else f"https://{host}/api/v3"
    api = f"{root}/repos/{owner}/{repo}/pulls/{number}"
    resp = requests.patch(
        api, json={"state": "closed"},
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=30,
    )
    return getattr(resp, "status_code", 500) < 400


def _default_mr_closer(mr_url: str, creds: UserCreds, config: Any) -> bool:
    """변경요청(GitLab MR / GitHub PR)을 닫는다. per-user 토큰. best-effort → bool.

    forge 판정은 **그 URL 자신**이 우선한다(:func:`app.forge.kind_for`) — 취소 롤백은
    이미 만들어진 링크를 되돌리는 일이라 링크의 모양이 가장 믿을 만한 근거다. 호스트가
    중립이면 ``config.forge.kind`` 로 내려간다. 토큰이 없거나 URL 모양이 안 맞으면 False.
    """
    from app.config import read_secret  # noqa: PLC0415

    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    token = read_secret(base_dir, _forge_token_ref(creds)) or ""
    if not token:
        return False
    kind = forge.kind_for(url=mr_url, config=config)
    if kind == forge.KIND_GITHUB:
        return _close_github_pr(mr_url, token)
    return _close_gitlab_mr(mr_url, token)


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
            # 원격도 best-effort 삭제(없으면 조용히 실패). #6: ambient `origin` 이 아니라
            # per-user 토큰 URL 로 삭제-push(귀속 오염 방지). 토큰 미해결이면 skip(ambient 폴백 금지).
            push_url = _user_token_push_url(git_run, cwd, creds, config)
            if push_url:
                try:
                    git_run(["push", push_url, "--delete", branch], cwd=cwd)
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


# --- 체크포인트(담당자 변경 핸드오프 시 WIP 보존, best-effort) ----------------


def _write_handoff_note(cwd: Optional[str], ticket: str, branch: Optional[str]) -> Optional[str]:
    """이관 저널 노트를 대상 레포 작업트리(runs/<ticket>/)에 기록(best-effort).

    이후 ``git add -A`` + commit + push로 브랜치에 함께 실려, 이관받은 Y가 브랜치를
    fetch하면 이 노트를 본다. POLICY-ENCODING: UTF-8(BOM 없음)·LF.
    """
    if not cwd:
        return None
    runs_dir = os.path.join(cwd, "runs", ticket)
    os.makedirs(runs_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(runs_dir, f"HANDOFF-{ts}.md")
    note = (
        f"# 담당자 변경 핸드오프 — {ticket}\n\n"
        f"- 브랜치: `{branch or ''}`\n"
        f"- 시각(UTC): {ts}\n\n"
        "이전 담당자의 선행 작업(WIP)을 checkpoint로 보존한 뒤 이관했다(롤백 아님). "
        "이관받은 담당자는 이 브랜치의 WIP를 리뷰하고 자신의 정체성·규칙으로 이어서 완성한다.\n"
    )
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(note)
    return path


def checkpoint_job(
    job: dict,
    creds: UserCreds,
    config: Any,
    *,
    git_run: Optional[Callable] = None,
    write_note: Optional[Callable] = None,
) -> dict:
    """핸드오프된 잡의 WIP를 **커밋·push로 보존**한다(담당자 변경 이관, §handoff).

    :func:`rollback_job` 의 거울상 — 롤백(브랜치 삭제) 대신 **commit + push**를 한다.
    각 대상 레포에서: 저널 노트 기록 → ``git add -A`` → ``git commit``(비어 있으면
    nonzero → 스킵) → ``git push origin <branch>``(선행 커밋이 있을 수 있어 커밋 여부와
    무관하게 시도). 모두 **best-effort**(개별 실패는 로그만). git_run/write_note는 주입
    가능(순수 테스트). per-user 자격증명(creds)은 worker만 보유한다.

    Returns: {checkpointed, committed, pushed, branch}.
    """
    ticket = str((job or {}).get("ticket") or "")
    branch = (job or {}).get("branch") or (f"auto/{ticket}" if ticket else None)
    target_repos = list((job or {}).get("target_repos") or [])
    workspace = getattr(getattr(config, "run", None), "workspace_dir", "") or ""
    git_run = git_run or _default_git_run
    write_note = write_note or _write_handoff_note

    committed = False
    pushed = False
    for repo in target_repos:
        cwd = os.path.join(workspace, repo) if workspace else repo
        # 이관 저널 노트(best-effort) — WIP와 함께 커밋·push되어 Y가 fetch 시 본다.
        try:
            write_note(cwd, ticket, branch)
        except Exception:  # noqa: BLE001 — 노트 실패는 checkpoint를 막지 않는다
            log.warning("핸드오프 저널 노트 기록 실패(격리): %s", repo)
        # 모든 변경 스테이징.
        try:
            git_run(["add", "-A"], cwd=cwd)
        except Exception:  # noqa: BLE001
            pass
        # 커밋(비어 있으면 nonzero → 스킵 = 커밋할 것 없음).
        try:
            r = git_run(
                ["commit", "-m",
                 f"chore(handoff): {ticket} 체크포인트 — 담당자 변경 이관(WIP 보존, 롤백X)"],
                cwd=cwd,
            )
            if getattr(r, "returncode", 1) == 0:
                committed = True
        except Exception:  # noqa: BLE001
            pass
        # 브랜치 push(선행 커밋이 있을 수 있으니 커밋 여부와 무관하게 시도). #6: ambient
        # `origin` 금지 — per-user 토큰 URL 로 명시 push(귀속 정확). 토큰 미해결이면 skip.
        if branch:
            push_url = _user_token_push_url(git_run, cwd, creds, config)
            if push_url:
                try:
                    pr = git_run(["push", push_url, branch], cwd=cwd)
                    if getattr(pr, "returncode", 1) == 0:
                        pushed = True
                except Exception:  # noqa: BLE001
                    pass
            else:
                log.warning(
                    "checkpoint: per-user 토큰/origin 미해결 — %s push skip(ambient 폴백 금지)", repo
                )

    return {
        "checkpointed": bool(committed or pushed),
        "committed": committed,
        "pushed": pushed,
        "branch": branch,
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
    notify: Callable,
    checkpoint: Optional[Callable] = None,
    completed: Optional[dict] = None,
) -> None:
    """단일 잡 처리 — 진행중 보고 → 실행(제어 폴링) → (한도면 재개 반복) → 최종 회신.

    실행 중 취소 신호(control)가 오면 agent_runner가 abort하고 STATUS_CANCELLED로
    돌려준다. 그러면 worker가 **롤백**(가역 산출 되돌리기)을 수행한 뒤 cancelled로
    회신한다(§10.4).

    실행 중 **핸드오프**(담당자 변경) 신호가 오면 agent_runner가 STATUS_HANDOFF로
    돌려준다. 그러면 worker가 **체크포인트**(WIP 커밋·push — 롤백 아님)를 수행한 뒤
    handed_off로 회신한다(재배정 ≠ 취소).

    각 터미널/전이 결과(성공·failed·interrupted·cancelled)마다 완료 알림을
    best-effort로 발송한다(notify). 알림은 채널 F 회신과 독립이며 잡을 죽이지 않는다.

    ⚠️ 무한루프 방지: 최종 터미널 회신이 연결 실패로 미도달(유한 재시도 소진)하면,
    같은 잡이 다음 tick에 다시 dispatch되더라도 **재실행하지 않도록** completed에
    티켓→재회신 페이로드를 캐시한다(worker_loop가 재실행 대신 회신만 재시도). 회신에
    성공하면 캐시를 비운다(central이 종결 처리 완료).
    """
    ticket = str(job.get("ticket") or "")
    branch = job.get("branch") or (f"auto/{ticket}" if ticket else None)
    buffer_sec = int(getattr(getattr(config, "resume", None), "reset_buffer_sec", 120) or 0)
    checkpoint = checkpoint or checkpoint_job

    # 즉시 진행중 보고(착수).
    _post_status(http, central, user, ticket, headers, {"status": "진행중", "branch": branch},
                 sleep=sleep, stop_event=stop_event)

    cancel_check = _make_control_check(
        control_poll, http, central, user, ticket, headers,
        now=now, interval_sec=control_poll_sec,
    )

    result = run(job, creds, config, cancel_check=cancel_check)
    if result.status == agent_runner.STATUS_CANCELLED:
        _handle_cancel(job, config, creds, result, http=http, central=central,
                       user=user, ticket=ticket, headers=headers, branch=branch,
                       rollback=rollback, notify=notify, sleep=sleep, stop_event=stop_event,
                       completed=completed)
        return
    if result.status == agent_runner.STATUS_HANDOFF:
        _handle_handoff(job, config, creds, result, http=http, central=central,
                        user=user, ticket=ticket, headers=headers, branch=branch,
                        checkpoint=checkpoint, notify=notify, sleep=sleep,
                        stop_event=stop_event, completed=completed)
        return

    # 한도(interrupted)면 reset_at까지 대기 후 재개를 반복.
    while result.status == agent_runner.STATUS_INTERRUPTED:
        _post_status(
            http, central, user, ticket, headers,
            _channel_f_payload(result, branch),
            sleep=sleep, stop_event=stop_event,
        )
        _safe_notify(notify, config, result, job, creds)  # 짧은 '멈춤' 알림(configurable)
        _wait_until(result.reset_at, sleep=sleep, now=now,
                    buffer_sec=buffer_sec, stop_event=stop_event)
        if stop_event is not None and stop_event.is_set():
            return
        sid = result.session_id or agent_runner.deterministic_session_id(ticket)
        result = resume(job, sid, creds, config, cancel_check=cancel_check)
        if result.status == agent_runner.STATUS_CANCELLED:
            _handle_cancel(job, config, creds, result, http=http, central=central,
                           user=user, ticket=ticket, headers=headers, branch=branch,
                           rollback=rollback, notify=notify, sleep=sleep, stop_event=stop_event,
                           completed=completed)
            return
        if result.status == agent_runner.STATUS_HANDOFF:
            _handle_handoff(job, config, creds, result, http=http, central=central,
                            user=user, ticket=ticket, headers=headers, branch=branch,
                            checkpoint=checkpoint, notify=notify, sleep=sleep,
                            stop_event=stop_event, completed=completed)
            return

    payload = _channel_f_payload(result, branch)
    ok, body = _post_status_core(http, central, user, ticket, headers, payload,
                                 sleep=sleep, stop_event=stop_event)
    # central이 커밋한 dlc-meta 사이클로그 상대경로를 알림에 실을 수 있게 job에 스탬프.
    _stamp_cycle_log(job, body)
    _safe_notify(notify, config, result, job, creds)  # 터미널(성공/failed) 알림
    _mark_completed(completed, ticket, payload, ok)


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
    notify: Callable,
    sleep: Optional[Callable] = None,
    stop_event=None,
    completed: Optional[dict] = None,
) -> None:
    """취소된 실행 후처리 — 롤백(best-effort) 후 cancelled 회신(§10.4) + 짧은 알림."""
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
    ok = _post_status(http, central, user, ticket, headers, payload,
                      sleep=sleep, stop_event=stop_event)
    _safe_notify(notify, config, result, job, creds)  # 짧은 '취소됨' 알림
    _mark_completed(completed, ticket, payload, ok)


def _handle_handoff(
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
    checkpoint: Callable,
    notify: Callable,
    sleep: Optional[Callable] = None,
    stop_event=None,
    completed: Optional[dict] = None,
) -> None:
    """핸드오프된 실행 후처리 — **체크포인트(WIP 커밋·push, 롤백X)** 후 handed_off 회신.

    ``_handle_cancel`` 의 거울상: 롤백 대신 checkpoint를 하고, cancelled 대신
    handed_off로 회신한다. central은 이 회신에서 롤백 없이 락+dedup을 정리하고,
    이관받을 Y가 있으면 같은 티켓을 Y로 재-dispatch(continue)한다(재배정 ≠ 취소).
    """
    try:
        cp = checkpoint(job, creds, config)
    except Exception:  # noqa: BLE001 — 체크포인트 실패해도 회신은 해야 락/dedup가 풀린다
        log.exception("체크포인트 실패(그래도 handed_off 회신)")
        cp = {"checkpointed": False}

    payload: dict = {
        "status": _STATUS_TO_CHANNEL_F.get(agent_runner.STATUS_HANDOFF, "handed_off"),
        "branch": cp.get("branch") or branch,
    }
    if result.session_id:
        payload["session_id"] = result.session_id
    if result.log_summary:
        payload["log_summary"] = result.log_summary
    audit = {k: cp[k] for k in ("committed", "pushed") if cp.get(k) is not None}
    if audit:
        payload["audit_refs"] = audit
    ok, body = _post_status_core(http, central, user, ticket, headers, payload,
                                 sleep=sleep, stop_event=stop_event)
    # 핸드오프도 완료(terminal) 회신 — central이 커밋한 사이클로그 경로를 알림에 싣는다.
    _stamp_cycle_log(job, body)
    _safe_notify(notify, config, result, job, creds)  # 짧은 '이관됨' 알림
    _mark_completed(completed, ticket, payload, ok)


def _mark_completed(
    completed: Optional[dict], ticket: str, payload: dict, posted_ok: bool
) -> None:
    """터미널(done/failed/cancelled) 처리 완료를 기록 — 무한 재실행 방지(수정 1).

    - posted_ok=True: central이 종결 회신을 받아 잡을 종결(레포락 해제)했으니 캐시 불필요
      → 혹시 남아 있던 캐시를 비운다.
    - posted_ok=False: 유한 재시도 소진으로 **회신 미도달**. 같은 잡이 다음 tick에 다시
      dispatch되어도 **재실행하지 않도록** 재회신 페이로드를 캐시한다(worker_loop가
      재실행 대신 회신만 재시도 → 결국 실제 결과가 도달; 도달 전까지 사람은 '재실행'
      버튼으로 재트리거 가능).
    """
    if completed is None or not ticket:
        return
    if posted_ok:
        completed.pop(ticket, None)
    else:
        completed[ticket] = payload


def _channel_f_payload(result: agent_runner.AgentResult, branch: Optional[str]) -> dict:
    """AgentResult → 채널 F POST 본문(상태 문자열 매핑 + None 생략)."""
    payload = result.to_status_payload(branch=branch)
    payload["status"] = _STATUS_TO_CHANNEL_F.get(result.status, result.status)
    return payload


def _stamp_cycle_log(job: dict, status_body: Optional[dict]) -> None:
    """central 완료 회신 본문의 ``cycle_log_path`` 를 job에 스탬프(알림용, best-effort).

    central이 공유 dlc-meta 클론에 그 잡의 사이클로그를 커밋하고 상대경로를 회신에
    실어 보낸다(:meth:`app.dispatch.Dispatcher.report_status`). 워커는 그 경로를 job에
    실어 :func:`app.notify.build_message` 가 `사이클로그: <relpath>` 라인을 붙이게 한다.
    본문/경로가 없으면 no-op(알림은 사이클로그 라인 없이 발송).
    """
    if not isinstance(job, dict) or not isinstance(status_body, dict):
        return
    relpath = status_body.get("cycle_log_path")
    if relpath:
        job["cycle_log_path"] = str(relpath)


def _safe_notify(
    notify: Callable,
    config: Any,
    result: agent_runner.AgentResult,
    job: dict,
    creds: UserCreds,
) -> None:
    """완료 알림 발송(best-effort 이중 격리) — 실패해도 잡/루프에 영향 없음.

    notify(=notify_job_end)는 자체적으로 예외를 삼키지만, 주입된 대역이 던질
    가능성까지 방어한다. 알림은 채널 F 회신과 **독립**이며 결코 잡을 죽이지 않는다.
    """
    try:
        notify(config, result, job, creds)
    except Exception:  # noqa: BLE001 — 알림 실패는 격리(런에 영향 없음)
        log.warning("완료 알림 실패(격리)")


def _run_concurrent_loop(
    config: Any,
    concurrency: int,
    *,
    http,
    creds: UserCreds,
    user: str,
    central: str,
    headers: dict,
    run: Callable,
    resume: Callable,
    sleep: Callable,
    now: Callable,
    stop_event,
    max_iterations: Optional[int],
    rollback: Callable,
    control_poll: Callable,
    control_poll_sec: int,
    notify: Callable,
    checkpoint: Callable,
    poll: int,
) -> int:
    """per-user 동시 실행 루프(concurrency≥2) — 최대 N개 잡을 스레드 풀로 병렬 처리.

    모델(fetch-while-running):
        - ``active`` = {ticket: Thread} — 지금 실행 중인 잡. 용량이 남으면
          ``_fetch_next(exclude=active 티켓)`` 로 **다른** 잡을 받아 새 스레드에서
          :func:`_process_job` (기존 경로 그대로: 자기 서브프로세스·상태회신·제어/취소/
          핸드오프·재개·completed 캐시)를 돌린다. 용량이 차면 폴 간격만큼 쉰다.
        - 한 잡이 끝나면(스레드 종료) reap해 슬롯을 비우고 다시 fetch한다.
        - **레포 충돌 없음**: central의 전역 레포락이 같은 레포 잡을 전 사용자에 걸쳐
          직렬화하므로, 동시에 dispatch되는 잡들은 항상 서로 다른 레포다 → worker
          내부 충돌해소가 불필요(설계 안전 근거).

    스레드 안전:
        - completed 캐시는 :class:`_LockedDict` (락 보호). 각 잡의 control-check는
          :func:`_make_control_check` 가 잡마다 독립 상태로 생성 → 공유 없음.
        - active 맵은 ``active_lock`` 으로 보호(루프 스레드만 쓰지만 명시적 일관성).
        - 공유 워크스페이스 프로비저닝(ensure_repos)은 서로 다른 레포라 안전하고,
          같은 레포라도 :func:`app.repos._provision_lock` 파일락이 직렬화한다.

    단일 잡 경로(concurrency=1)와 동일한 회복력을 각 잡에 그대로 적용한다
    (fetch 재시도·터미널 회신 미도달 시 completed 캐시 재회신·취소/핸드오프/롤백).
    """
    active: dict = {}                 # ticket -> Thread
    active_lock = threading.Lock()
    completed = _LockedDict()         # 스레드 안전 재회신 캐시
    iterations = 0

    def _active_tickets() -> set:
        with active_lock:
            return set(active.keys())

    def _reap_join() -> None:
        """끝난 잡 스레드를 조인·제거해 슬롯을 비운다(락 밖에서 조인 = 짧게)."""
        with active_lock:
            done_items = [(tk, th) for tk, th in active.items() if not th.is_alive()]
            for tk, _th in done_items:
                active.pop(tk, None)
        for _tk, th in done_items:
            th.join()

    def _job_thread(job: dict, ticket: str) -> None:
        try:
            _process_job(
                job, config, creds,
                http=http, central=central, user=user, headers=headers,
                run=run, resume=resume, sleep=sleep, now=now, stop_event=stop_event,
                rollback=rollback, control_poll=control_poll,
                control_poll_sec=control_poll_sec, notify=notify,
                checkpoint=checkpoint, completed=completed,
            )
        except Exception:  # noqa: BLE001 — 한 잡 실패가 다른 잡/루프를 죽이지 않게 격리
            log.exception("worker 잡 스레드 실패(격리): %s", ticket)

    while stop_event is None or not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            _reap_join()
            if len(_active_tickets()) >= concurrency:
                sleep(poll)                       # 용량 참 → 슬롯이 빌 때까지 대기
                continue
            exclude = _active_tickets()           # 이미 처리 중인 티켓 배제(중복 집기 방지)
            job = _fetch_next(http, central, user, headers, sleep=sleep,
                              stop_event=stop_event, exclude=exclude)
            if job is None:
                sleep(poll)
                continue
            ticket = str(job.get("ticket") or "")
            # 터미널 회신 미도달 잡 재수신 → 재실행 금지, 캐시된 회신만 재시도(수정 1).
            cached = completed.get(ticket) if ticket else None
            if cached is not None:
                log.warning("종결 잡 재수신 — 재실행 없이 회신만 재시도: %s", ticket)
                if _post_status(http, central, user, ticket, headers, cached,
                                sleep=sleep, stop_event=stop_event):
                    completed.pop(ticket, None)
                sleep(poll)
                continue
            if ticket and ticket in exclude:
                # 방어(정상적으론 exclude가 걸러야 함) — 이미 처리 중이면 잠깐 쉰다.
                sleep(poll)
                continue
            th = threading.Thread(target=_job_thread, args=(job, ticket),
                                  name=f"jad-job-{ticket}", daemon=True)
            with active_lock:
                active[ticket] = th
            th.start()
            # 용량이 남으면 슬립 없이 즉시 다음 잡을 채우러 재폴한다.
        except Exception:  # noqa: BLE001 — 루프 반복 격리(잡 스레드와 별개)
            log.exception("worker 동시 루프 반복 실패(격리)")
            sleep(poll)

    # 드레인: 남은 잡 스레드가 최종 회신을 마치도록 대기(정지·테스트 결정성).
    with active_lock:
        remaining = list(active.values())
    for th in remaining:
        th.join()
    return iterations


def _fractal_enabled(config: Any) -> bool:
    """프랙탈 P1 신경로 사용 여부(지연 import — OFF면 session_manager 미로딩)."""
    from app.session_manager import fractal_enabled  # noqa: PLC0415

    return fractal_enabled(config)


def _run_fractal_loop(
    config: Any,
    *,
    http,
    creds: UserCreds,
    user: str,
    central: str,
    headers: dict,
    sleep: Callable,
    now: Callable,
    stop_event,
    max_iterations: Optional[int],
    rollback: Callable,
    control_poll: Callable,
    control_poll_sec: int,
    notify: Callable,
    checkpoint: Callable,
    poll: int,
    session: Optional[Any] = None,
) -> int:
    """프랙탈 P1 신경로 루프 — 사용자당 **지속 세션 하나**에 티켓을 이어 주입.

    기존 per-ticket 경로(run_job/_consume)와의 유일한 차이: ``run``/``resume`` 이
    프로세스를 티켓마다 새로 띄우지 않고, :class:`app.session_manager.UserSession` 의
    ``run_ticket``/``resume_ticket`` 으로 **같은 세션 핸들에 이어 주입**(재사용-if-up)한다.
    나머지 라이프사이클(진행중 회신 → 실행 → 취소/핸드오프/재개 → 터미널 회신 → 알림 →
    completed 재회신 캐시)은 :func:`_process_job` 을 그대로 재사용해 채널 F 브리지를 유지한다.

    **drain-종료(설계 §4)**: 폴이 잡을 못 받으면(204/None) 세션을 drain 해 종료한다
    (유휴 = 프로세스 없음). 다음 잡이 오면 세션이 다시 스폰된다(재사용은 버스트 내에서).
    루프 종료 시에도 남은 세션을 drain 한다(좀비 방지 — tini/killpg 백스톱 재사용).
    """
    if session is None:
        from app.session_manager import UserSession  # noqa: PLC0415

        session = UserSession(config, creds, user=user)

    run = session.run_ticket
    resume = session.resume_ticket
    completed: dict = {}
    iterations = 0
    try:
        while stop_event is None or not stop_event.is_set():
            if max_iterations is not None and iterations >= max_iterations:
                break
            iterations += 1
            try:
                job = _fetch_next(http, central, user, headers, sleep=sleep, stop_event=stop_event)
                if job is None:
                    # 큐 비면 세션 drain(더 이상 주입할 잡 없음 = 정상 종료, 설계 §4).
                    session.drain()
                    sleep(poll)
                    continue
                ticket = str(job.get("ticket") or "")
                if ticket and ticket in completed:
                    # 종결 회신 미도달 잡 재수신 — 재실행 금지, 캐시 회신만 재시도.
                    log.warning("종결 잡 재수신 — 재실행 없이 회신만 재시도: %s", ticket)
                    if _post_status(http, central, user, ticket, headers, completed[ticket],
                                    sleep=sleep, stop_event=stop_event):
                        completed.pop(ticket, None)
                    sleep(poll)
                    continue
                _process_job(
                    job, config, creds,
                    http=http, central=central, user=user, headers=headers,
                    run=run, resume=resume, sleep=sleep, now=now, stop_event=stop_event,
                    rollback=rollback, control_poll=control_poll,
                    control_poll_sec=control_poll_sec, notify=notify,
                    checkpoint=checkpoint, completed=completed,
                )
            except Exception:  # noqa: BLE001 — 한 잡 실패가 루프를 죽이지 않게 격리
                log.exception("worker 프랙탈 루프 반복 실패(격리)")
                sleep(poll)
    finally:
        # 루프 종료 — 남은 세션을 drain(좀비 방지).
        try:
            session.drain()
        except Exception:  # noqa: BLE001
            log.warning("세션 drain 실패(격리)")
    return iterations


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
    notify: Optional[Callable] = None,
    checkpoint: Optional[Callable] = None,
    concurrency: Optional[int] = None,
    session: Optional[Any] = None,
) -> int:
    """중앙 폴링 루프(worker 프로세스 진입점). 처리 반복 수를 반환(테스트용).

    env DISPATCH_USER / CENTRAL_URL / WORKER_SHARED_SECRET 를 읽어 폴링한다.
    각 반복은 예외 격리되어(한 잡 실패가 루프를 죽이지 않음), 204면 폴링 주기만큼
    대기한다. max_iterations/stop_event로 정지한다. rollback/control_poll은
    취소 플로우(§10.4) 배선점으로 주입 가능(테스트).

    **동시 실행:** worker는 central이 dispatch한 잡을 **모두** 동시에 굴린다(진짜 스로틀은
    central의 서버 자원 어드미션). ``concurrency`` (미지정 시 env WORKER_CONCURRENCY 또는
    config.run.worker_max_concurrency = **안전 상한** 64로 파생)가 ≥2면, 서로 다른(=서로
    다른 레포, central의 전역 레포락이 보장) 잡을 그 상한까지 **스레드 풀**로 동시 실행한다
    (각 잡은 자기 ``claude`` 서브프로세스에서 블록되므로 스레드로 충분). 이 상한은 정책 cap이
    아니라 runaway 방지 백스톱이다. concurrency=1이면 아래 단일 잡 경로가 **기존과 100%
    동일**하게 동작한다(회귀 안전 — 잡이 하나뿐일 때의 동작도 이 경로와 동치).
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
    notify = notify or notify_job_end
    checkpoint = checkpoint or checkpoint_job
    poll = _poll_interval(env)
    control_poll_sec = _control_poll_interval(env)
    headers = {"X-Worker-Secret": secret} if secret else {}

    # --- 프랙탈 P1 신경로(기본 OFF) ---
    # run.fractal_worker 가 True 면 **사용자당 지속 세션 하나**(app/session_manager)에
    # 티켓을 이어 주입(재사용-if-up)하고 drain 때 종료하는 신경로로 라우팅한다. OFF면
    # 이 분기를 **아예 타지 않아** 아래 기존 per-ticket 경로가 byte-for-byte 그대로 돈다
    # (무동작변경). 세션은 주입 가능(테스트) — 미지정이면 여기서 구성한다.
    if _fractal_enabled(config):
        return _run_fractal_loop(
            config, http=http, creds=creds, user=user, central=central, headers=headers,
            sleep=sleep, now=now, stop_event=stop_event, max_iterations=max_iterations,
            rollback=rollback, control_poll=control_poll, control_poll_sec=control_poll_sec,
            notify=notify, checkpoint=checkpoint, poll=poll, session=session,
        )

    concurrency = concurrency if concurrency is not None else _worker_concurrency(env, config)
    if concurrency > 1:
        # per-user 동시 실행 경로(Increment 2). 단일 잡 경로는 아래 그대로 보존.
        return _run_concurrent_loop(
            config, concurrency, http=http, creds=creds, user=user,
            central=central, headers=headers, run=run, resume=resume, sleep=sleep,
            now=now, stop_event=stop_event, max_iterations=max_iterations,
            rollback=rollback, control_poll=control_poll, control_poll_sec=control_poll_sec,
            notify=notify, checkpoint=checkpoint, poll=poll,
        )

    # 터미널 회신이 미도달한 잡의 재회신 큐(ticket→payload). 같은 잡이 다시
    # dispatch되어도 **재실행하지 않고** 회신만 재시도해 무한 재실행을 막는다(수정 1).
    completed: dict = {}

    iterations = 0
    while stop_event is None or not stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        iterations += 1
        try:
            job = _fetch_next(http, central, user, headers, sleep=sleep, stop_event=stop_event)
            if job is None:
                sleep(poll)
                continue
            ticket = str(job.get("ticket") or "")
            if ticket and ticket in completed:
                # 이미 종결 처리한 잡을 central이 다시 넘김(터미널 회신 미도달) →
                # ⚠️ 재실행 금지. 캐시된 종결 회신만 재시도(성공하면 캐시 제거).
                log.warning("종결 잡 재수신 — 재실행 없이 회신만 재시도: %s", ticket)
                if _post_status(http, central, user, ticket, headers, completed[ticket],
                                sleep=sleep, stop_event=stop_event):
                    completed.pop(ticket, None)
                sleep(poll)
                continue
            _process_job(
                job, config, creds,
                http=http, central=central, user=user, headers=headers,
                run=run, resume=resume, sleep=sleep, now=now, stop_event=stop_event,
                rollback=rollback, control_poll=control_poll,
                control_poll_sec=control_poll_sec, notify=notify,
                checkpoint=checkpoint, completed=completed,
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
    "checkpoint_job",
    "rollback_job",
    "worker_loop",
]
