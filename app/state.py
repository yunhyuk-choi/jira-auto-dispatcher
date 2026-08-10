"""영속 계층 — state/*.json.

역할:
    재시작에도 살아남아야 하는 상태를 파일로 영속한다:
        - jobs        사용자별 잡 스토어(queue.py/dispatch.py/scheduler.py가 사용)
        - watermark   폴러 high-watermark(마지막으로 처리한 created 시각/커서)
        - dedup       이미 claim 된 티켓 집합(gate.py가 사용)
        - registry    등록 사용자 레코드(registry.py가 사용, 온보딩으로 채워짐)

역할 소속: **central** (worker는 state를 영속하지 않는다 — 무상태 실행체).

구현 Phase: **Phase 1** (config + state 영속).

참고:
    - 원자적 쓰기(임시파일 → os.replace)로 부분 기록 손상 방지.
    - POLICY-ENCODING: JSON은 UTF-8(BOM 없음)·LF, ensure_ascii=False.
    - state/ 디렉토리는 gitignore(런타임 산출물).
    - 상태 디렉토리는 :func:`set_state_dir` 로 재지정 가능하다(테스트 격리).
      env ``JAD_STATE_DIR`` 이 있으면 import 시 초기값으로 사용한다.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

# 기본 상태 디렉토리(재지정 가능). 파일명은 이 디렉토리 기준.
_DEFAULT_STATE_DIR = os.environ.get("JAD_STATE_DIR", "state")
_state_dir = _DEFAULT_STATE_DIR

# 파일 basename(디렉토리는 _state_dir로 조합).
JOBS_NAME = "jobs.json"
WATERMARK_NAME = "watermark.json"
DEDUP_NAME = "dedup.json"
REGISTRY_NAME = "registry.json"
SECRETS_SUBDIR = "secrets"  # 사용자 시크릿(참조 대상). gitignore.

# 파일 I/O 직렬화 락(프로세스 내 다중 스레드 안전).
_io_lock = threading.RLock()

# --- 하위호환 상수(과거 코드가 절대경로 상수를 참조할 수 있어 유지) ---
STATE_DIR = _state_dir
JOBS_FILE = os.path.join(_state_dir, JOBS_NAME)
WATERMARK_FILE = os.path.join(_state_dir, WATERMARK_NAME)
DEDUP_FILE = os.path.join(_state_dir, DEDUP_NAME)
REGISTRY_FILE = os.path.join(_state_dir, REGISTRY_NAME)
SECRETS_DIR = os.path.join(_state_dir, SECRETS_SUBDIR)


def set_state_dir(path: str) -> None:
    """상태 디렉토리를 재지정한다(테스트 격리 / 배포 볼륨 지정).

    이후 모든 load/save 헬퍼가 이 디렉토리를 기준으로 동작한다.
    """
    global _state_dir, STATE_DIR, JOBS_FILE, WATERMARK_FILE, DEDUP_FILE
    global REGISTRY_FILE, SECRETS_DIR
    _state_dir = path
    STATE_DIR = path
    JOBS_FILE = os.path.join(path, JOBS_NAME)
    WATERMARK_FILE = os.path.join(path, WATERMARK_NAME)
    DEDUP_FILE = os.path.join(path, DEDUP_NAME)
    REGISTRY_FILE = os.path.join(path, REGISTRY_NAME)
    SECRETS_DIR = os.path.join(path, SECRETS_SUBDIR)


def get_state_dir() -> str:
    """현재 상태 디렉토리."""
    return _state_dir


def _path(name: str) -> str:
    return os.path.join(_state_dir, name)


def ensure_state_dir() -> None:
    """state/ 및 state/secrets/ 디렉토리를 보장(없으면 생성)."""
    with _io_lock:
        os.makedirs(_state_dir, exist_ok=True)
        os.makedirs(os.path.join(_state_dir, SECRETS_SUBDIR), exist_ok=True)


def load_json(path: str, default: Any = None) -> Any:
    """JSON 파일을 읽어 반환(없거나 손상 시 default).

    손상(부분 기록 등)된 파일은 예외를 던지지 않고 default로 폴백한다 —
    원자적 쓰기가 정상 경로를 보장하므로 손상은 예외적 상황이다.
    """
    with _io_lock:
        if not os.path.exists(path):
            return default
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return default


def atomic_write_json(path: str, data: Any) -> None:
    """JSON을 원자적으로 기록(임시파일 → flush/fsync → os.replace).

    UTF-8(BOM 없음)·LF·ensure_ascii=False.
    """
    with _io_lock:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)


# --- 대상별 편의 헬퍼(basename → 현재 _state_dir 조합) ---


def load_jobs(default: Any = None) -> Any:
    """잡 목록 로드(없으면 default 또는 빈 리스트)."""
    return load_json(_path(JOBS_NAME), default if default is not None else [])


def save_jobs(value: Any) -> None:
    """잡 목록 저장(원자적)."""
    atomic_write_json(_path(JOBS_NAME), value)


def load_dedup(default: Any = None) -> Any:
    """dedup(claim 된 티켓 키) 목록 로드."""
    return load_json(_path(DEDUP_NAME), default if default is not None else [])


def save_dedup(value: Any) -> None:
    """dedup 목록 저장(원자적)."""
    atomic_write_json(_path(DEDUP_NAME), value)


def load_watermark() -> Any:
    """폴러 high-watermark 로드(없으면 None)."""
    return load_json(_path(WATERMARK_NAME), None)


def save_watermark(value: Any) -> None:
    """폴러 high-watermark 저장(원자적)."""
    atomic_write_json(_path(WATERMARK_NAME), value)


def load_registry() -> Any:
    """등록 사용자 레지스트리 로드(없으면 빈 목록)."""
    return load_json(_path(REGISTRY_NAME), [])


def save_registry(value: Any) -> None:
    """등록 사용자 레지스트리 저장(원자적).

    시크릿 "값"이 실수로 섞여도 여기서는 그대로 기록한다 — 방어는 registry.py의
    upsert(참조만 저장)가 담당한다.
    """
    atomic_write_json(_path(REGISTRY_NAME), value)
