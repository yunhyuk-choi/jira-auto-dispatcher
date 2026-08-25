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

import contextlib
import json
import os
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Optional

# 기본 상태 디렉토리(재지정 가능). 파일명은 이 디렉토리 기준.
_DEFAULT_STATE_DIR = os.environ.get("JAD_STATE_DIR", "state")
_state_dir = _DEFAULT_STATE_DIR

# 파일 basename(디렉토리는 _state_dir로 조합).
JOBS_NAME = "jobs.json"
WATERMARK_NAME = "watermark.json"
CANCEL_WATERMARK_NAME = "cancel_watermark.json"  # 취소 감시축 updated 커서(§10.2)
REOPEN_WATERMARK_NAME = "reopen_watermark.json"  # 재오픈 감시축 updated 커서(§10.2)
ASSIGNEE_WATERMARK_NAME = "assignee_watermark.json"  # 담당자-변경 트리거축(B) 폴 시각 커서
PENDING_RESOLUTION_NAME = "pending_resolution.json"  # central AI 한도(축1) 미해석 대기 티켓
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


def _now_iso() -> str:
    """활동시각(updated_at) 스탬프용 ISO-8601 **UTC** 문자열.

    잡 레코드의 마지막 활동시각은 파이썬 코드로만 찍는다(오케스트레이터/claude 무관).
    :class:`app.queue.JobQueue` 의 ``_now_iso`` 와 동일 규약 — 관리 콘솔이 KST 로 변환·표시.
    """
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 크로스프로세스 파일락(jobs.json read-modify-write 경계)
# ---------------------------------------------------------------------------
#
# 문제(프랙탈 P2 관측성): 이제 **여러 프로세스**가 jobs.json 을 read-modify-write 한다 —
#   (1) central 프로세스의 poller 스레드/스케줄러(:class:`app.queue.JobQueue`),
#   (2) ``worker_dispatch.py`` 프로세스(running/failed 전이),
#   (3) ``gchat.py`` 프로세스(done 마킹),
#   (4) ``track.py`` 프로세스(에이전트 세만틱 기록).
# 모두 central 컨테이너 안의 별개 프로세스이며 같은 jad-state 볼륨(/app/state)을 공유한다.
# 인프로세스 ``_io_lock`` 만으로는 프로세스 간을 조율하지 못해 동시 갱신이 서로 덮어써
# **유실**된다. 그래서 jobs.json 의 load→수정→save 전체 경계를 크로스프로세스 advisory
# 파일락(fcntl.flock)으로 직렬화한다. 패턴은 :mod:`app.repos` 의 ``_flock_fd``/``.lock``
# advisory-lock 을 재사용한다(프로덕션=리눅스 컨테이너, Windows/테스트는 best-effort 무락).

_JOBS_LOCK_SUFFIX = ".lock"


def _jobs_lock_path() -> str:
    """jobs.json 의 락파일 경로(``<jobs.json>.lock``). 현재 _state_dir 기준."""
    return _path(JOBS_NAME) + _JOBS_LOCK_SUFFIX


def _flock_ex(fd: int) -> bool:
    """fd 에 배타적 파일락(blocking). 성공 True. flock 미지원(Windows 등)이면 False.

    프로덕션은 리눅스 컨테이너라 fcntl.flock 으로 프로세스 간(공유 볼륨 위 락파일)
    상호배제된다. fcntl 이 없는 환경(테스트 Windows)에서는 무락으로 진행한다
    (best-effort — 그 환경엔 동시 프로세스가 없으니 무해).
    """
    try:
        import fcntl  # noqa: PLC0415 — POSIX 전용, 지연 import
    except ImportError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def jobs_lock():
    """jobs.json read-modify-write 를 **크로스프로세스로 직렬화**하는 advisory 파일락.

    락파일(``<state>/jobs.json.lock``)을 공유 볼륨 위에 두어 fcntl.flock 이 central
    컨테이너의 모든 프로세스(central·worker_dispatch·gchat·track) 사이에서 상호배제한다.
    락을 걸 수 없는 환경(fcntl 부재·경로 생성 불가)에서는 조용히 무락으로 진행한다
    (락 실패로 갱신을 막지 않는다). ``os.close`` 가 flock 도 해제한다.

    이중 보호: (a) flock 으로 **프로세스 간**(리눅스), (b) 프로세스 내 ``_io_lock``(RLock)로
    **스레드 간**(전 플랫폼) 상호배제한다 — flock 이 없는 환경(Windows/테스트)에서도
    같은 프로세스의 JobQueue 스레드와 record_job_event 호출이 _io_lock 으로 직렬화돼 유실이
    없다. flock 은 오래 걸릴 수 있으므로 _io_lock 은 flock 을 얻은 **뒤에** 잡아, flock 대기
    중 무관한 파일 I/O(watermark 등)를 막지 않는다(락 순서 flock→_io_lock 고정).

    ⚠️ flock 재진입 금지: 같은 프로세스가 이 락을 **중첩** 획득하면(서로 다른 fd) 자기
    자신과 교착한다. 한 쓰기 경로는 이 락을 한 번만 잡고 그 안에서 load→수정→save 를
    끝낸다(다른 락-획득 함수를 락 안에서 호출하지 않는다). ``_io_lock`` 은 RLock 이라
    load_jobs/save_jobs 의 내부 재획득은 안전하다.
    """
    lock_path = _jobs_lock_path()
    fd: Optional[int] = None
    try:
        parent = os.path.dirname(lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        fd = None
    if fd is not None:
        _flock_ex(fd)
    try:
        with _io_lock:  # 프로세스 내 스레드 간 상호배제(flock 부재 환경도 보호).
            yield
    finally:
        if fd is not None:
            try:
                os.close(fd)  # close 가 flock 도 해제(프로세스 종료·close 시 자동 해제).
            except OSError:
                pass


def read_modify_write_jobs(mutator: Callable[[list], Any]) -> Any:
    """jobs.json 을 크로스프로세스 락 안에서 **load→mutate→save** (유실 방지 단일 경계).

    ``mutator(jobs_list)`` 로 현재 잡 목록(dict 리스트)을 넘겨 in-place 수정하게 한 뒤,
    수정된 목록을 원자적으로 되쓴다. mutator 의 반환값을 그대로 돌려준다(예: 갱신된
    레코드 사본). 락(jobs_lock, 프로세스 간) + _io_lock(스레드 간)을 함께 잡아 로드와
    저장 사이에 다른 writer 가 끼어들지 못하게 한다 — 이것이 "각 writer 가 락 안에서
    load→수정→save" 규약의 단일 구현점이다.
    """
    with jobs_lock():
        with _io_lock:
            jobs = load_json(_path(JOBS_NAME), [])
            if not isinstance(jobs, list):
                jobs = []
            result = mutator(jobs)
            atomic_write_json(_path(JOBS_NAME), jobs)
            return result


# 상태 문자열 상수(app.queue 의 값을 미러 — 순환 import 방지를 위해 리터럴로 둔다).
# ⚠️ app.queue 의 CANCELLED/HANDED_OFF/CANCELLING/HANDING_OFF 와 값이 일치해야 한다.
_DEFAULT_PROTECT_STATUSES = frozenset(
    {"cancelled", "handed_off", "cancelling", "handing_off"}
)

# Job 스키마의 알려진 스칼라/딕트 필드(app.queue.Job 미러). 그 외 키는 meta 로 보낸다.
_KNOWN_JOB_FIELDS = frozenset(
    {
        "user", "autonomy_mode", "session_id", "branch", "reset_at", "mr_url",
        "log_summary", "attempts", "cancel_requested", "cancel_reason",
        "control_action", "continue_from_wip", "correlation_id",
        "target_repos", "context_refs", "audit_refs", "meta",
    }
)


def _merge_meta(dst: dict, src: dict) -> None:
    """meta 를 1단계 깊이로 병합(중첩 dict 은 update, 그 외는 덮어씀)."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            dst[k].update(v)
        else:
            dst[k] = v


def record_job_event(
    ticket: str,
    *,
    status: Optional[str] = None,
    create_if_missing: bool = True,
    defaults: Optional[dict] = None,
    protect: frozenset = _DEFAULT_PROTECT_STATUSES,
    meta: Optional[dict] = None,
    **fields: Any,
) -> Optional[dict]:
    """한 티켓의 잡 레코드를 크로스프로세스 안전하게 갱신/생성한다(별개 프로세스용 단일 API).

    ``worker_dispatch.py``/``gchat.py``/``track.py`` 가 jobs.json(= central 의 JobQueue 와
    같은 store)에 직접 라이프사이클을 기록하는 경로다. :func:`read_modify_write_jobs` 위에서
    동작하므로 central 의 JobQueue(같은 락)와 서로 덮어쓰지 않는다.

    - ``status`` 는 현재 상태가 ``protect`` 집합(기본: 취소/이관 계열)에 있으면 **덮어쓰지
      않는다** — 진행 중 취소/핸드오프 신호를 관측성 계측이 되돌리지 않게 한다.
    - 알려진 Job 필드(``mr_url``/``log_summary``/``user``/``branch``/``target_repos`` 등)는
      레코드 최상위에, 그 외 키워드는 ``meta`` 아래에 기록한다. ``meta=`` 는 1단계 병합.
    - 레코드가 없고 ``create_if_missing`` 이면 최소 레코드(``status="queued"`` + defaults)를
      만든 뒤 갱신한다(뼈대가 이미 만들었을 가능성이 높지만 방어적으로).

    반환: 갱신된 레코드 사본(없고 미생성이면 None).
    """
    ticket = (ticket or "").strip()
    if not ticket:
        return None

    def _mut(jobs: list) -> Optional[dict]:
        rec: Optional[dict] = None
        for d in jobs:
            if isinstance(d, dict) and str(d.get("ticket", "")) == ticket:
                rec = d
                break
        created = False
        if rec is None:
            if not create_if_missing:
                return None
            rec = {"ticket": ticket, "status": "queued"}
            if defaults:
                for k, v in defaults.items():
                    if v is not None:
                        rec.setdefault(k, v)
            jobs.append(rec)
            created = True

        changed = created
        if status is not None:
            cur = str(rec.get("status", ""))
            if cur not in protect and status != cur:
                rec["status"] = status
                changed = True
        for k, v in fields.items():
            if v is None:
                continue
            if k in _KNOWN_JOB_FIELDS:
                rec[k] = v
            else:
                m = rec.get("meta")
                if not isinstance(m, dict):
                    m = {}
                    rec["meta"] = m
                m[k] = v
            changed = True
        if meta:
            m = rec.get("meta")
            if not isinstance(m, dict):
                m = {}
            _merge_meta(m, meta)
            rec["meta"] = m
            changed = True
        if changed:
            rec["updated_at"] = _now_iso()
        return dict(rec)

    return read_modify_write_jobs(_mut)


def resolve_runtime_state_dir() -> str:
    """컨테이너 런타임에서 jobs.json 등을 둘 상태 디렉토리를 결정적으로 앵커한다.

    별개 프로세스(worker_dispatch/gchat/track)는 cwd=<workspace>/orchestrator 에서
    ``python /app/*.py`` 로 불려 상대 "state" 가 볼륨(/app/state)을 빗나간다. 그래서:
      1) env ``JAD_STATE_DIR`` 이 있으면 그것(배포 오버라이드/테스트 격리).
      2) 없으면 현재 _state_dir. 절대경로면 그대로(테스트 격리 dir 존중), 상대경로면
         컨테이너(``/app`` 존재)에서만 ``/app`` 에 앵커(= /app/state, jad-state 볼륨).
         컨테이너가 아니면(테스트 머신) 상대값을 그대로 둔다(``/app`` 를 만들지 않는다).
    (gchat 의 기존 ``_resolve_state_dir`` 와 정합 — 그쪽도 이 함수로 위임한다.)
    """
    d = os.environ.get("JAD_STATE_DIR")
    if d:
        return d
    base = _state_dir or "state"
    if os.path.isabs(base):
        return base
    if os.path.isdir("/app"):
        return os.path.join("/app", base)
    return base


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


def load_cancel_watermark() -> Any:
    """취소 감시축 updated-watermark 로드(§10.2; 없으면 None)."""
    return load_json(_path(CANCEL_WATERMARK_NAME), None)


def save_cancel_watermark(value: Any) -> None:
    """취소 감시축 updated-watermark 저장(원자적)."""
    atomic_write_json(_path(CANCEL_WATERMARK_NAME), value)


def load_reopen_watermark() -> Any:
    """재오픈 감시축 updated-watermark 로드(§10.2; 없으면 None)."""
    return load_json(_path(REOPEN_WATERMARK_NAME), None)


def save_reopen_watermark(value: Any) -> None:
    """재오픈 감시축 updated-watermark 저장(원자적)."""
    atomic_write_json(_path(REOPEN_WATERMARK_NAME), value)


def load_assignee_watermark() -> Any:
    """담당자-변경 트리거축(B) 폴 시각 watermark 로드(없으면 None)."""
    return load_json(_path(ASSIGNEE_WATERMARK_NAME), None)


def save_assignee_watermark(value: Any) -> None:
    """담당자-변경 트리거축(B) 폴 시각 watermark 저장(원자적)."""
    atomic_write_json(_path(ASSIGNEE_WATERMARK_NAME), value)


def load_pending_resolution(default: Any = None) -> Any:
    """central AI 한도(축1) 미해석 대기 티켓 목록 로드(없으면 빈 목록).

    쿨다운 동안 수신은 됐으나(claim 유지) 아직 레포 해석(claude)을 못 한 티켓들.
    각 원소는 ``{"key": ..., "issue": {...}}`` (드레인이 저장된 issue로 재해석).
    """
    return load_json(_path(PENDING_RESOLUTION_NAME), default if default is not None else [])


def save_pending_resolution(value: Any) -> None:
    """central AI 한도(축1) 미해석 대기 티켓 목록 저장(원자적)."""
    atomic_write_json(_path(PENDING_RESOLUTION_NAME), value)


def load_registry() -> Any:
    """등록 사용자 레지스트리 로드(없으면 빈 목록)."""
    return load_json(_path(REGISTRY_NAME), [])


def save_registry(value: Any) -> None:
    """등록 사용자 레지스트리 저장(원자적).

    시크릿 "값"이 실수로 섞여도 여기서는 그대로 기록한다 — 방어는 registry.py의
    upsert(참조만 저장)가 담당한다.
    """
    atomic_write_json(_path(REGISTRY_NAME), value)
