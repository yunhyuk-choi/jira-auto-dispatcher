"""central Flask 앱 회귀테스트 — GET / 가 index.html을 렌더한다.

버그 회귀 방지: main.py가 app/ 패키지 안이라 Flask 기본 template_folder가
app/templates로 잡혀 실제 템플릿(레포 루트 templates/)을 못 찾고 GET / 이
TemplateNotFound으로 HTTP 500이 나던 문제. 수정 후 200 + index.html 렌더.

라이브 Jira/Docker/네트워크는 호출하지 않는다(컴포넌트는 생성만 되고 기동
안 함). config는 tmp 파일로 최소 구성한다.
"""

from __future__ import annotations

import textwrap

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
              project: HAN
              poll_interval_sec: 60
              watcher_token_file: service/jira-token
            match: {{ statuses: ["해야 할 일"] }}
            webhook: {{ enabled: false, path: /jira-webhook }}
            secrets: {{ base_dir: "{secrets_dir.as_posix()}" }}
            run: {{ concurrency_per_worker: 1 }}
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
