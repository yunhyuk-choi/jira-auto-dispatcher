"""notify 단위테스트 — provider 어댑터(페이로드 모양·게이팅)와 시크릿 미노출.

라이브 네트워크는 호출하지 않는다(HTTP는 대역 주입).

⚠️ 이 파일이 다루는 표면은 **발송 관문**(:func:`app.notify.send_text`) 하나다. 예전에
있던 워커 잡-종료 통지자(``notify_job_end``/``build_message``/``format_mention``)는
레거시 old-path 은퇴로 제거됐다 — 완료 통지는 프랙탈 센트럴 세션이 ``notify_report.py``
로 단일 발송한다(완료 티켓당 채팅 2건이던 이중-통지의 근본 제거). 그 통지자에 매달려
있던 메시지-조립 테스트는 함께 지웠고, **provider 어댑터 계약**(어떤 채널로 어떤 모양이
나가는가)을 덮던 테스트는 살아남은 관문으로 옮겼다.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from app import notify as N


# --- 대역 --------------------------------------------------------------------


class FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code


class FakeHTTP:
    def __init__(self, status_code=200, raise_exc=None):
        self.status_code = status_code
        self.raise_exc = raise_exc
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self.raise_exc is not None:
            raise self.raise_exc
        return FakeResp(self.status_code)


def _write_webhook(base, ref, url):
    path = os.path.join(base, ref)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(url)


def _legacy_config(tmp_path, *, enabled=True, webhook_ref="svc/google-chat-webhook",
                   webhook_url=None):
    """``notifier`` 섹션이 없던 **옛 설정** 대역(레거시 ``notify`` 만 보유)."""
    base = str(tmp_path / "secrets")
    if webhook_ref and webhook_url is not None:
        _write_webhook(base, webhook_ref, webhook_url)
    return SimpleNamespace(
        notify=SimpleNamespace(enabled=enabled, webhook_ref=webhook_ref),
        secrets=SimpleNamespace(base_dir=base),
    )


def _cfg_provider(tmp_path, provider, *, webhook_ref="svc/webhook", webhook_url=None,
                  legacy_enabled=None):
    """notifier(정본) + notify(레거시 미러)를 함께 갖춘 설정 대역."""
    base = str(tmp_path / "secrets")
    if webhook_ref and webhook_url is not None:
        _write_webhook(base, webhook_ref, webhook_url)
    enabled = (provider != "none") if legacy_enabled is None else legacy_enabled
    return SimpleNamespace(
        notifier=SimpleNamespace(provider=provider, webhook_ref=webhook_ref),
        notify=SimpleNamespace(enabled=enabled, webhook_ref=webhook_ref),
        secrets=SimpleNamespace(base_dir=base),
    )


# --- provider 해석(정본 notifier ← 레거시 notify 미러) -----------------------


def test_resolve_provider_prefers_notifier_over_legacy_mirror(tmp_path):
    cfg = _cfg_provider(tmp_path, "slack")
    assert N.resolve_provider(cfg) == "slack"


def test_resolve_provider_derives_google_chat_from_legacy_enabled(tmp_path):
    """notifier 섹션이 없는 옛 설정: notify.enabled=True 는 그 시절 의미(google_chat)."""
    assert N.resolve_provider(_legacy_config(tmp_path, enabled=True)) == "google_chat"
    assert N.resolve_provider(_legacy_config(tmp_path, enabled=False)) == "none"


def test_resolve_provider_defaults_to_none():
    assert N.resolve_provider(SimpleNamespace()) == "none"


# --- 페이로드 모양 --------------------------------------------------------------


def test_build_payload_text_shape_for_chat_providers():
    assert N.build_payload("본문", provider="google_chat") == {"text": "본문"}
    assert N.build_payload("본문", provider="slack") == {"text": "본문"}


def test_build_payload_generic_has_documented_keys():
    """generic_webhook 은 문서화된 **고정 스키마**를 보낸다(키는 항상 존재)."""
    p = N.build_payload("본문", provider="generic_webhook", ticket="PROJ-1",
                        status="done", mention_id="U777", event="report")
    assert p == {
        "source": "jira-auto-dispatcher",
        "event": "report",
        "ticket": "PROJ-1",
        "status": "done",
        "mention_id": "U777",
        "text": "본문",
    }
    # 모르는 값은 빈 문자열 — 수신기가 키 부재를 다루지 않아도 되게.
    empty = N.build_payload("본문", provider="generic_webhook")
    assert empty["ticket"] == "" and empty["status"] == "" and empty["mention_id"] == ""
    assert empty["event"] == "job_end"


def test_build_payload_rejects_unknown_provider():
    with pytest.raises(ValueError):
        N.build_payload("본문", provider="teams")


# --- 발송 관문(send_text): provider 분기 ---------------------------------------
#
# cfg.notifier.provider 가 정본이고 cfg.notify.* 는 하위호환 미러다. 아래는 provider 를
# 바꿨을 때 **페이로드 모양이 실제로 갈라지는지**, 그리고 알 수 없는 값이면 **아무것도
# 나가지 않는지**를 못 박는다(잘못된 모양이 남의 채널로 나가는 것 차단).


def test_send_text_slack_uses_minimal_text_shape(tmp_path):
    """⚠️ 회귀 방지: provider=slack 인데 다른 채널 페이로드가 나가면 안 된다."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "slack", webhook_url="https://hooks.slack.example/T/B/X")
    assert N.send_text(cfg, "완료-리포트 본문", ticket="PROJ-1", http=http) is True
    assert set(http.posts[0]["json"]) == {"text"}   # Slack incoming webhook 최소 형태
    assert http.posts[0]["json"]["text"] == "완료-리포트 본문"


def test_send_text_generic_webhook_posts_structured_payload(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "generic_webhook", webhook_url="https://hooks.example/in")
    sent = N.send_text(cfg, "완료-리포트 본문", ticket="PROJ-2", status="done",
                       mention_id="U777", event="report", http=http)
    assert sent is True
    body = http.posts[0]["json"]
    assert body["source"] == "jira-auto-dispatcher"
    assert body["event"] == "report"
    assert body["ticket"] == "PROJ-2"
    assert body["status"] == "done"
    assert body["mention_id"] == "U777"
    assert body["text"] == "완료-리포트 본문"


def test_send_text_google_chat_keeps_todays_behavior(tmp_path):
    """provider=google_chat 은 현행 동작 그대로(text 한 필드)."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url="https://chat.example/hook")
    assert N.send_text(cfg, "본문", http=http) is True
    assert set(http.posts[0]["json"]) == {"text"}


def test_send_text_none_provider_is_silent_noop(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "none", webhook_url="https://chat.example/hook")
    assert N.send_text(cfg, "본문", http=http) is False
    assert http.posts == []


def test_send_text_unknown_provider_refuses_to_send(tmp_path, caplog):
    """알 수 없는 provider 는 **발송하지 않고** 경고를 남긴다(오발송 < 미발송)."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "teams", webhook_url="https://chat.example/hook")
    with caplog.at_level("WARNING", logger="jad.notify"):
        assert N.send_text(cfg, "본문", http=http) is False
    assert http.posts == []                          # 아무것도 나가지 않는다
    assert any("provider" in r.getMessage() for r in caplog.records)


def test_send_text_skips_empty_body(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "slack", webhook_url="https://hooks.slack.example/T/B/X")
    assert N.send_text(cfg, "   ", http=http) is False
    assert http.posts == []


def test_send_text_missing_webhook_ref_returns_false(tmp_path):
    """provider 는 켜져 있는데 웹훅 파일이 없으면 조용히 False(발송 생략)."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url=None)   # 파일 미생성
    assert N.send_text(cfg, "본문", http=http) is False
    assert http.posts == []


def test_send_text_uses_http_factory_when_no_http(tmp_path):
    """http 미주입 시 http_factory로 클라이언트를 만든다(라이브 requests 미호출)."""
    made = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url="https://chat.example/hook")
    assert N.send_text(cfg, "본문", http_factory=lambda: made) is True
    assert len(made.posts) == 1


def test_notifier_webhook_ref_wins_over_legacy_mirror(tmp_path):
    """정본(notifier)과 레거시(notify) 참조가 다르면 정본이 이긴다."""
    base = str(tmp_path / "secrets")
    _write_webhook(base, "svc/new", "https://new.example/hook")
    _write_webhook(base, "svc/old", "https://old.example/hook")
    cfg = SimpleNamespace(
        notifier=SimpleNamespace(provider="slack", webhook_ref="svc/new"),
        notify=SimpleNamespace(enabled=True, webhook_ref="svc/old"),
        secrets=SimpleNamespace(base_dir=base),
    )
    http = FakeHTTP()
    assert N.send_text(cfg, "본문", http=http) is True
    assert http.posts[0]["url"] == "https://new.example/hook"


# --- 시크릿 미노출 -------------------------------------------------------------


def test_webhook_url_never_leaks_into_body_or_logs(tmp_path, caplog):
    """시크릿 미노출: 웹훅 URL(시크릿)은 본문에도 로그에도 섞이지 않는다."""
    http = FakeHTTP()
    webhook = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=TOP-SECRET-KEY"
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url=webhook)
    with caplog.at_level("DEBUG", logger="jad.notify"):
        assert N.send_text(cfg, "본문", ticket="PROJ-1", http=http) is True
    assert "TOP-SECRET-KEY" not in http.posts[0]["json"]["text"]
    assert "TOP-SECRET-KEY" not in caplog.text


# --- 사용자 식별자 일반화(레지스트리 하위호환) --------------------------------


def test_registry_record_accepts_either_user_id_key():
    """레지스트리는 신규 notify_user_id 를 쓰되 옛 google_chat_user_id 도 계속 읽는다.

    이 값은 알림 채널의 사용자 id(멘션용)이며, 워커 에이전트에게
    ``DISPATCH_NOTIFY_USER_ID`` 로 실려 나간다(:mod:`app.agent_runner`).
    """
    from app.registry import UserRecord

    new = UserRecord.from_dict({"username": "u1", "notify_user_id": "U1"})
    assert new.notify_user_id == "U1"
    assert new.google_chat_user_id == "U1"      # 레거시 미러(옛 키를 읽는 코드용)

    old = UserRecord.from_dict({"username": "u1", "google_chat_user_id": "G1"})
    assert old.notify_user_id == "G1"           # 옛 registry.json 그대로 동작

    none = UserRecord.from_dict({"username": "u1"})
    assert none.notify_user_id == "" and none.google_chat_user_id == ""
