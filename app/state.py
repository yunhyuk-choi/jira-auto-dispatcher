"""영속 계층 — state/*.json.

역할:
    재시작에도 살아남아야 하는 상태를 파일로 영속한다:
        - jobs        사용자별 잡 스토어(queue.py/dispatch.py가 사용)
        - watermark   폴러 high-watermark(마지막으로 처리한 updated 시각/커서)
        - dedup       이미 claim 된 티켓 집합(gate.py가 사용)
        - registry    등록 사용자 레코드(registry.py가 사용, 온보딩으로 채워짐)

역할 소속: **central** (worker는 state를 영속하지 않는다 — 무상태 실행체).

구현 Phase: **Phase 1** (config + state 영속).

참고:
    - 원자적 쓰기(임시파일 → os.replace)로 부분 기록 손상 방지.
    - POLICY-ENCODING: JSON은 UTF-8(BOM 없음)·LF, ensure_ascii=False.
    - state/ 디렉토리는 gitignore(런타임 산출물).
"""

from __future__ import annotations

from typing import Any

STATE_DIR = "state"
JOBS_FILE = "state/jobs.json"
WATERMARK_FILE = "state/watermark.json"
DEDUP_FILE = "state/dedup.json"
REGISTRY_FILE = "state/registry.json"
SECRETS_DIR = "state/secrets"  # 사용자 시크릿(참조 대상). gitignore.


def ensure_state_dir() -> None:
    """state/ 디렉토리를 보장(없으면 생성).

    TODO(Phase 1): os.makedirs(exist_ok=True).
    """
    raise NotImplementedError("TODO(Phase 1): state 디렉토리 보장")


def load_json(path: str, default: Any = None) -> Any:
    """JSON 파일을 읽어 반환(없으면 default).

    TODO(Phase 1): UTF-8 읽기 + json.load. 손상 시 복구 정책.
    """
    raise NotImplementedError("TODO(Phase 1): JSON 로드")


def atomic_write_json(path: str, data: Any) -> None:
    """JSON을 원자적으로 기록(임시파일 → os.replace).

    TODO(Phase 1): 임시파일 write → flush/fsync → os.replace.
    """
    raise NotImplementedError("TODO(Phase 1): 원자적 JSON 쓰기")


def load_watermark() -> Any:
    """폴러 high-watermark 로드.

    TODO(Phase 1): WATERMARK_FILE 읽기.
    """
    raise NotImplementedError("TODO(Phase 1): watermark 로드")


def save_watermark(value: Any) -> None:
    """폴러 high-watermark 저장.

    TODO(Phase 1): WATERMARK_FILE 원자적 저장.
    """
    raise NotImplementedError("TODO(Phase 1): watermark 저장")


def load_registry() -> Any:
    """등록 사용자 레지스트리 로드(registry.py가 사용).

    TODO(Phase 1): REGISTRY_FILE 읽기(없으면 빈 목록).
    """
    raise NotImplementedError("TODO(Phase 1): registry 로드")


def save_registry(value: Any) -> None:
    """등록 사용자 레지스트리 저장(registry.py가 사용).

    TODO(Phase 1): REGISTRY_FILE 원자적 저장(시크릿 값 미포함 방어).
    """
    raise NotImplementedError("TODO(Phase 1): registry 저장")
