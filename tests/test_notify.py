"""notify 단위테스트 — 메시지 조립(A/B 분기·멘션), best-effort 격리, 시크릿 미노출.

라이브 네트워크는 호출하지 않는다(HTTP는 대역 주입). notify_job_end는 어떤
예외도 삼켜 런을 죽이지 않아야 한다(best-effort 계약).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from app import agent_runner as ar
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


def _result(status=ar.STATUS_DONE, final_text="마무리합니다.", mr_url=None, reset_at=None):
    return ar.AgentResult(
        status=status, final_text=final_text, mr_url=mr_url, reset_at=reset_at,
    )


def _creds(gcid="", git_name="Choi", user="testuser"):
    return ar.UserCreds(user=user, git_name=git_name, google_chat_user_id=gcid)


def _config(tmp_path, *, enabled=True, webhook_ref="svc/google-chat-webhook",
            notify_interrupted=True, notify_cancelled=True, webhook_url=None):
    base = str(tmp_path / "secrets")
    if webhook_ref and webhook_url is not None:
        path = os.path.join(base, webhook_ref)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(webhook_url)
    return SimpleNamespace(
        notify=SimpleNamespace(
            enabled=enabled, webhook_ref=webhook_ref,
            notify_interrupted=notify_interrupted, notify_cancelled=notify_cancelled,
        ),
        secrets=SimpleNamespace(base_dir=base),
    )


# --- format_mention ----------------------------------------------------------


def test_format_mention_with_user_id():
    assert N.format_mention("123456", "Choi") == "<users/123456>"


def test_format_mention_without_user_id_falls_back_to_name():
    m = N.format_mention("", "Choi")
    assert m == "Choi"
    assert "<users/" not in m


def test_format_mention_empty_both():
    assert N.format_mention("", "") == ""


# --- build_message A/B 분기 --------------------------------------------------


def test_build_message_mode_a_has_mr_and_review_next_step():
    job = {"ticket": "PROJ-1", "autonomy_mode": "A"}
    res = _result(mr_url="https://gitlab.example.com/g/p/-/merge_requests/7")
    msg = N.build_message(result=res, job=job, creds=_creds(gcid="999"))
    assert "<users/999>" in msg               # 멘션
    assert "PROJ-1" in msg
    assert "https://gitlab.example.com/g/p/-/merge_requests/7" in msg  # MR URL 그대로
    assert "MR을 리뷰·머지" in msg             # A 다음 할 일
    assert "마무리합니다." in msg              # 마무리 멘트
    # A에는 브랜치 fetch 안내가 없다.
    assert "이어서 완성" not in msg


def test_build_message_mode_b_has_branch_journal_and_fetch_next_step():
    job = {"ticket": "PROJ-2", "autonomy_mode": "B"}
    res = _result(final_text="1차 산출 남김")
    msg = N.build_message(result=res, job=job, creds=_creds(gcid="", git_name="Choi"))
    # userId 없음 → 이름만(하드 핑 없음).
    assert "Choi" in msg and "<users/" not in msg
    assert "auto/PROJ-2" in msg                # 브랜치명
    assert "runs/PROJ-2/" in msg               # 저널 위치
    assert "이어서 완성" in msg                # B 다음 할 일
    assert "1차 산출 남김" in msg
    # B에는 MR 리뷰·머지 안내가 없다.
    assert "MR을 리뷰·머지" not in msg


def test_build_message_interrupted_is_short_with_reset():
    job = {"ticket": "PROJ-3", "autonomy_mode": "B"}
    res = _result(status=ar.STATUS_INTERRUPTED, reset_at="2099-01-01T00:00:00Z")
    msg = N.build_message(result=res, job=job, creds=_creds())
    assert "재개 예정" in msg
    assert "2099-01-01T00:00:00Z" in msg
    # 짧은 알림 — 다음 할 일/브랜치 상세 없음.
    assert "이어서 완성" not in msg


def test_build_message_cancelled_is_short():
    job = {"ticket": "PROJ-4", "autonomy_mode": "A"}
    res = _result(status=ar.STATUS_CANCELLED)
    msg = N.build_message(result=res, job=job, creds=_creds())
    assert "취소됨" in msg
    assert "롤백" in msg


# --- 사이클로그 핸드오프 라인(Phase 3a) --------------------------------------


def test_build_message_appends_cycle_log_line_from_job():
    # central이 커밋한 사이클로그 상대경로(job에 스탬프됨) → 고정 라인으로 append.
    job = {"ticket": "PROJ-9", "autonomy_mode": "B",
           "cycle_log_path": "n/cycles/2026-08-19-abc"}
    res = _result(final_text="1차 산출")
    msg = N.build_message(result=res, job=job, creds=_creds())
    assert "사이클로그: n/cycles/2026-08-19-abc" in msg
    # 자유 서술(마무리 멘트)은 위에 그대로 남는다.
    assert "1차 산출" in msg


def test_build_message_appends_branch_ref_when_non_master():
    # 작업이 master가 아닌 브랜치(auto/<ticket>)면 브랜치 ref도 함께.
    job = {"ticket": "PROJ-9", "autonomy_mode": "B", "branch": "auto/PROJ-9",
           "cycle_log_path": "n/cycles/C1"}
    msg = N.build_message(result=_result(), job=job, creds=_creds())
    assert "사이클로그: n/cycles/C1" in msg
    assert "브랜치: auto/PROJ-9" in msg


def test_build_message_no_cycle_log_line_when_absent():
    # 경로가 없으면(초기/실패) 사이클로그 라인을 붙이지 않는다(회귀 안전).
    job = {"ticket": "PROJ-9", "autonomy_mode": "B"}
    msg = N.build_message(result=_result(), job=job, creds=_creds())
    assert "사이클로그:" not in msg


def test_build_message_cycle_log_from_result_attr():
    # result에 직접 실려도 읽는다(우선순위: result → job).
    res = _result()
    res.cycle_log_path = "n/cycles/FROM-RESULT"
    job = {"ticket": "PROJ-9", "autonomy_mode": "A"}
    msg = N.build_message(result=res, job=job, creds=_creds())
    assert "사이클로그: n/cycles/FROM-RESULT" in msg


# --- notify_job_end: 발송/게이팅/best-effort/시크릿 -------------------------


def test_notify_job_end_disabled_returns_false_no_post(tmp_path):
    http = FakeHTTP()
    cfg = _config(tmp_path, enabled=False, webhook_url="https://chat.example/hook")
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(), http=http)
    assert sent is False
    assert http.posts == []


def test_notify_job_end_posts_text_and_returns_true(tmp_path):
    http = FakeHTTP(status_code=200)
    webhook = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRET&token=SECRET2"
    cfg = _config(tmp_path, webhook_url=webhook)
    job = {"ticket": "PROJ-1", "autonomy_mode": "B"}
    sent = N.notify_job_end(cfg, _result(), job, _creds(gcid="42"), http=http)
    assert sent is True
    assert len(http.posts) == 1
    post = http.posts[0]
    assert post["url"] == webhook               # 웹훅 URL로 POST
    assert "text" in post["json"]               # 단순 text 메시지
    assert "auto/PROJ-1" in post["json"]["text"]


def test_notify_job_end_best_effort_swallows_exception(tmp_path):
    """웹훅 POST가 예외를 던져도 notify_job_end는 예외를 전파하지 않고 False."""
    http = FakeHTTP(raise_exc=RuntimeError("network down"))
    cfg = _config(tmp_path, webhook_url="https://chat.example/hook")
    # 예외가 전파되면 이 테스트가 실패한다.
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(), http=http)
    assert sent is False


def test_notify_job_end_missing_webhook_ref_returns_false(tmp_path):
    """enabled인데 웹훅 파일이 없으면 조용히 False(발송 생략)."""
    http = FakeHTTP()
    cfg = _config(tmp_path, webhook_url=None)  # 파일 미생성
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(), http=http)
    assert sent is False
    assert http.posts == []


def test_notify_job_end_interrupted_gating(tmp_path):
    http = FakeHTTP()
    cfg = _config(tmp_path, notify_interrupted=False, webhook_url="https://chat.example/hook")
    res = _result(status=ar.STATUS_INTERRUPTED, reset_at="2099-01-01T00:00:00Z")
    sent = N.notify_job_end(cfg, res, {"ticket": "PROJ-1", "autonomy_mode": "B"},
                            _creds(), http=http)
    assert sent is False
    assert http.posts == []


def test_notify_job_end_cancelled_gating(tmp_path):
    http = FakeHTTP()
    cfg = _config(tmp_path, notify_cancelled=False, webhook_url="https://chat.example/hook")
    res = _result(status=ar.STATUS_CANCELLED)
    sent = N.notify_job_end(cfg, res, {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(), http=http)
    assert sent is False
    assert http.posts == []


def test_notify_message_does_not_contain_webhook_secret(tmp_path):
    """시크릿 미노출: 웹훅 URL(시크릿)이 메시지 본문(text)에 절대 섞이지 않는다."""
    http = FakeHTTP()
    webhook = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=TOP-SECRET-KEY"
    cfg = _config(tmp_path, webhook_url=webhook)
    N.notify_job_end(cfg, _result(final_text="ok"), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                     _creds(), http=http)
    text = http.posts[0]["json"]["text"]
    assert "TOP-SECRET-KEY" not in text
    assert webhook not in text


def test_notify_job_end_uses_http_factory_when_no_http(tmp_path):
    """http 미주입 시 http_factory로 클라이언트를 만든다(라이브 requests 미호출)."""
    made = FakeHTTP()
    cfg = _config(tmp_path, webhook_url="https://chat.example/hook")
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(), http_factory=lambda: made)
    assert sent is True
    assert len(made.posts) == 1


# --- provider 어댑터(프레임워크화) -------------------------------------------
#
# cfg.notifier.provider 가 정본이고 cfg.notify.* 는 하위호환 미러다. 아래는 provider 를
# 바꿨을 때 **페이로드·멘션 문법이 실제로 갈라지는지**, 그리고 알 수 없는 값이면
# **아무것도 나가지 않는지**를 못 박는다(잘못된 모양이 남의 채널로 나가는 것 차단).


def _cfg_provider(tmp_path, provider, *, webhook_ref="svc/webhook", webhook_url=None,
                  notify_interrupted=True, notify_cancelled=True, legacy_enabled=None):
    """notifier(정본) + notify(레거시 미러)를 함께 갖춘 설정 대역."""
    base = str(tmp_path / "secrets")
    if webhook_ref and webhook_url is not None:
        path = os.path.join(base, webhook_ref)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(webhook_url)
    enabled = (provider != "none") if legacy_enabled is None else legacy_enabled
    return SimpleNamespace(
        notifier=SimpleNamespace(
            provider=provider, webhook_ref=webhook_ref,
            notify_interrupted=notify_interrupted, notify_cancelled=notify_cancelled,
        ),
        notify=SimpleNamespace(
            enabled=enabled, webhook_ref=webhook_ref,
            notify_interrupted=notify_interrupted, notify_cancelled=notify_cancelled,
        ),
        secrets=SimpleNamespace(base_dir=base),
    )


def test_resolve_provider_prefers_notifier_over_legacy_mirror(tmp_path):
    cfg = _cfg_provider(tmp_path, "slack")
    assert N.resolve_provider(cfg) == "slack"


def test_resolve_provider_derives_google_chat_from_legacy_enabled(tmp_path):
    """notifier 섹션이 없는 옛 설정: notify.enabled=True 는 그 시절 의미(google_chat)."""
    cfg = _config(tmp_path, enabled=True)          # notify 만 있는 대역
    assert N.resolve_provider(cfg) == "google_chat"
    assert N.resolve_provider(_config(tmp_path, enabled=False)) == "none"


def test_resolve_provider_defaults_to_none():
    assert N.resolve_provider(SimpleNamespace()) == "none"


# --- 멘션 문법 -----------------------------------------------------------------


def test_format_mention_slack_syntax():
    assert N.format_mention("U123", "Choi", "slack") == "<@U123>"


def test_format_mention_generic_does_not_invent_syntax():
    """모르는 채널에 문법을 지어내지 않는다 — 사람이 읽는 이름만(기계용 id는 페이로드로)."""
    assert N.format_mention("U123", "Choi", "generic_webhook") == "Choi"
    assert N.format_mention("U123", "", "generic_webhook") == "U123"
    assert "<users/" not in N.format_mention("U123", "Choi", "generic_webhook")


def test_notify_user_id_prefers_neutral_name_and_falls_back():
    """provider 중립 notify_user_id 가 정본, 레거시 google_chat_user_id 도 계속 읽는다."""
    assert N.notify_user_id(SimpleNamespace(notify_user_id="U1", google_chat_user_id="G1")) == "U1"
    assert N.notify_user_id(SimpleNamespace(google_chat_user_id="G1")) == "G1"
    assert N.notify_user_id(SimpleNamespace()) == ""


# --- 페이로드 모양 --------------------------------------------------------------


def test_build_payload_text_shape_for_chat_providers():
    assert N.build_payload("본문", provider="google_chat") == {"text": "본문"}
    assert N.build_payload("본문", provider="slack") == {"text": "본문"}


def test_build_payload_generic_has_documented_keys():
    """generic_webhook 계약: 키는 항상 존재하고 모르는 값은 ""(수신 측이 파싱 가능)."""
    p = N.build_payload("본문", provider="generic_webhook", ticket="PROJ-1",
                        status="done", mention_id="U9")
    assert p == {
        "source": "jira-auto-dispatcher",
        "event": "job_end",
        "ticket": "PROJ-1",
        "status": "done",
        "mention_id": "U9",
        "text": "본문",
    }
    empty = N.build_payload("본문", provider="generic_webhook")
    assert set(empty) == set(p)                     # 키 집합은 항상 같다
    assert empty["ticket"] == "" and empty["status"] == "" and empty["mention_id"] == ""


def test_build_payload_rejects_unknown_provider():
    try:
        N.build_payload("본문", provider="teams")
    except ValueError:
        pass
    else:                                            # pragma: no cover — 실패 시에만
        raise AssertionError("알 수 없는 provider 는 페이로드를 만들면 안 된다")


# --- 발송: provider 분기 -------------------------------------------------------


def test_notify_job_end_slack_uses_slack_mention_not_google_chat(tmp_path):
    """⚠️ 회귀 방지: provider=slack 인데 Google Chat 페이로드/문법이 나가면 안 된다."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "slack", webhook_url="https://hooks.slack.example/T/B/X")
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "B"},
                            _creds(gcid="U777"), http=http)
    assert sent is True
    text = http.posts[0]["json"]["text"]
    assert "<@U777>" in text
    assert "<users/" not in text                     # Google Chat 문법 미사용
    assert set(http.posts[0]["json"]) == {"text"}    # Slack incoming webhook 최소 형태


def test_notify_job_end_generic_webhook_posts_structured_payload(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "generic_webhook", webhook_url="https://hooks.example/in")
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-2", "autonomy_mode": "B"},
                            _creds(gcid="U777"), http=http)
    assert sent is True
    body = http.posts[0]["json"]
    assert body["source"] == "jira-auto-dispatcher"
    assert body["event"] == "job_end"
    assert body["ticket"] == "PROJ-2"
    assert body["status"] == ar.STATUS_DONE
    assert body["mention_id"] == "U777"
    assert "auto/PROJ-2" in body["text"]
    assert "<users/" not in body["text"] and "<@" not in body["text"]


def test_notify_job_end_none_provider_is_silent_noop(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "none", webhook_url="https://chat.example/hook")
    assert N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1"}, _creds(), http=http) is False
    assert http.posts == []


def test_notify_job_end_unknown_provider_refuses_to_send(tmp_path, caplog):
    """알 수 없는 provider 는 **발송하지 않고** 경고를 남긴다(오발송 < 미발송)."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "teams", webhook_url="https://chat.example/hook")
    with caplog.at_level("WARNING", logger="jad.notify"):
        sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                                _creds(), http=http)
    assert sent is False
    assert http.posts == []                          # 아무것도 나가지 않는다
    assert any("provider" in r.getMessage() for r in caplog.records)


def test_notify_job_end_google_chat_provider_keeps_todays_behavior(tmp_path):
    """provider=google_chat 은 현행 동작 그대로(text + <users/{id}>)."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url="https://chat.example/hook")
    sent = N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1", "autonomy_mode": "A"},
                            _creds(gcid="42"), http=http)
    assert sent is True
    assert set(http.posts[0]["json"]) == {"text"}
    assert "<users/42>" in http.posts[0]["json"]["text"]


def test_notify_job_end_reads_neutral_notify_user_id(tmp_path):
    """레지스트리/creds 가 provider 중립 필드만 채워도 멘션이 동작한다."""
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "google_chat", webhook_url="https://chat.example/hook")
    creds = SimpleNamespace(user="testuser", git_name="Choi", notify_user_id="55")
    assert N.notify_job_end(cfg, _result(), {"ticket": "PROJ-1"}, creds, http=http) is True
    assert "<users/55>" in http.posts[0]["json"]["text"]


def test_send_text_skips_empty_body(tmp_path):
    http = FakeHTTP()
    cfg = _cfg_provider(tmp_path, "slack", webhook_url="https://hooks.slack.example/T/B/X")
    assert N.send_text(cfg, "   ", http=http) is False
    assert http.posts == []


def test_notifier_webhook_ref_wins_over_legacy_mirror(tmp_path):
    """정본(notifier)과 레거시(notify) 참조가 다르면 정본이 이긴다."""
    base = str(tmp_path / "secrets")
    os.makedirs(os.path.join(base, "svc"), exist_ok=True)
    with open(os.path.join(base, "svc", "new"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("https://new.example/hook")
    with open(os.path.join(base, "svc", "old"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("https://old.example/hook")
    cfg = SimpleNamespace(
        notifier=SimpleNamespace(provider="slack", webhook_ref="svc/new"),
        notify=SimpleNamespace(enabled=True, webhook_ref="svc/old"),
        secrets=SimpleNamespace(base_dir=base),
    )
    http = FakeHTTP()
    assert N.send_text(cfg, "본문", http=http) is True
    assert http.posts[0]["url"] == "https://new.example/hook"


# --- 사용자 식별자 일반화(레지스트리 하위호환) --------------------------------


def test_registry_record_accepts_either_user_id_key():
    """레지스트리는 신규 notify_user_id 를 쓰되 옛 google_chat_user_id 도 계속 읽는다."""
    from app.registry import UserRecord

    new = UserRecord.from_dict({"username": "u1", "notify_user_id": "U1"})
    assert new.notify_user_id == "U1"
    assert new.google_chat_user_id == "U1"      # 레거시 미러(옛 키를 읽는 코드용)
    assert N.notify_user_id(new) == "U1"

    old = UserRecord.from_dict({"username": "u1", "google_chat_user_id": "G1"})
    assert old.notify_user_id == "G1"           # 옛 registry.json 그대로 동작
    assert N.notify_user_id(old) == "G1"

    none = UserRecord.from_dict({"username": "u1"})
    assert none.notify_user_id == "" and none.google_chat_user_id == ""
