"""central Flask 앱 회귀테스트 — GET / 가 index.html을 렌더한다.

버그 회귀 방지: main.py가 app/ 패키지 안이라 Flask 기본 template_folder가
app/templates로 잡혀 실제 템플릿(레포 루트 templates/)을 못 찾고 GET / 이
TemplateNotFound으로 HTTP 500이 나던 문제. 수정 후 200 + index.html 렌더.

라이브 Jira/Docker/네트워크는 호출하지 않는다(컴포넌트는 생성만 되고 기동
안 함). config는 tmp 파일로 최소 구성한다.
"""

from __future__ import annotations

import re
import textwrap
import time

import pytest

from app.main import create_central_app


def _write_config(tmp_path):
    """create_central_app이 요구하는 최소 유효 config.yaml을 tmp에 쓴다."""
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
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
            webhook: {{ enabled: false }}
            secrets: {{ base_dir: "{secrets_dir.as_posix()}" }}
            run: {{ worker_max_concurrency: 64 }}
            """
        ).strip(),
        encoding="utf-8",
    )
    return str(cfg)


@pytest.fixture()
def central_client(tmp_path, isolated_state):
    config_path = _write_config(tmp_path)
    app = create_central_app(config_path)
    app.config.update(TESTING=True)
    return app.test_client()


def test_index_renders_ok(central_client):
    """GET / 는 200이며 index.html 이 렌더된다(수정 전엔 500/TemplateNotFound)."""
    res = central_client.get("/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # index.html 의 고유 마커가 렌더 결과에 포함된다.
    assert "central 관리 콘솔" in body


def test_healthz_ok(central_client):
    """헬스는 그대로 200(회귀 방지 겸 스모크)."""
    res = central_client.get("/healthz")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok", "role": "central"}


def test_rerun_endpoint_unknown_job_404(central_client):
    """POST /api/jobs/<ticket>/rerun — 미존재 잡은 404(수정 2)."""
    res = central_client.post("/api/jobs/NOPE-1/rerun")
    assert res.status_code == 404
    assert res.get_json()["error"] == "unknown job"


def _terminal_job(ticket="PROJ-77"):
    """그 티켓을 종결(failed) 상태로 만들어 rerun 대상으로 세운다."""
    from app import main
    from app import queue as q
    from app.queue import Job

    scheduler = main._components["scheduler"]
    scheduler.enqueue(Job(ticket=ticket, user="u1", target_repos=["repoA"]))
    scheduler.on_complete(ticket, q.FAILED)
    assert scheduler.jobs.get(ticket).status == q.FAILED
    return scheduler


def test_rerun_endpoint_routes_to_central_seam(central_client, monkeypatch):
    """수동 재실행이 정상 폴링 티켓과 **동일한 센트럴 세션 seam** 으로 라우팅된다.

    구 경로(scheduler.rerun = reopen + tick → running)는 은퇴했다 — 그 잡을 집어 실행할
    워커 폴링 소비자가 없어 running 인 채로 스턱되기 때문이다. 잡은 queued + fractal
    표식으로 남아 tick 이 건너뛴다(이중 실행 방지).
    """
    from app import central_dispatch
    from app import queue as q

    scheduler = _terminal_job()
    calls = []

    def fake_emit(config, sink, job_queue, gate, job):
        calls.append(job.ticket)
        central_dispatch.record_fractal_job(job_queue, job)  # 실제 seam 과 동일 관측성 기록

    monkeypatch.setattr(central_dispatch, "central_active", lambda cfg, sink: True)
    monkeypatch.setattr(central_dispatch, "emit_to_central", fake_emit)

    res = central_client.post("/api/jobs/PROJ-77/rerun")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True and body.get("fractal") is True
    assert calls == ["PROJ-77"]                          # 센트럴 seam 으로 방출됨
    job = scheduler.jobs.get("PROJ-77")
    assert job.status == q.QUEUED                        # 구 tick 으로 running 만들지 않음
    assert job.is_fractal is True                        # 스케줄러 디스패치 제외 표식
    assert scheduler.tick() == []                        # 프랙탈 잡은 tick 이 건너뛴다


def test_rerun_endpoint_refuses_when_central_inactive(central_client, monkeypatch):
    """센트럴 세션이 성립하지 않는 오설정 배포에서는 **거부**한다(409).

    레거시 실행 경로가 없으므로 "재실행했다"고 답하면 거짓말이 된다 — 잡은 종결 상태
    그대로 두고 사람에게 설정을 고치라고 알린다.
    """
    from app import central_dispatch
    from app import queue as q

    scheduler = _terminal_job("PROJ-78")
    monkeypatch.setattr(central_dispatch, "central_active", lambda cfg, sink: False)

    res = central_client.post("/api/jobs/PROJ-78/rerun")
    assert res.status_code == 409
    assert scheduler.jobs.get("PROJ-78").status == q.FAILED   # 손대지 않았다


def test_rerun_endpoint_reports_retryable_when_inject_fails(central_client, monkeypatch):
    """주입 실패는 503 — seam 이 claim 을 되돌렸으니 사람이 다시 눌러 볼 수 있다."""
    from app import central_dispatch

    _terminal_job("PROJ-79")

    def boom(config, sink, job_queue, gate, job):
        raise central_dispatch.CentralInjectFailed(job.ticket)

    monkeypatch.setattr(central_dispatch, "central_active", lambda cfg, sink: True)
    monkeypatch.setattr(central_dispatch, "emit_to_central", boom)

    res = central_client.post("/api/jobs/PROJ-79/rerun")
    assert res.status_code == 503


def test_scheduler_state_endpoint_read_only(central_client):
    """GET /scheduler/state — 스냅샷을 반환하고 상태를 바꾸지 않는다(Phase 3b-0)."""
    from app import main
    from app.queue import Job

    scheduler = main._components["scheduler"]
    scheduler.enqueue(Job(ticket="PROJ-7", user="u1", target_repos=["repoA"]))
    scheduler.enqueue(Job(ticket="PROJ-8", user="u2", target_repos=["repoA"]))  # 대기
    before = {j.ticket: (j.status, j.attempts) for j in scheduler.jobs.list_jobs()}

    res = central_client.get("/scheduler/state")
    assert res.status_code == 200
    body = res.get_json()
    assert body["running_count"] == 1
    assert {r["ticket"] for r in body["running"]} == {"PROJ-7"}
    assert body["locked_repos"] == ["repoA"]
    assert "resource" in body and "has_headroom" in body["resource"]

    # 부작용 0: GET 호출이 잡 상태·attempts를 바꾸지 않는다.
    after = {j.ticket: (j.status, j.attempts) for j in scheduler.jobs.list_jobs()}
    assert before == after
    assert scheduler.jobs.get("PROJ-8").status == "queued"


# ---------------------------------------------------------------------------
# 설정 자가진단 노출(/api/doctor) — 부팅을 막지도 늦추지도 않는다
# ---------------------------------------------------------------------------


def test_doctor_endpoint_reports_pending_before_the_first_run(central_client):
    """create_central_app 만으로는 진단이 돌지 않는다(실행은 백그라운드 기동에서).

    그래서 이 시점의 응답은 'pending' 이며, 온보딩도 막지 않는다.
    """
    body = central_client.get("/api/doctor").get_json()
    assert body["state"] == "pending"
    assert body["checks"] == [] and body["onboarding_blocked"] is False


def test_doctor_endpoint_serves_the_cached_result_without_rerunning(central_client):
    from app import main
    from app import setup_doctor as D

    calls = []
    doctor = main._components["doctor"]

    def fake_run(cfg, **kwargs):
        calls.append(kwargs)
        return [D.CheckResult("config", D.STATUS_FAIL, "자리표시자가 남았습니다",
                              "실제 값으로 바꾸세요")]

    doctor._run_checks = fake_run
    doctor.run_once()

    for _ in range(3):
        body = central_client.get("/api/doctor").get_json()
    assert len(calls) == 1                      # 조회는 재실행하지 않는다(캐시)
    assert body["state"] == "ready" and body["ok"] is False
    assert body["blocking"] == ["config"]
    assert body["onboarding_blocked"] is True
    assert body["checks"][0]["hint"]            # 고치는 법이 UI 로 나간다


def test_doctor_refresh_endpoint_triggers_a_rerun_and_returns_immediately(central_client):
    from app import main
    from app import setup_doctor as D

    calls = []
    doctor = main._components["doctor"]
    doctor._run_checks = lambda cfg, **kw: (calls.append(kw) or
                                            [D.CheckResult("config", D.STATUS_PASS, "ok")])
    doctor.run_once()
    res = central_client.post("/api/doctor/refresh")
    assert res.status_code == 202
    for _ in range(200):
        if len(calls) >= 2:
            break
        time.sleep(0.01)
    assert len(calls) == 2                      # 수동 재실행 경로가 실제로 돈다


def test_onboarding_is_blocked_through_the_real_app(central_client):
    """게이트는 UI 가 아니라 **서버**에 있다 — 폼을 우회해 POST 해도 막힌다."""
    from app import main
    from app import setup_doctor as D

    doctor = main._components["doctor"]
    doctor._run_checks = lambda cfg, **kw: [
        D.CheckResult("docker", D.STATUS_FAIL, "접속 실패", "socket-proxy 를 확인하세요")]
    doctor.run_once()
    res = central_client.post("/onboard", json={
        "username": "u1", "jira_account_id": "a", "jira_email": "e",
        "jira_token": "TOK", "claude_setup_token": "CTOK"})
    assert res.status_code == 409
    assert res.get_json()["blocking"] == ["docker"]
    assert "TOK" not in res.get_data(as_text=True)


def test_index_shows_the_doctor_banner_at_the_top(central_client):
    """관리 UI 최상단에 진단 배너가 있다(실패를 여기서 먼저 본다)."""
    body = central_client.get("/").get_data(as_text=True)
    assert 'id="doctor-panel"' in body
    assert body.index('id="doctor-panel"') < body.index('id="onboard-section"')
    assert "/api/doctor" in body and "/api/doctor/refresh" in body


def test_onboarding_guide_is_wired_into_the_real_app(central_client):
    """관리 UI 가 폼·준비물 안내를 그리는 원천이 실제 앱에 배선돼 있다.

    이 엔드포인트가 없으면 index.html 은 입력칸을 하나도 그리지 못한다(안내를 HTML 에
    하드코딩하지 않기로 한 대가다) — 그래서 배선 자체가 회귀 대상이다.
    """
    res = central_client.get("/api/onboarding/guide")
    assert res.status_code == 200
    body = res.get_json()
    assert [s["id"] for s in body["steps"]] == ["local", "web"]
    keys = {f["key"] for sec in body["sections"] for f in sec["fields"]}
    assert {"username", "jira_token", "forge_token", "claude_setup_token"} <= keys
    assert "forge_token" in body["required"]
    assert body["consent_key"] in body["required"]
    # 이 인스턴스의 Jira 사이트가 안내에 반영된다(하드코딩이면 반영되지 않는다).
    assert body["jira"]["base_url"] == "https://example.atlassian.net"


def test_index_html_does_not_hardcode_onboarding_guidance(central_client):
    """준비물 안내는 템플릿이 아니라 스키마가 소유한다(하드코딩 회귀 방지).

    예전 index.html 에는 발급 경로("아바타 → Edit profile → Access Tokens")와 필드 힌트가
    통째로 박혀 있었다. 그러면 forge 를 GitHub 으로 바꾼 배포의 합류자에게 존재하지 않는
    화면 경로를 보여 준다.
    """
    body = central_client.get("/").get_data(as_text=True)
    # 온보딩 마크업만 본다 — 다른 구역(「토큰 회전」·「폴백 로그인」)은 별개의 안내라
    # 같은 단어를 쓴다. 경계는 **그 요소 자신의 닫는 태그**로 잡는다. 「다음 구역의 id」로
    # 잡으면 순서를 바꾸는 순간 엉뚱한 것을 읽고, 닫는 태그를 `</section>` 으로 **박아
    # 두면** 온보딩이 다른 태그(예: `<dialog>`)로 바뀌는 순간 경계가 무너져 뒤쪽 구역까지
    # 빨아들인다. 그래서 여는 태그에서 태그 이름을 읽어 그 짝을 경계로 쓴다.
    start = body.index('id="onboard-section"')
    tag = re.search(r"<(\w+)", body[body.rindex("<", 0, start):]).group(1)
    section = body[start:body.index("</%s>" % tag, start)]
    for hardcoded in ("Edit profile", "Developer settings", "id.atlassian.com",
                      "setup-token", "accountId", "PROJECT_KEY"):
        assert hardcoded not in section, hardcoded
    # 대신 스키마에서 렌더할 자리와 2단 절차 자리가 있다.
    assert 'id="onboard-fields"' in section
    assert 'id="join-steps"' in section
