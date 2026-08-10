"""에이전트 러너 — `claude -p`(오케스트레이터) 실행(워커 전용).

역할:
    worker가 수신한 잡 1건을, 그 잡의 소유 사용자 정체성으로 오케스트레이터
    (`claude` CLI)를 자율 실행한다. per-user attribution(git author=사용자,
    Jira actor=사용자 토큰, MR 생성자=사용자 GitLab 토큰, 실행=사용자 Claude)의
    실제 주입 지점이 여기다.

역할 소속: **worker**.

구현 Phase: **Phase 5** (워커 + 에이전트 실행).

공개 API(순수/부수효과 분리):
    - ``build_prompt(job, config)``            채널 E 프롬프트 문자열(순수)
    - ``build_command(job, config, ...)``      claude 실행 인자 리스트(순수)
    - ``build_env(job, creds, config, ...)``   env dict + 시크릿 값 목록(부수: 시크릿 읽기)
    - ``run_job(job, creds, config, ...)``      신규 실행 → AgentResult
    - ``resume_job(job, session_id, ...)``      ``--resume`` 재개 실행 → AgentResult
    파싱 유틸(순수함수, 단위테스트 대상):
    - ``parse_stream_event(line)``             stream-json 한 줄 → dict|None
    - ``extract_session_id(event)``            방어적 session_id 추출
    - ``detect_limit_and_reset(event|text)``   (한도여부, reset_at) 추출

실행 계약:
    cwd = config.run.orchestrator_repo                # 오케스트레이터 정체성
    claude -p "<프롬프트>"
        --output-format stream-json                   # 라인 단위 이벤트
        --dangerously-skip-permissions                # 승인자 없음(사내망 한정)
        --session-id <uuid5(NAMESPACE, ticket)>       # 티켓 결정적 UUID = 재개 키
    재개: claude -p "<계속>" --output-format stream-json
        --dangerously-skip-permissions --resume <session-id> [--from-pr <PR#>]

⚠️ 시크릿 규율:
    토큰 "값"은 로그·에러·AgentResult에 절대 노출하지 않는다. 값은
    ``config.read_secret``(base_dir 상대)로만 읽고, 실행 요약은 주입된 시크릿
    값을 방어적으로 마스킹(:func:`_redact`)한 뒤 반환한다.
    GitHub 토큰은 주입하지 않는다(central 전용).

Popen 관용(claude-hacker/worker 계승):
    text=True, encoding='utf-8', errors='replace', bufsize=1
    ANSI 이스케이프 제거 정규식(:data:`ANSI_ESCAPE`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app.config import read_secret
from app.repos import ensure_repos

log = logging.getLogger("jad.agent_runner")

# --- 상수 -----------------------------------------------------------------

# 터미널 ANSI 이스케이프 제거(부수 출력 정리용). worker가 이 상수를 계승한다.
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# 티켓 → 결정적 session-id(uuid5) 네임스페이스. 고정 UUID(변경 금지 — 재개 키 안정성).
SESSION_NAMESPACE = uuid.UUID("7d3e0a2c-2b6f-5e14-9c3a-1f5b8d0e4a67")

# AgentResult.status(채널 F로 회신하는 잡 상태 문자열; central이 정규화).
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
STATUS_CANCELLED = "cancelled"      # 취소 신호로 abort된 실행(§10.3) — worker가 롤백 후 회신

# 취소 시 subprocess terminate → kill 대기 상한(초).
TERMINATE_GRACE_SEC = 5

# stream-json 이벤트에서 session_id가 담길 수 있는 후보 키(버전차 방어).
_SESSION_ID_KEYS = ("session_id", "sessionId", "sessionID", "session")

# 이벤트 안에서 중첩 탐색할 컨테이너 키(방어적).
_NESTED_KEYS = ("data", "system", "message", "result", "init", "meta", "usage", "error")

# 한도(usage limit) 감지 패턴(영/한, 버전차 방어).
_LIMIT_PATTERN = re.compile(
    r"(usage\s+limit|rate\s*limit|limit\s+reached|limit\s+exceeded|"
    r"too\s+many\s+requests|out\s+of\s+(?:usage|quota)|quota\s+exceeded|"
    r"사용\s*한도|사용량\s*한도|한도\s*(?:도달|초과))",
    re.IGNORECASE,
)

# reset 시각이 담길 수 있는 구조화 필드 키.
_RESET_KEYS = (
    "reset_at", "resetAt", "resets_at", "resetsAt", "reset",
    "reset_time", "resetTime", "reset_timestamp", "resetTimestamp",
)

# ISO8601 날짜/시각 패턴(reset 시각 텍스트 추출용).
_ISO_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)

# "reset ... <epoch>" 형태에서 에폭(초/밀리초) 추출.
_RESET_EPOCH_PATTERN = re.compile(r"reset\w*[^0-9]{0,24}(\d{10,13})", re.IGNORECASE)

# MR/PR URL 추출(GitLab merge_requests 우선, 일반 PR 폴백).
_MR_URL_PATTERN = re.compile(
    r"https?://\S+?/-/merge_requests/\d+"
    r"|https?://\S+?/merge_requests/\d+"
    r"|https?://\S+?/pull/\d+",
    re.IGNORECASE,
)

# worker에는 절대 주입하지 않는 GitHub 계열 토큰 env(central 전용 — 상속분도 제거).
_GITHUB_ENV_KEYS = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
)

_SUMMARY_MAX_CHARS = 4000


# --- 사용자 자격 --------------------------------------------------------------


@dataclass
class UserCreds:
    """worker가 대리하는 사용자 자격/정체성(값이 아니라 대부분 참조).

    시크릿은 ``*_token_ref``(secrets.base_dir 상대 경로/참조)로만 담고, 실토큰
    값은 :func:`build_env` 실행 시점에 ``config.read_secret``로만 읽는다.
    ``claude_oauth_token_value``는 컨테이너 env(CLAUDE_CODE_OAUTH_TOKEN)로 이미
    주입된 값을 위한 폴백이다.
    """

    user: str = ""
    git_name: str = ""
    git_email: str = ""
    jira_email: str = ""
    jira_token_ref: str = ""
    gitlab_token_ref: str = ""
    claude_oauth_token_ref: str = ""
    claude_oauth_token_value: str = ""

    @staticmethod
    def from_env(user: str, env: Optional[dict] = None) -> "UserCreds":
        """스포너가 주입한 worker env에서 사용자 자격을 조립.

        env 계약(스포너/Phase 6가 채움):
            DISPATCH_GIT_NAME / DISPATCH_GIT_EMAIL   git author 정체성
            DISPATCH_JIRA_EMAIL                      Jira Basic actor 이메일
            JIRA_TOKEN_REF / GITLAB_TOKEN_REF        secrets.base_dir 상대 참조
            CLAUDE_OAUTH_TOKEN_REF                    (선택) 참조
            CLAUDE_CODE_OAUTH_TOKEN                   (폴백) 값 직접
        """
        e = env if env is not None else os.environ
        return UserCreds(
            user=user,
            git_name=e.get("DISPATCH_GIT_NAME", "") or e.get("GIT_AUTHOR_NAME", ""),
            git_email=e.get("DISPATCH_GIT_EMAIL", "") or e.get("GIT_AUTHOR_EMAIL", ""),
            jira_email=e.get("DISPATCH_JIRA_EMAIL", "") or e.get("JIRA_EMAIL", ""),
            jira_token_ref=e.get("JIRA_TOKEN_REF", ""),
            gitlab_token_ref=e.get("GITLAB_TOKEN_REF", ""),
            claude_oauth_token_ref=e.get("CLAUDE_OAUTH_TOKEN_REF", ""),
            claude_oauth_token_value=e.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
        )

    @staticmethod
    def from_registry_record(record: Any) -> "UserCreds":
        """레지스트리 UserRecord(identity/secrets_ref)에서 자격을 조립(참조만)."""
        identity = getattr(record, "identity", None)
        secrets_ref = getattr(record, "secrets_ref", None)
        return UserCreds(
            user=getattr(record, "username", ""),
            git_name=getattr(identity, "git_name", "") if identity else "",
            git_email=getattr(identity, "git_email", "") if identity else "",
            jira_email=getattr(record, "jira_email", ""),
            jira_token_ref=getattr(secrets_ref, "jira_token", "") if secrets_ref else "",
            gitlab_token_ref=getattr(secrets_ref, "gitlab_token", "") if secrets_ref else "",
            claude_oauth_token_ref=(
                getattr(secrets_ref, "claude_oauth_token", "") if secrets_ref else ""
            ),
        )


# --- 실행 결과 --------------------------------------------------------------


@dataclass
class AgentResult:
    """단일 실행의 채널 F 회신 재료(시크릿 미포함)."""

    status: str = STATUS_FAILED           # done | failed | interrupted
    session_id: Optional[str] = None
    mr_url: Optional[str] = None
    reset_at: Optional[str] = None
    log_summary: str = ""
    returncode: Optional[int] = None
    audit_refs: dict = field(default_factory=dict)

    def to_status_payload(self, *, branch: Optional[str] = None) -> dict:
        """채널 F POST 본문(None 필드는 생략)."""
        payload: dict = {"status": self.status}
        for k, v in (
            ("session_id", self.session_id),
            ("mr_url", self.mr_url),
            ("reset_at", self.reset_at),
            ("log_summary", self.log_summary),
            ("branch", branch),
        ):
            if v:
                payload[k] = v
        if self.audit_refs:
            payload["audit_refs"] = self.audit_refs
        return payload


# --- 순수 파싱 유틸(단위테스트 대상) -----------------------------------------


def strip_ansi(text: Optional[str]) -> str:
    """ANSI 이스케이프 제거."""
    return ANSI_ESCAPE.sub("", text or "")


def parse_stream_event(line: str) -> Optional[dict]:
    """stream-json 한 줄을 dict로 파싱. 공백/비-JSON/비-객체는 None.

    stream-json은 줄마다 하나의 JSON 오브젝트다. 부수 출력(ANSI·진행바 등)이
    섞일 수 있으므로 관대하게(실패=None) 파싱한다.
    """
    if not line:
        return None
    s = strip_ansi(line).strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _iter_candidate_dicts(event: dict):
    """event 자신 + 한 단계 중첩 dict들을 순회(방어적 탐색용)."""
    yield event
    for key in _NESTED_KEYS:
        v = event.get(key)
        if isinstance(v, dict):
            yield v


def extract_session_id(event: Optional[dict]) -> Optional[str]:
    """이벤트에서 session_id를 방어적으로 추출(여러 후보 키 + 한 단계 중첩)."""
    if not isinstance(event, dict):
        return None
    for scope in _iter_candidate_dicts(event):
        for key in _SESSION_ID_KEYS:
            val = scope.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return None


def _to_iso(value: Any) -> Optional[str]:
    """epoch(초/밀리초)/ISO 문자열을 ISO8601(UTC 'Z')로 정규화. 실패 시 None."""
    if value is None:
        return None
    # 숫자(에폭)
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        num = float(value)
        if num > 1e12:      # 밀리초로 보이는 값
            num /= 1000.0
        if num <= 0:
            return None
        try:
            dt = datetime.fromtimestamp(num, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
        return dt.isoformat().replace("+00:00", "Z")
    # ISO 문자열
    if isinstance(value, str):
        m = _ISO_PATTERN.search(value)
        if m:
            return m.group(0)
    return None


def _find_reset_field(obj: Any, _depth: int = 0) -> Optional[str]:
    """dict/list를 재귀 탐색해 reset 시각 필드 값을 ISO로 반환(방어적)."""
    if _depth > 4:
        return None
    if isinstance(obj, dict):
        for key in _RESET_KEYS:
            if key in obj:
                iso = _to_iso(obj[key])
                if iso:
                    return iso
        for v in obj.values():
            iso = _find_reset_field(v, _depth + 1)
            if iso:
                return iso
    elif isinstance(obj, list):
        for v in obj:
            iso = _find_reset_field(v, _depth + 1)
            if iso:
                return iso
    return None


def _reset_from_text(text: str) -> Optional[str]:
    """자유 텍스트에서 reset 시각 추출(ISO 우선, 없으면 'reset ... epoch')."""
    m = _ISO_PATTERN.search(text)
    if m:
        return m.group(0)
    m = _RESET_EPOCH_PATTERN.search(text)
    if m:
        return _to_iso(m.group(1))
    return None


def detect_limit_and_reset(event_or_text: Any) -> "tuple[bool, Optional[str]]":
    """(한도 도달 여부, reset_at ISO|None)를 방어적으로 추출.

    event(dict)면 구조화 필드에서 reset을 먼저 찾고, 없으면 직렬화 텍스트에서
    한도 문자열/리셋시각을 매칭한다. 순수 문자열도 그대로 처리한다.
    """
    if isinstance(event_or_text, dict):
        text = json.dumps(event_or_text, ensure_ascii=False)
    else:
        text = str(event_or_text or "")

    is_limit = bool(_LIMIT_PATTERN.search(text))

    reset_at: Optional[str] = None
    if isinstance(event_or_text, dict):
        reset_at = _find_reset_field(event_or_text)
    if reset_at is None:
        reset_at = _reset_from_text(text)

    return is_limit, reset_at


def extract_mr_url(event_or_text: Any) -> Optional[str]:
    """이벤트/텍스트에서 MR(또는 PR) URL을 추출(구조화 필드 우선)."""
    if isinstance(event_or_text, dict):
        for scope in _iter_candidate_dicts(event_or_text):
            v = scope.get("mr_url")
            if isinstance(v, str) and v.strip():
                return v.strip()
        text = json.dumps(event_or_text, ensure_ascii=False)
    else:
        text = str(event_or_text or "")
    m = _MR_URL_PATTERN.search(text)
    return m.group(0) if m else None


def _redact(text: str, secret_values) -> str:
    """텍스트에서 주입 시크릿 값을 마스킹(로그 유출 방어)."""
    if not text:
        return text or ""
    out = text
    for sv in secret_values or ():
        if sv and len(sv) >= 4:
            out = out.replace(sv, "***")
    return out


# --- 프롬프트/커맨드/환경 구성 ------------------------------------------------


def _job_field(job: Any, name: str, default: Any = None) -> Any:
    """job(dict 또는 객체)에서 필드 접근(worker는 dict, 테스트는 객체 가능)."""
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def deterministic_session_id(ticket: str) -> str:
    """티켓 기반 결정적 session-id(uuid5) — 재개 키의 안정성 근거."""
    return str(uuid.uuid5(SESSION_NAMESPACE, ticket or ""))


def build_prompt(job: Any, config: Any = None) -> str:
    """채널 E 실현 — 오케스트레이터에게 줄 프롬프트 문자열(순수, §3·§6).

    핵심 불변식(루프 방지): **트리거 티켓 = 작업 티켓**. 새 사이클 티켓을 만들지
    않고 이 티켓에 바인딩한다.
    """
    ticket = str(_job_field(job, "ticket", "") or "")
    mode = str(_job_field(job, "autonomy_mode", "B") or "B").upper()
    branch = _job_field(job, "branch", None) or f"auto/{ticket}"
    ctx = _job_field(job, "context_refs", {}) or {}
    target_repos = _job_field(job, "target_repos", []) or []

    if mode == "A":
        mode_desc = (
            "A(완전자율) — 코드까지 완성하고 MR 초안까지 생성한다. "
            "착수 시 '진행 중'으로 전이하고, 완료 시 '완료'로 전이한 뒤 MR을 만든다."
        )
    else:
        mode_desc = (
            "B(경량 1차) — 트리아지 + 브랜치 + 스캐폴딩 + 최소 1차 시도 + "
            "runs 저널까지 수행한다. 실질적 완성은 로컬 사람 루프에서 이뤄지므로 "
            "MR을 강행하지 말고 1차 산출과 저널을 남긴다. 착수 시 '진행 중'으로 전이한다."
        )

    lines = [
        f"{ticket} 티켓을 오케스트레이터로서 수행하라.",
        "이 트리거 티켓이 곧 작업 티켓이다 — 새 사이클 티켓을 만들지 말고 "
        "이 티켓에 바인딩하라(루프 방지).",
        f"autonomy_mode={mode}: {mode_desc}",
        f"브랜치는 {branch} 를 사용하라.",
        f"작업 산출은 runs/{ticket}/ 저널에 기록하라(재개 컨텍스트).",
    ]
    if target_repos:
        lines.append("대상 레포: " + ", ".join(str(r) for r in target_repos) + ".")

    ctx_bits = []
    for label, key in (("dlc-meta", "dlc_meta"), ("dataspace_docs", "dataspace_docs"),
                       ("runs 저널", "runs")):
        val = ctx.get(key)
        if val:
            ctx_bits.append(f"{label}={val}")
    if ctx_bits:
        lines.append("컨텍스트 참조: " + ", ".join(ctx_bits) + ".")

    return "\n".join(lines)


def build_command(
    job: Any,
    config: Any,
    *,
    resume: bool = False,
    session_id: Optional[str] = None,
    from_pr: Optional[Any] = None,
) -> list:
    """claude 실행 인자 리스트 구성(신규/재개 분기, 순수).

    신규: --session-id <uuid5(ticket)>. 재개: --resume <session-id> [--from-pr N].
    """
    run = getattr(config, "run", None)
    claude_bin = getattr(run, "claude_bin", "claude") if run else "claude"
    output_format = getattr(run, "output_format", "stream-json") if run else "stream-json"

    ticket = str(_job_field(job, "ticket", "") or "")
    prompt = build_prompt(job, config)

    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        output_format,
    ]
    # stream-json은 --print(-p)와 함께 쓸 때 CLI가 --verbose를 강제한다
    # (없으면 "requires --verbose" 에러로 즉시 실패 → session_id 유실). 방어적으로
    # output_format이 stream-json일 때만 붙인다(신규·resume 공통).
    if output_format == "stream-json":
        cmd.append("--verbose")
    cmd.append("--dangerously-skip-permissions")

    if resume:
        sid = session_id or deterministic_session_id(ticket)
        cmd += ["--resume", sid]
        if from_pr is not None and str(from_pr) != "":
            cmd += ["--from-pr", str(from_pr)]
    else:
        cmd += ["--session-id", deterministic_session_id(ticket)]

    return cmd


def _secret_abs_path(config: Any, ref: str) -> Optional[str]:
    """secrets.base_dir 상대 참조의 절대 경로(파일 존재 시)."""
    if not ref:
        return None
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    path = os.path.join(base_dir, ref) if base_dir else ref
    return path if os.path.exists(path) else None


def build_env(
    job: Any,
    creds: UserCreds,
    config: Any,
    base_env: Optional[dict] = None,
) -> "tuple[dict, list]":
    """사용자 정체성 주입 env dict + 주입된 시크릿 값 목록(로그 마스킹용).

    주입:
        GIT_AUTHOR_NAME/EMAIL, GIT_COMMITTER_NAME/EMAIL  (git author=사용자)
        JIRA_EMAIL, JIRA_API_TOKEN[, JIRA_TOKEN_FILE]    (Jira actor)
        GITLAB_TOKEN[, GITLAB_TOKEN_FILE]                (MR 생성자)
        CLAUDE_CODE_OAUTH_TOKEN                          (사용자 Claude)
    제거:
        GITHUB_TOKEN/GH_TOKEN/... (central 전용 — 상속분도 삭제)

    시크릿 값은 config.read_secret(base_dir 상대)로만 읽고, 반환 목록에 담아
    실행 요약 마스킹에 쓴다. 값 자체는 로그로 내보내지 않는다.
    """
    env = dict(base_env if base_env is not None else os.environ)
    secret_values: list = []

    # GitHub 계열 토큰은 worker에 절대 남기지 않는다(상속분 제거).
    for k in _GITHUB_ENV_KEYS:
        env.pop(k, None)

    # git author 정체성.
    if creds.git_name:
        env["GIT_AUTHOR_NAME"] = creds.git_name
        env["GIT_COMMITTER_NAME"] = creds.git_name
    if creds.git_email:
        env["GIT_AUTHOR_EMAIL"] = creds.git_email
        env["GIT_COMMITTER_EMAIL"] = creds.git_email
    if creds.jira_email:
        env["JIRA_EMAIL"] = creds.jira_email

    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""

    # Jira actor 토큰.
    if creds.jira_token_ref:
        val = read_secret(base_dir, creds.jira_token_ref)
        if val:
            env["JIRA_API_TOKEN"] = val
            secret_values.append(val)
        path = _secret_abs_path(config, creds.jira_token_ref)
        if path:
            env["JIRA_TOKEN_FILE"] = path

    # GitLab MR 생성자 토큰.
    if creds.gitlab_token_ref:
        val = read_secret(base_dir, creds.gitlab_token_ref)
        if val:
            env["GITLAB_TOKEN"] = val
            secret_values.append(val)
        path = _secret_abs_path(config, creds.gitlab_token_ref)
        if path:
            env["GITLAB_TOKEN_FILE"] = path

    # 사용자 Claude(setup-token): 참조 우선, 없으면 env 폴백.
    claude_val = None
    if creds.claude_oauth_token_ref:
        claude_val = read_secret(base_dir, creds.claude_oauth_token_ref)
    if not claude_val:
        claude_val = creds.claude_oauth_token_value or env.get("CLAUDE_CODE_OAUTH_TOKEN")
    if claude_val:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = claude_val
        secret_values.append(claude_val)

    return env, secret_values


# --- 실행 --------------------------------------------------------------------


def _default_popen(cmd: list, *, cwd: Optional[str], env: dict):
    """기본 Popen 팩토리(claude-hacker 관용 계승)."""
    return subprocess.Popen(
        cmd,
        cwd=cwd or None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )


def _summarize_event(event: dict) -> Optional[str]:
    """이벤트에서 요약 한 줄 추출(결과/에러/타입) — 시크릿 미포함 가정."""
    etype = event.get("type")
    if etype == "result":
        sub = event.get("subtype") or ("error" if event.get("is_error") else "ok")
        text = event.get("result") or event.get("error") or ""
        return f"[result:{sub}] {text}".strip()
    if etype == "error" or event.get("is_error"):
        return f"[error] {event.get('error') or event.get('message') or ''}".strip()
    if etype in ("system", "init"):
        sub = event.get("subtype")
        if sub:
            return f"[{etype}:{sub}]"
    return None


def _terminate_proc(proc) -> None:
    """실행 중 subprocess를 종료(terminate → 유예 후 kill). best-effort."""
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001 — 이미 죽었거나 대역 객체일 수 있음
        pass
    try:
        proc.wait(timeout=TERMINATE_GRACE_SEC)
        return
    except TypeError:
        # wait()가 timeout 인자를 받지 않는 대역/구현.
        try:
            proc.wait()
            return
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — 유예 초과 등
        pass
    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


def _consume(proc, secret_values: list, *, cancel_check: Optional[Callable[[], bool]] = None) -> AgentResult:
    """프로세스 stdout(라인 이터러블)을 소비해 AgentResult로 환원.

    proc는 ``.stdout``(라인 이터러블) + ``.wait()``/``.returncode`` 를 갖는 객체.

    ``cancel_check``가 주어지면 **이벤트 라인 단위**로 취소 여부를 폴링한다(§10.4).
    True면 subprocess를 종료하고 ``STATUS_CANCELLED`` 결과를 반환한다(롤백/회신은
    호출부인 worker가 담당).
    """
    session_id: Optional[str] = None
    mr_url: Optional[str] = None
    reset_at: Optional[str] = None
    limit_hit = False
    error_seen = False
    cancelled = False
    summary_parts: list = []

    stdout = getattr(proc, "stdout", None)
    if stdout is not None:
        for raw in stdout:
            # 취소 폴링(이벤트마다). 신호 감지 시 즉시 종료.
            if cancel_check is not None and cancel_check():
                cancelled = True
                break
            event = parse_stream_event(raw)
            if event is None:
                continue
            sid = extract_session_id(event)
            if sid:
                session_id = sid
            url = extract_mr_url(event)
            if url:
                mr_url = url
            is_lim, r = detect_limit_and_reset(event)
            if is_lim:
                limit_hit = True
                if r:
                    reset_at = r
            if event.get("type") == "result" and event.get("is_error"):
                error_seen = True
            if event.get("type") == "error" or event.get("is_error"):
                error_seen = True
            line = _summarize_event(event)
            if line:
                summary_parts.append(line)

    if cancelled:
        _terminate_proc(proc)
        summary_parts.append("[cancelled] 취소 신호로 실행 중단")
        summary = _redact("\n".join(summary_parts)[-_SUMMARY_MAX_CHARS:], secret_values)
        return AgentResult(
            status=STATUS_CANCELLED,
            session_id=session_id,
            mr_url=mr_url,
            log_summary=summary,
            returncode=getattr(proc, "returncode", None),
        )

    rc = proc.wait()

    if limit_hit:
        status = STATUS_INTERRUPTED
    elif rc == 0 and not error_seen:
        status = STATUS_DONE
    else:
        status = STATUS_FAILED

    summary = _redact("\n".join(summary_parts)[-_SUMMARY_MAX_CHARS:], secret_values)

    return AgentResult(
        status=status,
        session_id=session_id,
        mr_url=mr_url,
        reset_at=reset_at,
        log_summary=summary,
        returncode=rc,
    )


def _gitlab_token(creds: UserCreds, config: Any) -> Optional[str]:
    """creds의 GitLab 토큰 참조를 secrets.base_dir 기준으로 읽어 값 반환(build_env와 동일 소스)."""
    ref = getattr(creds, "gitlab_token_ref", "") or ""
    if not ref:
        return None
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    return read_secret(base_dir, ref)


def _provision_repos(
    creds: UserCreds,
    config: Any,
    ensure_repos_fn: Optional[Callable],
) -> Optional[AgentResult]:
    """claude 실행 **전** 오케스트레이터 3개 레포를 프로비저닝(clone/pull).

    사용자 GitLab 토큰으로 :func:`app.repos.ensure_repos` 를 호출한다. 개별 레포
    실패는 로그만 남기고 잡은 진행하되(오케스트레이터가 자체 처리할 수도),
    **orchestrator_repo 자체가 프로비저닝 실패**하면 오케스트레이터 cwd가 없어
    실행 불가이므로 명확한 실패 AgentResult를 반환한다(그 외엔 None → 진행).
    """
    fn = ensure_repos_fn or ensure_repos
    token = _gitlab_token(creds, config)
    try:
        results = fn(config, token) or {}
    except Exception as exc:  # noqa: BLE001 — 프로비저닝 예외는 잡을 막지 않는다
        log.warning("레포 프로비저닝 중 예외(무시하고 진행): %s", type(exc).__name__)
        return None
    orch = str(results.get("orchestrator", ""))
    if orch.startswith("err"):
        log.error("orchestrator_repo 프로비저닝 실패 — 잡 중단: %s", orch)
        return AgentResult(
            status=STATUS_FAILED,
            log_summary="[repos-error] orchestrator_repo 프로비저닝 실패: " + orch,
        )
    return None


def run_job(
    job: Any,
    creds: UserCreds,
    config: Any,
    *,
    popen_factory: Optional[Callable] = None,
    base_env: Optional[dict] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    ensure_repos_fn: Optional[Callable] = None,
) -> AgentResult:
    """잡 1건을 사용자 정체성으로 신규 실행하고 AgentResult를 반환.

    실행 전 오케스트레이터 3개 레포를 프로비저닝한다(:func:`_provision_repos`).
    ``cancel_check``가 주어지면 실행 중 취소 신호를 폴링해 abort할 수 있다(§10.4).
    """
    fatal = _provision_repos(creds, config, ensure_repos_fn)
    if fatal is not None:
        return fatal
    cmd = build_command(job, config, resume=False)
    env, secret_values = build_env(job, creds, config, base_env=base_env)
    cwd = getattr(getattr(config, "run", None), "orchestrator_repo", "") or ""
    factory = popen_factory or _default_popen
    try:
        proc = factory(cmd, cwd=cwd, env=env)
    except (OSError, FileNotFoundError) as exc:
        return AgentResult(
            status=STATUS_FAILED,
            log_summary=_redact(f"[spawn-error] {exc}", secret_values),
            returncode=None,
        )
    return _consume(proc, secret_values, cancel_check=cancel_check)


def resume_job(
    job: Any,
    session_id: str,
    creds: UserCreds,
    config: Any,
    *,
    from_pr: Optional[Any] = None,
    popen_factory: Optional[Callable] = None,
    base_env: Optional[dict] = None,
    cancel_check: Optional[Callable[[], bool]] = None,
    ensure_repos_fn: Optional[Callable] = None,
) -> AgentResult:
    """중단된 잡을 ``--resume``(+옵션 ``--from-pr``)로 재개 실행.

    재개도 신규와 동일하게 실행 전 오케스트레이터 레포를 프로비저닝한다
    (dlc-meta는 per-user 학습이 갱신되므로 매 실행 pull이 중요).
    """
    fatal = _provision_repos(creds, config, ensure_repos_fn)
    if fatal is not None:
        fatal.session_id = session_id
        return fatal
    cmd = build_command(job, config, resume=True, session_id=session_id, from_pr=from_pr)
    env, secret_values = build_env(job, creds, config, base_env=base_env)
    cwd = getattr(getattr(config, "run", None), "orchestrator_repo", "") or ""
    factory = popen_factory or _default_popen
    try:
        proc = factory(cmd, cwd=cwd, env=env)
    except (OSError, FileNotFoundError) as exc:
        return AgentResult(
            status=STATUS_FAILED,
            session_id=session_id,
            log_summary=_redact(f"[spawn-error] {exc}", secret_values),
            returncode=None,
        )
    result = _consume(proc, secret_values, cancel_check=cancel_check)
    if not result.session_id:
        result.session_id = session_id
    return result


# --- 하위호환 클래스 래퍼(문서/기존 인터페이스) ------------------------------


class AgentRunner:
    """함수형 API의 얇은 래퍼(CLAUDE.md가 참조하는 인터페이스)."""

    def __init__(self, config) -> None:
        self.config = config

    def build_prompt(self, job) -> str:
        return build_prompt(job, self.config)

    def build_command(self, job, resume: bool = False, session_id: Optional[str] = None) -> list:
        return build_command(job, self.config, resume=resume, session_id=session_id)

    def build_env(self, job, creds: UserCreds, base_env: Optional[dict] = None):
        return build_env(job, creds, self.config, base_env=base_env)

    def run(self, job, creds: UserCreds, **kw) -> AgentResult:
        return run_job(job, creds, self.config, **kw)

    def resume(self, job, session_id: str, creds: UserCreds, **kw) -> AgentResult:
        return resume_job(job, session_id, creds, self.config, **kw)
