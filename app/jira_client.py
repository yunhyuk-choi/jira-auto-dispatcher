"""Jira REST 클라이언트(중앙 전용).

역할:
    Jira Cloud REST로 이슈를 조회/전이/코멘트하고, JQL로 신규 할당 티켓을
    검색한다. 인증은 Basic auth(email:token). 중앙은 감시 토큰
    (jira.watcher_token_file, secrets.base_dir 상대)으로만 Jira를 읽는다.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다. 티켓 상태 전이는
    worker가 실행하는 오케스트레이터가 "사용자" 토큰으로 직접 수행한다).

구현 Phase: **Phase 2** (Jira 연동).

엔드포인트(사실 검증됨 2026-08):
    - GET  /rest/api/2/issue/{key}                 이슈 상세
    - PUT  /rest/api/2/issue/{key}                 필드 갱신(set_fields)
    - GET  /rest/api/2/issue/{key}/transitions     가능한 전이 목록
    - POST /rest/api/2/issue/{key}/transitions     상태 전이
    - POST /rest/api/2/issue/{key}/comment         코멘트
    - POST /rest/api/3/search/jql                  JQL 검색(신 API, nextPageToken)
    ⚠️ /rest/api/2/search·/rest/api/3/search 는 Atlassian이 삭제 — 사용 금지.

커스텀 필드(HAN 프로젝트):
    - customfield_10015  시작날짜(착수 시 필수)
    - duedate            마감일(착수 시 필수)
    - customfield_10187  실제 시작일(완료 전이 시 필수)
    - customfield_10186  실제 종료일(완료 전이 시 필수)
    완료 전이 id는 보통 41(운영 환경 기준). list_transitions로 실측 권장.

참고:
    - 상태 전이/코멘트/브랜치는 원칙적으로 실제 실행 주체(worker/오케스트레이터)가
      "사용자" 토큰으로 수행한다. 이 클라이언트는 그 헬퍼를 노출하되, 폴러가
      필요로 하는 최소 읽기(search_jql/get_issue)가 핵심 용도다.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

# HAN 프로젝트 커스텀 필드 상수(전이 헬퍼에서 사용).
FIELD_START_DATE = "customfield_10015"   # 시작날짜(착수)
FIELD_DUE_DATE = "duedate"               # 마감일(착수)
FIELD_ACTUAL_START = "customfield_10187"  # 실제 시작일(완료)
FIELD_ACTUAL_END = "customfield_10186"    # 실제 종료일(완료)
DONE_TRANSITION_ID = "41"                 # 완료 전이(운영 기본값)


class JiraError(Exception):
    """Jira REST 호출 실패(4xx/5xx 또는 네트워크)."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class JiraClient:
    """Jira Cloud REST 클라이언트."""

    def __init__(self, base_url: str, email: str, token: str, timeout: int = 30) -> None:
        """base_url·email·token(=Basic auth 자격)로 세션 구성.

        Basic auth = (email, token). 한글 body는 항상 json= 로 보낸다.
        """
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._token = token
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    # ------------------------------------------------------------------
    # 저수준 요청
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise JiraError(f"{method} {path} 요청 실패: {exc}") from exc
        if resp.status_code >= 400:
            body: Any
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise JiraError(
                f"{method} {path} → HTTP {resp.status_code}: {body}",
                status_code=resp.status_code,
                body=body,
            )
        return resp

    @staticmethod
    def _json_or_empty(resp: requests.Response) -> dict:
        if resp.status_code == 204 or not (resp.content or b"").strip():
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # 이슈 읽기/쓰기
    # ------------------------------------------------------------------

    def get_issue(self, key: str, fields: Optional[list] = None) -> dict:
        """이슈 상세 GET (/rest/api/2/issue/{key})."""
        params = {}
        if fields:
            params["fields"] = ",".join(fields)
        resp = self._request("GET", f"/rest/api/2/issue/{key}", params=params or None)
        return self._json_or_empty(resp)

    def set_fields(self, key: str, fields: dict) -> None:
        """이슈 필드 PUT (/rest/api/2/issue/{key}). body={fields:...}."""
        self._request("PUT", f"/rest/api/2/issue/{key}", json={"fields": fields})

    # 하위호환 별칭(기존 스텁 시그니처 유지).
    def update_issue(self, key: str, fields: dict) -> None:
        """set_fields 별칭(하위호환)."""
        self.set_fields(key, fields)

    def list_transitions(self, key: str) -> list:
        """가능한 전이 목록 GET (/rest/api/2/issue/{key}/transitions)."""
        resp = self._request("GET", f"/rest/api/2/issue/{key}/transitions")
        return self._json_or_empty(resp).get("transitions", [])

    # 하위호환 별칭.
    def get_transitions(self, key: str) -> list:
        """list_transitions 별칭(하위호환)."""
        return self.list_transitions(key)

    def transition(self, key: str, transition_id: str, fields: Optional[dict] = None) -> None:
        """상태 전이 POST (/rest/api/2/issue/{key}/transitions).

        fields 를 함께 넘기면 전이와 동시에 필드를 기입한다(완료 전이 시 실제
        시작/종료일 등).
        """
        body: dict = {"transition": {"id": str(transition_id)}}
        if fields:
            body["fields"] = fields
        self._request("POST", f"/rest/api/2/issue/{key}/transitions", json=body)

    # 하위호환 별칭.
    def transition_issue(self, key: str, transition_id: str) -> None:
        """transition 별칭(하위호환)."""
        self.transition(key, transition_id)

    def add_comment(self, key: str, body: str) -> None:
        """코멘트 POST (/rest/api/2/issue/{key}/comment). 한글 body는 json=."""
        self._request("POST", f"/rest/api/2/issue/{key}/comment", json={"body": body})

    # ------------------------------------------------------------------
    # 착수/완료 전이 헬퍼(POLICY-ISSUE-TRACKING 필수 필드 선기입)
    # ------------------------------------------------------------------

    def start_work(self, key: str, start_date: str, due_date: str) -> None:
        """착수 필수 필드(시작날짜+마감일) 기입.

        상태 전이(→ 진행중)는 호출부(오케스트레이터)가 별도로 수행한다. 여기서는
        필수 필드만 선기입한다(YYYY-MM-DD).
        """
        self.set_fields(key, {FIELD_START_DATE: start_date, FIELD_DUE_DATE: due_date})

    def transition_done(
        self,
        key: str,
        actual_start: str,
        actual_end: str,
        transition_id: str = DONE_TRANSITION_ID,
    ) -> None:
        """완료 전이 — 실제 시작/종료일(YYYY-MM-DD) 선기입 후 done 전이.

        전이와 동시에 필드를 넣는다(단일 트랜잭션). transition_id 는 환경에 따라
        다를 수 있으므로 list_transitions 로 실측 후 넘기는 것을 권장.
        """
        self.transition(
            key,
            transition_id,
            fields={FIELD_ACTUAL_START: actual_start, FIELD_ACTUAL_END: actual_end},
        )

    # ------------------------------------------------------------------
    # JQL 검색(신 API + nextPageToken 자동 페이지네이션)
    # ------------------------------------------------------------------

    def search_jql_page(
        self,
        jql: str,
        fields: Optional[list] = None,
        max_results: int = 50,
        next_page_token: Optional[str] = None,
    ) -> dict:
        """단일 페이지 JQL 검색 POST (/rest/api/3/search/jql).

        반환: 원 응답 dict({issues, nextPageToken?, isLast?, ...}).
        """
        body: dict = {"jql": jql, "maxResults": max_results}
        if fields is not None:
            body["fields"] = fields
        if next_page_token:
            body["nextPageToken"] = next_page_token
        resp = self._request("POST", "/rest/api/3/search/jql", json=body)
        return self._json_or_empty(resp)

    def search_jql(
        self,
        jql: str,
        fields: Optional[list] = None,
        max_results: int = 50,
    ) -> dict:
        """JQL 검색 — nextPageToken을 따라 전 페이지를 모아 반환.

        반환: ``{"issues": [...전체...], "total": N}``.
        max_results 는 페이지 크기로 사용한다(전체 상한이 아님).
        """
        all_issues: list = []
        token: Optional[str] = None
        seen_tokens: set = set()
        while True:
            page = self.search_jql_page(jql, fields=fields, max_results=max_results, next_page_token=token)
            all_issues.extend(page.get("issues", []) or [])
            token = page.get("nextPageToken")
            # isLast=True 또는 토큰 소진 시 종료. 토큰 반복(무한루프) 방어.
            if page.get("isLast") is True or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
        return {"issues": all_issues, "total": len(all_issues)}
