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
    cancelling  취소 요청 접수(실행 중 잡) — worker에 취소 플래그 전달, 회신 대기
    cancelled   취소 확정(worker abort+롤백 회신 또는 큐 대기분 드롭) — 종결
    handing_off 담당자 변경 핸드오프 요청 접수(실행 중 잡) — worker에 checkpoint
                플래그 전달, 회신 대기(레포락 유지). **취소와 다르다 — 롤백 없이 WIP 보존**.
    handed_off  핸드오프 확정(worker가 WIP를 커밋·push하고 회신) — 종결이되 "WIP 보존,
                소유권 이관"을 뜻한다(cancelled=롤백과 구별).
    done        완료
    failed      복구 불가 실패

취소/재오픈(RECURSIVE-DISPATCH §10):
    - `취소됨`(Jira 상태)이 신호. `완료`(정상)와 statusCategory가 같으므로 **이름**으로만
      구분한다. `취소됨`=중단+롤백, `완료`=정상 종료(롤백 X).
    - cancelling/cancelled는 이 신호를 잡 상태머신으로 실현한 것이다(§10.3).
    - cancelled는 종결이지만 dedup는 해제된다 — 재오픈(취소됨→해야할일) 시 같은 티켓을
      다시 claim/enqueue 하기 위함(§10.4).

담당자 변경 = 핸드오프(재배정 ≠ 취소):
    - 담당자가 X→Y로 바뀌고 티켓에 X 소유 활성 잡이 있으면 **취소가 아니라 이관**이다.
    - 취소 = abort + 롤백(WIP 폐기). 핸드오프 = checkpoint(커밋·push로 WIP 보존) + 이관.
    - handing_off/handed_off는 이 이관 신호를 잡 상태머신으로 실현한 것이다.
    - handed_off도 종결이므로 dedup 해제 대상이지만, Y로 이어서 이관(continue) 시에는
      같은 티켓 슬롯을 재-소유(reown)하므로 claim을 유지한다(park 시에만 해제).

참고:
    - 잡은 state.py(jobs.json)로 영속 → 재시작 후 running/interrupted 복원.
    - 결정적 브랜치(auto/<TICKET>) + ticket=job_id 로 잡 멱등성 보장(dedup 전제).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from app import state


def _now_iso() -> str:
    """활동시각 스탬프용 ISO-8601 **UTC** 문자열.

    잡의 마지막 활동시각(updated_at)은 **파이썬 코드로만** 기록한다(오케스트레이터/
    claude 무관·비용 무시 가능). 관리 콘솔은 이 UTC 값을 KST로 변환해 표시한다.
    """
    return datetime.now(timezone.utc).isoformat()

# 잡 상태 상수
QUEUED = "queued"
RUNNING = "running"
INTERRUPTED = "interrupted"
CANCELLING = "cancelling"
CANCELLED = "cancelled"
HANDING_OFF = "handing_off"     # 핸드오프 요청 접수(실행 중) — checkpoint 회신 대기(레포락 유지)
HANDED_OFF = "handed_off"       # 핸드오프 확정 — WIP 보존·소유권 이관(롤백 X)로 종결
DONE = "done"
FAILED = "failed"

# 프랙탈(센트럴 에이전트 주도) 잡 표식. meta[FRACTAL_META_KEY]=True 이면 이 잡은
# 상주 센트럴 라이브 세션이 조율·실행하며, **구 경로(HTTP 디스패치) 스케줄러가
# 디스패치하지 않는다**(이중 실행 방지 — scheduler._eligible_now 가 이 표식을 건너뛴다).
# 관측성만을 위해 JobQueue(같은 store)에 레코드로 실리며 대시보드에 뜬다.
FRACTAL_META_KEY = "fractal"

# 취소 사유(cancel_reason) 상수 — cancelled 잡이 *왜* 취소됐는지 구별한다.
# ⚠️ **취소됨(Jira 상태)과 추적해제(opt-out 라벨)는 반드시 구별**된다(관리 UI 핵심 요구):
#   전자는 사람이 티켓을 취소한 것, 후자는 자동화 추적만 뗀 것으로 의미가 전혀 다르다.
CANCEL_STATUS_CANCELLED = "status_cancelled"   # Jira 상태 = 취소됨(중단+롤백)
CANCEL_UNTRACKED_OPTOUT = "untracked_optout"   # 추적 해제 라벨(opt-out) — 자동화만 중단
CANCEL_REASSIGNED = "reassigned"               # 담당자 재배정으로 큐 대기분 드롭(park)
CANCEL_MANUAL = "manual"                        # UI 수동 취소
CANCEL_OTHER = "other"                          # 기타/미분류

# 종결 상태(레포 락을 더 이상 점유하지 않는다). cancelled·handed_off 포함.
# handed_off는 "WIP 보존·이관"으로 종결되지만 락 점유 관점에선 종결이다(cancelled와 동일).
TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED, HANDED_OFF})

# 활성 상태(레포 락·동시성 cap을 점유한다). cancelling은 worker가 아직 abort/롤백
# 중이라 레포를 붙들고 있으므로 running과 동일하게 점유로 센다(§10.3). handing_off도
# worker가 아직 checkpoint(커밋·push) 중이라 레포를 붙들고 있으므로 점유로 센다.
ACTIVE_STATUSES = frozenset({RUNNING, CANCELLING, HANDING_OFF})

# 채널 F의 한글 상태 → 내부 상태 매핑(worker/오케스트레이터가 한/영으로 보고할 수 있음).
STATUS_ALIASES = {
    "진행중": RUNNING,
    "진행 중": RUNNING,
    "완료": DONE,
    "실패": FAILED,
    "중단": INTERRUPTED,
    # 취소: Jira 상태 이름과 내부 상태를 함께 흡수. "취소됨"=취소 확정 회신.
    "취소됨": CANCELLED,
    "취소중": CANCELLING,
    "취소 중": CANCELLING,
    # 핸드오프(담당자 변경 이관) — 취소와 구별되는 별도 신호.
    "핸드오프": HANDED_OFF,
    "이관": HANDED_OFF,
    "이관중": HANDING_OFF,
    "이관 중": HANDING_OFF,
    "interrupted": INTERRUPTED,
    "running": RUNNING,
    "cancelling": CANCELLING,
    "cancelled": CANCELLED,
    "canceled": CANCELLED,
    "handing_off": HANDING_OFF,
    "handed_off": HANDED_OFF,
    "handedoff": HANDED_OFF,
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
    correlation_id: Optional[str] = None    # 명시적 요청/응답 correlation id(Phase 3b-1). None=티켓(job_id)로 폴백 → 하위호환. 위임 시 register_pending이 티켓으로 명시 세팅.
    target_repos: list = field(default_factory=list)  # REPO-MAP 매핑 결과([]=미해석→전역직렬)
    autonomy_mode: str = "B"                # "A"(완전자율) | "B"(경량 1차)
    status: str = QUEUED
    session_id: Optional[str] = None        # claude --session-id (재개 키)
    branch: Optional[str] = None            # auto/<TICKET>
    reset_at: Optional[str] = None          # interrupted 시 재개 예정 시각(ISO8601)
    updated_at: Optional[str] = None        # 마지막 활동시각(ISO8601 UTC) — 코드가 매 쓰기경로에서 스탬프. UI가 KST로 표시·정렬.
    mr_url: Optional[str] = None            # 완료 시 worker가 회신하는 MR URL
    context_refs: dict = field(default_factory=dict)  # {runs, dlc_meta, docs}
    log_summary: str = ""                   # 채널 F 실행 요약
    audit_refs: dict = field(default_factory=dict)    # 브랜치/커밋/저널 등
    attempts: int = 0
    cancel_requested: bool = False          # 취소 제어 플래그(worker가 control 폴링 §10.4)
    cancel_reason: Optional[str] = None     # 취소 사유(위 CANCEL_* 상수). cancelled 잡만 의미 있음.
    control_action: str = "none"            # 제어 액션(none|cancel|handoff) — worker control 폴링
    continue_from_wip: bool = False         # 이관받은 continue 잡: 브랜치에 선행 WIP 존재(프롬프트 힌트)
    meta: dict = field(default_factory=dict)

    @property
    def job_id(self) -> str:
        """잡 식별자(= ticket; dedup으로 티켓당 1잡 보장)."""
        return self.ticket

    @property
    def is_fractal(self) -> bool:
        """이 잡이 프랙탈(센트럴 에이전트 주도) 경로 소속인지(meta 표식).

        True 면 구 경로 스케줄러가 디스패치하지 않는다(이중 실행 방지). 관측성만을
        위해 store 에 실린다 — 상태 전이는 fractal 배관(worker_dispatch/gchat/track)이 찍는다.
        """
        return bool(self.meta.get(FRACTAL_META_KEY))

    @property
    def corr_id(self) -> str:
        """해소된 correlation id(Phase 3b-1) — 명시값 없으면 티켓(job_id)로 폴백.

        채널 F 요청/응답 correlation의 **단일 해소 지점**. 구 워커는 correlation_id를
        안 보내고 위임되지 않은(순수 폴러) 잡은 field가 None이므로 티켓으로 폴백한다
        → 기존 동작과 완전 동일(하위호환). 위임(delegation)된 잡만 register_pending이
        correlation_id를 명시 세팅한다.
        """
        return self.correlation_id or self.ticket

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Job":
        d = dict(d or {})
        return Job(
            ticket=str(d.get("ticket", "")),
            user=str(d.get("user", "")),
            correlation_id=d.get("correlation_id"),   # 구 레코드엔 없음 → None(티켓 폴백)
            target_repos=list(d.get("target_repos", []) or []),
            autonomy_mode=str(d.get("autonomy_mode", "B")),
            status=str(d.get("status", QUEUED)),
            session_id=d.get("session_id"),
            branch=d.get("branch"),
            reset_at=d.get("reset_at"),
            updated_at=d.get("updated_at"),   # 구 레코드엔 없을 수 있음 → None 허용
            mr_url=d.get("mr_url"),
            context_refs=dict(d.get("context_refs", {}) or {}),
            log_summary=str(d.get("log_summary", "")),
            audit_refs=dict(d.get("audit_refs", {}) or {}),
            attempts=int(d.get("attempts", 0)),
            cancel_requested=bool(d.get("cancel_requested", False)),
            cancel_reason=d.get("cancel_reason"),
            control_action=str(d.get("control_action", "none") or "none"),
            continue_from_wip=bool(d.get("continue_from_wip", False)),
            meta=dict(d.get("meta", {}) or {}),
        )


class JobQueue:
    """영속 잡 스토어 + 상태 전이."""

    def __init__(self) -> None:
        """락 + 영속된 잡 목록 로드로 초기화(ticket 키 인덱스).

        ⚠️ 프랙탈 P2 관측성: 이제 **여러 프로세스**(central·worker_dispatch·gchat·track)가
        jobs.json 을 read-modify-write 한다. 인메모리 ``_jobs`` 는 **캐시**이며, 모든 읽기/
        쓰기 경로가 :meth:`_reload_locked` 로 디스크에서 다시 읽어 최신화한 뒤 동작한다 —
        그래야 (a) 외부 프로세스가 찍은 running/done/failed 를 대시보드가 보고, (b) central 의
        저장이 외부 갱신을 덮어써 유실시키지 않는다. 경계는 :func:`state.jobs_lock`(크로스
        프로세스 flock)으로 직렬화한다.
        """
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        for j in state.load_jobs([]) or []:
            job = Job.from_dict(j)
            if job.ticket:
                self._jobs[job.ticket] = job

    def _reload_locked(self) -> None:
        """디스크(jobs.json)에서 ``_jobs`` 를 다시 읽어 인메모리 미러를 최신화한다.

        **반드시** ``self._lock`` + ``state.jobs_lock()`` 를 쥔 상태에서 호출한다 — 이 락
        안에서 load→(수정)→save 를 원자적으로 끝내야 외부 프로세스와 유실 없이 병합된다.
        삽입 순서(dict)를 유지해 스케줄러의 결정적 선택 순서를 보존한다.
        """
        fresh: dict[str, Job] = {}
        for j in state.load_jobs([]) or []:
            job = Job.from_dict(j)
            if job.ticket:
                fresh[job.ticket] = job
        self._jobs = fresh

    def _save_locked(self) -> None:
        """현재 ``_jobs`` 를 디스크로 저장(반드시 락 안, 직전에 _reload_locked 로 병합한 상태)."""
        state.save_jobs([j.to_dict() for j in self._jobs.values()])

    @staticmethod
    def _stamp(job: Job) -> None:
        """잡의 활동시각(updated_at)을 현재 UTC로 스탬프.

        모든 쓰기 경로(enqueue/set_status/update/reopen/reassign)가 이 헬퍼를
        거쳐 활동시각을 남긴다. 순수 파이썬 — 오케스트레이터/claude 개입 없음.
        """
        job.updated_at = _now_iso()

    def enqueue(self, job: Job) -> None:
        """새 잡을 등록(중복 티켓이면 기존을 덮지 않고 무시).

        dedup 게이트가 앞단이지만, 재시작/재적격 케이스를 위해 티켓 멱등.
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            if job.ticket in self._jobs:
                return
            self._stamp(job)
            self._jobs[job.ticket] = job
            self._save_locked()

    def next_queued(self, user: Optional[str] = None) -> Optional[Job]:
        """실행 대기 잡 하나(queued)를 반환. user 지정 시 그 사용자 것만.

        스케줄러의 레포락 판단과 무관한 단순 조회(테스트/보조용).
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            for j in self._jobs.values():
                if j.status != QUEUED:
                    continue
                if user is not None and j.user != user:
                    continue
                return j
        return None

    def set_status(self, ticket: str, status: str, **fields) -> None:
        """잡 상태/부가 필드 갱신 후 영속."""
        with self._lock, state.jobs_lock():
            self._reload_locked()
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
            self._stamp(j)
            self._save_locked()

    def update(self, ticket: str, **fields) -> None:
        """상태 변경 없이 부가 필드만 갱신(진행중 보고용)."""
        with self._lock, state.jobs_lock():
            self._reload_locked()
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
            self._stamp(j)
            self._save_locked()

    def clear_fields(self, ticket: str, *names: str) -> None:
        """지정 필드를 기본값(None/빈)으로 명시적으로 비운다.

        set_status/update는 None을 "미변경"으로 보므로, 값을 실제로 지울 때 사용.
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            j = self._jobs.get(ticket)
            if j is None:
                raise KeyError(f"알 수 없는 잡: {ticket}")
            for name in names:
                if name in ("reset_at", "mr_url", "session_id", "branch"):
                    setattr(j, name, None)
                elif hasattr(j, name):
                    setattr(j, name, type(getattr(j, name))())
            self._save_locked()

    def reopen(self, job: Job) -> None:
        """취소 확정된 티켓을 재작업용으로 재등록(재오픈 §10.4).

        enqueue는 티켓 멱등(기존 잡 무시)이라 취소 확정(cancelled) 잡을 되살리지
        못한다. 이 메서드는 같은 티켓 슬롯을 **새 실행으로 초기화**해 큐에 올린다 —
        재개 잔재(session_id/reset_at/mr_url/cancel_requested)를 비우고 queued로.

        ⚠️ 크로스프로세스 안전 리로드로 인해 인메모리 객체가 매 호출 새로 만들어지므로,
        호출자가 넘긴 stale ``job`` 대신 **디스크 최신 레코드**(reload 후)를 대상으로 초기화한다
        — 직전 update(예: branch 회신)로 디스크에 기록된 필드를 유실하지 않게 한다.
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            target = self._jobs.get(job.ticket) or job
            target.status = QUEUED
            target.reset_at = None
            target.mr_url = None
            target.session_id = None
            target.cancel_requested = False
            target.cancel_reason = None
            target.control_action = "none"
            target.attempts = 0
            self._stamp(target)
            self._jobs[target.ticket] = target
            self._save_locked()

    def reassign(self, job: Job, new_user: str, *, autonomy_mode: Optional[str] = None,
                 continue_from_wip: bool = False, prev_user: str = "") -> None:
        """같은 티켓 슬롯을 **새 담당자 Y로 재-소유**해 재작업용으로 초기화(담당자 변경 이관).

        핸드오프(실행 중 잡의 checkpoint 회신) 또는 큐 대기분 재배정에서 공용으로 쓴다.
        reopen과 동일하게 재개 잔재(session/reset_at/mr_url/cancel/control)를 비우고
        queued로 만들되, **소유자(user)를 Y로 바꾸고** autonomy_mode/continue 힌트를 갱신한다.
        ``continue_from_wip=True`` 면 브랜치에 선행 WIP가 있다는 힌트가 프롬프트로 전달된다.
        target_repos/branch/context_refs는 **그대로 유지**(같은 티켓·같은 작업).
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            # stale ``job`` 대신 디스크 최신 레코드를 대상으로(직전 update 의 branch 등 보존).
            target = self._jobs.get(job.ticket) or job
            target.user = new_user
            if autonomy_mode:
                target.autonomy_mode = autonomy_mode
            target.status = QUEUED
            target.reset_at = None
            target.mr_url = None
            target.session_id = None
            target.cancel_requested = False
            target.cancel_reason = None
            target.control_action = "none"
            target.attempts = 0
            target.continue_from_wip = bool(continue_from_wip)
            if prev_user:
                target.meta["handed_off_from"] = prev_user
            target.meta.pop("reassign", None)
            self._stamp(target)
            self._jobs[target.ticket] = target
            self._save_locked()

    def get(self, ticket: str) -> Optional[Job]:
        """티켓으로 잡 조회(디스크 최신화 후 — 외부 프로세스 갱신 반영)."""
        with self._lock, state.jobs_lock():
            self._reload_locked()
            return self._jobs.get(ticket)

    def list_jobs(self) -> list:
        """전체 잡 목록 스냅샷(관리 UI 현황용) — 디스크 최신화 후.

        프랙탈 잡의 running/done/failed 는 외부 프로세스(worker_dispatch/gchat/track)가
        디스크에 찍으므로, 대시보드가 그 최신 상태를 보려면 여기서 다시 읽어야 한다.
        """
        with self._lock, state.jobs_lock():
            self._reload_locked()
            return list(self._jobs.values())
