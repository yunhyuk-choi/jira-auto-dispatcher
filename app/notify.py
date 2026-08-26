"""완료 알림 — incoming webhook 발송(provider 어댑터, best-effort).

역할:
    설정된 알림 채널(provider)로 텍스트 한 건을 보내는 **단일 관문**(:func:`send_text`)
    을 제공한다 — provider 분기(페이로드 모양)·웹훅 참조 조회·POST 가 전부 여기 모인다.
    소비자는 프랙탈 센트럴 세션의 완료-리포트 상신기(리포 루트 ``notify_report.py``)와
    설치 진단의 테스트 발송(:mod:`app.setup_doctor`)이다.

    ⚠️ 과거 이 모듈에는 **워커 잡-종료 통지자**(``notify_job_end``/``build_message``/
    ``format_mention`` 등)가 있었으나, 레거시 old-path 은퇴로 제거됐다 — 완료 통지는
    프랙탈 센트럴 세션이 ``notify_report.py`` 로 **단일 발송**한다(완료 티켓당 채팅 2건
    이던 이중-통지의 근본 제거). 워커는 더 이상 통지자가 아니다.

역할 소속: **central**(프랙탈 센트럴 세션이 관찰한 완료-리포트를 상신).

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
    - **팀 웹훅 1개**(사용자별 아님). 개인화가 필요하면 호출자가 본문에 멘션을 싣고,
      기계용 사용자 id 는 ``mention_id`` 필드로 따로 나간다(generic_webhook).
    - **best-effort**: 알림 실패가 상위(센트럴)를 죽이면 안 된다 — 예외 경계는 호출자가
      감싼다(:func:`send_text` 는 게이팅만 하고 예외를 삼키지 않는다).
    - 메시지는 단순 ``text``(리포트 본문이 길고 자유형이라 카드/blocks 대신 text로 충분).
      Google Chat·Slack 모두 최소 공통 형태 ``{"text": ...}`` 로 수용한다.
    - 링크(변경요청 URL 등)는 그대로 URL 텍스트로 싣는다.

⚠️ 시크릿 규율:
    웹훅 URL은 **시크릿**이다. 값이 아니라 참조(secrets.base_dir 상대)로만 다루고
    (:attr:`config.notifier.webhook_ref` → :func:`app.config.read_secret`), 로그·예외
    메시지에 절대 남기지 않는다(웹훅 URL·토큰 미노출).
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

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
