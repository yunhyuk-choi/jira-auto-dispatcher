"""완료 알림 — incoming webhook 발송(provider 어댑터, best-effort).

역할:
    worker가 자율 실행 pass를 끝낸 **터미널 결과**(done/failed) 또는 짧은 상태
    전이(interrupted/cancelled)에서, 팀 채널 웹훅으로 마무리 멘트 + 다음 할 일을
    POST한다. 담당자를 채널 문법으로 @멘션해 개인 알림 효과를 낸다(사용자 id가 없으면
    display_name 텍스트로 degrade — 하드 핑 없음).

역할 소속: **worker**(오케스트레이터 실행 뒤 이어짐). 프랙탈 경로의 완료-리포트 상신은
    리포 루트 ``notify_report.py`` 가 이 모듈을 재사용한다(재발명 금지).

provider 어댑터(:data:`NOTIFIER_PROVIDERS` — 정본은 ``config.notifier.provider``):
    ``none``            알림 없음(**기본**). 아무것도 보내지 않는다.
    ``google_chat``     Google Chat incoming webhook. ``{"text": ...}``, 멘션 ``<users/{id}>``.
    ``slack``           Slack incoming webhook. ``{"text": ...}``, 멘션 ``<@{id}>``.
    ``generic_webhook`` 사내/임의 수신기. 아래 **고정 JSON 스키마**를 POST한다.

    ⚠️ 알 수 없는 provider 값이면 **발송하지 않는다**(경고 로그 후 미발송). 잘못된 모양의
    페이로드가 남의 채널로 나가는 것보다 안 나가는 편이 낫다.

generic_webhook 페이로드 계약(수신 측이 파싱할 수 있도록 **키는 항상 존재**하고, 모르는
값은 빈 문자열이다)::

    {
      "source": "jira-auto-dispatcher",   # 고정 — 발신 시스템 식별
      "event": "job_end",                 # job_end(잡 종료) | report(완료-리포트 상신)
      "ticket": "PROJ-1",                 # 티켓 키(모르면 "")
      "status": "done",                   # done | failed | interrupted | cancelled | ""
      "mention_id": "U123",               # 담당자 채널 사용자 id(없으면 "")
      "text": "...사람이 읽는 전체 메시지..."
    }

설계:
    - **팀 웹훅 1개**(사용자별 아님). 멘션으로 개인화.
    - **best-effort**: 알림 실패가 런을 죽이면 안 된다 — :func:`notify_job_end` 는
      어떤 예외도 삼키고 bool을 반환한다(발송 여부).
    - 메시지는 단순 ``text``(마무리 멘트가 길 수 있어 카드/blocks 대신 text로 충분).
      Google Chat·Slack 모두 최소 공통 형태 ``{"text": ...}`` 로 수용한다.
    - 링크(MR URL 등)는 그대로 URL 텍스트로 싣는다.

⚠️ 시크릿 규율:
    웹훅 URL은 **시크릿**이다. 값이 아니라 참조(secrets.base_dir 상대)로만 다루고
    (:attr:`config.notifier.webhook_ref` → :func:`app.config.read_secret`), 로그·예외
    메시지에 절대 남기지 않는다(웹훅 URL·토큰 미노출). 메시지 본문은 agent_runner가
    이미 시크릿을 마스킹(:func:`app.agent_runner._redact`)한 필드에서만 조립한다.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from app.agent_runner import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_INTERRUPTED,
)
from app import forge
from app.config import read_secret
from app.setup_schema import NOTIFIER_PROVIDERS

log = logging.getLogger("jad.notify")

# --- provider 식별자(정본은 app/setup_schema.NOTIFIER_PROVIDERS) --------------
PROVIDER_NONE = "none"
PROVIDER_GOOGLE_CHAT = "google_chat"
PROVIDER_SLACK = "slack"
PROVIDER_GENERIC_WEBHOOK = "generic_webhook"

# generic_webhook 페이로드의 발신 시스템 식별자(수신 측 라우팅용 고정값).
GENERIC_SOURCE = "jira-auto-dispatcher"

# 웹훅 POST 타임아웃(초). 알림은 부수적이므로 짧게 클램프.
NOTIFY_TIMEOUT_SEC = 10

# 사이클로그 핸드오프 라인 접두(고정 — central이 커밋한 dlc-meta 상대경로를 싣는다).
# ⚠️ 기계 생성 라인이다(파싱 대상). 로컬 오케스트레이터가 dlc-meta를 pull해 이 경로를
# 읽는다(로컬 측은 별도 구현). 위쪽 자유 서술(마무리 멘트)은 에이전트 산출·자유형이고,
# 이 라인만 기계적으로 덧붙인다.
CYCLE_LOG_PREFIX = "사이클로그: "

# 브랜치 라인 접두(작업이 master가 아닌 브랜치에 있을 때만 덧붙인다).
BRANCH_PREFIX = "브랜치: "


# 상태 → 사람이 읽는 라벨(메시지 헤더).
_STATUS_LABEL = {
    STATUS_DONE: "완료(리뷰 대기)",
    STATUS_FAILED: "실패",
    STATUS_INTERRUPTED: "일시 중단",
    STATUS_CANCELLED: "취소됨",
}


# --- provider 해석(정본 notifier ← 레거시 notify 미러) -----------------------


def resolve_provider(config: Any) -> str:
    """이 설정이 쓰는 알림 provider(소문자 문자열). 모르면 ``none``.

    정본은 :attr:`config.notifier.provider` 다(``app/config.py`` 가 채운다). 그 섹션이
    없는 **옛 설정 객체**(레거시 ``notify`` 만 가진 형태·테스트 대역)면 ``notify.enabled``
    로 파생한다 — 켜져 있었다면 그 시절 유일 구현이던 ``google_chat`` 이 그 의미다.

    ⚠️ 여기서 값을 **검증하지 않는다**(모르는 값도 그대로 돌려준다). 거부 판단은
    :func:`send_text` 가 한다 — 알 수 없는 provider 로는 발송하지 않는다.
    """
    nc = getattr(config, "notifier", None)
    provider = str(getattr(nc, "provider", "") or "").strip().lower() if nc is not None else ""
    if provider:
        return provider
    legacy = getattr(config, "notify", None)
    if legacy is not None and getattr(legacy, "enabled", False):
        return PROVIDER_GOOGLE_CHAT
    return PROVIDER_NONE


def _notifier_attr(config: Any, name: str, default: Any) -> Any:
    """알림 설정 값 하나를 정본(``notifier``) 우선, 레거시(``notify``) 폴백으로 읽는다."""
    for section in ("notifier", "notify"):
        sec = getattr(config, section, None)
        if sec is None:
            continue
        val = getattr(sec, name, None)
        if val is not None and val != "":   # False·0 은 유효한 답(빈 값만 폴백)
            return val
    return default


# --- 순수 조립 유틸(단위테스트 대상) ----------------------------------------


def _job_get(job: Any, name: str, default: Any = None) -> Any:
    """job(dict 또는 객체)에서 필드 접근(worker는 dict, 테스트는 객체 가능)."""
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def notify_user_id(creds: Any) -> str:
    """담당자의 **채널 사용자 id**(멘션용). 없으면 빈 문자열.

    정본 필드는 provider 중립 이름 ``notify_user_id`` 이고, 레거시
    ``google_chat_user_id`` 를 계속 읽는다(하위호환 — 레지스트리·env 계약이 그 이름으로
    남아 있는 배포가 있다). 값 자체는 시크릿이 아니다.
    """
    for name in ("notify_user_id", "google_chat_user_id"):
        val = str(getattr(creds, name, "") or "").strip()
        if val:
            return val
    return ""


def format_mention(
    user_id: Optional[str],
    display_name: Optional[str],
    provider: str = PROVIDER_GOOGLE_CHAT,
) -> str:
    """provider별 @멘션 텍스트 조립.

    사용자 id가 있으면 채널 문법으로 실제 핑을 만든다:
        - ``google_chat``     → ``<users/{id}>``
        - ``slack``           → ``<@{id}>``
        - 그 밖(``generic_webhook``·미상) → **문법을 지어내지 않는다.** 사람이 읽는
          텍스트(display_name, 없으면 id)만 싣고, 기계용 id는 페이로드의 ``mention_id``
          필드로 따로 나간다(:func:`build_payload`).

    id가 없으면 display_name만 텍스트로 싣는다(하드 핑 없이 degrade). 둘 다 없으면 "".
    """
    uid = (user_id or "").strip()
    name = (display_name or "").strip()
    p = (provider or "").strip().lower()
    if uid:
        if p == PROVIDER_GOOGLE_CHAT:
            return f"<users/{uid}>"
        if p == PROVIDER_SLACK:
            return f"<@{uid}>"
        return name or uid
    return name


def build_message(*, result: Any, job: Any, creds: Any,
                  provider: str = PROVIDER_GOOGLE_CHAT,
                  forge_kind: Any = None) -> str:
    """알림 메시지 텍스트(순수) — 마무리 멘트 + 티켓/상태 + 다음 할 일.

    A일 때 MR URL, B일 때 브랜치명(auto/<ticket>) + runs/<ticket>/ 저널 위치를 싣고,
    "다음 할 일" 한 줄을 모드별로 분기한다(A=로컬 MR 리뷰·머지 / B=브랜치 fetch해
    이어서 완성). interrupted/cancelled는 짧은 메시지.

    ``provider`` 는 **멘션 문법에만** 영향한다(본문 구조는 채널 무관 — 어느 채널이든
    같은 정보를 같은 순서로 읽는다). ``forge_kind`` 는 변경요청 용어(MR/PR)에만 영향한다
    — 주지 않으면 기본 forge 의 용어(MR)를 쓴다(옛 호출자 동작 유지).
    """
    cr = forge.change_abbr(forge_kind)   # "MR" | "PR"
    ticket = str(_job_get(job, "ticket", "") or "")
    mode = str(_job_get(job, "autonomy_mode", "B") or "B").upper()
    status = getattr(result, "status", "") or ""
    branch = _job_get(job, "branch", None) or (f"auto/{ticket}" if ticket else "")

    mention = format_mention(
        notify_user_id(creds),
        getattr(creds, "git_name", "") or getattr(creds, "user", ""),
        provider,
    )
    label = _STATUS_LABEL.get(status, status)
    header = " ".join(p for p in (mention, f"[{ticket}]", f"오케스트레이터 자율 실행 {label}") if p)

    lines = [header]

    # interrupted / cancelled → 짧은 알림(마무리 멘트/다음 할 일 생략).
    if status == STATUS_INTERRUPTED:
        reset = getattr(result, "reset_at", None)
        if reset:
            lines.append(f"토큰 한도로 멈춤 — {reset}에 재개 예정")
        else:
            lines.append("토큰 한도로 멈춤 — 한도 리셋 후 재개 예정")
        return "\n".join(lines)
    if status == STATUS_CANCELLED:
        lines.append("취소됨 — 진행 중이던 산출은 롤백됩니다.")
        return "\n".join(lines)

    # 터미널(done/failed) → 마무리 멘트 + 산출 위치 + 다음 할 일.
    final = (getattr(result, "final_text", "") or "").strip()
    if final:
        lines.append(final)

    if mode == "A":
        mr = getattr(result, "mr_url", None)
        if mr:
            lines.append(f"{cr}: {mr}")
        lines.append(f"다음 할 일: 로컬에서 {cr}을 리뷰·머지하세요.")
    else:
        if branch:
            lines.append(f"브랜치: {branch}")
        if ticket:
            lines.append(f"저널: runs/{ticket}/")
        nxt = f"{branch} 브랜치를 fetch해" if branch else "브랜치를 fetch해"
        lines.append(f"다음 할 일: {nxt} 로컬에서 이어서 완성하세요.")

    # --- dlc-meta 사이클로그 핸드오프 라인(Phase 3a) ---
    # central이 공유 dlc-meta 클론에 그 잡의 사이클로그를 커밋하고, 커밋한 상대경로를
    # 채널 F 회신으로 돌려준다. 워커가 그 경로를 job/result에 실어 넘기면 여기서 **고정
    # 라인**을 기계적으로 덧붙인다(위 자유 서술은 그대로). 작업이 master가 아닌 브랜치에
    # 있으면 브랜치 ref도 함께 싣는다(리뷰어가 fetch 대상을 알도록).
    cycle_log = _cycle_log_path(result, job)
    if cycle_log:
        lines.append(f"{CYCLE_LOG_PREFIX}{cycle_log}")
        if branch and str(branch).strip() and str(branch).strip() != "master":
            lines.append(f"{BRANCH_PREFIX}{branch}")

    return "\n".join(lines)


def _cycle_log_path(result: Any, job: Any) -> str:
    """central이 커밋한 사이클로그 상대경로를 result/job에서 꺼낸다(없으면 "").

    워커가 채널 F 회신(``cycle_log_path``)을 받아 job dict(또는 result)에 실어 둔다.
    result 우선, 없으면 job.
    """
    val = getattr(result, "cycle_log_path", None)
    if val:
        return str(val).strip()
    val = _job_get(job, "cycle_log_path", "")
    return str(val).strip() if val else ""


# --- 발송(부수효과) ----------------------------------------------------------


def _default_http():
    """기본 HTTP 클라이언트(requests.Session). 지연 import로 테스트 격리."""
    import requests  # noqa: PLC0415

    return requests.Session()


def _read_webhook(config: Any) -> str:
    """``notifier.webhook_ref`` 를 secrets.base_dir 기준으로 읽어 웹훅 URL 반환.

    참조가 비었거나 파일이 없으면 빈 문자열(발송 생략). 값·경로를 로깅하지 않는다.
    레거시 ``notify.webhook_ref`` 도 계속 읽는다(:func:`_notifier_attr`).
    """
    ref = str(_notifier_attr(config, "webhook_ref", "") or "")
    if not ref:
        return ""
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    return read_secret(base_dir, ref) or ""


def build_payload(
    text: str,
    *,
    provider: str,
    ticket: str = "",
    status: str = "",
    mention_id: str = "",
    event: str = "job_end",
) -> dict:
    """provider별 웹훅 요청 본문(순수).

    - ``google_chat``·``slack`` → 최소 공통 형태 ``{"text": ...}``. 두 채널 모두 이
      한 필드만으로 게시된다(카드/blocks 는 쓰지 않는다 — 마무리 멘트가 길고 자유형).
    - ``generic_webhook`` → 모듈 docstring의 **고정 JSON 스키마**(키는 항상 존재,
      모르는 값은 ""). 수신기가 텍스트만 필요하면 ``text`` 하나만 읽으면 되고,
      라우팅·상태 반영이 필요하면 ``ticket``/``status``/``mention_id`` 를 쓴다.

    Raises:
        ValueError: 알 수 없는 provider(발송하면 안 되는 상태 — 호출자가 거부한다).
    """
    p = (provider or "").strip().lower()
    if p in (PROVIDER_GOOGLE_CHAT, PROVIDER_SLACK):
        return {"text": text}
    if p == PROVIDER_GENERIC_WEBHOOK:
        return {
            "source": GENERIC_SOURCE,
            "event": str(event or "job_end"),
            "ticket": str(ticket or ""),
            "status": str(status or ""),
            "mention_id": str(mention_id or ""),
            "text": text,
        }
    raise ValueError(f"알 수 없는 notifier provider: {p!r}")


def _post(webhook: str, text: str, *, http=None, http_factory: Optional[Callable] = None,
          payload: Optional[dict] = None) -> bool:
    """웹훅으로 JSON POST. 2xx면 True. (웹훅 URL은 로깅 금지.)

    ``payload`` 를 주면 그 본문을 그대로 보내고, 없으면 ``{"text": text}``
    (google_chat·slack 형태)로 보낸다 — 옛 호출자 하위호환.
    """
    client = http if http is not None else (http_factory or _default_http)()
    body = payload if payload is not None else {"text": text}
    resp = client.post(webhook, json=body, timeout=NOTIFY_TIMEOUT_SEC)
    code = getattr(resp, "status_code", 0) or 0
    return bool(0 < code < 400)


def send_text(
    config: Any,
    text: str,
    *,
    ticket: str = "",
    status: str = "",
    mention_id: str = "",
    event: str = "job_end",
    http=None,
    http_factory: Optional[Callable] = None,
) -> bool:
    """provider 분기 발송 — 알림 발송의 **단일 관문**(발송 여부 bool).

    :func:`notify_job_end`(잡 종료 알림)와 ``notify_report.py``(완료-리포트 상신)가
    모두 이 함수를 지난다. 게이팅 순서:

    1. ``none`` → 조용히 미발송(기본값 — 알림 없이도 시스템은 완전히 동작한다).
    2. **알 수 없는 provider → 거부 + 경고 로그.** 모양을 모르는 페이로드를 남의 채널로
       쏘지 않는다(설정 오타가 조용한 오발송이 되는 것을 막는다).
    3. 웹훅 참조가 비었거나 파일 부재 → 경고 후 미발송(값·경로는 로깅하지 않는다).

    예외는 삼키지 않는다 — 호출자(best-effort 경계)가 감싼다.
    """
    provider = resolve_provider(config)
    if provider == PROVIDER_NONE:
        return False
    if provider not in NOTIFIER_PROVIDERS:
        # 값은 시크릿이 아니라 provider 이름이므로 로그에 남겨도 안전하다(진단 가치 큼).
        log.warning("알 수 없는 notifier.provider(%r) — 발송 거부(설정 확인). "
                    "허용: %s", provider, "|".join(NOTIFIER_PROVIDERS))
        return False
    if not (text or "").strip():
        log.warning("빈 알림 본문 — 발송 생략")
        return False

    webhook = _read_webhook(config)
    if not webhook:
        # 활성인데 참조가 비었거나 파일 부재 — 값은 로깅하지 않는다.
        log.warning("완료 알림 활성이나 웹훅 참조를 읽을 수 없어 발송 생략")
        return False

    payload = build_payload(text, provider=provider, ticket=ticket,
                            status=status, mention_id=mention_id, event=event)
    return _post(webhook, text, http=http, http_factory=http_factory, payload=payload)


def notify_job_end(
    config: Any,
    result: Any,
    job: Any,
    creds: Any,
    *,
    http=None,
    http_factory: Optional[Callable] = None,
) -> bool:
    """잡 종료(터미널/상태 전이)에 완료 알림을 발송(best-effort → 발송 여부 bool).

    ⚠️ 어떤 예외도 삼킨다(알림 실패가 런을 죽이면 안 된다). 비활성(``provider: none``)/
    알 수 없는 provider/웹훅 미설정/비대상 상태면 조용히 False. 웹훅 URL·토큰은 로그에
    절대 남기지 않는다. 페이로드·멘션 문법은 provider 어댑터가 고른다(:func:`send_text`).
    """
    try:
        provider = resolve_provider(config)
        if provider == PROVIDER_NONE:
            return False

        status = getattr(result, "status", "") or ""
        if status == STATUS_INTERRUPTED and not _notifier_attr(config, "notify_interrupted", True):
            return False
        if status == STATUS_CANCELLED and not _notifier_attr(config, "notify_cancelled", True):
            return False

        # 알 수 없는 provider 면 메시지 조립조차 하지 않는다 — send_text 가 거부·경고한다.
        text = build_message(result=result, job=job, creds=creds, provider=provider,
                             forge_kind=forge.resolve_kind(config))
        return send_text(
            config, text,
            ticket=str(_job_get(job, "ticket", "") or ""),
            status=status,
            mention_id=notify_user_id(creds),
            event="job_end",
            http=http, http_factory=http_factory,
        )
    except Exception:  # noqa: BLE001 — best-effort: 알림 실패는 런에 영향 주지 않는다
        # 예외 메시지에 웹훅 URL이 섞일 수 있어 트레이스백을 남기지 않는다.
        log.warning("완료 알림 발송 실패(무시)")
        return False
