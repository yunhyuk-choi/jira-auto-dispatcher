"""POST /webhook/jira 수신 라우트 테스트 — 인증(503/401)·키추출·비동기 202.

라이브 Jira/Docker/claude/네트워크는 호출하지 않는다. 실제 디스패치(trigger_ticket)는
페이크로 치환해 데몬 스레드가 claude를 부르지 않게 한다(호출 인자만 검증).
"""

from __future__ import annotations

import textwrap
import threading

import pytest

from app import main
from app.main import create_central_app

_SECRET = "s3cr3t-token"


def _write_config(tmp_path, *, webhook_enabled=True, write_secret=True):
    """create_central_app용 최소 config.yaml + (옵션) 웹훅 시크릿 파일을 쓴다."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    # 감시 토큰(build_central_components가 읽음).
    (secrets_dir / "service").mkdir()
    (secrets_dir / "service" / "jira-token").write_text("watcher-tok", encoding="utf-8")
    if write_secret:
        (secrets_dir / "service" / "jira-webhook").write_text(_SECRET, encoding="utf-8")

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        textwrap.dedent(
            f"""
            role: central
            server: {{ host: 0.0.0.0, port: 8787 }}
            jira:
              base_url: https://example.atlassian.net
              project: PROJ
              poll_interval_sec: 60
              watcher_token_file: service/jira-token
            match: {{ statuses: ["해야 할 일"] }}
            webhook: {{ enabled: {str(webhook_enabled).lower()}, secret_ref: service/jira-webhook }}
            secrets: {{ base_dir: "{secrets_dir.as_posix()}" }}
            run: {{ worker_max_concurrency: 64 }}
            """
        ).strip(),
        encoding="utf-8",
    )
    return str(cfg)


class _RecordingTrigger:
    """poller.trigger_ticket 대체 — 호출 키를 기록하고 Event로 완료를 알린다."""

    def __init__(self):
        self.keys = []
        self.events = []
        self.called = threading.Event()

    def __call__(self, key, event=None):
        self.keys.append(key)
        self.events.append(event)
        self.called.set()
        return True


def _build(tmp_path, isolated_state, **kw):
    """central 앱 + 테스트 클라이언트 + trigger 레코더(주입)."""
    config_path = _write_config(tmp_path, **kw)
    app = create_central_app(config_path)
    app.config.update(TESTING=True)
    rec = _RecordingTrigger()
    # 라우트 클로저가 잡은 poller와 동일 인스턴스의 메서드를 치환.
    main._components["poller"].trigger_ticket = rec
    return app.test_client(), rec


def test_webhook_no_secret_configured_503(tmp_path, isolated_state):
    client, _ = _build(tmp_path, isolated_state, write_secret=False)
    res = client.post("/webhook/jira", json={"issueKey": "PROJ-1"})
    assert res.status_code == 503
    assert res.get_json() == {"error": "webhook secret not configured"}


def test_webhook_missing_token_401(tmp_path, isolated_state):
    client, rec = _build(tmp_path, isolated_state)
    res = client.post("/webhook/jira", json={"issueKey": "PROJ-1"})
    assert res.status_code == 401
    assert res.get_json() == {"error": "unauthorized"}
    assert rec.keys == []                      # 디스패치 미시작


def test_webhook_wrong_token_401(tmp_path, isolated_state):
    client, rec = _build(tmp_path, isolated_state)
    res = client.post(
        "/webhook/jira",
        headers={"X-Jira-Webhook-Token": "nope"},
        json={"issueKey": "PROJ-1"},
    )
    assert res.status_code == 401
    assert rec.keys == []


def test_webhook_custom_payload_202_and_triggers(tmp_path, isolated_state):
    client, rec = _build(tmp_path, isolated_state)
    res = client.post(
        "/webhook/jira",
        headers={"X-Jira-Webhook-Token": _SECRET},
        json={"issueKey": "PROJ-1"},
    )
    assert res.status_code == 202
    assert res.get_json() == {"accepted": True, "ticket": "PROJ-1"}
    assert rec.called.wait(timeout=5) is True   # 데몬 스레드가 trigger_ticket 호출
    assert rec.keys == ["PROJ-1"]


def test_webhook_standard_payload_via_header_token_202(tmp_path, isolated_state):
    client, rec = _build(tmp_path, isolated_state)
    # 표준 Jira 웹훅 페이로드({"issue": {"key": ...}}) + 헤더 토큰.
    res = client.post(
        "/webhook/jira",
        headers={"X-Jira-Webhook-Token": _SECRET},
        json={"issue": {"key": "PROJ-2"}},
    )
    assert res.status_code == 202
    assert res.get_json() == {"accepted": True, "ticket": "PROJ-2"}
    assert rec.called.wait(timeout=5) is True
    assert rec.keys == ["PROJ-2"]


def test_webhook_query_token_rejected_401(tmp_path, isolated_state):
    # 쿼리 파라미터 토큰은 access 로그 유출 표면이라 더 이상 수용하지 않는다(헤더 전용).
    client, rec = _build(tmp_path, isolated_state)
    res = client.post(
        "/webhook/jira?token=" + _SECRET,
        json={"issue": {"key": "PROJ-2"}},
    )
    assert res.status_code == 401
    assert res.get_json() == {"error": "unauthorized"}
    assert rec.keys == []                      # 디스패치 미시작


def test_webhook_passes_event_string_for_logging(tmp_path, isolated_state):
    """이벤트 문자열(webhookEvent)은 판단이 아니라 로깅용으로 trigger에 전달된다."""
    client, rec = _build(tmp_path, isolated_state)
    res = client.post(
        "/webhook/jira",
        headers={"X-Jira-Webhook-Token": _SECRET},
        json={"issue": {"key": "PROJ-9"}, "webhookEvent": "jira:issue_updated"},
    )
    assert res.status_code == 202
    assert rec.called.wait(timeout=5) is True
    assert rec.keys == ["PROJ-9"]
    assert rec.events == ["jira:issue_updated"]


def test_webhook_no_issue_key_400(tmp_path, isolated_state):
    client, rec = _build(tmp_path, isolated_state)
    res = client.post(
        "/webhook/jira",
        headers={"X-Jira-Webhook-Token": _SECRET},
        json={"foo": "bar"},
    )
    assert res.status_code == 400
    assert res.get_json() == {"error": "no issue key"}
    assert rec.keys == []


def test_webhook_route_absent_when_disabled(tmp_path, isolated_state):
    """webhook.enabled=false면 라우트를 배선하지 않는다(404)."""
    client, _ = _build(tmp_path, isolated_state, webhook_enabled=False)
    res = client.post("/webhook/jira", json={"issueKey": "PROJ-1"})
    assert res.status_code == 404
