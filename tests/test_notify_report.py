"""notify_report.py 단위테스트 — 완료-리포트를 설정된 알림 채널로 발송(프랙탈 P2, 설계 §3.1·§6.4).

라이브 웹훅은 절대 치지 않는다(HTTP 를 대역). notify_report 가 notify 의 provider 어댑터·
웹훅 조회/POST 를 **재사용**해 리포트를 발송하는지, 비활성/미설정/알 수 없는 provider 에서
조용히 생략하는지, 옛 이름(``notify_report.py``) 호출이 그대로 동작하는지 검증한다.
"""

from __future__ import annotations

from types import SimpleNamespace

import notify_report


def _cfg(enabled=True, webhook_ref="svc/webhook", base_dir="/secrets"):
    return SimpleNamespace(
        notify=SimpleNamespace(enabled=enabled, webhook_ref=webhook_ref),
        secrets=SimpleNamespace(base_dir=base_dir),
    )


class _FakeHTTP:
    """notify._post 가 쓰는 http 클라이언트 대역(POST 캡처)."""

    def __init__(self, code=200):
        self.calls = []
        self._code = code

    def post(self, url, json=None, timeout=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return SimpleNamespace(status_code=self._code)


# --- build_report_text(순수) --------------------------------------------------


def test_build_report_text_headers_ticket_and_keeps_body():
    body = "- 티켓: HAN-1 — 요지\n- MR: http://x"
    out = notify_report.build_report_text(body, ticket="HAN-1")
    assert out.startswith("[HAN-1] 오케스트레이터 완료-리포트")
    assert "요지" in out and "http://x" in out
    # 티켓 없으면 본문 그대로.
    assert notify_report.build_report_text(body) == body
    # 옛 이름도 같은 함수를 가리킨다(하위호환 별칭).
    assert notify_report.build_gchat_text is notify_report.build_report_text


# --- send_report: notify 재사용(웹훅 조회 + _post) ---------------------------


def test_send_report_reads_webhook_and_posts(monkeypatch):
    http = _FakeHTTP(code=200)
    # 웹훅 조회를 대역(secret 파일 없이도 URL 을 돌려주도록).
    monkeypatch.setattr(notify_report.notify, "_read_webhook", lambda config: "https://chat.example/hook")

    ok = notify_report.send_report(_cfg(enabled=True), "리치 완료-리포트 본문", ticket="HAN-7", http=http)

    assert ok is True
    assert len(http.calls) == 1
    call = http.calls[0]
    assert call["url"] == "https://chat.example/hook"
    # 리포트 본문이 그대로 실렸다(에이전트 리포트 품질 = 메시지 품질).
    assert "리치 완료-리포트 본문" in call["json"]["text"]
    assert call["json"]["text"].startswith("[HAN-7]")


def test_send_report_disabled_skips(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setattr(notify_report.notify, "_read_webhook", lambda config: "https://chat.example/hook")
    ok = notify_report.send_report(_cfg(enabled=False), "본문", ticket="HAN-1", http=http)
    assert ok is False
    assert http.calls == []   # 발송 시도조차 안 함


def test_send_report_no_webhook_skips():
    http = _FakeHTTP()
    # webhook_ref 비어 있음 → _read_webhook 이 "" → 발송 생략.
    ok = notify_report.send_report(_cfg(enabled=True, webhook_ref=""), "본문", ticket="HAN-1", http=http)
    assert ok is False
    assert http.calls == []


def test_send_report_empty_body_skips(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setattr(notify_report.notify, "_read_webhook", lambda config: "https://chat.example/hook")
    ok = notify_report.send_report(_cfg(enabled=True), "   ", ticket="HAN-1", http=http)
    assert ok is False
    assert http.calls == []


def test_send_report_post_failure_returns_false(monkeypatch):
    http = _FakeHTTP(code=500)   # 5xx → _post False
    monkeypatch.setattr(notify_report.notify, "_read_webhook", lambda config: "https://chat.example/hook")
    ok = notify_report.send_report(_cfg(enabled=True), "본문", ticket="HAN-1", http=http)
    assert ok is False
    assert len(http.calls) == 1  # 시도는 했다


# --- CLI main(리포트 소스 분기) ----------------------------------------------


def test_main_reads_report_file_and_sends(monkeypatch, tmp_path, isolated_state):
    rpt = tmp_path / "r.md"
    rpt.write_text("파일 리포트 본문", encoding="utf-8")
    sent = {}
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sent.update(report=report, kw=kw) or True)
    rc = notify_report.main(["--ticket", "HAN-3", "--report-file", str(rpt)])
    assert rc == 0
    assert sent["report"] == "파일 리포트 본문"
    assert sent["kw"]["ticket"] == "HAN-3"


def test_main_report_arg_and_failure_exit_code(monkeypatch, isolated_state):
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=False))
    monkeypatch.setattr(notify_report, "send_report", lambda config, report, **kw: False)
    rc = notify_report.main(["--ticket", "HAN-4", "--report", "본문"])
    assert rc == 1


# --- 티켓당 멱등(dedup): sent-marker (작업 B, 결정적 백스톱) ---------------------


def test_claim_marker_atomic_single_fire(isolated_state):
    # 첫 선점은 True(마커 생성), 두 번째는 False(이미 존재) — 동시호출도 하나만 발송.
    assert notify_report.already_sent("KR-1") is False
    assert notify_report.claim_marker("KR-1") is True
    assert notify_report.already_sent("KR-1") is True
    assert notify_report.claim_marker("KR-1") is False   # 재선점 불가(멱등)
    # 롤백하면 다시 선점 가능(정당한 재시도).
    notify_report.release_marker("KR-1")
    assert notify_report.already_sent("KR-1") is False
    assert notify_report.claim_marker("KR-1") is True


def test_marker_path_sanitizes_ticket(isolated_state):
    # 경로 트래버설/분리자 차단 — 마지막 컴포넌트는 파일명 안전 형태로만(분리자·.. 없음).
    p = notify_report.marker_path("../evil/KR 1")
    last = p.replace("\\", "/").split("/")[-1]
    assert "/" not in last and "\\" not in last and ".." not in last
    assert last == "___evil_KR_1"
    # 마커는 항상 notify-sent/ 하위에 머문다(트래버설로 상위를 못 벗어난다).
    assert "notify-sent" in p.replace("\\", "/")


def test_main_second_call_skips_and_force_resends(monkeypatch, isolated_state):
    # 첫 호출은 발송(마커 생성), 둘째 호출은 dedup skip(성공 exit 0, 발송 안 함),
    # --force 는 재전송. 실제 POST 는 send_report 대역으로 격리(네트워크 없이).
    sends = []
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)

    rc1 = notify_report.main(["--ticket", "KR-9", "--report", "완료 본문"])
    assert rc1 == 0
    assert sends == ["KR-9"]                 # 1회 발송
    assert notify_report.already_sent("KR-9") is True  # 마커 생성됨

    rc2 = notify_report.main(["--ticket", "KR-9", "--report", "완료 본문(재호출)"])
    assert rc2 == 0                          # skip 도 성공 exit 0
    assert sends == ["KR-9"]                 # 두 번째는 발송하지 않음(dedup)

    rc3 = notify_report.main(["--ticket", "KR-9", "--report", "완료 본문", "--force"])
    assert rc3 == 0
    assert sends == ["KR-9", "KR-9"]         # --force 는 재전송


def test_main_send_failure_rolls_back_marker(monkeypatch, isolated_state):
    # 발송 실패면 마커를 남기지 않는다(정당한 재시도가 막히지 않게).
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report", lambda config, report, **kw: False)
    rc = notify_report.main(["--ticket", "KR-X", "--report", "본문"])
    assert rc == 1
    assert notify_report.already_sent("KR-X") is False   # 실패 → 마커 없음(재시도 가능)


def test_main_no_ticket_skips_dedup(monkeypatch, isolated_state):
    # 티켓 키 없으면 dedup 불가 — 항상 발송 시도(마커 없음).
    sends = []
    monkeypatch.delenv("ROLE", raising=False)
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc1 = notify_report.main(["--report", "본문"])
    rc2 = notify_report.main(["--report", "본문"])
    assert rc1 == 0 and rc2 == 0
    assert sends == [None, None]   # 티켓 없으니 매번 발송(dedup 대상 아님)


# --- ROLE 가드: 워커는 통지자가 아니다(센트럴이 유일 통지자, 결정적 백스톱) ------------


def test_is_worker_role_only_true_for_worker():
    # ROLE env 로만 결정적으로 판정(worker 만 True; central·미설정·기타는 False).
    assert notify_report._is_worker_role({"ROLE": "worker"}) is True
    assert notify_report._is_worker_role({"ROLE": "WORKER"}) is True   # 대소문자 무관
    assert notify_report._is_worker_role({"ROLE": " worker "}) is True  # 공백 무관
    assert notify_report._is_worker_role({"ROLE": "central"}) is False
    assert notify_report._is_worker_role({"ROLE": ""}) is False
    assert notify_report._is_worker_role({}) is False                   # 미설정
    # DISPATCH_USER 만으로는 판정하지 않는다(ROLE 이 정본 — 오탐 방지).
    assert notify_report._is_worker_role({"DISPATCH_USER": "yhchoi"}) is False


def test_main_worker_role_skips_send(monkeypatch, isolated_state):
    # ROLE=worker 컨텍스트면 상신하지 않고 skip(POST 안 함, 성공 exit 0). 마커도 안 남긴다.
    sends = []
    monkeypatch.setenv("ROLE", "worker")
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc = notify_report.main(["--ticket", "KR-W", "--report", "완료 본문"])
    assert rc == 0                       # skip 은 성공 exit 0
    assert sends == []                   # 발송 시도조차 없음
    assert notify_report.already_sent("KR-W") is False   # 마커도 남기지 않음(센트럴이 상신할 여지)


def test_main_worker_role_skips_even_with_force(monkeypatch, isolated_state):
    # --force 여도 워커에선 나가지 않는다(중복 알림 근본 차단 — 결정적 백스톱).
    sends = []
    monkeypatch.setenv("ROLE", "worker")
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc = notify_report.main(["--ticket", "KR-WF", "--report", "완료 본문", "--force"])
    assert rc == 0
    assert sends == []


def test_main_central_role_sends_normally(monkeypatch, isolated_state):
    # ROLE=central 이면 기존대로 정상 상신(가드가 정상 경로를 막지 않는다).
    sends = []
    monkeypatch.setenv("ROLE", "central")
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc = notify_report.main(["--ticket", "KR-C", "--report", "완료 본문"])
    assert rc == 0
    assert sends == ["KR-C"]             # 센트럴은 정상 발송


def test_main_unset_role_sends_normally(monkeypatch, isolated_state):
    # ROLE 미설정(테스트/구배포)이면 기존대로 정상 상신(하위호환).
    sends = []
    monkeypatch.delenv("ROLE", raising=False)
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc = notify_report.main(["--ticket", "KR-U", "--report", "완료 본문"])
    assert rc == 0
    assert sends == ["KR-U"]


# --- provider 중립화(프레임워크화) -------------------------------------------


def _cfg_provider(provider, webhook_ref="svc/webhook"):
    """정본 notifier 섹션을 가진 설정 대역(레거시 미러 없이)."""
    return SimpleNamespace(
        notifier=SimpleNamespace(provider=provider, webhook_ref=webhook_ref),
        secrets=SimpleNamespace(base_dir="/secrets"),
    )


def test_send_report_slack_uses_minimal_text_payload(monkeypatch):
    http = _FakeHTTP(code=200)
    monkeypatch.setattr(notify_report.notify, "_read_webhook",
                        lambda config: "https://hooks.slack.example/T/B/X")
    ok = notify_report.send_report(_cfg_provider("slack"), "완료 본문", ticket="HAN-7", http=http)
    assert ok is True
    assert set(http.calls[0]["json"]) == {"text"}          # Slack incoming webhook 형태
    assert http.calls[0]["json"]["text"].startswith("[HAN-7]")


def test_send_report_generic_webhook_carries_ticket_and_event(monkeypatch):
    """generic_webhook 은 문서화된 구조 페이로드로 나간다(event=report)."""
    http = _FakeHTTP(code=200)
    monkeypatch.setattr(notify_report.notify, "_read_webhook",
                        lambda config: "https://hooks.example/in")
    ok = notify_report.send_report(_cfg_provider("generic_webhook"), "완료 본문", ticket="HAN-8",
                                   http=http)
    assert ok is True
    body = http.calls[0]["json"]
    assert body["source"] == "jira-auto-dispatcher"
    assert body["event"] == "report"
    assert body["ticket"] == "HAN-8"
    assert "완료 본문" in body["text"]


def test_send_report_unknown_provider_refuses(monkeypatch):
    """알 수 없는 provider 면 아무것도 나가지 않는다(오발송 < 미발송)."""
    http = _FakeHTTP()
    monkeypatch.setattr(notify_report.notify, "_read_webhook",
                        lambda config: "https://chat.example/hook")
    ok = notify_report.send_report(_cfg_provider("teams"), "완료 본문", ticket="HAN-9", http=http)
    assert ok is False
    assert http.calls == []


def test_send_report_provider_none_skips(monkeypatch):
    http = _FakeHTTP()
    monkeypatch.setattr(notify_report.notify, "_read_webhook",
                        lambda config: "https://chat.example/hook")
    assert notify_report.send_report(_cfg_provider("none"), "본문", ticket="HAN-1",
                                     http=http) is False
    assert http.calls == []


# --- 마커 이름 변경의 하위호환(옛 gchat-sent/ 도 읽는다) ------------------------


def test_already_sent_reads_legacy_marker_dir(isolated_state, tmp_path):
    """업그레이드 직후: 옛 이름 디렉토리에 남은 마커도 '이미 상신됨'으로 인정한다."""
    import os

    legacy = notify_report.legacy_marker_path("KR-OLD")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("KR-OLD\n")
    assert notify_report.already_sent("KR-OLD") is True
    assert notify_report.marker_path("KR-OLD") != legacy      # 새 마커는 신규 경로에 쓴다


def test_main_skips_when_only_legacy_marker_exists(monkeypatch, isolated_state):
    """옛 마커만 있어도 재전송하지 않는다(이름 변경이 중복 알림을 만들지 않는다)."""
    import os

    sends = []
    legacy = notify_report.legacy_marker_path("KR-OLD2")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("KR-OLD2\n")
    monkeypatch.delenv("ROLE", raising=False)
    monkeypatch.setattr(notify_report, "_load_config", lambda p: _cfg(enabled=True))
    monkeypatch.setattr(notify_report, "send_report",
                        lambda config, report, **kw: sends.append(kw.get("ticket")) or True)
    rc = notify_report.main(["--ticket", "KR-OLD2", "--report", "본문"])
    assert rc == 0
    assert sends == []


# --- 옛 이름(gchat.py) 하위호환 shim ------------------------------------------


def test_gchat_module_is_an_alias_of_notify_report():
    """``python /app/gchat.py`` 를 부르는 구 프롬프트·문서가 그대로 동작해야 한다.

    shim 은 별칭이라 **같은 모듈 객체**다 — 상태(마커·설정)가 갈라지지 않고,
    monkeypatch 도 한 곳에만 걸면 된다.
    """
    import gchat

    assert gchat is notify_report
    assert gchat.main is notify_report.main
    assert gchat.send_report is notify_report.send_report
