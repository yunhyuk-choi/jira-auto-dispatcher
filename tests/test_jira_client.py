"""jira_client 단위테스트 — requests 를 mock(라이브 호출 금지).

검증: search/jql nextPageToken 자동 페이지네이션, 전이 POST 경로/바디,
완료 전이 필수 필드 선기입, 4xx→JiraError.

프레임워크화(인스턴스 어댑터) 이후 추가 검증:
    - 커스텀필드 id·완료 전이를 **설정에서 주입**하고, 미설정이면 모듈 상수로 폴백(하위호환).
    - 명시 빈 값 = "이 인스턴스엔 없는 필드" → 그 필드를 아예 전송하지 않는다.
    - 완료 전이 확정 우선순위: 설정 id → 이름 매칭 → done 카테고리 단일 → (레거시 폴백) → 에러.
    - JQL 검색의 **Jira Cloud 전용** 의존성이 조용한 빈 결과가 아니라 에러로 드러난다.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.jira_client import (DEFAULT_CUSTOM_FIELDS, DEFAULT_DONE_TRANSITION_NAMES,
                             DONE_TRANSITION_ID, FIELD_ACTUAL_END,
                             FIELD_ACTUAL_START, FIELD_DUE_DATE,
                             FIELD_START_DATE, JiraClient, JiraError)


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
    """'실제 시작/종료일'은 **그 인스턴스의 id 를 설정한 경우에만** 전이에 실린다."""
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client_from_config(
        responder,
        custom_fields={"actual_start": "customfield_10352",
                       "actual_end": "customfield_10353"},
        done_transition_id="41",
    )
    c.transition_done("PROJ-9", "2026-08-01", "2026-08-10")
    fields = captured["json"]["fields"]
    assert fields["customfield_10352"] == "2026-08-01"
    assert fields["customfield_10353"] == "2026-08-10"
    assert captured["json"]["transition"]["id"] == "41"


def test_actual_date_fields_have_no_default_and_are_not_sent():
    """★ 남의 인스턴스 customfield id 를 기본값으로 밀어넣지 않는다.

    ``actual_start``·``actual_end`` 는 조직 고유 워크플로우 필드라 안전한 기본값이 없다
    (실측: 옛 기본값 customfield_10187/10186 은 다른 인스턴스에 아예 없었다 — 그쪽 실제
    값은 10352/10353). 설정하지 않으면 **아예 보내지 않는다** — 없는 필드를 보내 400 을
    맞는 대신, 워크플로우가 정말 그 필드를 요구하면 Jira 가 '필수입니다'라고 정확히 말하게
    둔다(그 편이 고칠 곳을 지목해 준다).
    """
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    assert FIELD_ACTUAL_START == "" and FIELD_ACTUAL_END == ""
    c = make_client(responder)               # 레거시(설정 미주입) 경로
    assert c.field_id("actual_start") is None and c.field_id("actual_end") is None
    c.transition_done("PROJ-9", "2026-08-01", "2026-08-10")
    assert "fields" not in captured["json"]  # 필드 없이 전이만 보낸다
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


# ===========================================================================
# 인스턴스 어댑터 — 커스텀필드/완료 전이 설정 주입 + 모듈 상수 폴백(하위호환)
# ===========================================================================


def make_config(**jira_kwargs):
    """``cfg.jira.*`` 만 흉내내는 최소 설정 대역(from_config 는 getattr 로만 읽는다)."""
    jira = SimpleNamespace(
        base_url="https://your-org.atlassian.net",
        custom_fields=jira_kwargs.pop("custom_fields", {}),
        done_transition_id=jira_kwargs.pop("done_transition_id", ""),
        done_transition_names=jira_kwargs.pop("done_transition_names", ["완료", "Done"]),
        **jira_kwargs,
    )
    return SimpleNamespace(jira=jira)


def make_client_from_config(responder, **jira_kwargs):
    c = JiraClient.from_config(make_config(**jira_kwargs), "you@example.com", "tok")
    c.session = FakeSession(responder)
    return c


def _transition(tid, name, to_name, category):
    """``GET .../transitions`` 응답 한 항목(전이 id·이름·목표 상태·상태 카테고리)."""
    return {
        "id": tid, "name": name,
        "to": {"name": to_name, "statusCategory": {"key": category}},
    }


def test_custom_fields_fall_back_to_module_constants_when_unset():
    """설정을 안 주면(레거시 생성 경로) 오늘의 모듈 상수를 그대로 쓴다(하위호환)."""
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client(responder)
    result = c.start_work("PROJ-1", "2026-08-01", "2026-08-10")
    fields = captured["json"]["fields"]
    assert fields[FIELD_START_DATE] == "2026-08-01"
    assert fields[FIELD_DUE_DATE] == "2026-08-10"
    assert result == {"updated": True, "skipped_fields": []}


def test_custom_fields_from_config_override_module_constants():
    """설정에 준 필드 id 가 모듈 상수를 이긴다(남의 Jira 는 id 가 다르다)."""
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client_from_config(
        responder,
        custom_fields={"start_date": "customfield_99001", "actual_end": "customfield_99002"},
    )
    c.start_work("PROJ-1", "2026-08-01", "2026-08-10")
    fields = captured["json"]["fields"]
    assert fields == {"customfield_99001": "2026-08-01", FIELD_DUE_DATE: "2026-08-10"}
    # 일부만 준 매핑에서 나머지 논리 키는 모듈 상수 폴백.
    assert c.field_id("due_date") == FIELD_DUE_DATE
    # actual_start 는 모듈 상수 자체가 '미설정'이라 폴백해도 비활성이다.
    assert c.field_id("actual_start") is None
    assert c.field_id("actual_end") == "customfield_99002"


def test_custom_field_explicit_empty_value_is_not_sent():
    """빈 값 = '이 인스턴스엔 없는 필드' → 그 필드를 아예 보내지 않는다(400 방지)."""
    captured = {}

    def responder(method, url, kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client_from_config(responder, custom_fields={"start_date": ""})
    result = c.start_work("PROJ-1", "2026-08-01", "2026-08-10")
    assert captured["json"]["fields"] == {FIELD_DUE_DATE: "2026-08-10"}
    assert result["skipped_fields"] == ["start_date"]
    assert c.field_id("start_date") is None


def test_start_work_skips_put_entirely_when_all_fields_disabled():
    """착수 필드가 둘 다 없는 인스턴스면 PUT 자체를 보내지 않는다(빈 갱신 금지)."""
    calls = []

    def responder(method, url, kwargs):
        calls.append((method, url))
        return FakeResponse(204)

    c = make_client_from_config(responder, custom_fields={"start_date": "", "due_date": ""})
    result = c.start_work("PROJ-1", "2026-08-01", "2026-08-10")
    assert calls == []
    assert result == {"updated": False, "skipped_fields": ["start_date", "due_date"]}


def test_done_transition_uses_configured_id_without_listing():
    """설정 id 가 있으면 전이 목록 조회 없이 그걸 쓴다(우선순위 1)."""
    calls = []

    def responder(method, url, kwargs):
        calls.append((method, url, kwargs.get("json")))
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_id="777")
    assert c.transition_done("PROJ-2", "2026-08-01", "2026-08-10") == "777"
    assert [m for m, _u, _j in calls] == ["POST"]        # GET .../transitions 없음
    assert calls[0][2]["transition"]["id"] == "777"


def test_done_transition_matches_by_name_when_id_unknown():
    """id 를 모르면 이름으로 찾는다(우선순위 2) — 인스턴스마다 다른 id 를 몰라도 된다."""
    posted = {}

    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("11", "진행 중으로", "진행 중", "indeterminate"),
                _transition("31", "완료로", "완료", "done"),
            ]})
        posted["json"] = kwargs.get("json")
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["완료", "Done"],
                                custom_fields={"actual_start": "customfield_10352"})
    assert c.transition_done("PROJ-3", "2026-08-01", "2026-08-10") == "31"
    assert posted["json"]["fields"]["customfield_10352"] == "2026-08-01"


def test_done_transition_name_match_is_normalized():
    """이름 매칭은 NFC·공백제거·casefold 후 비교한다('Done' ↔ 'done ')."""
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("42", "Mark  DONE", "Closed", "done"),
            ]})
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["mark done"])
    assert c.resolve_done_transition_id("PROJ-4") == "42"


def test_done_transition_falls_back_to_single_done_category():
    """이름을 몰라도 done 카테고리 후보가 정확히 하나면 모호성이 없으니 쓴다(우선순위 3)."""
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("11", "Start", "In Progress", "indeterminate"),
                _transition("51", "Ship it", "Shipped", "done"),
            ]})
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["완료"])
    assert c.resolve_done_transition_id("PROJ-5") == "51"


def test_done_transition_raises_with_available_transitions_when_ambiguous():
    """done 후보가 여럿이고 이름도 안 맞으면 **조용히 실패하지 않고** 목록을 담아 에러."""
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("51", "Ship it", "Shipped", "done"),
                _transition("52", "Reject", "Rejected", "done"),
            ]})
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["완료"])
    with pytest.raises(JiraError) as exc:
        c.resolve_done_transition_id("PROJ-6")
    msg = str(exc.value)
    assert "id=51" in msg and "id=52" in msg           # 고를 수 있었던 후보를 보여준다
    assert "jira.done_transition_id" in msg            # 어떻게 고치는지도 알려준다


def test_done_transition_uses_module_constant_when_it_is_a_done_transition():
    """done 후보가 모호해도 모듈 상수 id 가 그 중 하나면 그걸로 폴백한다(스키마가 약속한 하위호환).

    단 **done 카테고리 전이일 때만** — 남의 인스턴스에서 같은 id 가 엉뚱한 전이인 경우를
    고르지 않기 위해서다(아래 ambiguous 테스트가 그 반대 케이스를 지킨다).
    """
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition(DONE_TRANSITION_ID, "Ship it", "Shipped", "done"),
                _transition("52", "Reject", "Rejected", "done"),
            ]})
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["완료"])
    assert c.resolve_done_transition_id("PROJ-6b") == DONE_TRANSITION_ID


def test_done_transition_raises_when_no_candidate_and_configured():
    """설정을 준 배포는 못 찾으면 레거시 상수로 폴백하지 않고 에러를 낸다(엉뚱한 id 금지)."""
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("11", "Start", "In Progress", "indeterminate"),
            ]})
        return FakeResponse(204)

    c = make_client_from_config(responder, done_transition_names=["완료"])
    with pytest.raises(JiraError):
        c.resolve_done_transition_id("PROJ-7")


def test_done_transition_legacy_client_falls_back_to_module_constant():
    """설정을 **전혀** 주지 않은 옛 생성 경로만 모듈 상수로 폴백한다(우선순위 4·하위호환)."""
    def responder(method, url, kwargs):
        if method == "GET":
            return FakeResponse(200, {"transitions": [
                _transition("11", "Start", "In Progress", "indeterminate"),
            ]})
        return FakeResponse(204)

    c = make_client(responder)   # JiraClient(...) 직접 — 설정 미주입
    assert c.resolve_done_transition_id("PROJ-8") == DONE_TRANSITION_ID


def test_from_config_reads_jira_section_and_base_url_override():
    """from_config 가 jira.* 를 읽고, 명시 base_url 이 config 보다 우선한다."""
    cfg = make_config(
        custom_fields={"start_date": "customfield_1"},
        done_transition_id="99",
        done_transition_names=["Finish"],
    )
    c = JiraClient.from_config(cfg, "you@example.com", "tok")
    assert c.base_url == "https://your-org.atlassian.net"
    assert c.field_id("start_date") == "customfield_1"
    assert c.done_transition_id == "99"
    assert c.done_transition_names == ["Finish"]

    c2 = JiraClient.from_config(cfg, "you@example.com", "tok",
                                base_url="https://other.atlassian.net/")
    assert c2.base_url == "https://other.atlassian.net"


def test_from_config_with_empty_jira_section_keeps_legacy_defaults():
    """설정 섹션이 비어 있으면(기존 config.yaml) 모듈 상수 그대로 = 오늘과 동일 동작."""
    cfg = SimpleNamespace(jira=SimpleNamespace(base_url="https://x.atlassian.net",
                                               custom_fields={},
                                               done_transition_id="",
                                               done_transition_names=[]))
    c = JiraClient.from_config(cfg, "you@example.com", "tok")
    # 기본값이 **있는** 논리 키만 활성화된다(빈 기본값 = 미설정 = 전송 안 함).
    assert c.custom_fields == {k: v for k, v in DEFAULT_CUSTOM_FIELDS.items() if v}
    assert c.done_transition_id == ""
    # names 가 비어 있으면 미주입으로 취급 → 레거시 폴백 경로가 살아 있다.
    assert c.done_transition_names == list(DEFAULT_DONE_TRANSITION_NAMES)


# --- Jira Cloud 전용 의존성 표면화 -------------------------------------------


def test_search_jql_404_surfaces_cloud_only_dependency():
    """/rest/api/3/search/jql 부재(Server/DC 신호)는 원인을 지목하는 에러로 올린다."""
    def responder(method, url, kwargs):
        return FakeResponse(404, {"errorMessages": ["null for uri: .../search/jql"]})

    c = make_client(responder)
    with pytest.raises(JiraError) as exc:
        c.search_jql("project = PROJ")
    msg = str(exc.value)
    assert "Jira Cloud" in msg and "Server/Data Center" in msg
    assert exc.value.status_code == 404
    # Jira 가 한 말도 함께 싣는다(우리 해석이 원문을 지우지 않는다).
    assert "null for uri" in msg


def test_error_messages_extracts_what_jira_actually_said():
    """★ 404 해석의 근거 — Jira 는 본문에 이유를 **직접** 적어 준다."""
    from app.jira_client import error_messages

    exc = JiraError("GET → HTTP 404", status_code=404,
                    body={"errorMessages": ["키가 'HAN'인 프로젝트를 찾을 수 없습니다."],
                          "errors": {}})
    assert error_messages(exc) == "키가 'HAN'인 프로젝트를 찾을 수 없습니다."
    # errors 맵도 함께 싣는다(필드 단위 사유).
    exc2 = JiraError("POST → HTTP 400", status_code=400,
                     body={"errorMessages": [], "errors": {"customfield_10187": "필드 없음"}})
    assert error_messages(exc2) == "customfield_10187: 필드 없음"
    # ⚠️ 문자열 본문(HTML 로그인 페이지 등)은 'Jira 가 한 말'이 아니다 — 빈 문자열.
    assert error_messages(JiraError("x", status_code=404, body="<html>Log in</html>")) == ""
    assert error_messages(JiraError("연결 끊김")) == ""


def test_get_project_probes_project_existence():
    """★ JQL 이 못 하는 질문 — '이 프로젝트 키가 이 사이트에 있는가'."""
    seen = []

    def responder(method, url, kwargs):
        seen.append((method, url))
        return FakeResponse(200, {"key": "PROJ", "name": "프로젝트"})

    assert make_client(responder).get_project("PROJ")["key"] == "PROJ"
    assert seen == [("GET", "https://your-org.atlassian.net/rest/api/3/project/PROJ")]


def test_get_project_404_keeps_the_jira_message():
    """없는 프로젝트의 404 는 **Jira 원문 그대로** 올라온다(Cloud 전용으로 덮지 않는다)."""
    def responder(method, url, kwargs):
        return FakeResponse(404, {"errorMessages": ["키가 'HAN'인 프로젝트를 찾을 수 없습니다."]})

    with pytest.raises(JiraError) as exc:
        make_client(responder).get_project("HAN")
    assert exc.value.status_code == 404
    assert "Server/Data Center" not in str(exc.value)
    from app.jira_client import error_messages
    assert "찾을 수 없습니다" in error_messages(exc.value)


def test_search_jql_html_response_raises_instead_of_empty_result():
    """JSON 이 아닌 응답(로그인/에러 HTML)을 빈 결과로 삼키지 않는다 — 최악의 실패 모드 차단."""
    def responder(method, url, kwargs):
        return FakeResponse(200, None, text="<html><body>Log in to Jira</body></html>")

    c = make_client(responder)
    with pytest.raises(JiraError) as exc:
        c.search_jql("project = PROJ")
    assert "Jira Cloud" in str(exc.value)


def test_search_jql_empty_result_is_still_ok():
    """진짜 '결과 0건'(issues 키가 있는 정상 응답)은 그대로 빈 목록을 돌려준다."""
    def responder(method, url, kwargs):
        return FakeResponse(200, {"issues": [], "isLast": True})

    c = make_client(responder)
    assert c.search_jql("project = PROJ") == {"issues": [], "total": 0}


def test_repr_does_not_leak_token():
    """토큰은 repr 에 절대 실리지 않는다(로그·예외에 객체가 섞여도 안전)."""
    c = JiraClient("https://your-org.atlassian.net", "you@example.com", "s3cr3t-token")
    assert "s3cr3t-token" not in repr(c)
    assert "token=***" in repr(c)


# ---------------------------------------------------------------------------
# 설치 시점 조회(discovery) — 부작용 없는 읽기
# ---------------------------------------------------------------------------


def test_list_fields_returns_the_array():
    def responder(method, url, kwargs):
        assert method == "GET" and url.endswith("/rest/api/3/field")
        return FakeResponse(200, [{"id": "duedate", "name": "Due date"}])

    assert make_client(responder).list_fields() == [{"id": "duedate", "name": "Due date"}]


def test_project_statuses_uses_the_project_key():
    seen = {}

    def responder(method, url, kwargs):
        seen["url"] = url
        return FakeResponse(200, [{"id": "1", "name": "작업", "statuses": []}])

    make_client(responder).project_statuses("ACME")
    assert seen["url"].endswith("/rest/api/3/project/ACME/statuses")


def test_array_endpoint_refuses_a_non_array_payload():
    """Server/DC·로그인 리다이렉트를 '필드가 없다'로 오인하지 않는다(조용한 실패 금지)."""
    def responder(method, url, kwargs):
        return FakeResponse(200, None, text="<html>login</html>")

    with pytest.raises(JiraError) as exc:
        make_client(responder).list_fields()
    assert "Jira Cloud 전용" in str(exc.value)


def test_list_labels_follows_start_at_pagination():
    pages = [
        {"values": ["a", "b"], "total": 3, "isLast": False, "startAt": 0},
        {"values": ["c"], "total": 3, "isLast": True, "startAt": 2},
    ]
    seen = []

    def responder(method, url, kwargs):
        seen.append(kwargs.get("params", {}).get("startAt"))
        return FakeResponse(200, pages[len(seen) - 1])

    out = make_client(responder).list_labels()
    assert out == {"labels": ["a", "b", "c"], "total": 3, "truncated": False}
    assert seen == [0, 2]


def test_list_labels_stops_and_reports_truncation():
    """라벨이 수만 개인 인스턴스가 있다 — 무한히 긁지 않고 잘렸다고 말한다."""
    def responder(method, url, kwargs):
        return FakeResponse(200, {"values": ["x"], "total": 999999, "isLast": False})

    out = make_client(responder).list_labels(max_pages=3)
    assert out["truncated"] is True and len(out["labels"]) == 3
