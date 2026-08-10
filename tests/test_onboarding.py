"""onboarding 단위테스트 — 검증·시크릿 저장(값 미노출)·라이프사이클 토글.

spawner는 mock으로 주입해 라이브 Docker 호출 없이 enable/disable/container가
spawn/stop을 부르는지 검증한다.
"""

from __future__ import annotations

import json
import os
from types import SimpleNamespace
from unittest.mock import MagicMock

from flask import Flask

from app.onboarding import register_onboarding_api
from app.registry import Registry


def _wire(tmp_path, spawner=None):
    base = str(tmp_path / "secrets")
    reg = Registry()
    cfg = SimpleNamespace(secrets=SimpleNamespace(base_dir=base))
    comps = {"registry": reg, "config": cfg, "spawner": spawner}
    app = Flask(__name__)
    register_onboarding_api(app, comps)
    return app.test_client(), reg, base


_FULL = {
    "username": "yh.choi",
    "display_name": "Choi",
    "jira_account_id": "712020:abc",
    "jira_email": "yh@x",
    "jira_token": "JIRA-TOK-VAL",
    "gitlab_token": "GL-TOK-VAL",
    "claude_setup_token": "CLAUDE-TOK-VAL",
    "git_name": "yunhyuk-choi",
    "git_email": "yh@x",
    "autonomy_mode": "A",
    "scope": "HAN, PORTAL",
}


def test_onboard_success_stores_refs_not_values(tmp_path, isolated_state):
    client, reg, base = _wire(tmp_path)
    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 201
    body = res.get_json()
    assert body["username"] == "yh.choi"
    assert body["enabled"] is False  # 안전 기본

    # 토큰 값이 응답에 절대 실리지 않는다.
    raw = res.get_data(as_text=True)
    assert "JIRA-TOK-VAL" not in raw
    assert "GL-TOK-VAL" not in raw
    assert "CLAUDE-TOK-VAL" not in raw

    rec = reg.get("yh.choi")
    assert rec is not None
    assert rec.enabled is False
    assert rec.autonomy_mode == "A"
    assert rec.permission_level == "bypass"
    assert rec.scope.projects == ["HAN", "PORTAL"]
    # secrets_ref는 값이 아니라 참조 경로.
    assert rec.secrets_ref.jira_token == "yh.choi/jira-token"
    assert rec.secrets_ref.gitlab_token == "yh.choi/gitlab-token"
    assert rec.secrets_ref.claude_oauth_token == "yh.choi/claude-oauth-token"
    assert "JIRA-TOK-VAL" not in json.dumps(rec.to_dict())

    # 시크릿 값은 파일로만 저장.
    jira_path = os.path.join(base, "yh.choi", "jira-token")
    assert open(jira_path, encoding="utf-8").read() == "JIRA-TOK-VAL"
    assert open(os.path.join(base, "yh.choi", "claude-oauth-token"), encoding="utf-8").read() == "CLAUDE-TOK-VAL"
    if os.name == "posix":
        assert oct(os.stat(jira_path).st_mode & 0o777) == oct(0o600)


def test_onboard_missing_required_400(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path)
    res = client.post("/onboard", json={"username": "x"})
    assert res.status_code == 400
    assert "missing" in res.get_json()
    assert reg.get("x") is None


def test_onboard_duplicate_409(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path)
    assert client.post("/onboard", json=_FULL).status_code == 201
    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 409


def test_onboard_optional_gitlab_omitted(tmp_path, isolated_state):
    client, reg, base = _wire(tmp_path)
    data = dict(_FULL)
    del data["gitlab_token"]
    assert client.post("/onboard", json=data).status_code == 201
    rec = reg.get("yh.choi")
    assert rec.secrets_ref.gitlab_token == ""
    assert not os.path.exists(os.path.join(base, "yh.choi", "gitlab-token"))


def test_enable_triggers_spawn(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    res = client.post("/users/yh.choi/enable")
    assert res.status_code == 200
    assert reg.get("yh.choi").enabled is True
    spawner.ensure_worker.assert_called_once()
    # ensure_worker에 넘어간 인자는 해당 사용자 레코드.
    passed = spawner.ensure_worker.call_args.args[0]
    assert passed.username == "yh.choi"


def test_disable_stops_worker(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    client.post("/users/yh.choi/enable")
    res = client.post("/users/yh.choi/disable")
    assert res.status_code == 200
    assert reg.get("yh.choi").enabled is False
    spawner.stop_worker.assert_called_once_with("yh.choi")


def test_enable_unknown_user_404(tmp_path, isolated_state):
    spawner = MagicMock()
    client, _, _ = _wire(tmp_path, spawner=spawner)
    assert client.post("/users/nobody/enable").status_code == 404
    spawner.ensure_worker.assert_not_called()


def test_autonomy_switch(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path)
    client.post("/onboard", json=_FULL)
    res = client.post("/users/yh.choi/autonomy", json={"autonomy_mode": "B"})
    assert res.status_code == 200
    assert reg.get("yh.choi").autonomy_mode == "B"
    # 잘못된 값은 400.
    assert client.post("/users/yh.choi/autonomy", json={"autonomy_mode": "C"}).status_code == 400


def test_container_start_stop(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    assert client.post("/users/yh.choi/container/start").status_code == 200
    spawner.ensure_worker.assert_called_once()
    assert client.post("/users/yh.choi/container/stop").status_code == 200
    spawner.stop_worker.assert_called_once_with("yh.choi")
    # 알 수 없는 action은 400.
    assert client.post("/users/yh.choi/container/frob").status_code == 400


def test_container_without_spawner_501(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path, spawner=None)
    client.post("/onboard", json=_FULL)
    assert client.post("/users/yh.choi/container/start").status_code == 501


def test_onboard_error_returns_json_not_html(tmp_path, isolated_state):
    """예상치 못한 예외는 HTML 500이 아니라 JSON 500으로(프론트 파싱 실패 방지).

    시크릿 값은 응답에 절대 노출되지 않아야 한다.
    """
    base = str(tmp_path / "secrets")
    reg = MagicMock()
    reg.get.return_value = None  # 중복 아님 → 진행
    reg.upsert.side_effect = RuntimeError("boom-internal")  # ValueError 아닌 예외
    cfg = SimpleNamespace(secrets=SimpleNamespace(base_dir=base))
    app = Flask(__name__)
    register_onboarding_api(app, {"registry": reg, "config": cfg, "spawner": None})
    client = app.test_client()

    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 500
    # JSON(콘텐츠 타입 + 파싱 가능) — HTML 아님.
    assert res.content_type.startswith("application/json")
    body = res.get_json()
    assert body is not None and "error" in body
    # 시크릿 값은 에러 응답에 절대 노출되지 않는다.
    raw = res.get_data(as_text=True)
    assert "JIRA-TOK-VAL" not in raw
    assert "GL-TOK-VAL" not in raw
    assert "CLAUDE-TOK-VAL" not in raw
