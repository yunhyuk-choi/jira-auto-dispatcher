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
    # 합류자 **본인**의 풀 퍼미션 동의 — 없으면 등록이 통과하지 않는다(서버 강제).
    "consent_full_permissions": True,
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


def test_onboard_requires_forge_token(tmp_path, isolated_state):
    """개인 forge 토큰 없이 등록되면 워커는 커밋만 하고 **MR/PR 을 못 만든다**.

    에러 없이 반쪽만 도는 그 상태가 이 시스템에서 가장 비싼 실패 모드라 필수로 올렸다.
    이 배포가 forge 를 안 쓰는 경우는 없다(forge.kind 는 기본값 있는 필수, dlc-meta URL 도
    필수, 두 자율 모드 모두 원격 push 를 지시한다) — 그래서 조건부가 아니라 무조건이다.
    """
    client, reg, base = _wire(tmp_path)
    data = dict(_FULL)
    del data["forge_token"]
    res = client.post("/onboard", json=data)
    assert res.status_code == 400
    body = res.get_json()
    assert "forge_token" in body["missing"]
    # ⚠️ 아무 것도 쓰지 않았다 — 검증이 시크릿 저장보다 앞선다.
    assert reg.get("testuser") is None
    assert not os.path.exists(os.path.join(base, "testuser"))


def test_onboard_requires_own_consent(tmp_path, isolated_state):
    """설치자의 동의로 갈음하지 않는다 — 본인 동의 없이는 서버가 막는다."""
    client, reg, base = _wire(tmp_path)
    for value in (None, False, "네"):
        data = dict(_FULL)
        if value is None:
            del data["consent_full_permissions"]
        else:
            data["consent_full_permissions"] = value
        res = client.post("/onboard", json=data)
        assert res.status_code == 400, value
        keys = [f["key"] for f in res.get_json()["findings"]]
        assert "consent_full_permissions" in keys, value
        assert reg.get("testuser") is None
        assert not os.path.exists(os.path.join(base, "testuser"))


def test_onboard_records_consent_with_server_timestamp(tmp_path, isolated_state):
    """동의 시각은 **서버 수신 시각**으로 기록한다(클라이언트가 보낸 시각은 안 쓴다)."""
    client, reg, _ = _wire(tmp_path)
    data = dict(_FULL)
    data["consent_accepted_at"] = "1999-01-01T00:00:00+09:00"   # 클라이언트 위조 시도
    res = client.post("/onboard", json=data)
    assert res.status_code == 201

    rec = reg.get("testuser")
    assert rec.consent.full_permissions is True
    stamped = rec.consent.accepted_at
    assert stamped and not stamped.startswith("1999")
    # ISO-8601 로 실제 파싱된다(감사 흔적).
    from datetime import datetime

    datetime.fromisoformat(stamped)
    # 레지스트리 왕복(직렬화)에서도 살아남는다.
    from app.registry import UserRecord

    assert UserRecord.from_dict(rec.to_dict()).consent.accepted_at == stamped
    assert res.get_json()["consent_accepted_at"] == stamped


def test_onboard_accepts_form_encoded_consent_checkbox(tmp_path, isolated_state):
    """HTML 폼 인코딩(체크박스는 "on", 목록은 쉼표 문자열)도 그대로 받는다."""
    client, reg, _ = _wire(tmp_path)
    form = {k: ("on" if k == "consent_full_permissions" else v)
            for k, v in _FULL.items()}
    res = client.post("/onboard", data=form)
    assert res.status_code == 201
    rec = reg.get("testuser")
    assert rec.consent.full_permissions is True
    assert rec.scope.projects == ["PROJ", "PORTAL"]


def test_onboard_reports_findings_per_field(tmp_path, isolated_state):
    """오류를 **전부 모아** 필드별 키로 돌려준다(UI 가 칸별로 표시한다)."""
    client, _reg, _ = _wire(tmp_path)
    res = client.post("/onboard", json={"username": "x",
                                        "consent_full_permissions": True,
                                        "autonomy_mode": "Z"})
    assert res.status_code == 400
    body = res.get_json()
    assert body["ok"] is False and body["error_count"] >= 2
    keys = {f["key"] for f in body["findings"]}
    assert {"jira_token", "forge_token", "claude_setup_token"} <= keys
    assert "autonomy_mode" in keys        # 허용값 밖(A|B)
    # findings 에는 힌트가 있고 값은 없다.
    assert all("hint" in f for f in body["findings"])


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
    """docker 에 닿지 못하는 상태로 사용자를 붙이면 조용히 실패하는 잡만 쌓인다."""
    from app import setup_doctor as D

    client, reg, base = _wire_with_doctor(
        tmp_path, [_check("docker", D.STATUS_FAIL)])
    res = client.post("/onboard", json=_FULL)
    assert res.status_code == 409
    body = res.get_json()
    assert body["blocking"] == ["docker"]
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


# ---------------------------------------------------------------------------
# 준비물 안내 · accountId 조회 — 합류자가 값을 어디서 구하는가
# ---------------------------------------------------------------------------


def _wire_full_config(tmp_path, kind="gitlab"):
    """forge·jira·run 섹션이 있는 config 로 배선(안내 렌더가 그것을 읽는다)."""
    base = str(tmp_path / "secrets")
    cfg = SimpleNamespace(
        secrets=SimpleNamespace(base_dir=base),
        forge=SimpleNamespace(kind=kind, base_url="", base_url_source="",
                              base_url_origin=""),
        jira=SimpleNamespace(base_url="https://acme.atlassian.net", project="PROJ"),
        run=SimpleNamespace(
            dlc_meta_repo_url="https://gitlab.acme.example/acme/dlc-meta.git",
            orchestrator_repo_url="https://github.com/yunhyuk-choi/ai-dlc-orchestrator.git",
            docs_repo_url=""),
    )
    app = Flask(__name__)
    register_onboarding_api(app, {"registry": Registry(), "config": cfg,
                                  "spawner": None})
    return app.test_client(), cfg


def test_guide_endpoint_renders_from_instance_config(tmp_path, isolated_state):
    """준비물 안내는 HTML 에 박히지 않고 **이 인스턴스 설정**에서 렌더된다."""
    client, _cfg = _wire_full_config(tmp_path, kind="gitlab")
    body = client.get("/api/onboarding/guide").get_json()

    assert body["forge"]["kind"] == "gitlab"
    assert body["forge"]["scope"] == "api"
    # 사내 GitLab 을 쓰면 토큰 발급 링크도 그 호스트를 가리킨다.
    assert body["forge"]["url"].startswith("https://gitlab.acme.example/")
    assert body["jira"]["myself_url"].startswith("https://acme.atlassian.net/")
    # 2단 절차(로컬 SETTER + 웹 등록)가 안내에 있다.
    assert [s["id"] for s in body["steps"]] == ["local", "web"]
    # 필수 목록에 forge 토큰과 본인 동의가 있다.
    assert "forge_token" in body["required"]
    assert body["consent_key"] in body["required"]


def test_guide_endpoint_switches_with_forge_kind(tmp_path, isolated_state):
    client, _cfg = _wire_full_config(tmp_path, kind="github")
    body = client.get("/api/onboarding/guide").get_json()
    assert body["forge"]["scope"] == "repo"
    assert body["forge"]["change_abbr"] == "PR"
    assert "GitLab" not in body["forge"]["path"]


def test_guide_endpoint_carries_no_secrets(tmp_path, isolated_state):
    """관리 UI 에는 인증이 없다 — 안내가 시크릿을 실으면 그대로 유출이다."""
    client, cfg = _wire_full_config(tmp_path)
    raw = client.get("/api/onboarding/guide").get_data(as_text=True)
    assert cfg.secrets.base_dir not in raw
    for token in ("JIRA-TOK-VAL", "GL-TOK-VAL", "CLAUDE-TOK-VAL"):
        assert token not in raw


def test_guide_endpoint_survives_a_partial_config(tmp_path, isolated_state):
    """secrets 만 있는 조립(테스트·임베드)에서도 500 이 아니라 안내가 나온다."""
    client, _reg, _base = _wire(tmp_path)
    res = client.get("/api/onboarding/guide")
    assert res.status_code == 200
    assert res.get_json()["sections"]


def test_whoami_reuses_the_setup_discovery(tmp_path, isolated_state, monkeypatch):
    """accountId 조회는 설치 관문이 쓰는 discover_account 를 그대로 재사용한다."""
    client, _cfg = _wire_full_config(tmp_path)
    seen = {}

    class _FakeClient:
        def __init__(self, base_url, email, token, *a, **kw):
            seen["base_url"] = base_url
            seen["email"] = email
            seen["token"] = token

        def myself(self):
            return {"accountId": "557058:abc", "displayName": "테스터",
                    "emailAddress": "you@example.com", "active": True}

    monkeypatch.setattr("app.jira_client.JiraClient", _FakeClient)
    res = client.post("/api/onboarding/whoami",
                      json={"jira_email": "you@example.com", "jira_token": "TOK"})
    assert res.status_code == 200
    assert res.get_json()["account_id"] == "557058:abc"
    # 대상 사이트는 **인스턴스 설정**이 정한다(요청이 임의 호스트를 고를 수 없다).
    assert seen["base_url"] == "https://acme.atlassian.net"
    # 토큰은 응답에 실리지 않는다.
    assert "TOK" not in res.get_data(as_text=True)


def test_whoami_requires_credentials(tmp_path, isolated_state):
    client, _cfg = _wire_full_config(tmp_path)
    assert client.post("/api/onboarding/whoami", json={}).status_code == 400


def test_whoami_is_unavailable_without_a_configured_site(tmp_path, isolated_state):
    """jira.base_url 이 없으면 조회하지 않는다(추측한 호스트로 자격을 보내지 않는다)."""
    client, _reg, _base = _wire(tmp_path)
    res = client.post("/api/onboarding/whoami",
                      json={"jira_email": "a@b", "jira_token": "T"})
    assert res.status_code == 503


def test_whoami_reports_auth_failure_without_the_token(tmp_path, isolated_state,
                                                       monkeypatch):
    client, _cfg = _wire_full_config(tmp_path)

    class _Failing:
        def __init__(self, *a, **kw):
            pass

        def myself(self):
            from app.jira_client import JiraError

            raise JiraError("unauthorized", status_code=401)

    monkeypatch.setattr("app.jira_client.JiraClient", _Failing)
    res = client.post("/api/onboarding/whoami",
                      json={"jira_email": "a@b", "jira_token": "SECRET-TOK"})
    assert res.status_code == 502
    raw = res.get_data(as_text=True)
    assert "SECRET-TOK" not in raw
    assert res.get_json()["error"]


def test_doctor_gate_runs_before_validation(tmp_path, isolated_state):
    """자가진단 차단이 입력값 검증보다 **앞선다** — 게이트 순서를 깨지 않는다."""
    from app import setup_doctor as D

    client, reg, base = _wire_with_doctor(tmp_path, [_check("docker", D.STATUS_FAIL)])
    # 비어 있는(=검증 실패할) 요청이라도 409(자가진단 차단)로 먼저 막힌다.
    res = client.post("/onboard", json={})
    assert res.status_code == 409
    assert res.get_json()["blocking"] == ["docker"]
