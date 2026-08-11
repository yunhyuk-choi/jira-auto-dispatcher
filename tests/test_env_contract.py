"""계약 회귀 테스트 — spawner.build_env → agent_runner.UserCreds.from_env 왕복.

실배포에서 잡힌 링치핀 버그: spawner가 넣는 env 키와 worker(from_env)가 읽는
키가 어긋나면 per-user attribution이 전부 깨진다(git author 공백, 토큰 참조 공백
→ ensure_repos가 orchestrator_repo를 프로비저닝하지 못해 잡 실패). 이 테스트는
그 드리프트를 회귀로 잡는다: **build_env 결과 dict를 그대로 from_env에 먹이면
git_name/git_email/jira_email/jira_token_ref/gitlab_token_ref/claude_oauth_token_ref
가 모두 채워져야 한다**(하나라도 비면 실패).

라이브 docker/claude는 호출하지 않는다.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from app.agent_runner import UserCreds
from app.registry import Identity, SecretsRef, UserRecord
from app.spawner import Spawner


def _cfg(base_dir: str):
    return SimpleNamespace(
        spawn=SimpleNamespace(
            image="jira-auto-dispatcher:latest",
            network="jad-net",
            central_url="http://central:8787",
            mem_limit="4g",
            docker_host="unix:///var/run/docker.sock",
            run_as="1000:1000",
            host_deploy_dir="",
        ),
        secrets=SimpleNamespace(base_dir=base_dir),
        worker_shared_secret="s3cr3t",
    )


def _user(google_chat_user_id=""):
    return UserRecord(
        username="testuser",
        jira_account_id="acc",
        jira_email="yh@x.com",
        google_chat_user_id=google_chat_user_id,
        permission_level="bypass",
        identity=Identity(git_name="Test User", git_email="you@example.com"),
        secrets_ref=SecretsRef(
            jira_token="testuser/jira-token",
            gitlab_token="testuser/gitlab-token",
            claude_oauth_token="testuser/claude-oauth-token",
        ),
    )


def _write(base: str, rel: str, value: str) -> None:
    path = os.path.join(base, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(value)


def test_build_env_to_from_env_roundtrip_populates_all(tmp_path):
    """핵심 회귀: build_env → from_env 왕복으로 모든 정체성/참조 필드가 채워진다."""
    base = str(tmp_path / "secrets")
    _write(base, "testuser/claude-oauth-token", "CLAUDE-XYZ")

    user = _user()
    env = Spawner(_cfg(base)).build_env(user)

    # 그 dict를 그대로 worker의 from_env에 먹인다(계약 왕복).
    creds = UserCreds.from_env(user.username, env=env)

    # 모든 필드가 비어 있지 않아야 한다(하나라도 빈 값이면 링치핀 버그 재발).
    assert creds.user == "testuser"
    assert creds.git_name == "Test User"
    assert creds.git_email == "you@example.com"
    assert creds.jira_email == "yh@x.com"
    assert creds.jira_token_ref == "testuser/jira-token"
    assert creds.gitlab_token_ref == "testuser/gitlab-token"
    assert creds.claude_oauth_token_ref == "testuser/claude-oauth-token"
    # Claude 값도 폴백 경로로 전달된다(참조 + 값 둘 다).
    assert creds.claude_oauth_token_value == "CLAUDE-XYZ"


def test_build_env_emits_from_env_contract_keys(tmp_path):
    """build_env가 from_env가 읽는 계약 키(*_REF·DISPATCH_*)를 실제로 emit한다."""
    base = str(tmp_path / "secrets")
    env = Spawner(_cfg(base)).build_env(_user())
    for key in (
        "DISPATCH_GIT_NAME", "DISPATCH_GIT_EMAIL", "DISPATCH_JIRA_EMAIL",
        "JIRA_TOKEN_REF", "GITLAB_TOKEN_REF", "CLAUDE_OAUTH_TOKEN_REF",
    ):
        assert env.get(key), f"build_env가 계약 키를 누락: {key}"
    # 참조는 상대 ref(base_dir 상대) — 절대경로/마운트경로가 아님.
    assert env["JIRA_TOKEN_REF"] == "testuser/jira-token"
    assert env["GITLAB_TOKEN_REF"] == "testuser/gitlab-token"
    assert env["CLAUDE_OAUTH_TOKEN_REF"] == "testuser/claude-oauth-token"


def test_build_env_roundtrips_google_chat_user_id_when_present(tmp_path):
    """google_chat_user_id가 있으면 spawner가 방출하고 from_env가 멘션용으로 수신."""
    base = str(tmp_path / "secrets")
    user = _user(google_chat_user_id="123456789")
    env = Spawner(_cfg(base)).build_env(user)
    assert env["DISPATCH_GOOGLE_CHAT_USER_ID"] == "123456789"
    creds = UserCreds.from_env(user.username, env=env)
    assert creds.google_chat_user_id == "123456789"


def test_build_env_omits_google_chat_user_id_when_absent(tmp_path):
    """없으면 방출 생략 → from_env는 빈 값(display_name 폴백)."""
    base = str(tmp_path / "secrets")
    env = Spawner(_cfg(base)).build_env(_user())  # google_chat_user_id=""
    assert "DISPATCH_GOOGLE_CHAT_USER_ID" not in env
    creds = UserCreds.from_env("testuser", env=env)
    assert creds.google_chat_user_id == ""


def test_build_env_does_not_leak_token_values(tmp_path):
    """토큰 '값'은 참조/파일경로로만 넘기고 env에 실리지 않는다(Claude 값 제외)."""
    base = str(tmp_path / "secrets")
    _write(base, "testuser/jira-token", "JIRA-SECRET-VAL")
    _write(base, "testuser/gitlab-token", "GL-SECRET-VAL")
    env = Spawner(_cfg(base)).build_env(_user())
    joined = "\n".join(str(v) for v in env.values())
    assert "JIRA-SECRET-VAL" not in joined
    assert "GL-SECRET-VAL" not in joined
