"""잡 스토어 + 상태머신(중앙 전용).

역할:
    claim 된 티켓을 잡(Job)으로 만들어 보관하고, 상태를 관리한다. 잡은 소유
    사용자(user)로 태깅되며, scheduler.py가 레포락/동시성으로 dispatch 여부를
    결정하고, dispatch.py가 이 스토어를 사용자별 큐로 인덱싱해 worker에 HTTP로
    넘긴다. 재개(interrupted+reset_at)는 스케줄러가 tick에서 재적격 처리한다.

역할 소속: **central** (scheduler/dispatch의 하부 저장/상태 계층).

구현 Phase: **Phase 3** (dedup 게이트 + 큐).

상태머신:
    queued      대기(claim 직후)
    running     스케줄러가 dispatch(레포락 획득, worker가 GET /next로 수령)
    interrupted 토큰 한도 등으로 중단(reset_at 이후 재개 대상)
    done        완료
    failed      복구 불가 실패

참고:
    - 잡은 state.py(jobs.json)로 영속 → 재시작 후 running/interrupted 복원.
    - 결정적 브랜치(auto/<TICKET>) + ticket=job_id 로 잡 멱등성 보장(dedup 전제).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

from app import state

# 잡 상태 상수
QUEUED = "queued"
RUNNING = "running"
INTERRUPTED = "interrupted"
DONE = "done"
FAILED = "failed"

TERMINAL_STATUSES = frozenset({DONE, FAILED})

# 채널 F의 한글 상태 → 내부 상태 매핑(worker/오케스트레이터가 한글로 보고할 수 있음).
STATUS_ALIASES = {
    "진행중": RUNNING,
    "완료": DONE,
    "실패": FAILED,
    "중단": INTERRUPTED,
    "interrupted": INTERRUPTED,
    "running": RUNNING,
    "done": DONE,
    "failed": FAILED,
    "queued": QUEUED,
}


def normalize_status(status: str) -> str:
    """채널 F 상태 문자열(한/영)을 내부 상태 상수로 정규화."""
    return STATUS_ALIASES.get((status or "").strip(), (status or "").strip())


@dataclass
class Job:
    """단일 디스패치 잡(§3 채널 E 스키마 포함)."""

    ticket: str = ""
    user: str = ""                          # 소유 사용자(DISPATCH_USER) — per-user 귀속
    target_repos: list = field(default_factory=list)  # REPO-MAP 매핑 결과([]=미해석→전역직렬)
    autonomy_mode: str = "B"                # "A"(완전자율) | "B"(경량 1차)
    status: str = QUEUED
    session_id: Optional[str] = None        # claude --session-id (재개 키)
    branch: Optional[str] = None            # auto/<TICKET>
    reset_at: Optional[str] = None          # interrupted 시 재개 예정 시각(ISO8601)
    mr_url: Optional[str] = None            # 완료 시 worker가 회신하는 MR URL
    context_refs: dict = field(default_factory=dict)  # {runs, dlc_meta, dataspace_docs}
    log_summary: str = ""                   # 채널 F 실행 요약
    audit_refs: dict = field(default_factory=dict)    # 브랜치/커밋/저널 등
    attempts: int = 0
    meta: dict = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        """잡 식별자(= ticket; dedup으로 티켓당 1잡 보장)."""
        return self.ticket

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        d = dict(d or {})
        return Job(
            ticket=str(d.get("ticket", "")),
            user=str(d.get("user", "")),
            target_repos=list(d.get("target_repos", []) or []),
            autonomy_mode=str(d.get("autonomy_mode", "B")),
            status=str(d.get("status", QUEUED)),
            session_id=d.get("session_id"),
            branch=d.get("branch"),
            reset_at=d.get("reset_at"),
            mr_url=d.get("mr_url"),
            context_refs=dict(d.get("context_refs", {}) or {}),
            log_summary=str(d.get("log_summary", "")),
            audit_refs=dict(d.get("audit_refs", {}) or {}),
            attempts=int(d.get("attempts", 0)),
            meta=dict(d.get("meta", {}) or {}),
        )


class JobQueue:
    """영속 잡 스토어 + 상태 전이."""

    def __init__(self) -> None:
        """락 + 영속된 잡 목록 로드로 초기화(ticket 키 인덱스)."""
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        for j in state.load_jobs([]) or []:
            job = Job.from_dict(j)
            if job.ticket:
                self._jobs[job.ticket] = job

    def _persist(self) -> None:
        state.save_jobs([j.to_dict() for j in self._jobs.values()])

    def enqueue(self, job: Job) -> None:
        """새 잡을 등록(중복 티켓이면 기존을 덮지 않고 무시).

        dedup 게이트가 앞단이지만, 재시작/재적격 케이스를 위해 티켓 멱등.
        """
        with self._lock:
            if job.ticket in self._jobs:
                return
            self._jobs[job.ticket] = job
            self._persist()

    def next_queued(self, user: Optional[str] = None) -> Optional[Job]:
        """실행 대기 잡 하나(queued)를 반환. user 지정 시 그 사용자 것만.

        스케줄러의 레포락 판단과 무관한 단순 조회(테스트/보조용).
        """
        with self._lock:
            for j in self._jobs.values():
                if j.status != QUEUED:
                    continue
                if user is not None and j.user != user:
                    continue
                return j
        return None

    def set_status(self, ticket: str, status: str, **fields) -> None:
        """잡 상태/부가 필드 갱신 후 영속."""
        with self._lock:
            j = self._jobs.get(ticket)
            if j is None:
                raise KeyError(f"알 수 없는 잡: {ticket}")
            j.status = normalize_status(status)
            for k, v in fields.items():
                if v is None:
                    continue
                if hasattr(j, k):
                    setattr(j, k, v)
                else:
                    j.meta[k] = v
            self._persist()

    def update(self, ticket: str, **fields) -> None:
        """상태 변경 없이 부가 필드만 갱신(진행중 보고용)."""
        with self._lock:
            j = self._jobs.get(ticket)
            if j is None:
                raise KeyError(f"알 수 없는 잡: {ticket}")
            for k, v in fields.items():
                if v is None:
                    continue
                if hasattr(j, k):
                    setattr(j, k, v)
                else:
                    j.meta[k] = v
            self._persist()

    def clear_fields(self, ticket: str, *names: str) -> None:
        """지정 필드를 기본값(None/빈)으로 명시적으로 비운다.

        set_status/update는 None을 "미변경"으로 보므로, 값을 실제로 지울 때 사용.
        """
        with self._lock:
            j = self._jobs.get(ticket)
            if j is None:
                raise KeyError(f"알 수 없는 잡: {ticket}")
            for name in names:
                if name in ("reset_at", "mr_url", "session_id", "branch"):
                    setattr(j, name, None)
                elif hasattr(j, name):
                    setattr(j, name, type(getattr(j, name))())
            self._persist()

    def get(self, ticket: str) -> Optional[Job]:
        """티켓으로 잡 조회."""
        with self._lock:
            return self._jobs.get(ticket)

    def list_jobs(self) -> list:
        """전체 잡 목록 스냅샷(관리 UI 현황용)."""
        with self._lock:
            return list(self._jobs.values())
