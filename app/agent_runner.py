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

실행 계약(**지속 세션** — Phase 1, HAN-537 지혈):
    cwd = config.run.orchestrator_repo                # 오케스트레이터 정체성
    claude -p --input-format stream-json              # stdin으로 user 메시지 주입(양방향)
        --output-format stream-json                   # 라인 단위 이벤트
        --verbose                                     # stream-json+print 강제 요건
        --dangerously-skip-permissions                # 승인자 없음(신뢰 네트워크 한정)
        --session-id <uuid5(NAMESPACE, ticket)>       # 티켓 결정적 UUID = 재개 키
    재개: 위와 동일 + --resume <session-id> [--from-pr <PR#>]

    ⚠️ **왜 지속 세션인가(HAN-537 근본 해결)**: 과거 ``claude -p "<프롬프트>"`` 는
    **일회성(one-shot)** 이었다 — 프롬프트를 인자로 주고 스트림이 끝나면 프로세스가
    종료했다. 오케스트레이터가 코드 편집을 **백그라운드 서브에이전트**에 위임하고
    "완료 알림을 기다린다"며 턴을 종료하면, one-shot 프로세스에는 **다음 턴도 알림
    배달 경로도 없어** 커밋 없이 종료 → FAILED, 편집물 미커밋(§DESIGN Phase 1).
    이제는 ``--input-format stream-json`` 으로 **살아있는 세션**을 띄우고 티켓 프롬프트를
    stdin에 한 줄(user 메시지 JSON)로 주입한다. 세션이 살아있으므로 백그라운드 서브
    완료 알림이 **다음 턴으로 배달** → 오케스트레이터 재개 → 커밋까지 한 세션에서 완결.
    **완료 판정**: 각 턴은 ``result`` 이벤트로 끝나지만, 백그라운드 위임이 살아있는
    동안(``background_tasks_changed`` 의 tasks 비어있지 않음)에는 그 result가 종결이
    아니다. **pending 백그라운드 태스크가 0인 상태의 result** 가 진짜 완료 → 그때 stdin을
    닫아(EOF) 프로세스를 정상 종료시킨다. 프레임워크(ai-dlc-orchestrator) 룰북은 불변.

⚠️ 시크릿 규율:
    토큰 "값"은 로그·에러·AgentResult에 절대 노출하지 않는다. 값은
    ``config.read_secret``(base_dir 상대)로만 읽고, 실행 요약은 주입된 시크릿
    값을 방어적으로 마스킹(:func:`_redact`)한 뒤 반환한다.
    **상속된** GitHub 계열 토큰(central 것일 수 있다)은 항상 제거한다. forge 가
    GitHub 인 배포에서는 그 자리에 **그 사용자 자신의** forge 토큰만 다시 넣는다
    (:func:`build_env`) — 남의 토큰이 워커로 새지 않으면서 push/PR 이 가능하다.

Popen 관용(claude-hacker/worker 계승):
    text=True, encoding='utf-8', errors='replace', bufsize=1
    ANSI 이스케이프 제거 정규식(:data:`ANSI_ESCAPE`).
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import forge
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
STATUS_HANDOFF = "handed_off"       # 담당자 변경 핸드오프 — worker가 checkpoint(커밋·push) 후 회신(롤백X)

# 취소 시 subprocess terminate → kill 대기 상한(초).
TERMINATE_GRACE_SEC = 5

# 지속 세션 백스톱(config에 값이 없을 때의 폴백). 정상 종료는 완료 판정(pending bg=0의
# result)으로 이뤄지고, 아래는 세션이 result를 영영 못 내는 이상 상황(행/누수)만 막는
# 안전망이다. 0 이하면 해당 백스톱 비활성.
DEFAULT_SESSION_MAX_SEC = 7200    # 세션 최대 수명(벽시계) — 초과 시 stdin 닫고 강제 종료.
DEFAULT_SESSION_IDLE_SEC = 1800   # 이벤트 무발생 유휴 상한 — 초과 시 stdin 닫고 강제 종료.

# background_tasks_changed 이벤트에서 pending 백그라운드 태스크 집합을 담는 키.
_BG_TASKS_SUBTYPE = "background_tasks_changed"

# 프로세스 그룹 kill용 시그널 상수(Windows엔 SIGKILL이 없어 getattr 폴백 — 참조만으로
# AttributeError 나지 않게. 실 사용은 POSIX(_killpg)에서만; Windows는 개별 kill 폴백).
_SIGTERM = getattr(signal, "SIGTERM", 15)
_SIGKILL = getattr(signal, "SIGKILL", 9)

# stream-json 이벤트에서 session_id가 담길 수 있는 후보 키(버전차 방어).
_SESSION_ID_KEYS = ("session_id", "sessionId", "sessionID", "session")

# 이벤트 안에서 중첩 탐색할 컨테이너 키(방어적).
_NESTED_KEYS = ("data", "system", "message", "result", "init", "meta", "usage", "error")

# 한도(usage/quota limit) 감지 패턴(영/한, 버전차 방어).
#
# ⚠️ 여기 매칭은 **오직 "구독/사용량 한도"** 에만 특정한다. 과거 광의 매칭
# (단독 ``rate limit``·``limit reached``·``limit exceeded``·``too many requests``)은
# 일반 코드·콘텐츠·thinking·툴출력(HTTP 429, 앱 rate limit 로그 등)에 흔히 등장해
# **오탐(false-positive) → 잡 interrupted** 를 유발했다(실측: 실제 quota 3% 미만인데도
# phantom limit 재개 루프). 따라서 "usage/quota/사용량 한도"에 특정되게 좁힌다.
_LIMIT_PATTERN = re.compile(
    r"(claude\s+usage\s+limit|usage\s+limit|"
    r"out\s+of\s+(?:usage|quota)|quota\s+exceeded|"
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

# 변경요청(MR/PR) URL 추출은 forge 어댑터가 안다 — GitLab ``/-/merge_requests/N`` /
# GitHub ``/pull/N``. 네이티브 패턴 우선 + 통합 폴백(:func:`app.forge.search_change_url`).

# worker가 **상속해서는 안 되는** GitHub 계열 토큰 env(central 것일 수 있다 — 항상 제거).
# forge 가 GitHub 인 배포에서는 제거 후 **그 사용자 자신의** forge 토큰을 다시 넣는다
# (:func:`build_env`) — 그래야 push·PR 생성이 되면서도 남의 토큰이 새지 않는다.
_GITHUB_ENV_KEYS = (
    "GITHUB_TOKEN", "GH_TOKEN", "GITHUB_PAT",
    "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN",
)

#: forge 가 GitHub 일 때 **사용자 자신의** 토큰을 실어 줄 env(공식 CLI 관용 이름).
_GITHUB_USER_TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN")

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
    # 개인 forge 토큰 참조(브랜치 push·MR/PR 생성 귀속의 근거). ``gitlab_token_ref`` 는
    # 같은 필드의 **레거시 이름**이며 :meth:`__post_init__` 이 두 값을 항상 같게 만든다
    # (옛 이름으로 생성/조회하는 코드·테스트가 그대로 동작한다).
    forge_token_ref: str = ""
    gitlab_token_ref: str = ""
    claude_oauth_token_ref: str = ""
    claude_oauth_token_value: str = ""
    # (선택) **알림 채널** 사용자 id — 완료 알림 @멘션용. 없으면 display_name 폴백.
    # 값의 모양은 provider 마다 다르다(Google Chat=숫자 / Slack=U…). 시크릿 아님.
    # ``google_chat_user_id`` 는 레거시 이름(같은 값으로 미러).
    notify_user_id: str = ""
    google_chat_user_id: str = ""

    def __post_init__(self) -> None:
        """신규/레거시 이름 쌍을 같은 값으로 수렴(직접 생성 경로 포함).

        ``UserCreds(gitlab_token_ref=...)`` 처럼 옛 이름만 준 호출도 신규 이름으로 읽히고
        그 반대도 성립한다 — 이름 이행 기간에 두 값이 갈라지는 사고를 원천 차단한다.
        """
        ref = self.forge_token_ref or self.gitlab_token_ref
        self.forge_token_ref = ref
        self.gitlab_token_ref = ref
        uid = self.notify_user_id or self.google_chat_user_id
        self.notify_user_id = uid
        self.google_chat_user_id = uid

    @staticmethod
    def from_env(user: str, env: Optional[dict] = None) -> "UserCreds":
        """스포너가 주입한 worker env에서 사용자 자격을 조립.

        env 계약(스포너/Phase 6가 채움):
            DISPATCH_GIT_NAME / DISPATCH_GIT_EMAIL   git author 정체성
            DISPATCH_JIRA_EMAIL                      Jira Basic actor 이메일
            JIRA_TOKEN_REF / FORGE_TOKEN_REF         secrets.base_dir 상대 참조
            CLAUDE_OAUTH_TOKEN_REF                    (선택) 참조
            CLAUDE_CODE_OAUTH_TOKEN                   (폴백) 값 직접
            DISPATCH_NOTIFY_USER_ID                   (선택) 완료 알림 @멘션 ID

        ⚠️ forge/알림 중립 이름이 정본이지만 **옛 이름도 계속 읽는다**
        (``GITLAB_TOKEN_REF``·``DISPATCH_GOOGLE_CHAT_USER_ID``) — 옛 스포너/이미지가
        띄운 컨테이너나 손으로 띄운 컨테이너가 그대로 동작해야 하기 때문이다.
        """
        e = env if env is not None else os.environ
        return UserCreds(
            user=user,
            git_name=e.get("DISPATCH_GIT_NAME", "") or e.get("GIT_AUTHOR_NAME", ""),
            git_email=e.get("DISPATCH_GIT_EMAIL", "") or e.get("GIT_AUTHOR_EMAIL", ""),
            jira_email=e.get("DISPATCH_JIRA_EMAIL", "") or e.get("JIRA_EMAIL", ""),
            jira_token_ref=e.get("JIRA_TOKEN_REF", ""),
            forge_token_ref=e.get("FORGE_TOKEN_REF", "") or e.get("GITLAB_TOKEN_REF", ""),
            claude_oauth_token_ref=e.get("CLAUDE_OAUTH_TOKEN_REF", ""),
            claude_oauth_token_value=e.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
            notify_user_id=(e.get("DISPATCH_NOTIFY_USER_ID", "")
                            or e.get("DISPATCH_GOOGLE_CHAT_USER_ID", "")),
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
            # forge 토큰 참조: 정본 forge_token, 없으면 레거시 gitlab_token(옛 레지스트리).
            forge_token_ref=(
                (getattr(secrets_ref, "forge_token", "")
                 or getattr(secrets_ref, "gitlab_token", "")) if secrets_ref else ""
            ),
            claude_oauth_token_ref=(
                getattr(secrets_ref, "claude_oauth_token", "") if secrets_ref else ""
            ),
            notify_user_id=(getattr(record, "notify_user_id", "")
                            or getattr(record, "google_chat_user_id", "")),
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
    # 최종 result 이벤트의 마무리 멘트(마스킹됨) — 완료 알림 본문에 쓰인다.
    final_text: str = ""
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


def extract_mr_url(event_or_text: Any, forge_kind: Any = None) -> Optional[str]:
    """이벤트/텍스트에서 변경요청(MR/PR) URL을 추출(구조화 필드 우선).

    ``forge_kind`` 를 주면 그 forge 의 네이티브 URL 모양을 먼저 찾고(GitLab
    ``/-/merge_requests/N`` / GitHub ``/pull/N``), 못 찾으면 통합 패턴으로 한 번 더
    훑는다(:func:`app.forge.search_change_url`). 주지 않으면 종전대로 통합 패턴만
    쓴다 — 옛 시그니처로 부르는 호출자는 동작이 그대로다.
    """
    if isinstance(event_or_text, dict):
        for scope in _iter_candidate_dicts(event_or_text):
            v = scope.get("mr_url")
            if isinstance(v, str) and v.strip():
                return v.strip()
        text = json.dumps(event_or_text, ensure_ascii=False)
    else:
        text = str(event_or_text or "")
    return forge.search_change_url(text, forge_kind)


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
    continue_from_wip = bool(_job_field(job, "continue_from_wip", False))

    # forge 용어(GitLab=MR / GitHub=PR)와 호스팅 이름을 설정에서 가져온다 — 프롬프트에
    # "GitLab"·"MR" 을 박아 두면 GitHub 배포의 에이전트가 존재하지 않는 것을 찾는다.
    forge_kind = forge.resolve_kind(config)
    cr = forge.change_abbr(forge_kind)          # "MR" | "PR"
    forge_label = forge.label(forge_kind)       # "GitLab" | "GitHub"

    # 자율 실행 공통 불변식: '완료(Done)' 전이는 절대 금지. 완료 전이는 로컬
    # 오케스트레이터가 리뷰·머지 시점에 사람 루프로 수행한다(자율 실행은 거기까지
    # 가지 않는다). 종료 시 티켓은 '진행 중'(리뷰 대기)으로 유지한다. 다만 **무엇을
    # 코멘트로 남기고 산출을 어떻게 넘기는지는 모드마다 다르다** — A는 MR/PR 링크,
    # B는 원격 push한 브랜치 + runs 저널 위치(변경요청 없음). 따라서 mode_desc·closing을
    # 모드별로 분기하고, 하드코딩된 "MR 링크" 문구가 B에 새지 않도록 한다.
    if mode == "A":
        mode_desc = (
            f"A(완전자율) — 코드까지 완성하고 {cr} 초안까지 생성한다. "
            "착수 시 '진행 중'으로 전이하되, 작업이 끝나도 '완료'로 전이하지 말라. "
            f"{cr}을 만든 뒤 그 {cr} 링크를 티켓 코멘트로 남기고, 티켓은 '진행 중'으로 "
            "유지한 채 '리뷰 대기' 상태로 종료하라."
        )
        closing = (
            "중요: 자율 실행은 티켓을 '완료(Done)'로 전이하지 말라(절대 금지). "
            f"완료 전이는 로컬 오케스트레이터가 {cr} 리뷰·머지 시점에 수행하며, "
            f"자율 실행은 거기까지 가지 않는다. 작업 종료 시 {cr} 링크를 코멘트로 남기고 "
            "티켓을 '진행 중'으로 유지한 채 '리뷰 대기'로 종료하라."
        )
    else:
        mode_desc = (
            "B(경량 1차) — 트리아지 + 브랜치 + 스캐폴딩 + 최소 1차 시도 + "
            "runs 저널까지 수행한다. 실질적 완성은 로컬 사람 루프에서 이뤄지므로 "
            f"{cr}은 만들지 말고 1차 산출과 저널을 남긴다. 단, 1차 산출이 워커 컨테이너 "
            f"안에만 갇히지 않도록 {branch} 브랜치를 원격({forge_label})에 **반드시 push**해 "
            "로컬 사용자가 fetch할 수 있게 하라. 착수 시 '진행 중'으로 전이하되, 작업이 "
            "끝나도 '완료'로 전이하지 말고 티켓을 '진행 중'으로 유지한 채 '리뷰 대기' "
            "상태로 종료하라."
        )
        closing = (
            "중요: 자율 실행은 티켓을 '완료(Done)'로 전이하지 말라(절대 금지). "
            "완료 전이는 로컬 오케스트레이터가 리뷰·머지 시점에 수행하며, 자율 실행은 "
            f"거기까지 가지 않는다. {cr}은 만들지 말라. 작업 종료 시 원격에 push한 "
            f"브랜치명({branch})과 runs/{ticket}/ 저널 위치를 티켓 코멘트로 남기고"
            "('로컬에서 fetch해 이어서 완성' 안내 포함), 티켓을 '진행 중'으로 유지한 채 "
            "'리뷰 대기'로 종료하라."
        )

    lines = [
        f"{ticket} 티켓을 오케스트레이터로서 수행하라.",
        "이 트리거 티켓이 곧 작업 티켓이다 — 새 사이클 티켓을 만들지 말고 "
        "이 티켓에 바인딩하라(루프 방지).",
        f"autonomy_mode={mode}: {mode_desc}",
        closing,
        f"브랜치는 {branch} 를 사용하라.",
        f"작업 산출은 runs/{ticket}/ 저널에 기록하라(재개 컨텍스트).",
    ]
    if target_repos:
        lines.append("대상 레포: " + ", ".join(str(r) for r in target_repos) + ".")

    # 담당자 변경으로 이관받은 continue 잡: 브랜치에 이전 담당자의 선행 WIP가 있다.
    if continue_from_wip:
        lines.append(
            f"이 브랜치({branch})에는 이전 담당자가 남긴 선행 작업(WIP)이 이미 있다 — "
            "먼저 리뷰한 뒤, 너의 정체성과 규칙에 따라 이어서 완성하라(판단에 따라 "
            "유지하거나 다시 하라). 새 브랜치를 만들지 말고 이 브랜치에서 이어가라."
        )

    # (라벨, 키 후보). 설계 문서 레포는 신규 키 ``docs`` 를 먼저 보되, 옛 central이 큐에
    # 넣어 둔 잡의 레거시 키(``dataspace_docs``)도 그대로 읽는다(무중단 배포 하위호환).
    ctx_bits = []
    for label, keys in (("dlc-meta", ("dlc_meta",)),
                        ("docs", ("docs", "dataspace_docs")),
                        ("runs 저널", ("runs",))):
        val = next((ctx.get(k) for k in keys if ctx.get(k)), None)
        if val:
            ctx_bits.append(f"{label}={val}")
    if ctx_bits:
        lines.append("컨텍스트 참조: " + ", ".join(ctx_bits) + ".")

    return "\n".join(lines)


def persistent_enabled(config: Any) -> bool:
    """지속 세션(양방향 stream-json) 사용 여부.

    조건: run.persistent_session 이 truthy(기본 True) **이고** 입·출력 포맷이 모두
    stream-json. 어느 하나라도 아니면 레거시 one-shot(``claude -p "<prompt>"``)으로
    폴백한다(안전 롤백 경로). CLI 계약상 ``--input-format``/``--output-format`` 은
    ``--print``(-p)와만 동작하므로 두 스트림 포맷을 모두 요구한다.
    """
    run = getattr(config, "run", None)
    if run is None:
        return True
    if not bool(getattr(run, "persistent_session", True)):
        return False
    output_format = getattr(run, "output_format", "stream-json") or "stream-json"
    input_format = getattr(run, "input_format", "stream-json") or "stream-json"
    return output_format == "stream-json" and input_format == "stream-json"


def _session_max_sec(config: Any) -> int:
    """지속 세션 최대 수명(초) — config.run.session_max_sec, 없으면 폴백."""
    run = getattr(config, "run", None)
    try:
        return int(getattr(run, "session_max_sec", DEFAULT_SESSION_MAX_SEC))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_MAX_SEC


def _session_idle_sec(config: Any) -> int:
    """지속 세션 유휴 상한(초) — config.run.session_idle_sec, 없으면 폴백."""
    run = getattr(config, "run", None)
    try:
        return int(getattr(run, "session_idle_sec", DEFAULT_SESSION_IDLE_SEC))
    except (TypeError, ValueError):
        return DEFAULT_SESSION_IDLE_SEC


def build_command(
    job: Any,
    config: Any,
    *,
    resume: bool = False,
    session_id: Optional[str] = None,
    from_pr: Optional[Any] = None,
) -> list:
    """claude 실행 인자 리스트 구성(신규/재개 분기, 순수).

    **지속 세션(기본)**: ``claude -p --input-format stream-json --output-format
    stream-json --verbose --dangerously-skip-permissions`` — 프롬프트는 인자로 넣지
    않고 stdin에 user 메시지(JSON 라인)로 주입한다(:func:`run_job`/`_consume`).
    **레거시 one-shot 폴백**(persistent 비활성): ``claude -p "<프롬프트>" ...``.

    신규: --session-id <uuid5(ticket)>. 재개: --resume <session-id> [--from-pr N].
    """
    run = getattr(config, "run", None)
    claude_bin = getattr(run, "claude_bin", "claude") if run else "claude"
    output_format = getattr(run, "output_format", "stream-json") if run else "stream-json"

    ticket = str(_job_field(job, "ticket", "") or "")
    persistent = persistent_enabled(config)

    if persistent:
        # 지속 세션: 프롬프트는 stdin으로 주입 → 인자에 넣지 않는다. --input-format
        # stream-json 이 양방향 입력을 연다(살아있는 세션, 여러 턴).
        cmd = [
            claude_bin,
            "-p",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
    else:
        # 레거시 one-shot(롤백/비-stream-json 경로): 프롬프트를 인자로.
        prompt = build_prompt(job, config)
        cmd = [claude_bin, "-p", prompt, "--output-format", output_format]
        # stream-json은 --print(-p)와 함께 쓸 때 CLI가 --verbose를 강제한다
        # (없으면 "requires --verbose" 에러로 즉시 실패 → session_id 유실).
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


def _encode_user_message(text: str) -> str:
    """지속 세션 stdin에 밀어넣을 stream-json user 메시지 한 줄(JSON + 개행).

    형식(§DESIGN enabler 1): ``{"type":"user","message":{"role":"user",
    "content":[{"type":"text","text":"..."}]}}``. 살아있는 세션의 stdin에 append하면
    새 턴(사용자 후속 지시)으로 처리된다.
    """
    payload = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }
    return json.dumps(payload, ensure_ascii=False) + "\n"


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
        FORGE_TOKEN[, FORGE_TOKEN_FILE]                  (MR/PR 생성자)
        GITLAB_TOKEN[, GITLAB_TOKEN_FILE]                (위와 같은 값 — 레거시 이름)
        CLAUDE_CODE_OAUTH_TOKEN                          (사용자 Claude)
    제거 후 조건부 재주입:
        GITHUB_TOKEN/GH_TOKEN/... 상속분은 **항상 삭제**한다(central 것일 수 있다).
        그 뒤 ``config.forge.kind == "github"`` 이면 **그 사용자 자신의** forge 토큰을
        GITHUB_TOKEN/GH_TOKEN 으로 다시 넣는다 — GitHub 배포에서 ``gh`` CLI·PR 생성이
        동작하려면 그 이름이 필요하고, 값은 어디까지나 이 사용자의 것이다.

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

    # forge MR/PR 생성자 토큰(브랜치 push 귀속의 근거).
    # 정본 이름은 FORGE_TOKEN[_FILE] 이고, 옛 이름 GITLAB_TOKEN[_FILE] 도 **같은 값**으로
    # 함께 넣는다(컨테이너 안에서 옛 이름을 읽는 지시문·스크립트 하위호환).
    forge_ref = creds.forge_token_ref or creds.gitlab_token_ref
    if forge_ref:
        val = read_secret(base_dir, forge_ref)
        if val:
            env["FORGE_TOKEN"] = val
            env["GITLAB_TOKEN"] = val
            secret_values.append(val)
            # forge 가 GitHub 이면 공식 CLI 관용 이름으로도 **사용자 자신의** 토큰을
            # 넣는다(위에서 상속분을 이미 지웠으므로 남의 토큰이 남을 여지는 없다).
            if forge.resolve_kind(config) == forge.KIND_GITHUB:
                for key in _GITHUB_USER_TOKEN_KEYS:
                    env[key] = val
        path = _secret_abs_path(config, forge_ref)
        if path:
            env["FORGE_TOKEN_FILE"] = path
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
    """기본 Popen 팩토리(claude-hacker 관용 계승).

    ⚠️ 좀비/고아 방지(방어심층 #2): ``start_new_session=True`` 로 claude 를 **새 프로세스
    그룹의 리더**로 띄운다(POSIX ``setsid``). 이렇게 하면 claude 가 spawn하는
    git/esbuild/gradle 손자들이 같은 프로세스 그룹에 묶이므로, 잡 종료(정상·취소·핸드
    오프) 시 :func:`_terminate_proc` 가 **그룹 전체**를 죽여 고아가 PID 1(python)로
    reparent되는 것을 원천 차단한다(tini 가 1차 안전망, 이건 소스 차단).

    Windows(테스트 호스트)에는 ``os.setsid`` 가 없다 → ``start_new_session`` 을 넘기지
    않는다(실 런타임은 Linux). POSIX 전용 인자를 hasattr 로 가드해 테스트 스위트가
    Windows에서 깨지지 않게 한다.
    """
    kwargs: dict = {}
    if hasattr(os, "setsid"):
        # POSIX 전용: 새 세션/프로세스 그룹 리더로 만든다(그룹 kill의 근거).
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        cmd,
        cwd=cwd or None,
        env=env,
        # 지속 세션: stdin으로 티켓 프롬프트(및 이후 후속 메시지)를 stream-json 라인으로
        # 밀어넣고, 완료 시 닫아(EOF) 프로세스를 정상 종료시킨다. 레거시 one-shot 경로는
        # 초기 프롬프트를 인자로 받으므로 _consume이 stdin을 즉시 닫는다(claude가 stdin을
        # 기다리지 않게).
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        **kwargs,
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


def _process_group_id(proc) -> Optional[int]:
    """proc 의 프로세스 그룹 ID(POSIX). 조회 불가/비POSIX/대역이면 None.

    ``start_new_session=True`` 로 띄웠으면 리더의 pgid == 리더 pid 이고 손자들이
    같은 그룹에 묶인다. Windows(``os.getpgid`` 없음)·pid 없는 대역·이미 사라진
    프로세스는 None(그룹 kill 불가 → 개별 terminate로 폴백).
    """
    pid = getattr(proc, "pid", None)
    if pid is None:
        return None
    if not (hasattr(os, "getpgid") and hasattr(os, "killpg")):
        return None
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return None


def _killpg(pgid: Optional[int], sig: int) -> bool:
    """프로세스 그룹에 시그널 전송(best-effort). 전송 시도했으면 True.

    POSIX 전용. pgid None(비POSIX/조회불가)이면 no-op(False). 이미 사라진 그룹은
    조용히 흡수한다.
    """
    if pgid is None or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _terminate_proc(proc) -> None:
    """실행 중 subprocess를 종료(terminate → 유예 후 kill). best-effort.

    ⚠️ **그룹 인지**: ``start_new_session`` 으로 띄운 리더의 **프로세스 그룹 전체**를
    죽여 claude 의 손자(git/esbuild/gradle)가 고아로 남아 PID 1(python)에 reparent
    → 좀비화되는 것을 차단한다. POSIX면 SIGTERM(그룹) → 유예 → SIGKILL(그룹), 그룹
    kill이 불가한 환경(Windows 테스트 호스트·pid 없는 대역)에서는 종전대로 개별
    ``proc.terminate()``/``proc.kill()`` 로 폴백한다.
    """
    # 리더 reap 전에 pgid 캡처(reap 후엔 getpgid가 실패할 수 있음).
    pgid = _process_group_id(proc)

    # 1) SIGTERM — 그룹 우선, 불가하면 개별.
    if not _killpg(pgid, _SIGTERM):
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001 — 이미 죽었거나 대역 객체일 수 있음
            pass

    # 2) 유예 대기.
    grace_expired = False
    try:
        proc.wait(timeout=TERMINATE_GRACE_SEC)
    except TypeError:
        # wait()가 timeout 인자를 받지 않는 대역/구현.
        try:
            proc.wait()
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001 — 유예 초과 등
        grace_expired = True

    # 3) 유예 초과 시 SIGKILL — 그룹 우선, 불가하면 개별.
    if grace_expired:
        if not _killpg(pgid, _SIGKILL):
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        # 리더가 남긴 손자까지 확실히 쓸어담고 최종 reap(좀비 방지).
        _killpg(pgid, _SIGKILL)
        try:
            proc.wait(timeout=TERMINATE_GRACE_SEC)
        except Exception:  # noqa: BLE001 — 대역/이미 종료
            pass


def _normalize_control(value: Any) -> str:
    """제어 체크 반환값을 액션 문자열로 정규화(하위호환: bool 또는 문자열).

    - "handoff"/STATUS_HANDOFF → "handoff"(담당자 변경 checkpoint)
    - True 또는 "cancel"        → "cancel"(취소 abort+롤백)
    - 그 외(False/None/"none")  → "none"
    """
    if value == "handoff" or value == STATUS_HANDOFF:
        return "handoff"
    if value is True or value == "cancel":
        return "cancel"
    return "none"


def _write_stdin(proc, data: str) -> bool:
    """proc.stdin에 한 줄 쓰고 flush(best-effort). stdin 없으면 no-op(False)."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return False
    try:
        stdin.write(data)
        stdin.flush()
        return True
    except (OSError, ValueError):  # 이미 닫힘/파이프 깨짐 — 흡수
        return False


def _close_stdin(proc) -> None:
    """proc.stdin을 닫아 claude에 EOF를 전달(더 이상 user 입력 없음 → 정상 종료)."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.close()
    except (OSError, ValueError):
        pass


def _bg_pending_from_event(event: dict) -> Optional[set]:
    """``background_tasks_changed`` 이벤트에서 현재 pending 백그라운드 태스크 id 집합.

    이 이벤트의 ``tasks`` 배열은 **현 시점 활성 백그라운드 태스크의 전체 스냅샷**이다
    (전량 치환). 빈 배열 = pending 없음. 해당 이벤트가 아니면 None(변화 없음).
    """
    if event.get("type") != "system" or event.get("subtype") != _BG_TASKS_SUBTYPE:
        return None
    tasks = event.get("tasks")
    if not isinstance(tasks, list):
        return set()
    ids = set()
    for t in tasks:
        if isinstance(t, dict):
            tid = t.get("task_id") or t.get("id")
            ids.add(str(tid) if tid is not None else json.dumps(t, sort_keys=True))
    return ids


def _start_session_watchdog(proc, state: dict, *, max_sec: int, idle_sec: int):
    """지속 세션 백스톱 워치독(데몬 스레드). result를 영영 못 내는 행/누수만 차단.

    실 Popen(``stdin`` 보유)에서만 동작한다. 최대 수명 또는 유휴 상한 초과 시
    ``state['timed_out']=True`` 로 표시하고 stdin을 닫아(정상 EOF 시도) 프로세스를
    종료시킨다. 유예 후에도 살아있으면 :func:`_terminate_proc` 로 그룹 강제 종료.
    Fake/유한 proc(‌stdin 없음)이나 백스톱 비활성(≤0)이면 (None, None) 반환.
    """
    if getattr(proc, "stdin", None) is None:
        return None, None
    if max_sec <= 0 and idle_sec <= 0:
        return None, None
    stop = threading.Event()
    start = time.monotonic()

    def _run() -> None:
        while not stop.wait(1.0):
            now = time.monotonic()
            last = state.get("last", start)
            over_max = max_sec > 0 and (now - start) > max_sec
            over_idle = idle_sec > 0 and (now - last) > idle_sec
            if over_max or over_idle:
                state["timed_out"] = "max" if over_max else "idle"
                _close_stdin(proc)  # 정상 EOF 우선
                # 유예 대기 후에도 살아있으면 그룹 강제 종료.
                if not stop.wait(TERMINATE_GRACE_SEC):
                    try:
                        alive = proc.poll() is None
                    except Exception:  # noqa: BLE001
                        alive = True
                    if alive:
                        _terminate_proc(proc)
                return

    th = threading.Thread(target=_run, name="jad-session-watchdog", daemon=True)
    th.start()
    return th, stop


def _consume(
    proc,
    secret_values: list,
    *,
    cancel_check: Optional[Callable[[], Any]] = None,
    initial_prompt: Optional[str] = None,
    session_max_sec: int = DEFAULT_SESSION_MAX_SEC,
    session_idle_sec: int = DEFAULT_SESSION_IDLE_SEC,
    forge_kind: Any = None,
) -> AgentResult:
    """프로세스 stdout(라인 이터러블)을 소비해 AgentResult로 환원.

    ``forge_kind``: 변경요청(MR/PR) URL 추출 시 그 forge 의 네이티브 모양을 우선 찾게
    하는 힌트(:func:`extract_mr_url`). 없으면 통합 패턴으로 종전과 동일하게 동작한다.

    proc는 ``.stdout``(라인 이터러블) + ``.wait()``/``.returncode`` 를 갖는 객체.

    **지속 세션(``initial_prompt`` 제공)**: proc.stdin에 티켓 프롬프트를 stream-json
    user 메시지로 주입하고 stdin을 **연 채로** 이벤트를 소비한다. 각 턴은 ``result``
    이벤트로 끝나지만, **백그라운드 위임이 살아있는 동안(pending bg > 0)에는 그 result가
    종결이 아니다** — 세션을 살려 다음 턴(백그라운드 완료 → 재개 → 커밋)을 받는다.
    **pending bg=0 상태의 result**(또는 한도/에러 result)가 진짜 종결 → stdin을 닫아
    (EOF) 프로세스를 정상 종료시키고 rc로 상태를 판정한다. 이것이 HAN-537의 근본
    해결(one-shot에는 '다음 턴'이 없었다)이다.
    ``initial_prompt``가 없으면(레거시 one-shot) stdin을 즉시 닫고 종전과 동일하게
    스트림을 끝까지 소비한다.

    ``cancel_check``가 주어지면 **이벤트 라인 단위**로 제어 신호를 폴링한다. 반환은
    하위호환적으로 bool(취소) 또는 액션 문자열("cancel"|"handoff"|"none")을 모두 받는다.
        - "cancel"(=True) → subprocess 종료 후 ``STATUS_CANCELLED``(롤백/회신은 worker).
        - "handoff" → subprocess 종료 후 ``STATUS_HANDOFF``(checkpoint 커밋·push/회신은
          worker; **롤백하지 않는다** — 재배정 ≠ 취소).
    """
    session_id: Optional[str] = None
    mr_url: Optional[str] = None
    reset_at: Optional[str] = None
    final_text = ""
    limit_hit = False
    error_seen = False
    cancelled = False
    handoff = False
    summary_parts: list = []
    pending_bg: set = set()   # 현재 살아있는 백그라운드 태스크 id(전량 치환 스냅샷)
    closing = False           # 완료 판정 후 stdin 닫음 — 이후 스트림 EOF까지만 소비
    watch_state: dict = {"last": time.monotonic()}

    # 지속 세션이면 티켓 프롬프트를 stdin으로 주입(연 채로 유지). one-shot 폴백이면
    # stdin을 즉시 닫아 claude가 stdin 입력을 기다리지 않게 한다.
    persistent = initial_prompt is not None and getattr(proc, "stdin", None) is not None
    if persistent:
        _write_stdin(proc, _encode_user_message(initial_prompt))
    else:
        _close_stdin(proc)

    watchdog, watch_stop = (None, None)
    if persistent:
        watchdog, watch_stop = _start_session_watchdog(
            proc, watch_state, max_sec=session_max_sec, idle_sec=session_idle_sec
        )

    stdout = getattr(proc, "stdout", None)
    if stdout is not None:
        for raw in stdout:
            watch_state["last"] = time.monotonic()
            # 제어 폴링(이벤트마다). 취소/핸드오프 신호 감지 시 즉시 종료.
            if cancel_check is not None:
                sig = _normalize_control(cancel_check())
                if sig == "handoff":
                    handoff = True
                    break
                if sig == "cancel":
                    cancelled = True
                    break
            event = parse_stream_event(raw)
            if event is None:
                continue
            # session_id·mr_url 추출은 기존대로 **모든 이벤트**에서 유지한다.
            sid = extract_session_id(event)
            if sid:
                session_id = sid
            url = extract_mr_url(event, forge_kind)
            if url:
                mr_url = url
            # 백그라운드 태스크 스냅샷 갱신(pending 판정의 근거). 지속 세션에서
            # "완료 알림 대기 중"인지 여부가 여기서 결정된다.
            bg = _bg_pending_from_event(event)
            if bg is not None:
                pending_bg = bg
            # 한도(usage/quota limit) 판정은 **result 이벤트에서만** 수행한다.
            # 중간 이벤트(assistant/tool/thinking)의 텍스트에 "rate limit" 같은
            # 문구가 섞여도 한도로 오판하지 않도록 게이트한다(실측 오탐 방지).
            if event.get("type") == "result":
                is_lim, r = detect_limit_and_reset(event)
                # 마무리 멘트(최종 텍스트) 캡처 — 완료 알림 본문 재료(마지막 것이 최종).
                ft = event.get("result") or event.get("error") or ""
                if isinstance(ft, str) and ft.strip():
                    final_text = ft.strip()
                is_err = bool(event.get("is_error")) or str(
                    event.get("subtype") or ""
                ).startswith("error")
                # --- 완료(종결) 판정 ---------------------------------------------
                # 한도/에러 result는 그 자리에서 종결. 정상 result는 **pending 백그라운드
                # 태스크가 없을 때만** 종결(있으면 다음 턴을 기다린다 = HAN-537 해결).
                terminal = False
                if is_lim:
                    limit_hit = True
                    if r:
                        reset_at = r
                    terminal = True
                elif is_err:
                    error_seen = True
                    terminal = True
                elif not pending_bg:
                    terminal = True
                if terminal and not closing:
                    closing = True
                    _close_stdin(proc)  # 지속 세션이면 EOF → 정상 종료. one-shot이면 no-op.
            elif event.get("type") == "error" or event.get("is_error"):
                error_seen = True
            line = _summarize_event(event)
            if line:
                summary_parts.append(line)

    if watch_stop is not None:
        watch_stop.set()

    timed_out = watch_state.get("timed_out")
    if timed_out and not (limit_hit or cancelled or handoff):
        # result를 영영 못 낸 세션(행/누수) — 백스톱이 종료시켰다. 정직하게 FAILED.
        error_seen = True
        summary_parts.append(f"[timeout] 지속 세션 백스톱 종료(reason={timed_out})")

    if cancelled:
        _close_stdin(proc)
        _terminate_proc(proc)
        summary_parts.append("[cancelled] 취소 신호로 실행 중단")
        summary = _redact("\n".join(summary_parts)[-_SUMMARY_MAX_CHARS:], secret_values)
        return AgentResult(
            status=STATUS_CANCELLED,
            session_id=session_id,
            mr_url=mr_url,
            log_summary=summary,
            final_text=_redact(final_text, secret_values),
            returncode=getattr(proc, "returncode", None),
        )

    if handoff:
        # 담당자 변경 핸드오프 — subprocess 종료 후 STATUS_HANDOFF 반환. checkpoint
        # (커밋·push·저널 노트)/회신은 worker가 담당한다(**롤백하지 않는다**).
        _close_stdin(proc)
        _terminate_proc(proc)
        summary_parts.append("[handoff] 담당자 변경 신호로 checkpoint 후 이관")
        summary = _redact("\n".join(summary_parts)[-_SUMMARY_MAX_CHARS:], secret_values)
        return AgentResult(
            status=STATUS_HANDOFF,
            session_id=session_id,
            mr_url=mr_url,
            log_summary=summary,
            final_text=_redact(final_text, secret_values),
            returncode=getattr(proc, "returncode", None),
        )

    # 정상 종료 경로: 리더 reap **전에** pgid를 캡처해 두었다가(reap 후엔 getpgid
    # 실패 가능), wait로 참 returncode를 얻은 뒤 남은 그룹 손자를 쓸어담는다(좀비 방지
    # 방어심층). 리더를 죽여 returncode를 오염시키지 않도록 반드시 wait '뒤에' 손자만 sweep.
    pgid = _process_group_id(proc)
    rc = proc.wait()
    if pgid is not None:
        _killpg(pgid, _SIGKILL)

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
        final_text=_redact(final_text, secret_values),
        returncode=rc,
    )


def _forge_token(creds: UserCreds, config: Any) -> Optional[str]:
    """creds의 forge 토큰 참조를 secrets.base_dir 기준으로 읽어 값 반환(build_env와 동일 소스).

    정본 ``forge_token_ref``, 없으면 레거시 ``gitlab_token_ref``(옛 creds 객체·테스트 대역).
    """
    ref = (getattr(creds, "forge_token_ref", "")
           or getattr(creds, "gitlab_token_ref", "") or "")
    if not ref:
        return None
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    return read_secret(base_dir, ref)


def _provision_repos(
    creds: UserCreds,
    config: Any,
    ensure_repos_fn: Optional[Callable],
    *,
    fresh: bool = True,
) -> Optional[AgentResult]:
    """claude 실행 **전** 오케스트레이터 3개 레포를 프로비저닝(clone/reset·pull).

    사용자 forge 토큰으로 :func:`app.repos.ensure_repos` 를 호출한다. 개별 레포
    실패는 로그만 남기고 잡은 진행하되(오케스트레이터가 자체 처리할 수도),
    **orchestrator_repo 자체가 프로비저닝 실패**하면 오케스트레이터 cwd가 없어
    실행 불가이므로 명확한 실패 AgentResult를 반환한다(그 외엔 None → 진행).

    ``fresh``: 신규 잡(:func:`run_job`)이면 True — dirty 워크스페이스를 원격에 강제
    정합(reset --hard + clean)해 프로비저닝 abort를 막는다. 재개(:func:`resume_job`)면
    False — 진행 중 미커밋 작업을 파괴적 reset으로 날리지 않도록 완만한 pull만 한다.
    """
    fn = ensure_repos_fn or ensure_repos
    token = _forge_token(creds, config)
    try:
        results = fn(config, token, fresh=fresh) or {}
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
    # 신규 잡: 워크트리를 원격에 강제 정합(fresh=True) — dirty 워크스페이스 abort 방지.
    fatal = _provision_repos(creds, config, ensure_repos_fn, fresh=True)
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
    # 지속 세션이면 티켓 프롬프트를 stdin으로 주입(_consume이 처리). one-shot이면 None.
    initial_prompt = build_prompt(job, config) if persistent_enabled(config) else None
    return _consume(
        proc, secret_values, cancel_check=cancel_check,
        initial_prompt=initial_prompt,
        session_max_sec=_session_max_sec(config),
        session_idle_sec=_session_idle_sec(config),
        forge_kind=forge.resolve_kind(config),
    )


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

    ⚠️ 재개 안전: 재개는 usage-limit 등으로 중단된 잡을 이어받는 것이므로, 워크트리에
    진행 중 미커밋 작업이 남아 있다. 신규 잡과 달리 **파괴적 reset/clean을 하지 않는다**
    (fresh=False) — 재개 중인 잡의 작업을 프로비저닝이 날리면 안 된다.
    """
    fatal = _provision_repos(creds, config, ensure_repos_fn, fresh=False)
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
    # 재개도 지속 세션이면 '계속' 프롬프트를 stdin으로 주입한다(--resume + stdin).
    initial_prompt = build_prompt(job, config) if persistent_enabled(config) else None
    result = _consume(
        proc, secret_values, cancel_check=cancel_check,
        initial_prompt=initial_prompt,
        session_max_sec=_session_max_sec(config),
        session_idle_sec=_session_idle_sec(config),
        forge_kind=forge.resolve_kind(config),
    )
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
