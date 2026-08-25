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
    "username": "testuser",
    "display_name": "Choi",
    "jira_account_id": "test-account-id",
    "jira_email": "yh@x",
    "jira_token": "JIRA-TOK-VAL",
    "forge_token": "GL-TOK-VAL",
    "claude_setup_token": "CLAUDE-TOK-VAL",
    "git_name": "Test User",
    "git_email": "yh@x",
    "autonomy_mode": "A",
    "scope": "PROJ, PORTAL",
}


def test_onboard_success_stores_refs_not_values(tmp_path, isolated_state):
    client, reg, base = _wire(tmp_path)
    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 201
    body = res.get_json()
    assert body["username"] == "testuser"
    assert body["enabled"] is False  # 안전 기본

    # 토큰 값이 응답에 절대 실리지 않는다.
    raw = res.get_data(as_text=True)
    assert "JIRA-TOK-VAL" not in raw
    assert "GL-TOK-VAL" not in raw
    assert "CLAUDE-TOK-VAL" not in raw

    rec = reg.get("testuser")
    assert rec is not None
    assert rec.enabled is False
    assert rec.autonomy_mode == "A"
    assert rec.permission_level == "bypass"
    assert rec.scope.projects == ["PROJ", "PORTAL"]
    # secrets_ref는 값이 아니라 참조 경로.
    assert rec.secrets_ref.jira_token == "testuser/jira-token"
    # forge 중립 파일명(forge-token). 레거시 이름 필드에도 같은 참조가 미러된다.
    assert rec.secrets_ref.forge_token == "testuser/forge-token"
    assert rec.secrets_ref.gitlab_token == "testuser/forge-token"
    assert rec.secrets_ref.claude_oauth_token == "testuser/claude-oauth-token"
    assert "JIRA-TOK-VAL" not in json.dumps(rec.to_dict())

    # 시크릿 값은 파일로만 저장.
    jira_path = os.path.join(base, "testuser", "jira-token")
    assert open(jira_path, encoding="utf-8").read() == "JIRA-TOK-VAL"
    assert open(os.path.join(base, "testuser", "claude-oauth-token"), encoding="utf-8").read() == "CLAUDE-TOK-VAL"
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


def test_onboard_optional_forge_token_omitted(tmp_path, isolated_state):
    client, reg, base = _wire(tmp_path)
    data = dict(_FULL)
    del data["forge_token"]
    assert client.post("/onboard", json=data).status_code == 201
    rec = reg.get("testuser")
    assert rec.secrets_ref.forge_token == ""
    assert rec.secrets_ref.gitlab_token == ""
    assert not os.path.exists(os.path.join(base, "testuser", "forge-token"))


def test_enable_triggers_spawn(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    res = client.post("/users/testuser/enable")
    assert res.status_code == 200
    assert reg.get("testuser").enabled is True
    spawner.ensure_worker.assert_called_once()
    # ensure_worker에 넘어간 인자는 해당 사용자 레코드.
    passed = spawner.ensure_worker.call_args.args[0]
    assert passed.username == "testuser"


def test_disable_stops_worker(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    client.post("/users/testuser/enable")
    res = client.post("/users/testuser/disable")
    assert res.status_code == 200
    assert reg.get("testuser").enabled is False
    spawner.stop_worker.assert_called_once_with("testuser")


def test_enable_unknown_user_404(tmp_path, isolated_state):
    spawner = MagicMock()
    client, _, _ = _wire(tmp_path, spawner=spawner)
    assert client.post("/users/nobody/enable").status_code == 404
    spawner.ensure_worker.assert_not_called()


def test_autonomy_switch(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path)
    client.post("/onboard", json=_FULL)
    res = client.post("/users/testuser/autonomy", json={"autonomy_mode": "B"})
    assert res.status_code == 200
    assert reg.get("testuser").autonomy_mode == "B"
    # 잘못된 값은 400.
    assert client.post("/users/testuser/autonomy", json={"autonomy_mode": "C"}).status_code == 400


def test_container_start_stop(tmp_path, isolated_state):
    spawner = MagicMock()
    client, reg, _ = _wire(tmp_path, spawner=spawner)
    client.post("/onboard", json=_FULL)
    assert client.post("/users/testuser/container/start").status_code == 200
    spawner.ensure_worker.assert_called_once()
    assert client.post("/users/testuser/container/stop").status_code == 200
    spawner.stop_worker.assert_called_once_with("testuser")
    # 알 수 없는 action은 400.
    assert client.post("/users/testuser/container/frob").status_code == 400


def test_container_without_spawner_501(tmp_path, isolated_state):
    client, reg, _ = _wire(tmp_path, spawner=None)
    client.post("/onboard", json=_FULL)
    assert client.post("/users/testuser/container/start").status_code == 501


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


def test_onboard_accepts_legacy_gitlab_token_form_field(tmp_path, isolated_state):
    """옛 관리 UI/스크립트가 보내는 gitlab_token 필드도 계속 받는다(하위호환).

    저장 파일명·레지스트리 키는 forge 중립 이름으로 수렴한다.
    """
    client, reg, base = _wire(tmp_path)
    data = dict(_FULL)
    data.pop("forge_token", None)
    data["gitlab_token"] = "LEGACY-GL-VAL"
    assert client.post("/onboard", json=data).status_code == 201

    rec = reg.get("testuser")
    assert rec.secrets_ref.forge_token == "testuser/forge-token"
    assert rec.secrets_ref.gitlab_token == "testuser/forge-token"
    with open(os.path.join(base, "testuser", "forge-token"), encoding="utf-8") as fh:
        assert fh.read() == "LEGACY-GL-VAL"


def test_onboard_prefers_new_forge_token_field(tmp_path, isolated_state):
    client, reg, base = _wire(tmp_path)
    data = dict(_FULL)
    data["forge_token"] = "NEW-VAL"
    data["gitlab_token"] = "OLD-VAL"
    assert client.post("/onboard", json=data).status_code == 201
    with open(os.path.join(base, "testuser", "forge-token"), encoding="utf-8") as fh:
        assert fh.read() == "NEW-VAL"


# ---------------------------------------------------------------------------
# 설정 자가진단 게이트 — 잘못된 설정으로 워커를 띄우지 않는다
# ---------------------------------------------------------------------------


def _wire_with_doctor(tmp_path, results):
    """대역 진단 결과를 물린 온보딩 앱(실제 검사는 부르지 않는다)."""
    from app import doctor_runtime as DR

    base = str(tmp_path / "secrets")
    reg = Registry()
    cfg = SimpleNamespace(secrets=SimpleNamespace(base_dir=base))
    doctor = DR.DoctorRuntime(cfg, run_checks=lambda _cfg, **_kw: list(results))
    doctor.run_once()
    app = Flask(__name__)
    register_onboarding_api(app, {"registry": reg, "config": cfg, "spawner": None,
                                  "doctor": doctor})
    return app.test_client(), reg, base


def _check(name, status):
    from app import setup_doctor as D

    return D.CheckResult(name, status, f"{name} 가 잘못됐습니다", f"{name} 를 고치세요")


def test_onboard_is_blocked_when_a_fatal_check_failed(tmp_path, isolated_state):
    """워커 바인드가 어긋난 상태로 사용자를 붙이면 조용히 실패하는 잡만 쌓인다."""
    from app import setup_doctor as D

    client, reg, base = _wire_with_doctor(
        tmp_path, [_check("host_deploy_dir", D.STATUS_FAIL)])
    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 409
    body = res.get_json()
    assert body["blocking"] == ["host_deploy_dir"]
    assert body["failures"][0]["hint"]          # 어떻게 고치는지가 응답에 있다
    # ⚠️ 아무 것도 쓰지 않았다 — 레지스트리도 시크릿 파일도 그대로다.
    assert reg.get("testuser") is None
    assert not os.path.exists(os.path.join(base, "testuser"))
    # 토큰 값이 응답에 실리지 않는다(관리 UI 에는 인증이 없다).
    raw = res.get_data(as_text=True)
    assert "JIRA-TOK-VAL" not in raw and "CLAUDE-TOK-VAL" not in raw


def test_onboard_is_not_blocked_by_a_degrading_failure(tmp_path, isolated_state):
    """central 자신의 git 경로(dlc-meta)가 깨져도 잡은 돈다 — 설치를 막지 않는다."""
    from app import setup_doctor as D

    client, _reg, _base = _wire_with_doctor(tmp_path, [_check("dlc_meta", D.STATUS_FAIL)])
    assert client.post("/onboard", json=_FULL).status_code == 201


def test_onboard_is_not_blocked_before_the_first_diagnosis(tmp_path, isolated_state):
    """진단이 아직 안 돌았으면 막지 않는다 — 부팅 직후 관리 UI 가 잠기면 안 된다."""
    from app import doctor_runtime as DR

    base = str(tmp_path / "secrets")
    cfg = SimpleNamespace(secrets=SimpleNamespace(base_dir=base))
    app = Flask(__name__)
    register_onboarding_api(app, {"registry": Registry(), "config": cfg,
                                  "spawner": None,
                                  "doctor": DR.DoctorRuntime(cfg)})   # 한 번도 안 돌았다
    assert app.test_client().post("/onboard", json=_FULL).status_code == 201


def test_onboard_works_without_a_doctor_component(tmp_path, isolated_state):
    """doctor 컴포넌트가 없는 조립(테스트·임베드)에서도 온보딩은 그대로 동작한다."""
    client, _reg, _base = _wire(tmp_path)
    assert client.post("/onboard", json=_FULL).status_code == 201
