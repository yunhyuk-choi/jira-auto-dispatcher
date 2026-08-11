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
