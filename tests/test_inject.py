"""스폰 시 주입(app/inject.py) 단위테스트 — 컨테이너·docker 없이 전 경로.

이 모듈이 지키는 계약:
    - central 이 인코딩한 것을 worker 가 **바이트 그대로** 되살린다(토큰 왕복).
    - 되살린 파일 권한이 0600(부모 0700) — 워커가 자기 uid 로 쓰고 자기 uid 로 읽는다.
    - 주입 env 가 없으면 no-op(옛 배포·수동 기동 호환).
    - 깨진 페이로드·경로 탈출 ref 는 **조용히 넘어가지 않고** 예외로 드러난다.
"""

from __future__ import annotations

import os
import stat

import pytest

from app import inject


# --- 왕복 ---------------------------------------------------------------


def test_config_roundtrip_preserves_text_exactly():
    text = "role: central\n한글: 값\nlist:\n  - a\n"
    assert inject.decode_config(inject.encode_config(text)) == text


def test_secrets_roundtrip_preserves_values_exactly():
    files = {
        "alice/jira-token": "ATATT-xyz\n",          # 끝 개행까지 그대로
        "alice/claude-settings.json": '{"a": 1}',
        "service/google-chat-webhook": "https://chat.example/hook?key=v&token=t",
    }
    assert inject.decode_secrets(inject.encode_secrets(files)) == files


def test_payload_is_not_plaintext():
    """페이로드는 base64 — 평문 토큰이 env 문자열에 그대로 보이지 않는다."""
    encoded = inject.encode_secrets({"alice/jira-token": "SUPER-SECRET-VALUE"})
    assert "SUPER-SECRET-VALUE" not in encoded


# --- materialize --------------------------------------------------------


def test_materialize_writes_config_and_secrets(tmp_path):
    cfg_dest = tmp_path / "app" / "config" / "config.yaml"
    secrets = tmp_path / "run" / "secrets"
    env = {
        inject.ENV_CONFIG: inject.encode_config("role: central\n"),
        inject.ENV_SECRETS: inject.encode_secrets({
            "alice/jira-token": "JIRA-VAL",
            "alice/claude-settings.json": "{}",
        }),
    }
    out = inject.materialize(env, config_dest=str(cfg_dest), secrets_dir=str(secrets))

    assert out["config"] == str(cfg_dest)
    assert cfg_dest.read_text(encoding="utf-8") == "role: central\n"
    assert (secrets / "alice" / "jira-token").read_text(encoding="utf-8") == "JIRA-VAL"
    assert len(out["secrets"]) == 2


def test_materialize_uses_secrets_dir_env_when_not_given(tmp_path):
    secrets = tmp_path / "s"
    env = {
        "SECRETS_DIR": str(secrets),
        inject.ENV_SECRETS: inject.encode_secrets({"alice/jira-token": "V"}),
    }
    inject.materialize(env)
    assert (secrets / "alice" / "jira-token").read_text(encoding="utf-8") == "V"


def test_materialize_is_noop_without_injection_env(tmp_path):
    """주입 env 가 없으면 아무 것도 만들지 않는다(bind 로 파일을 받는 옛 배포 호환)."""
    dest = tmp_path / "config.yaml"
    out = inject.materialize({}, config_dest=str(dest), secrets_dir=str(tmp_path / "s"))
    assert out == {"config": None, "secrets": []}
    assert not dest.exists()
    assert not (tmp_path / "s").exists()


def test_materialize_is_idempotent(tmp_path):
    """매 부팅마다 다시 돈다 — 덮어쓰기로 항상 최신 상태에 수렴."""
    secrets = tmp_path / "s"
    env = {inject.ENV_SECRETS: inject.encode_secrets({"alice/t": "one"})}
    inject.materialize(env, secrets_dir=str(secrets))
    env2 = {inject.ENV_SECRETS: inject.encode_secrets({"alice/t": "two"})}
    inject.materialize(env2, secrets_dir=str(secrets))
    assert (secrets / "alice" / "t").read_text(encoding="utf-8") == "two"


@pytest.mark.skipif(os.name == "nt", reason="POSIX 권한 비트는 윈도우에서 의미 없음")
def test_materialize_permissions_are_owner_only(tmp_path):
    """⚠️ 워커는 **자기 uid 로 쓰고 자기 uid 로 읽는다** — 소유권 드리프트가 불가능하다.

    (예전엔 호스트가 만든 디렉토리를 uid 1000 워커가 ro 로 읽어야 해서, 소유권/권한이
    어긋나면 워커가 조용히 시크릿을 못 읽을 수 있었다.)
    """
    secrets = tmp_path / "s"
    inject.materialize(
        {inject.ENV_SECRETS: inject.encode_secrets({"alice/jira-token": "V"})},
        secrets_dir=str(secrets),
    )
    path = secrets / "alice" / "jira-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # 쓴 주체(=이 프로세스 uid)가 읽을 수 있다.
    assert path.read_text(encoding="utf-8") == "V"
    assert path.stat().st_uid == os.getuid()


def test_materialize_writes_lf_and_no_bom(tmp_path):
    """POLICY-ENCODING: UTF-8(BOM 없음)·LF."""
    dest = tmp_path / "config.yaml"
    inject.materialize({inject.ENV_CONFIG: inject.encode_config("a: 1\nb: 2\n")},
                       config_dest=str(dest))
    raw = dest.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw


# --- 실패는 드러난다(조용한 성공 금지) ------------------------------------


def test_materialize_rejects_absolute_ref(tmp_path):
    payload = inject.encode_secrets({"/etc/passwd": "x"})
    with pytest.raises(inject.InjectError):
        inject.materialize({inject.ENV_SECRETS: payload}, secrets_dir=str(tmp_path))


def test_materialize_rejects_parent_traversal_ref(tmp_path):
    payload = inject.encode_secrets({"alice/../../etc/passwd": "x"})
    with pytest.raises(inject.InjectError):
        inject.materialize({inject.ENV_SECRETS: payload}, secrets_dir=str(tmp_path))


def test_materialize_raises_on_corrupt_payload(tmp_path):
    with pytest.raises(inject.InjectError):
        inject.materialize({inject.ENV_SECRETS: "!!!not-base64!!!"},
                           secrets_dir=str(tmp_path))


def test_encode_raises_when_payload_too_large():
    """조용히 자르지 않는다 — env 한도를 넘으면 spawn 이 실패하며 이유를 말한다."""
    with pytest.raises(inject.InjectError):
        inject.encode_config("x" * (inject.MAX_INJECT_BYTES + 10))


@pytest.mark.parametrize("ref,ok", [
    ("alice/jira-token", True),
    ("service/google-chat-webhook", True),
    ("", False),
    ("   ", False),
    ("/abs/path", False),
    ("C:/win/path", False),
    ("alice/../service/jira-token", False),
    ("..", False),
])
def test_is_safe_ref(ref, ok):
    assert inject.is_safe_ref(ref) is ok
