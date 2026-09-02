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


# --- username 이름 규칙 + 기존 레지스트리 하위호환 -----------------------------


def test_username_rule_accepts_people_names_and_rejects_paths():
    """규칙의 단일 원천 — 온보딩 게이트와 소비 지점이 **같은 이 함수**를 본다."""
    from app.registry import is_valid_username

    for good in ("yhchoi", "yh.choi", "u1", "a", "A_b-c", "9lives", "x" * 32):
        assert is_valid_username(good) is True, good

    for bad in (
        "../evil", "../../etc/x", "a/b", "..", "/abs", "C:/win",
        "a" + chr(92) + "b",        # 백슬래시
        "has space", "", "   ",
        ".hidden", "-flag",         # 시작 문자 규칙(docker 도 첫 글자를 영숫자로 요구한다)
        "a.", "a-", "a_",           # 끝 문자 규칙(Windows 가 끝의 점·공백을 잘라낸다)
        "CON", "nul", "com1", "con.txt",   # Windows 예약 장치명
        "user" + chr(10) + "name",  # 개행
        "중문",                 # 비ASCII
        "x" * 33,                   # 길이 상한
        None, 123,                  # 문자열이 아닌 값
    ):
        assert is_valid_username(bad) is False, repr(bad)


def test_registry_keeps_a_legacy_username_and_never_blocks_boot(isolated_state, caplog):
    """이름 규칙보다 먼저 등록된 이름은 **그대로 싣되 경고로 드러낸다.**

    떨어뜨리면 그 사람의 티켓이 조용히 매핑되지 않고, 예외로 올리면 그 한 줄이 central
    부팅 전체를 막는다 — 둘 다 이 시스템에서 가장 비싼 실패 모드다.
    """
    from app import state
    from app.registry import Registry

    state.save_registry({"users": [
        {"username": "../legacy-escape", "jira_account_id": "acc-legacy"},
        {"username": "normal", "jira_account_id": "acc-normal"},
    ]})

    with caplog.at_level("WARNING", logger="jad.registry"):
        reg = Registry()

    # 부팅은 막히지 않았고 두 사람 다 살아 있다.
    assert reg.get("../legacy-escape") is not None
    assert reg.get("normal") is not None
    # 폴러의 담당자 매핑도 그대로 산다 — 소급 차단은 하지 않는다(등록은 이미 끝났다).
    assert reg.find_by_account_id("acc-legacy") is not None

    # 조용한 관용은 은폐다 — 경고로 드러난다(값은 repr 이라 개행이 이스케이프된다).
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "../legacy-escape" in warned
    assert "normal" not in warned or "../legacy-escape" in warned


def test_case_only_duplicate_is_refused_at_upsert(isolated_state):
    """``Alice`` 와 ``alice`` 는 대소문자 무시 파일시스템에서 **같은 시크릿 디렉토리**다.

    레지스트리 dict 는 정확 일치라 둘을 다른 사람으로 받아들이지만, 온보딩이 토큰을 쓰는
    ``secrets/<user>/`` 는 호스트 파일시스템이고 Windows·macOS 기본은 대소문자를
    구별하지 않는다 → 나중 등록이 앞사람의 토큰을 덮어쓴다. 그래서 조립 자체를 거부한다.
    """
    import pytest

    from app.registry import Registry

    r = Registry()
    r.upsert(_rec(username="Alice", account_id="acc-A"))
    with pytest.raises(ValueError) as caught:
        r.upsert(_rec(username="alice", account_id="acc-a"))
    assert "Alice" in str(caught.value)
    # 기존 등록은 흔들리지 않는다.
    assert r.get("Alice") is not None and r.get("alice") is None

    # **같은 이름을 그대로 다시 upsert 하는 것은 충돌이 아니다**(갱신 경로).
    r.upsert(_rec(username="Alice", account_id="acc-A2"))
    assert r.get("Alice").jira_account_id == "acc-A2"


def test_find_case_conflict_ignores_exact_match(isolated_state):
    from app.registry import Registry

    r = Registry()
    r.upsert(_rec(username="yh.choi"))
    assert r.find_case_conflict("yh.choi") is None      # 정확 일치는 충돌이 아니다
    assert r.find_case_conflict("YH.Choi") == "yh.choi"
    assert r.find_case_conflict("other") is None


def test_existing_case_conflict_warns_but_never_blocks_boot(isolated_state, caplog):
    """이미 들어와 있는 충돌 짝은 **경고로 드러내고 부팅은 막지 않는다.**

    떨어뜨리면 그 사람이 예고 없이 사라지고, 예외로 올리면 한 줄이 central 부팅 전체를
    막는다 — 그리고 여기서는 **어느 쪽을 버릴지 고를 근거도 없다**(둘 다 실재하는 사람이다).
    선례는 형식 위반 이름 처리와 같다.
    """
    from app import state
    from app.registry import Registry

    state.save_registry({"users": [
        {"username": "Alice", "jira_account_id": "acc-A"},
        {"username": "alice", "jira_account_id": "acc-a"},
        {"username": "bob", "jira_account_id": "acc-b"},
    ]})

    with caplog.at_level("WARNING", logger="jad.registry"):
        reg = Registry()

    assert reg.get("Alice") is not None and reg.get("alice") is not None
    assert reg.find_by_account_id("acc-b") is not None
    warned = " ".join(r.getMessage() for r in caplog.records)
    assert "Alice" in warned and "alice" in warned
    assert "bob" not in warned                          # 충돌 없는 이름은 조용하다
