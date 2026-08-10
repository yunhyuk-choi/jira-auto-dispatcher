"""Jira REST 클라이언트(중앙 전용).

역할:
    Jira Cloud REST로 이슈를 조회/전이/코멘트하고, JQL로 신규 할당 티켓을
    검색한다. 인증은 Basic auth(email:token). 중앙은 감시 토큰
    (jira.watcher_token_file, secrets.base_dir 상대)으로만 Jira를 읽는다.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다. 티켓 상태 전이는
    worker가 실행하는 오케스트레이터가 "사용자" 토큰으로 직접 수행한다).

구현 Phase: **Phase 2** (Jira 연동).

엔드포인트(설계 기준):
    - GET  /rest/api/2/issue/{key}                 이슈 상세
    - PUT  /rest/api/2/issue/{key}                 필드 갱신
    - GET  /rest/api/2/issue/{key}/transitions     가능한 전이 목록
    - POST /rest/api/2/issue/{key}/transitions     상태 전이
    - POST /rest/api/2/issue/{key}/comment         코멘트
    - POST /rest/api/3/search/jql                  JQL 검색(신 API)

참고:
    - 폴러의 high-watermark JQL은 project=HAN AND assignee=<id>
      AND status in (...) AND updated >= <watermark> 형태.
    - 상태 전이/코멘트/브랜치는 원칙적으로 오케스트레이터가 수행한다.
      이 클라이언트는 디스패처가 필요로 하는 최소 읽기/표식용.
"""

from __future__ import annotations

from typing import Any, Optional


class JiraClient:
    """Jira Cloud REST 클라이언트(스텁)."""

    def __init__(self, base_url: str, email: str, token: str) -> None:
        """base_url·email·token(=Basic auth 자격)로 세션 구성.

        TODO(Phase 2): requests.Session + Basic auth 헤더 구성.
        """
        self.base_url = base_url
        self.email = email
        self._token = token

    def get_issue(self, key: str) -> dict:
        """이슈 상세 GET.

        TODO(Phase 2): GET /rest/api/2/issue/{key}.
        """
        raise NotImplementedError("TODO(Phase 2): get_issue")

    def update_issue(self, key: str, fields: dict) -> None:
        """이슈 필드 PUT.

        TODO(Phase 2): PUT /rest/api/2/issue/{key}.
        """
        raise NotImplementedError("TODO(Phase 2): update_issue")

    def get_transitions(self, key: str) -> list:
        """가능한 전이 목록 GET.

        TODO(Phase 2): GET /rest/api/2/issue/{key}/transitions.
        """
        raise NotImplementedError("TODO(Phase 2): get_transitions")

    def transition_issue(self, key: str, transition_id: str) -> None:
        """상태 전이 POST.

        TODO(Phase 2): POST /rest/api/2/issue/{key}/transitions.
        """
        raise NotImplementedError("TODO(Phase 2): transition_issue")

    def add_comment(self, key: str, body: str) -> None:
        """코멘트 POST.

        TODO(Phase 2): POST /rest/api/2/issue/{key}/comment.
        """
        raise NotImplementedError("TODO(Phase 2): add_comment")

    def search_jql(
        self,
        jql: str,
        fields: Optional[list] = None,
        next_page_token: Optional[str] = None,
        max_results: int = 50,
    ) -> dict:
        """JQL 검색 POST(신 search/jql API, 토큰 페이지네이션).

        TODO(Phase 2): POST /rest/api/3/search/jql.
        """
        raise NotImplementedError("TODO(Phase 2): search_jql")
