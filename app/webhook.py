"""얇은 Jira 웹훅 엔드포인트(기본 비활성, 중앙 전용).

역할:
    Jira 웹훅 콜백을 받아 shared_secret으로 검증하고, 페이로드를 신뢰하지 않고
    Jira에서 이슈를 **재검증**한 뒤 dedup 게이트로 넘긴다(gate.claim()). 이후
    폴러와 동일하게 담당자를 등록 사용자로 매핑해(enabled만) 그 사용자 큐에
    디스패치한다(dispatch.enqueue). 폴러와 동일 수렴점이라 중복은 게이트가 흡수.

역할 소속: **central**.

구현 Phase: **Phase 4** (폴러 + 웹훅).

기본값: webhook.enabled=false — 개발서버 인바운드 포트가 열릴 때만 on.

보안:
    - shared_secret_file로 서명/시크릿 검증(미검증 요청 거부).
    - 웹훅 페이로드는 신뢰하지 않는다 — 항상 Jira GET으로 재검증 후 claim.
"""

from __future__ import annotations

from flask import Blueprint

# 라우트는 main.py에서 config.webhook.path에 등록한다(Phase 4).
webhook_bp = Blueprint("webhook", __name__)


def verify_signature(request, shared_secret: str) -> bool:
    """웹훅 요청 서명/시크릿 검증.

    TODO(Phase 4): 헤더 서명 또는 공유 시크릿 상수시간 비교.
    """
    raise NotImplementedError("TODO(Phase 4): verify_signature")


def handle_webhook(request, jira_client, gate, registry, dispatcher):
    """웹훅 처리 — 검증 → 재검증 → gate.claim → 사용자 매핑 → 디스패치.

    TODO(Phase 4): 서명 검증 → 이슈키 추출 → Jira 재조회 → 조건 재검증
    → gate.claim → registry.find_by_account_id(enabled) → dispatch.enqueue.
    미매핑/비활성은 skip+로그. 응답은 즉시 2xx(비동기 처리).
    """
    raise NotImplementedError("TODO(Phase 4): handle_webhook")
