"""스폰 시 주입 — worker 컨테이너에 config·시크릿을 **호스트 경로 없이** 전달한다.

역할:
    central 이 worker 를 띄울 때, worker 가 읽어야 할 파일(``config/config.yaml`` ·
    per-user 시크릿 · 사전 인가 ``claude-settings.json`` · 알림 웹훅)을 **컨테이너
    스펙의 env** 로 실어 보내고, worker 가 부팅 시 자기 컨테이너 안에 그대로
    **materialize**(파일로 기록)한다.

역할 소속: **central(인코딩) + worker(디코딩·기록)** 공용 — 두 쪽이 같은 계약을
    읽도록 이 모듈 하나가 단일 원천이다.

왜 이 방식인가 (bind 마운트 제거):
    central 은 워커를 자기가 만들지 않고 **호스트 docker 데몬**에게 요청한다
    (sibling container). 그래서 바인드 마운트의 source 경로는 central 컨테이너
    내부가 아니라 **호스트가** 해석한다 — central 안의 /run/secrets 를 그대로
    넘기면 호스트의 없는 경로가 빈 디렉토리로 마운트되고, 워커는 **정상적으로 뜬
    뒤** 잡 실행 시점에야 엉뚱한 에러로 죽는다. 그 함정을 피하려고 설치자가
    ``spawn.host_deploy_dir``(호스트 절대경로)를 정확히 적어야 했고, 틀리면 조용히
    깨졌다.

    주입은 그 개념 자체를 없앤다 — **호스트 경로가 등장하지 않으므로** 틀릴 값이
    없다. 워커의 마운트는 named 볼륨 2개(``jad-<user>`` · 공유 워크스페이스)만
    남고, 그것들은 볼륨 *이름* 으로 docker 가 해석하므로 호스트 경로와 무관하다.

격리(중요):
    한 워커의 env 에는 **그 사용자 자신의 시크릿** 과 (설정된 경우) 팀 공용 알림
    웹훅 하나만 담는다. 다른 사용자 시크릿은 값도, 경로도, 마운트도 존재하지
    않는다 — 워커 A 의 파일시스템에서 워커 B 의 시크릿으로 가는 길이 아예 없다
    (전엔 같은 호스트 secrets/ 트리를 부모로 공유했다). 이 불변식은
    :meth:`app.spawner.Spawner.collect_injected_secrets` 가 ref 접두어로 강제하고
    회귀 테스트가 지킨다.

⚠️ 노출 면(정직한 기술):
    주입된 값은 컨테이너 스펙에 들어가므로 ``docker inspect`` 로 보인다. 이는 이미
    이 시스템의 전제와 같은 수준이다 — ``CLAUDE_CODE_OAUTH_TOKEN`` ·
    ``WORKER_SHARED_SECRET`` 은 전부터 env 로 전달됐고, docker API 에 닿을 수 있는
    주체는 이미 호스트 root 동치라 호스트의 secrets/ 를 직접 읽을 수 있다
    (SECURITY.md). 대신 **디스크 잔류**는 줄었다: 워커 쪽 시크릿은 tmpfs(RAM)에만
    materialize 되고 컨테이너가 죽으면 사라진다.

POLICY-ENCODING: 기록 파일은 UTF-8(BOM 없음)·LF. 값은 바이트 그대로 왕복한다.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import posixpath
from typing import Mapping, Optional

log = logging.getLogger("jad.inject")

#: worker env 키 — config.yaml **원문**(base64(UTF-8)).
ENV_CONFIG = "JAD_INJECT_CONFIG"

#: worker env 키 — 시크릿 묶음. base64(UTF-8(JSON))이고, JSON 은
#: {"<secrets.base_dir 상대 ref>": "<base64(파일 내용)>"} 모양이다.
ENV_SECRETS = "JAD_INJECT_SECRETS"

#: worker 컨테이너에서 config.yaml 이 놓이는 자리(WORKDIR=/app 기준 상대 경로와 동치).
DEFAULT_CONFIG_DEST = "/app/config/config.yaml"

#: worker 컨테이너의 시크릿 루트(env ``SECRETS_DIR`` 이 우선).
DEFAULT_SECRETS_DIR = "/run/secrets"

#: 주입 env 한 개의 상한(base64 문자열 기준). 리눅스 execve 의 단일 env 문자열
#: 한도(MAX_ARG_STRLEN=128KiB)에 여유를 둔 값이다. 넘으면 **조용히 자르지 않고**
#: 예외로 알린다 — 조용한 절단이야말로 이 작업이 없애려는 실패 모드다.
MAX_INJECT_BYTES = 96 * 1024

#: 기록 권한(시크릿은 소유자만, config 는 일반 읽기).
SECRET_FILE_MODE = 0o600
SECRET_DIR_MODE = 0o700
CONFIG_FILE_MODE = 0o644


class InjectError(ValueError):
    """주입 페이로드 인코딩/디코딩/기록 실패."""


# ---------------------------------------------------------------------------
# 인코딩(central 쪽)
# ---------------------------------------------------------------------------


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001 — 어떤 디코딩 실패든 계약 위반으로 정규화
        raise InjectError(
            f"주입 페이로드 base64 디코딩 실패: {type(exc).__name__}"
        ) from exc


def _guard_size(name: str, encoded: str) -> str:
    """단일 env 상한 초과를 **예외**로 알린다(조용한 절단 금지)."""
    if len(encoded) > MAX_INJECT_BYTES:
        raise InjectError(
            f"{name} 주입 페이로드가 상한을 넘었습니다"
            f"({len(encoded)} > {MAX_INJECT_BYTES} 바이트). "
            "config.yaml 이 비정상적으로 크거나, 시크릿 참조가 파일이 아닌지 확인하세요."
        )
    return encoded


def encode_config(text: str) -> str:
    """config.yaml 원문 → base64 문자열(worker env 값)."""
    return _guard_size(ENV_CONFIG, _b64encode(text.encode("utf-8")))


def encode_secrets(files: Mapping[str, str]) -> str:
    """``{ref: 내용}`` → base64(JSON) 문자열(worker env 값).

    ``ref`` 는 ``secrets.base_dir`` 상대 경로(예: alice/jira-token)이고, worker 는 자기
    ``SECRETS_DIR`` 아래 **같은 상대 경로**로 되살린다 — 그래서 기존 ``*_REF``/``*_FILE``
    계약(:meth:`app.agent_runner.UserCreds.from_env`)과 ``read_secret`` 이 손대지 않아도
    그대로 동작한다.
    """
    payload = {ref: _b64encode(content.encode("utf-8")) for ref, content in files.items()}
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return _guard_size(ENV_SECRETS, _b64encode(blob.encode("utf-8")))


# ---------------------------------------------------------------------------
# 디코딩·기록(worker 쪽)
# ---------------------------------------------------------------------------


def decode_config(value: str) -> str:
    """:func:`encode_config` 의 역."""
    return _b64decode(value).decode("utf-8")


def decode_secrets(value: str) -> dict:
    """:func:`encode_secrets` 의 역 → ``{ref: 내용}``."""
    raw = _b64decode(value).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InjectError(f"주입 시크릿 JSON 파싱 실패: {exc}") from exc
    if not isinstance(payload, dict):
        raise InjectError("주입 시크릿 페이로드는 매핑이어야 합니다")
    return {str(ref): _b64decode(str(enc)).decode("utf-8") for ref, enc in payload.items()}


def is_safe_ref(ref: str) -> bool:
    """``ref`` 가 시크릿 루트 **안쪽**을 가리키는 상대 경로인지(경로 탈출 차단).

    절대경로·``..``·빈 문자열·드라이브 표기를 거부한다. 심층 방어다 — ref 는 central 이
    만들지만, 조작된 레지스트리 한 줄이 컨테이너 아무 경로나 덮어쓰게 두지 않는다.
    """
    if not ref or not str(ref).strip():
        return False
    norm = str(ref).replace("\\", "/")
    if norm.startswith("/") or (len(norm) > 1 and norm[1] == ":"):
        return False
    return ".." not in norm.split("/")


def _write_text(path: str, text: str, *, file_mode: int, dir_mode: int) -> None:
    """UTF-8(BOM 없음)·LF 로 기록하고 권한을 조인다(POLICY-ENCODING)."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
        try:
            os.chmod(parent, dir_mode)
        except OSError:  # Windows 등 chmod 미지원 — 무해
            pass

    def _opener(p, flags):
        return os.open(p, flags, file_mode)

    with open(path, "w", encoding="utf-8", newline="\n", opener=_opener) as fh:
        fh.write(text)
    try:
        os.chmod(path, file_mode)
    except OSError:  # Windows 등 chmod 미지원 — 무해
        pass


def secret_dest(secrets_dir: str, ref: str) -> str:
    """``ref`` 를 이 컨테이너의 절대 경로로(호스트 경로 개념 없음)."""
    rel = posixpath.normpath(str(ref).replace("\\", "/"))
    return os.path.join(secrets_dir, *rel.split("/"))


def write_secret(secrets_dir: str, ref: str, value: str) -> str:
    """시크릿 **값**을 ``<secrets_dir>/<ref>`` 에 0600(부모 0700)으로 쓰고 경로를 돌려준다.

    "설정에는 참조만, 값은 0600 파일로" 규율의 **쓰기 쪽 단일 원천**이다. 워커 부팅의
    :func:`materialize` 와 설치 마법사(:mod:`app.setup_wizard`)가 같은 함수를 쓴다 —
    권한·인코딩 규칙이 두 벌이 되면 한쪽이 반드시 낡는다(마법사가 0644 로 쓰면
    ``doctor --only secrets`` 가 그제서야 잡는다).

    ⚠️ 값은 반환값·로그·예외 메시지 어디에도 싣지 않는다(경로만).

    Raises:
        InjectError: ``ref`` 가 시크릿 루트를 벗어날 때(경로 탈출 차단).
    """
    if not is_safe_ref(ref):
        raise InjectError(f"시크릿 ref 가 시크릿 루트를 벗어납니다: {ref!r}")
    path = secret_dest(secrets_dir, ref)
    _write_text(path, value, file_mode=SECRET_FILE_MODE, dir_mode=SECRET_DIR_MODE)
    return path


def materialize(env: Optional[Mapping] = None, *,
                config_dest: str = DEFAULT_CONFIG_DEST,
                secrets_dir: str = "") -> dict:
    """주입 env 를 읽어 이 컨테이너 안에 파일로 되살린다(worker 부팅 1단계).

    - ``JAD_INJECT_CONFIG`` → ``config_dest`` (0644).
    - ``JAD_INJECT_SECRETS`` → ``<secrets_dir>/<ref>`` (0600, 부모 0700).

    주입 env 가 **없으면 아무 것도 하지 않는다**(no-op) — 옛 배포·수동 기동처럼 파일을
    직접 마운트해 주는 경우를 그대로 지원한다.

    ⚠️ 시크릿 **값**은 로그·예외 메시지에 절대 싣지 않는다(경로와 개수만).

    Args:
        env: 환경 매핑(기본 ``os.environ``).
        config_dest: config.yaml 을 쓸 경로.
        secrets_dir: 시크릿 루트(비면 env ``SECRETS_DIR``, 그것도 비면
            :data:`DEFAULT_SECRETS_DIR`).

    Returns:
        ``{"config": <경로 or None>, "secrets": [<경로>, ...]}``.

    Raises:
        InjectError: 페이로드가 깨졌거나 ref 가 시크릿 루트를 벗어날 때. 조용히
            넘어가지 않는다 — 이 단계의 침묵이 곧 "빈 디렉토리 마운트" 재현이다.
    """
    env = os.environ if env is None else env
    out: dict = {"config": None, "secrets": []}

    raw_config = str(env.get(ENV_CONFIG, "") or "").strip()
    if raw_config:
        _write_text(config_dest, decode_config(raw_config),
                    file_mode=CONFIG_FILE_MODE, dir_mode=0o755)
        out["config"] = config_dest
        log.info("주입 config materialize 완료 → %s", config_dest)

    raw_secrets = str(env.get(ENV_SECRETS, "") or "").strip()
    if raw_secrets:
        root = secrets_dir or str(env.get("SECRETS_DIR", "") or "") or DEFAULT_SECRETS_DIR
        for ref, content in decode_secrets(raw_secrets).items():
            out["secrets"].append(write_secret(root, ref, content))
        # 값은 로깅하지 않는다 — 개수만.
        log.info("주입 시크릿 materialize 완료 — %d개 (루트=%s)", len(out["secrets"]), root)

    return out
