"""jira_client 단위테스트 — requests 를 mock(라이브 호출 금지).

검증: search/jql nextPageToken 자동 페이지네이션, 전이 POST 경로/바디,
완료 전이 필수 필드 선기입, 4xx→JiraError.
"""

from __future__ import annotations

import pytest

from app.jira_client import (FIELD_ACTUAL_END, FIELD_ACTUAL_START, JiraClient,
                             JiraError)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"x" if (payload is not None or text) else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """세션 대역 — request 호출을 기록하고 스크립트된 응답을 반환."""

    def __init__(self, responder):
        self.auth = None
        self.headers = {}
        self.responder = responder
        self.calls = []

    def request(self, method, url, timeout=None, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responder(method, url, kwargs)


def make_client(responder):
    c = JiraClient("https://your-org.atlassian.net", "you@example.com", "tok")
    c.session = FakeSession(responder)
    return c


def test_search_jql_pagination_follows_next_page_token():
    pages = [
        {"issues": [{"key": "PROJ-1"}], "nextPageToken": "t2"},
        {"issues": [{"key": "PROJ-2"}], "nextPageToken": "t3"},
        {"issues": [{"key": "PROJ-3"}], "isLast": True},  # 마지막
    ]
    seq = {"i": 0}
    bodies = []

    def responder(method, url, kwargs):
        assert method == "POST"
        assert url.endswith("/rest/api/3/search/jql")  # 삭제된 /search 아님
        bodies.append(kwargs["json"])
        p = pages[seq["i"]]
        seq["i"] += 1
        return FakeResponse(200, p)

    c = make_client(responder)
    result = c.search_jql("project = PROJ", fields=["status"], max_results=1)
    assert [i["key"] for i in result["issues"]] == ["PROJ-1", "PROJ-2", "PROJ-3"]
    assert result["total"] == 3
    # 2·3페이지 요청에 nextPageToken 실림
    assert "nextPageToken" not in bodies[0]
    assert bodies[1]["nextPageToken"] == "t2"
    assert bodies[2]["nextPageToken"] == "t3"


def test_transition_posts_correct_path_and_body():
    captured = {}

    def responder(method, url, kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client(responder)
    c.transition("PROJ-5", "41", fields={"customfield_x": "v"})
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/rest/api/2/issue/PROJ-5/transitions")
    assert captured["json"]["transition"]["id"] == "41"
    assert captured["json"]["fields"] == {"customfield_x": "v"}


def test_transition_done_prefills_actual_dates():
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client(responder)
    c.transition_done("PROJ-9", "2026-08-01", "2026-08-10")
    fields = captured["json"]["fields"]
    assert fields[FIELD_ACTUAL_START] == "2026-08-01"
    assert fields[FIELD_ACTUAL_END] == "2026-08-10"
    assert captured["json"]["transition"]["id"] == "41"


def test_get_issue_and_comment_paths():
    seen = []

    def responder(method, url, kwargs):
        seen.append((method, url, kwargs.get("json"), kwargs.get("params")))
        return FakeResponse(200, {"key": "PROJ-1"})

    c = make_client(responder)
    c.get_issue("PROJ-1", fields=["status", "assignee"])
    c.add_comment("PROJ-1", "한글 코멘트")
    assert seen[0][0] == "GET" and seen[0][1].endswith("/rest/api/2/issue/PROJ-1")
    assert seen[0][3] == {"fields": "status,assignee"}
    assert seen[1][0] == "POST" and seen[1][1].endswith("/comment")
    assert seen[1][2] == {"body": "한글 코멘트"}  # json= 로 전송


def test_4xx_raises_jira_error():
    def responder(method, url, kwargs):
        return FakeResponse(403, {"errorMessages": ["forbidden"]})

    c = make_client(responder)
    with pytest.raises(JiraError) as exc:
        c.get_issue("PROJ-1")
    assert exc.value.status_code == 403
