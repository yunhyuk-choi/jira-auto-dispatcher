"""얇은 Jira 웹훅 엔드포인트(기본 비활성, 중앙 전용).

역할:
    Jira 웹훅 콜백을 받아 shared_secret으로 검증하고, 페이로드를 신뢰하지 않고
    Jira에서 이슈를 **재검증**한 뒤(get_issue) dedup 게이트로 넘긴다(gate.claim).
    이후 폴러와 동일하게 담당자를 등록(enabled) 사용자로 매핑해 그 사용자 큐에
    디스패치한다(dispatcher.enqueue). 폴러와 동일 수렴점이라 중복은 게이트가 흡수.

역할 소속: **central**.

구현 Phase: **Phase 4** (폴러 + 웹훅).

기본값: webhook.enabled=false — 라우트를 아예 등록하지 않아 404. 개발서버
    인바운드 포트가 열릴 때만 config로 on.

보안:
    - shared_secret_file(또는 config)로 ``X-Webhook-Secret`` 상수시간 검증(미검증 거부).
    - 웹훅 페이로드는 신뢰하지 않는다 — 항상 Jira GET으로 재검증 후 claim.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from flask import Blueprint, request

from app.poller import build_job

log = logging.getLogger("jad.webhook")

# 라우트는 register_webhook가 config.webhook.path 에 동적으로 배선한다.
webhook_bp = Blueprint("webhook", __name__)


def verify_signature(req, shared_secret: str) -> bool:
    """웹훅 요청의 X-Webhook-Secret 상수시간 검증. 시크릿 미설정이면 통과."""
    if not shared_secret:
        return True
    provided = req.headers.get("X-Webhook-Secret")
    return bool(provided) and hmac.compare_digest(str(provided), shared_secret)


def _extract_key(payload: dict) -> Optional[str]:
    """웹훅 페이로드에서 이슈 키만 추출(값은 신뢰하지 않음, 키만 취함)."""
    if not isinstance(payload, dict):
        return None
    issue = payload.get("issue")
    if isinstance(issue, dict) and issue.get("key"):
        return str(issue["key"])
    return payload.get("issue_key") or payload.get("key")


def handle_webhook(req, config, jira_client, gate, registry, dispatcher, shared_secret: str = ""):
    """웹훅 처리 — 검증 → 재검증(get_issue) → gate.claim → 매핑 → 디스패치.

    응답은 즉시 2xx(이미 처리/무시 포함). 인증 실패는 401.
    """
    if not verify_signature(req, shared_secret):
        return {"error": "unauthorized"}, 401

    payload = req.get_json(silent=True) or {}
    key = _extract_key(payload)
    if not key:
        return {"status": "ignored", "reason": "no issue key"}, 200

    # 페이로드를 믿지 않고 Jira에서 재검증.
    try:
        issue = jira_client.get_issue(
            key, fields=["assignee", "status", "created", "summary", "components", "labels"]
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("웹훅 재검증 실패(%s): %s", key, exc)
        return {"status": "error", "reason": "reverify failed"}, 200

    fields = (issue or {}).get("fields", {}) or {}

    # 상태 재검증.
    statuses = list(getattr(config.match, "statuses", []) or [])
    status_name = ((fields.get("status") or {}).get("name")) if isinstance(fields.get("status"), dict) else None
    if statuses and status_name not in statuses:
        return {"status": "ignored", "reason": "status mismatch"}, 200

    # 담당자 → enabled 사용자 매핑.
    assignee = fields.get("assignee") or {}
    account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    user = registry.get_by_account_id(account_id) if account_id else None
    if user is None:
        return {"status": "ignored", "reason": "unmapped/disabled"}, 200

    # dedup 게이트(폴러와 공용 수렴점).
    if not gate.claim(key):
        return {"status": "duplicate"}, 200

    job = build_job(config, key, issue, user)
    dispatcher.enqueue(user.username, job)
    log.info("webhook dispatch: %s → user=%s repos=%s", key, user.username, job.target_repos)
    return {"status": "accepted", "ticket": key, "user": user.username}, 200


def register_webhook(app, config, jira_client, gate, registry, dispatcher) -> bool:
    """webhook.enabled=true 이면 config.webhook.path에 라우트를 배선한다.

    Returns: 등록 여부(비활성이면 False → 경로는 404).
    """
    wh = getattr(config, "webhook", None)
    if not wh or not getattr(wh, "enabled", False):
        return False

    from app.config import read_secret

    secret = ""
    if getattr(wh, "shared_secret_file", ""):
        secret = read_secret(config.secrets.base_dir, wh.shared_secret_file) or ""

    def _view():
        return handle_webhook(request, config, jira_client, gate, registry, dispatcher, secret)

    app.add_url_rule(wh.path, endpoint="jira_webhook", view_func=_view, methods=["POST"])
    return True
