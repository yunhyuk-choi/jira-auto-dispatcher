"""central Flask 앱 회귀테스트 — GET / 가 index.html을 렌더한다.

버그 회귀 방지: main.py가 app/ 패키지 안이라 Flask 기본 template_folder가
app/templates로 잡혀 실제 템플릿(레포 루트 templates/)을 못 찾고 GET / 이
TemplateNotFound으로 HTTP 500이 나던 문제. 수정 후 200 + index.html 렌더.

라이브 Jira/Docker/네트워크는 호출하지 않는다(컴포넌트는 생성만 되고 기동
안 함). config는 tmp 파일로 최소 구성한다.
"""

from __future__ import annotations

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
            webhook: {{ enabled: false, path: /jira-webhook }}
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


def test_rerun_endpoint_requeues_terminal_job(central_client):
    """종결(failed) 잡을 rerun 엔드포인트가 queued로 되돌리고 재-dispatch 시도(수정 2)."""
    from app import main
    from app import queue as q
    from app.queue import Job

    scheduler = main._components["scheduler"]
    scheduler.enqueue(Job(ticket="PROJ-9", user="u1", target_repos=["repoA"]))
    scheduler.on_complete("PROJ-9", q.FAILED)
    assert scheduler.jobs.get("PROJ-9").status == q.FAILED

    res = central_client.post("/api/jobs/PROJ-9/rerun")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert "PROJ-9" in body["dispatched"]
    assert scheduler.jobs.get("PROJ-9").status == q.RUNNING


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
