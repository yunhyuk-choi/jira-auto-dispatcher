"""완료 알림 — Google Chat incoming webhook 발송(worker 전용, best-effort).

역할:
    worker가 자율 실행 pass를 끝낸 **터미널 결과**(done/failed) 또는 짧은 상태
    전이(interrupted/cancelled)에서, 팀 스페이스 웹훅으로 마무리 멘트 + 다음 할 일을
    Google Chat에 POST한다. 담당자를 ``<users/{id}>`` 로 @멘션해 개인 알림 효과를
    낸다(userId 없으면 display_name 텍스트로 degrade — 하드 핑 없음).

역할 소속: **worker**(오케스트레이터 실행 뒤 이어짐).

설계:
    - **팀 웹훅 1개**(사용자별 아님). 멘션으로 개인화.
    - **best-effort**: 알림 실패가 런을 죽이면 안 된다 — :func:`notify_job_end` 는
      어떤 예외도 삼키고 bool을 반환한다(발송 여부).
    - 메시지는 단순 ``text``(마무리 멘트가 길 수 있어 cardsV2 대신 text로 충분).
    - 링크(MR URL 등)는 그대로 URL 텍스트로 싣는다.

⚠️ 시크릿 규율:
    웹훅 URL은 **시크릿**이다. 값이 아니라 참조(secrets.base_dir 상대)로만 다루고
    (:attr:`config.notify.webhook_ref` → :func:`app.config.read_secret`), 로그·예외
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
from app.config import read_secret

log = logging.getLogger("jad.notify")

# 웹훅 POST 타임아웃(초). 알림은 부수적이므로 짧게 클램프.
NOTIFY_TIMEOUT_SEC = 10

# 상태 → 사람이 읽는 라벨(메시지 헤더).
_STATUS_LABEL = {
    STATUS_DONE: "완료(리뷰 대기)",
    STATUS_FAILED: "실패",
    STATUS_INTERRUPTED: "일시 중단",
    STATUS_CANCELLED: "취소됨",
}


# --- 순수 조립 유틸(단위테스트 대상) ----------------------------------------


def _job_get(job: Any, name: str, default: Any = None) -> Any:
    """job(dict 또는 객체)에서 필드 접근(worker는 dict, 테스트는 객체 가능)."""
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def format_mention(user_id: Optional[str], display_name: Optional[str]) -> str:
    """Google Chat @멘션 텍스트 조립.

    userId가 있으면 ``<users/{id}>`` (실제 핑), 없으면 display_name만 텍스트로
    싣는다(하드 핑 없이 degrade). 둘 다 없으면 빈 문자열.
    """
    uid = (user_id or "").strip()
    if uid:
        return f"<users/{uid}>"
    return (display_name or "").strip()


def build_message(*, result: Any, job: Any, creds: Any) -> str:
    """알림 메시지 텍스트(순수) — 마무리 멘트 + 티켓/상태 + 다음 할 일.

    A일 때 MR URL, B일 때 브랜치명(auto/<ticket>) + runs/<ticket>/ 저널 위치를 싣고,
    "다음 할 일" 한 줄을 모드별로 분기한다(A=로컬 MR 리뷰·머지 / B=브랜치 fetch해
    이어서 완성). interrupted/cancelled는 짧은 메시지.
    """
    ticket = str(_job_get(job, "ticket", "") or "")
    mode = str(_job_get(job, "autonomy_mode", "B") or "B").upper()
    status = getattr(result, "status", "") or ""
    branch = _job_get(job, "branch", None) or (f"auto/{ticket}" if ticket else "")

    mention = format_mention(
        getattr(creds, "google_chat_user_id", ""),
        getattr(creds, "git_name", "") or getattr(creds, "user", ""),
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
            lines.append(f"MR: {mr}")
        lines.append("다음 할 일: 로컬에서 MR을 리뷰·머지하세요.")
    else:
        if branch:
            lines.append(f"브랜치: {branch}")
        if ticket:
            lines.append(f"저널: runs/{ticket}/")
        nxt = f"{branch} 브랜치를 fetch해" if branch else "브랜치를 fetch해"
        lines.append(f"다음 할 일: {nxt} 로컬에서 이어서 완성하세요.")

    return "\n".join(lines)


# --- 발송(부수효과) ----------------------------------------------------------


def _default_http():
    """기본 HTTP 클라이언트(requests.Session). 지연 import로 테스트 격리."""
    import requests  # noqa: PLC0415

    return requests.Session()


def _read_webhook(config: Any) -> str:
    """config.notify.webhook_ref 를 secrets.base_dir 기준으로 읽어 웹훅 URL 반환.

    참조가 비었거나 파일이 없으면 빈 문자열(발송 생략). 값·경로를 로깅하지 않는다.
    """
    nc = getattr(config, "notify", None)
    ref = getattr(nc, "webhook_ref", "") if nc else ""
    if not ref:
        return ""
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    return read_secret(base_dir, ref) or ""


def _post(webhook: str, text: str, *, http=None, http_factory: Optional[Callable] = None) -> bool:
    """웹훅으로 ``{"text": ...}`` POST. 2xx면 True. (웹훅 URL은 로깅 금지.)"""
    client = http if http is not None else (http_factory or _default_http)()
    resp = client.post(webhook, json={"text": text}, timeout=NOTIFY_TIMEOUT_SEC)
    code = getattr(resp, "status_code", 0) or 0
    return bool(0 < code < 400)


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

    ⚠️ 어떤 예외도 삼킨다(알림 실패가 런을 죽이면 안 된다). 비활성/웹훅 미설정/
    비대상 상태면 조용히 False. 웹훅 URL·토큰은 로그에 절대 남기지 않는다.
    """
    try:
        nc = getattr(config, "notify", None)
        if not nc or not getattr(nc, "enabled", False):
            return False

        status = getattr(result, "status", "") or ""
        if status == STATUS_INTERRUPTED and not getattr(nc, "notify_interrupted", True):
            return False
        if status == STATUS_CANCELLED and not getattr(nc, "notify_cancelled", True):
            return False

        webhook = _read_webhook(config)
        if not webhook:
            # enabled인데 참조가 비었거나 파일 부재 — 값은 로깅하지 않는다.
            log.warning("완료 알림 활성이나 웹훅 참조를 읽을 수 없어 발송 생략")
            return False

        text = build_message(result=result, job=job, creds=creds)
        return _post(webhook, text, http=http, http_factory=http_factory)
    except Exception:  # noqa: BLE001 — best-effort: 알림 실패는 런에 영향 주지 않는다
        # 예외 메시지에 웹훅 URL이 섞일 수 있어 트레이스백을 남기지 않는다.
        log.warning("완료 알림 발송 실패(무시)")
        return False
