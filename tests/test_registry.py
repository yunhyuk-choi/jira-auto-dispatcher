"""registry 단위테스트(upsert·enabled 필터·영속)."""

from __future__ import annotations

from app.registry import Registry, UserRecord


def _rec(username="testuser", account_id="acc-1", enabled=True):
    return UserRecord(
        username=username, jira_account_id=account_id, enabled=enabled,
        autonomy_mode="B", per_repo={"portal-frontend": "A"},
        secrets_ref=__import__("app.registry", fromlist=["SecretsRef"]).SecretsRef(
            jira_token="users/yh/jira-token"),
    )


def test_upsert_and_get(isolated_state):
    r = Registry()
    r.upsert(_rec())
    got = r.get("testuser")
    assert got is not None and got.jira_account_id == "acc-1"
    assert got.secrets_ref.jira_token == "users/yh/jira-token"


def test_get_by_account_id_enabled_only(isolated_state):
    r = Registry()
    r.upsert(_rec(username="a", account_id="acc-a", enabled=True))
    r.upsert(_rec(username="b", account_id="acc-b", enabled=False))
    assert r.get_by_account_id("acc-a").username == "a"
    assert r.get_by_account_id("acc-b") is None       # 비활성 제외
    assert r.find_by_account_id("acc-b").username == "b"  # 순수 조회는 찾음


def test_upsert_accepts_dict_and_persists(isolated_state):
    Registry().upsert({"username": "z", "jira_account_id": "acc-z", "enabled": True})
    r2 = Registry()  # 재로드
    assert r2.get("z").jira_account_id == "acc-z"


def test_set_enabled(isolated_state):
    r = Registry()
    r.upsert(_rec(username="a", account_id="acc-a", enabled=True))
    r.set_enabled("a", False)
    assert r.get_by_account_id("acc-a") is None


def test_secrets_ref_only_holds_references(isolated_state):
    # 실토큰 값이 섞여 들어와도 화이트리스트 필드만 보존(추가 키 무시).
    r = Registry()
    r.upsert({"username": "a", "jira_account_id": "acc",
              "secrets_ref": {"jira_token": "path/ref", "leaked_value": "REALTOKEN"}})
    d = r.get("a").to_dict()
    # forge_token 이 정본이고 gitlab_token 은 같은 값의 레거시 미러다(둘 다 존재).
    assert d["secrets_ref"] == {"jira_token": "path/ref", "forge_token": "",
                                "gitlab_token": "", "claude_oauth_token": ""}
    assert "leaked_value" not in d["secrets_ref"]


def test_secrets_ref_forge_token_mirrors_legacy_name(isolated_state):
    """forge_token 이 정본이고 gitlab_token 은 같은 값의 레거시 미러다(양방향)."""
    from app.registry import SecretsRef, UserRecord

    # 옛 registry.json(gitlab_token 만) → 신규 이름으로도 읽힌다.
    old = UserRecord.from_dict({"username": "a",
                                "secrets_ref": {"gitlab_token": "a/gitlab-token"}})
    assert old.secrets_ref.forge_token == "a/gitlab-token"
    assert old.secrets_ref.gitlab_token == "a/gitlab-token"

    # 신규 키만 → 옛 이름을 읽는 코드도 그대로 동작한다.
    new = UserRecord.from_dict({"username": "a",
                                "secrets_ref": {"forge_token": "a/forge-token"}})
    assert new.secrets_ref.gitlab_token == "a/forge-token"

    # 직접 생성 경로(테스트 대역·옛 코드)도 수렴한다.
    assert SecretsRef(gitlab_token="x/gl").forge_token == "x/gl"
    assert SecretsRef(forge_token="x/f").gitlab_token == "x/f"


def test_user_record_notify_user_id_mirrors_legacy_name(isolated_state):
    from app.registry import UserRecord

    assert UserRecord(username="a", google_chat_user_id="G1").notify_user_id == "G1"
    assert UserRecord(username="a", notify_user_id="U1").google_chat_user_id == "U1"
