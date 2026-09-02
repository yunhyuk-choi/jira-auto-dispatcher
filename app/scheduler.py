"""자원 인지형 레포락 스케줄러(중앙 전용) — RECURSIVE-DISPATCH §4.

역할:
    central의 직렬/병렬 판단(프레임워크 책임 3의 재귀 적용)을 **결정적 레포락 +
    서버 자원 기반 어드미션**으로 구현한다.

        - **레포 단위 락(정확성)** — 한 레포에는 활성(running) 잡이 1개만. 서로 다른
          레포의 잡은 병렬. 이는 잡 수 cap이 아니라 **공유 워크스페이스 충돌 방지**다.
        - **자원 기반 어드미션(스로틀)** — dispatch 여부는 "잡 수"가 아니라 **호스트
          서버의 실제 자원 상태**(가용 메모리 + CPU 부하)로 판단한다. 여유가 있으면 준비된
          잡을 dispatch, 압박이면 큐에 대기. 자원이 넉넉하면 한 사용자가 (서로 다른 레포에서)
          여러 잡을 동시에 굴릴 수도 있다 — **per-user 잡 수 cap도, 전역 잡 수 cap도 없다.**
          ⚠️ 토큰/레이트 한도는 central의 관심사가 아니다(각 worker 컨테이너 오케스트레이터 몫).
        - **겹치면 defer** — 대상 레포가 잠겨 있거나 서버가 자원 압박이면 큐에서 대기.
          완료 notify(채널 F) 수신 시 레포 락 해제·메모리 회수 → 다음 tick에서 재시도.
        - **미해석(target_repos=[]) = 전역 직렬** — REPO-MAP으로 레포를 못 정한 잡은
          보수적으로 단독 실행(다른 모든 잡을 배제하고 혼자 running).
        - **재개(interrupted+reset_at)** — 중단 잡은 레포락을 놓고 대기하다, reset_at이
          지나면 tick의 적격 풀로 복귀해 재-dispatch(resume)된다.

역할 소속: **central** (실제 실행은 각 사용자 worker가 한다).

구현 Phase: **Phase 3~5** (레포락 스케줄러 + 재개) + 자원 인지 어드미션.

설계 메모:
    - 레포 락/전역 락/active 수는 **active 잡 상태의 순수 함수**로 매 tick 재계산한다
      (별도 락 테이블을 영속하지 않음 → 재시작 후에도 jobs.json에서 자동 복원).
    - dispatch = 잡을 running으로 표시(해당 레포를 사실상 점유). 실제 실행은 프랙탈
      센트럴 세션이 워커 컨테이너에 docker exec 로 주입한다 — 워커가 중앙을 폴링해
      잡을 당겨오던 레거시 경로는 은퇴했다(이중 실행 근본 차단).
    - reset_at 비교 기준 시각은 now_provider로 주입 가능(테스트 결정성).
    - 자원 프로브(resource_probe)는 주입 가능(테스트 결정성). 기본은 호스트 /proc를 읽는다
      (컨테이너 안에서도 /proc/meminfo·/proc/loadavg는 **호스트 값**을 보고한다 = 서버 상태).
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from app import queue as q
from app.queue import Job, JobQueue

log = logging.getLogger("jad.scheduler")

# 프로브가 호스트 /proc를 읽지 못할 때(비-Linux 개발 환경 등) 쓰는 "무압박" 폴백.
# 자원 스로틀만 잃을 뿐 정확성(레포락)은 그대로이므로 fail-open으로 admit한다.
_PROBE_FALLBACK_MEM_MB = 1 << 30  # 사실상 무한(항상 헤드룸)
_PROBE_FALLBACK_LOAD = 0.0


def _read_mem_available_mb() -> Optional[float]:
    """/proc/meminfo의 ``MemAvailable``(kB)을 MB로 반환. 못 읽으면 None.

    컨테이너 안에서도 이 값은 네임스페이스가 아닌 **호스트** 가용 메모리를 보고한다 —
    우리가 원하는 서버 상태다.
    """
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    return float(parts[1]) / 1024.0  # kB → MB
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_loadavg_1min() -> Optional[float]:
    """/proc/loadavg의 1분 부하를 반환. 못 읽으면 None."""
    try:
        with open("/proc/loadavg", "r", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def default_resource_probe() -> dict:
    """호스트 자원 상태를 읽어 어드미션 판단 입력을 만든다(기본 프로브).

    반환: ``{"mem_available_mb": float, "loadavg_1min": float, "ncpu": int}``.
    /proc를 못 읽는 환경(비-Linux 개발 등)에서는 무압박 폴백으로 admit한다
    (정확성은 레포락이 보장하므로 fail-open 안전). 절대 시크릿을 읽거나 로깅하지 않는다.
    """
    mem = _read_mem_available_mb()
    load = _read_loadavg_1min()
    if mem is None or load is None:
        log.debug("자원 프로브: /proc 미가용 → 무압박 폴백(admit)")
    return {
        "mem_available_mb": _PROBE_FALLBACK_MEM_MB if mem is None else mem,
        "loadavg_1min": _PROBE_FALLBACK_LOAD if load is None else load,
        "ncpu": os.cpu_count() or 1,
    }

# reassign_or_handoff 결과 신호(호출부 poller가 후속 동작을 분기).
REASSIGN_NO_JOB = "no_job"                   # 추적 잡 없음 → 호출부가 일반 신규 dispatch
REASSIGN_SAME_OWNER = "same_owner"           # 같은 소유자(중복 트리거) → skip
REASSIGN_REDISPATCH = "redispatch"           # 큐 대기분을 Y로 재-소유·재-dispatch(WIP 없음)
REASSIGN_PARKED = "parked"                   # 큐 대기분 드롭 → Y 미가용이라 park(미dispatch)
REASSIGN_HANDOFF_REQUESTED = "handoff"       # 실행 중 잡에 핸드오프(checkpoint) 요청(회신 대기)


def _parse_iso(value: str) -> Optional[datetime]:
    """ISO8601(옵션 'Z') → aware datetime. 실패 시 None."""
    if not value:
        return None
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Scheduler:
    """결정적 레포락 스케줄러."""

    def __init__(
        self,
        config,
        job_queue: JobQueue,
        now_provider: Optional[Callable[[], datetime]] = None,
        gate=None,
        resource_probe: Optional[Callable[[], dict]] = None,
        on_dispatch: Optional[Callable[[set], None]] = None,
    ) -> None:
        self.config = config
        self.jobs = job_queue
        self._lock = threading.RLock()
        self._now = now_provider or (lambda: datetime.now(timezone.utc))
        # dedup 게이트(취소/재오픈 시 dedup 해제 — §10.4). 없으면 no-op(테스트 격리).
        self.gate = gate
        # dispatch 후 훅(주입) — 이번 tick이 잡을 실제 dispatch했을 때 **락 밖에서**
        # 현재 locked_repos 스냅샷을 넘겨 호출한다. central은 이걸로 미락 참고 레포를
        # 기계적 최신화(app.freshen)한다. 없으면 no-op(테스트/워커 격리). best-effort —
        # 훅 실패가 dispatch/스케줄링을 절대 막지 않는다.
        self._on_dispatch = on_dispatch
        # 서버 자원 프로브(주입 가능, 테스트 결정성). 기본은 호스트 /proc를 읽는다.
        self._resource_probe = resource_probe or default_resource_probe

        # 어드미션 임계치(자원 기반). 잡 수 cap은 없다 — 스로틀은 오직 서버 자원이다.
        adm = getattr(config, "admission", None)
        self.min_free_mem_mb = float(getattr(adm, "min_free_mem_mb", 1536)) if adm else 1536.0
        self.per_job_mem_reserve_mb = float(getattr(adm, "per_job_mem_reserve_mb", 1024)) if adm else 1024.0
        self.max_load_per_core = float(getattr(adm, "max_load_per_core", 0.9)) if adm else 0.9

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def enqueue(self, job: Job) -> list:
        """잡을 큐에 등록(queued)하고 즉시 tick. dispatch된 잡 id 목록 반환."""
        self.jobs.enqueue(job)
        return self.tick()

    def tick(self) -> list:
        """적격 잡을 **서버 자원이 허용하는 만큼** dispatch(running 표시 + 레포 점유).

        적격 = (queued) 또는 (interrupted & reset_at 도래) 이면서
               (a) 대상 레포 전부 unlock(전역 레포락, 정확성) &&
               (b) 전역직렬 충돌 없음 &&
               (c) 서버에 자원 헤드룸(메모리 + 부하) 존재.

        잡 수 cap은 없다. 프로브는 tick 시작에 **한 번** 읽고, in-flight 예약(active 수)은
        admit마다 재계산한다 — 한 tick 안에서 버스트가 메모리를 과다 커밋하지 못하게 한다
        (loadavg 지연 완화). id 목록 반환.
        """
        dispatched: list = []
        locked_after: set = set()
        with self._lock:
            probe = self._read_probe()
            load_per_core = probe["loadavg_1min"] / max(1, probe["ncpu"])
            while True:
                snap = self._snapshot()
                # 전역직렬 잡이 이미 running이면 아무것도 못 뜬다(정확성).
                if snap["global_lock"]:
                    break
                # 부하 상한(2차 거친 천장, tick 내 불변) — 넘으면 이번 tick은 큐잉.
                if load_per_core >= self.max_load_per_core:
                    if snap["queued_count"]:
                        log.info("queue: load pressure (load/core=%.2f ≥ %.2f, ncpu=%d)",
                                 load_per_core, self.max_load_per_core, probe["ncpu"])
                    break
                # 메모리 헤드룸(PRIMARY, in-flight 예약 반영 — admit마다 active가 늘어 재계산).
                effective_mb = probe["mem_available_mb"] - snap["running_count"] * self.per_job_mem_reserve_mb
                if effective_mb < self.min_free_mem_mb:
                    if snap["queued_count"]:
                        log.info("queue: mem pressure (effective=%.0fMB < %.0fMB, in-flight=%d)",
                                 effective_mb, self.min_free_mem_mb, snap["running_count"])
                    break
                picked = self._pick_eligible(snap)
                if picked is None:
                    break
                self._dispatch(picked)
                dispatched.append(picked.ticket)
                log.debug("admit: %s (effective=%.0fMB, in-flight→%d, load/core=%.2f)",
                          picked.ticket, effective_mb, snap["running_count"] + 1, load_per_core)
            # 이번 tick이 실제로 잡을 dispatch했으면, 최종 locked_repos 스냅샷을 잡아
            # 락 밖 훅(미락 참고 레포 최신화)에 넘긴다. 락 안에서 계산한다(일관성).
            if dispatched:
                locked_after = self._snapshot()["locked_repos"]
        # dispatch 후 훅 — **락 밖에서** best-effort로 호출한다(느린 git I/O가 스케줄러
        # 락을 잡지 않도록). 실제 dispatch가 있을 때만(유휴=0), 훅 실패는 격리한다.
        if dispatched and self._on_dispatch is not None:
            try:
                self._on_dispatch(locked_after)
            except Exception:  # noqa: BLE001 — 훅 실패가 dispatch/스케줄링을 막지 않는다
                log.warning("dispatch 후 훅(미락 레포 최신화) 실패(격리)")
        return dispatched

    def _read_probe(self) -> dict:
        """자원 프로브를 안전하게 호출(실패 시 무압박 폴백 → fail-open admit)."""
        try:
            probe = self._resource_probe() or {}
        except Exception:  # noqa: BLE001 — 프로브 실패가 스케줄링을 죽이지 않게 격리
            log.warning("자원 프로브 호출 실패 → 무압박 폴백(admit)")
            probe = {}
        return {
            "mem_available_mb": float(probe.get("mem_available_mb", _PROBE_FALLBACK_MEM_MB)),
            "loadavg_1min": float(probe.get("loadavg_1min", _PROBE_FALLBACK_LOAD)),
            "ncpu": int(probe.get("ncpu", os.cpu_count() or 1)),
        }

    def on_complete(self, job_id: str, status: str = q.DONE, **fields) -> list:
        """terminal 보고(done/failed) — 레포 락 해제 → 상태 확정 → tick.

        레포 락은 잡 상태의 함수이므로 status를 terminal로 바꾸면 자동 해제된다.
        """
        norm = q.normalize_status(status)
        if norm not in q.TERMINAL_STATUSES:
            norm = q.DONE
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, norm, **fields)
        return self.tick()

    def on_interrupt(self, job_id: str, reset_at: Optional[str] = None, **fields) -> list:
        """중단 보고(interrupted) — 레포 락 해제 → interrupted+reset_at 기록 → tick.

        이 잡은 reset_at이 지날 때까지 적격 풀에서 제외되고, 다른 잡은 진행한다.
        """
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, q.INTERRUPTED, reset_at=reset_at, **fields)
        return self.tick()

    def on_progress(self, job_id: str, **fields) -> None:
        """진행중 보고 — 상태(running) 유지, 부가 필드만 갱신(락 불변)."""
        with self._lock:
            self.jobs.update(job_id, **fields)

    def report(self, job_id: str, status: str, **fields) -> list:
        """채널 F 통합 라우터(dispatch.report_status가 사용).

        cancelled → confirm_cancelled(락+dedup 해제), terminal → on_complete,
        interrupted → on_interrupt, 그 외 → on_progress.
        """
        norm = q.normalize_status(status)
        # cancelled는 TERMINAL에 속하지만 dedup 해제까지 해야 하므로 먼저 분기.
        if norm == q.CANCELLED:
            return self.confirm_cancelled(job_id, **fields)
        # handed_off도 TERMINAL이지만 롤백 없이 락 해제 + (이관 vs park) 분기가 필요하므로 먼저.
        if norm == q.HANDED_OFF:
            return self.confirm_handed_off(job_id, **fields)
        if norm in q.TERMINAL_STATUSES:
            return self.on_complete(job_id, norm, **fields)
        if norm == q.INTERRUPTED:
            reset_at = fields.pop("reset_at", None)
            return self.on_interrupt(job_id, reset_at=reset_at, **fields)
        self.on_progress(job_id, **fields)
        return []

    # ------------------------------------------------------------------
    # 취소 / 재오픈 (RECURSIVE-DISPATCH §10)
    # ------------------------------------------------------------------

    def _release_dedup(self, ticket: str) -> None:
        """dedup 게이트에서 티켓 claim 해제(재오픈 대비). 게이트 없으면 no-op."""
        if self.gate is not None:
            self.gate.release(ticket)

    def cancel_job(self, job_id: str, reason: Optional[str] = None) -> list:
        """취소 신호 반영(§10.3).

        - 이미 종결(done/failed/cancelled): no-op.
        - 실행 중(running/cancelling): `cancelling` 표시 + worker 취소 플래그 세팅.
          레포 락은 **유지**한다(worker의 cancelled 회신을 기다린다). tick 안 함.
        - 큐 대기(queued/interrupted): 즉시 `cancelled`로 드롭 + dedup 해제 → tick
          (막혀 있던 다음 잡을 dispatch할 수 있음).

        ``reason``(queue.CANCEL_* 상수)이 주어지면 잡에 기록한다 — 관리 UI가
        취소됨(상태) vs 추적해제(라벨) vs 재배정 vs 수동을 구별해 표시한다. 실행 중
        분기에서 기록해 두면 worker의 cancelled 회신(confirm_cancelled)이 상태만 확정
        하고 사유는 그대로 보존된다. None이면 미변경(set_status가 None을 건너뜀).
        """
        do_tick = False
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            if job.status in q.TERMINAL_STATUSES:
                return []
            if job.status in q.ACTIVE_STATUSES:
                # 실행 중 → worker에 취소 위임(회신 대기). 레포 락 유지. 사유 선기록.
                self.jobs.set_status(job_id, q.CANCELLING, cancel_requested=True,
                                     cancel_reason=reason)
                return []
            # 큐 대기(락 미점유) → 즉시 드롭 + dedup 해제.
            self.jobs.set_status(job_id, q.CANCELLED, cancel_requested=False,
                                 cancel_reason=reason)
            self._release_dedup(job_id)
            do_tick = True
        return self.tick() if do_tick else []

    def confirm_cancelled(self, job_id: str, **fields) -> list:
        """worker의 cancelled 회신 확정(§10.4) — 락 해제 + dedup 해제 → tick.

        레포 락은 상태의 함수이므로 cancelled로 바꾸면 자동 해제된다. dedup까지
        풀어야 재오픈(취소됨→해야할일) 때 같은 티켓을 다시 claim할 수 있다.
        """
        with self._lock:
            if self.jobs.get(job_id) is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            self.jobs.set_status(job_id, q.CANCELLED, cancel_requested=False, **fields)
            self._release_dedup(job_id)
        return self.tick()

    def is_cancel_requested(self, job_id: str) -> bool:
        """worker control 폴링용 — 이 잡에 취소가 요청됐는지."""
        job = self.jobs.get(job_id)
        return bool(job and job.cancel_requested)

    # ------------------------------------------------------------------
    # 담당자 변경 = 핸드오프 / 재배정 (재배정 ≠ 취소)
    # ------------------------------------------------------------------

    def reassign_or_handoff(self, ticket: str, new_user: str, *,
                            enabled: bool = True,
                            autonomy_mode: Optional[str] = None,
                            on_redispatch: Optional[Callable[[str], None]] = None) -> str:
        """티켓 담당자가 X→Y로 바뀐 상황을 잡 상태에 따라 처리(재배정 ≠ 취소).

        - 추적 잡 없음/종결 → ``REASSIGN_NO_JOB``(호출부가 일반 신규 dispatch).
        - 같은 소유자(Y==X) → ``REASSIGN_SAME_OWNER``(중복 트리거 흡수, no-op).
        - 큐 대기(queued/interrupted, WIP 없음):
            · Y 가용(enabled) → 같은 슬롯을 Y로 재-소유(queued) → 재-dispatch → ``REASSIGN_REDISPATCH``.
              (dedup claim은 유지 — 같은 티켓을 Y가 이어받는다.) 재-dispatch 는 ``on_redispatch``
              훅이 주어지면 **그 훅**(프랙탈: 센트럴 세션 주입)에 위임하고, 없으면 구 경로
              ``tick()``(레거시 스케줄러 dispatch)으로 처리한다.
            · Y 미가용 → 즉시 드롭(cancelled) + dedup 해제 → park → ``REASSIGN_PARKED``.
        - 실행 중(running/cancelling, WIP 존재):
            · **롤백 없이 핸드오프** — control_action=handoff + status=handing_off(레포락 유지).
              worker가 checkpoint(커밋·push) 후 handed_off 회신하면 confirm_handed_off가
              이관(Y가용) 또는 park(미가용)를 마무리한다. 이관 의도는 meta.reassign에 stash.
              → ``REASSIGN_HANDOFF_REQUESTED``.

        ``new_user`` 는 username 문자열. ``enabled``/``autonomy_mode`` 는 Y의 가용성/모드
        (레지스트리를 모르는 스케줄러에 주입). ``on_redispatch``(선택)은 REDISPATCH 시 재-소유된
        슬롯을 프랙탈 센트럴 세션으로 라우팅하는 훅(호출부 poller 주입) — 락 밖에서 호출된다.
        """
        do_tick = False
        redispatch = False
        signal = REASSIGN_SAME_OWNER
        with self._lock:
            job = self.jobs.get(ticket)
            if job is None or job.status in q.TERMINAL_STATUSES:
                return REASSIGN_NO_JOB
            if job.user == new_user:
                return REASSIGN_SAME_OWNER

            if job.status in q.ACTIVE_STATUSES:
                # 실행 중 → 롤백 없이 핸드오프 요청(worker가 checkpoint). 레포락·dedup 유지.
                self.jobs.set_status(
                    ticket, q.HANDING_OFF,
                    control_action="handoff",
                    reassign={"to": new_user, "enabled": bool(enabled),
                              "autonomy_mode": autonomy_mode or ""},
                )
                return REASSIGN_HANDOFF_REQUESTED

            # 큐 대기(queued/interrupted) — WIP 없음.
            if enabled:
                # 같은 슬롯을 Y로 재-소유(WIP 없음 → continue 아님). dedup는 유지.
                self.jobs.reassign(job, new_user, autonomy_mode=autonomy_mode,
                                   continue_from_wip=False, prev_user=job.user)
                signal = REASSIGN_REDISPATCH
                redispatch = True
            else:
                # Y 미가용 → 드롭 + dedup 해제 → park(나중에 enable/재배정 시 재트리거).
                # 재배정으로 유발된 취소 → 사유 기록(status_cancelled/opt-out과 구별).
                self.jobs.set_status(ticket, q.CANCELLED, cancel_requested=False,
                                     control_action="none",
                                     cancel_reason=q.CANCEL_REASSIGNED)
                self._release_dedup(ticket)
                signal = REASSIGN_PARKED
                do_tick = True
        # 재-dispatch(REDISPATCH): 프랙탈 훅이 있으면 센트럴 세션으로 라우팅(구 tick 아님).
        # 훅이 없으면(레거시 배포) 구 경로 tick 으로 재-dispatch. 둘 다 락 밖에서 수행.
        if redispatch:
            if on_redispatch is not None:
                on_redispatch(ticket)
            else:
                self.tick()
        # PARKED: 슬롯을 드롭·dedup 해제했으니 다른 대기 잡 admit 을 위해 tick(레거시 경로에서만
        # 실효 — 프랙탈 잡은 tick 이 건너뛴다). 프랙탈 배포에선 무해 no-op.
        if do_tick:
            self.tick()
        return signal

    def confirm_handed_off(self, job_id: str, **fields) -> list:
        """worker의 handed_off 회신 확정(담당자 변경 핸드오프) — 롤백 없이 락 해제 + 이관/park.

        cancelled와 달리 **롤백하지 않는다**(WIP는 브랜치에 보존). meta.reassign에 stash된
        이관 의도에 따라:
            - Y 가용 → 같은 티켓 슬롯을 Y로 재-소유(continue 잡, continue_from_wip=True) +
              tick. dedup claim은 유지(같은 티켓을 Y가 이어받음).
            - Y 미가용 → handed_off(종결)로 기록 + dedup 해제 → park(미dispatch, 브랜치 보존).

        레포 락은 상태의 함수이므로 handed_off/queued 어느 쪽이든 handing_off를 벗어나면
        자동 해제된다.
        """
        with self._lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise KeyError(f"알 수 없는 잡: {job_id}")
            reassign = dict(job.meta.get("reassign") or {})
            to = str(reassign.get("to") or "")
            enabled = bool(reassign.get("enabled"))
            mode = reassign.get("autonomy_mode") or None
            prev_user = job.user
            if to and enabled:
                # WIP 보존 상태로 Y가 이어받는다(continue 잡). dedup 유지.
                # branch 등 회신 부가필드를 먼저 반영한 뒤 재-소유한다.
                if fields:
                    self.jobs.update(job_id, **fields)
                self.jobs.reassign(job, to, autonomy_mode=mode,
                                   continue_from_wip=True, prev_user=prev_user)
            else:
                # Y 미가용(또는 이관 대상 없음) → handed_off 종결 + dedup 해제 → park.
                self.jobs.set_status(job_id, q.HANDED_OFF, cancel_requested=False,
                                     control_action="none", **fields)
                self._release_dedup(job_id)
        return self.tick()

    # ⚠️ fractal-OFF 은퇴(chore/remove-legacy-serving): 레거시 재-dispatch 진입 래퍼
    # ``reopen(job)``(status_watcher 구 경로)·``rerun(job_id)``(관리 UI 구 경로)는
    # 삭제됐다 — 재오픈/수동 재실행은 이제 프랙탈 seam(``jobs.reopen`` + ``emit_to_central``)
    # 으로 수렴한다(status_watcher._reopen_dispatch / main.api_rerun). 슬롯 리셋 프리미티브는
    # ``JobQueue.reopen`` 이 그대로 제공한다(프랙탈 경로가 사용).

    # ------------------------------------------------------------------
    # 읽기 전용 상태 접근자 (Phase 3b-0 — 무동작변경 기반)
    # ------------------------------------------------------------------

    def state_snapshot(self) -> dict:
        """현재 스케줄링 상태의 **읽기 전용** 구조화 스냅샷(Phase 3b-0).

        미래 Tier-1/2 에이전트가 순서/배치/페이싱을 **제안**하려고 조회할 상태를,
        기존 ``_snapshot()``(활성 잡/락 재계산) + 자원 프로브(어드미션 입력)를 **조합만**
        해서 돌려준다. **스케줄링/디스패치 로직을 한 줄도 바꾸지 않고 READ만 한다** — 잡
        상태·락·큐를 일절 변경하지 않는다(부작용 0, 멱등). 1차 소비자는 인프로세스 SDK
        에이전트(파이썬 함수 직접 호출)이고, HTTP 노출은 대시보드/디버그 가시성용이다.

        반환(JSON 직렬화 가능):
            generated_at    스냅샷 시각(ISO8601 UTC)
            running         활성(running/cancelling/handing_off) 잡
                            [{ticket,user,target_repos,status}]
            running_count   전역 활성 잡 수
            per_user_active {user: 활성 잡 수}
            locked_repos    현재 잠긴 레포(정렬)
            global_lock     미해석(target_repos=[]) 활성 잡으로 전역 직렬 점유 중인가
            eligible        적격(queued | interrupted&reset_at 도래) 잡
                            [{ticket,user,target_repos,status,reason}]
            eligible_count  적격 잡 수
            queued_count    순수 queued 잡 수
            interrupted_waiting  interrupted지만 reset_at 미도래(대기) 잡 수
            resource        어드미션 프로브 raw + 헤드룸 판정(아래)

        resource:
            mem_available_mb/loadavg_1min/ncpu   프로브 raw
            load_per_core                        loadavg_1min / max(1, ncpu)
            effective_mb   mem_available_mb - running_count*per_job_mem_reserve_mb
            min_free_mem_mb/per_job_mem_reserve_mb/max_load_per_core  임계치(설정)
            mem_pressure/load_pressure           tick()과 동일 판정
            has_headroom   (!mem_pressure && !load_pressure && !global_lock)
        """
        with self._lock:
            # 활성 잡/락 재계산 + 잡 목록 + 프로브를 **락 안에서 일관되게** 읽는다(READ만).
            snap = self._snapshot()
            probe = self._read_probe()
            all_jobs = list(self.jobs.list_jobs())
            running = list(snap["running"])

            per_user_active: dict = {}
            for j in running:
                per_user_active[j.user] = per_user_active.get(j.user, 0) + 1

            eligible: list = []
            queued_count = 0
            interrupted_waiting = 0
            for j in all_jobs:
                if j.status == q.QUEUED:
                    queued_count += 1
                if self._eligible_now(j):
                    reason = "queued" if j.status == q.QUEUED else "interrupted_ready"
                    eligible.append({
                        "ticket": j.ticket,
                        "user": j.user,
                        "target_repos": list(j.target_repos),
                        "status": j.status,
                        "reason": reason,
                    })
                elif j.status == q.INTERRUPTED:
                    interrupted_waiting += 1

        # 순수 산술/직렬화 — 락 밖에서 계산(스냅샷 값은 위에서 확정됨).
        load_per_core = probe["loadavg_1min"] / max(1, probe["ncpu"])
        effective_mb = probe["mem_available_mb"] - snap["running_count"] * self.per_job_mem_reserve_mb
        mem_pressure = effective_mb < self.min_free_mem_mb
        load_pressure = load_per_core >= self.max_load_per_core

        return {
            "generated_at": self._now().isoformat(),
            "running": [
                {"ticket": j.ticket, "user": j.user,
                 "target_repos": list(j.target_repos), "status": j.status}
                for j in running
            ],
            "running_count": snap["running_count"],
            "per_user_active": per_user_active,
            "locked_repos": sorted(snap["locked_repos"]),
            "global_lock": snap["global_lock"],
            "eligible": eligible,
            "eligible_count": len(eligible),
            "queued_count": queued_count,
            "interrupted_waiting": interrupted_waiting,
            "resource": {
                "mem_available_mb": probe["mem_available_mb"],
                "loadavg_1min": probe["loadavg_1min"],
                "ncpu": probe["ncpu"],
                "load_per_core": load_per_core,
                "effective_mb": effective_mb,
                "min_free_mem_mb": self.min_free_mem_mb,
                "per_job_mem_reserve_mb": self.per_job_mem_reserve_mb,
                "max_load_per_core": self.max_load_per_core,
                "mem_pressure": mem_pressure,
                "load_pressure": load_pressure,
                "has_headroom": (not mem_pressure and not load_pressure
                                 and not snap["global_lock"]),
            },
        }

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    def _snapshot(self) -> dict:
        """현재 활성 잡으로부터 락/카운트 상태를 재계산.

        활성 = running + cancelling. cancelling 잡은 worker가 아직 abort/롤백 중이라
        레포를 붙들고 있으므로 락·cap 점유로 센다(§10.3) — 회신(cancelled) 전까지는
        같은 레포에 다른 잡이 들어오지 못한다.
        """
        all_jobs = list(self.jobs.list_jobs())
        active = [j for j in all_jobs if j.status in q.ACTIVE_STATUSES]
        locked_repos: set = set()
        global_lock = False
        for j in active:
            if j.target_repos:
                locked_repos.update(j.target_repos)
            else:
                # 미해석 잡이 활성 = 전역 직렬 점유.
                global_lock = True
        # running_count = in-flight 잡 수. 자원 어드미션의 **메모리 예약** 기준이다
        # (잡 수 cap이 아니라, admit할 때마다 예약 메모리를 늘려 과다 커밋을 막는 계수).
        return {
            "running": active,
            "running_count": len(active),
            "queued_count": sum(1 for j in all_jobs if self._eligible_now(j)),
            "locked_repos": locked_repos,
            "global_lock": global_lock,
        }

    def _eligible_now(self, job: Job) -> bool:
        """상태 기준 적격(시각 조건 포함) — cap/락은 별도 판단.

        ⚠️ 프랙탈 잡(meta.fractal)은 **상주 센트럴 라이브 세션**이 조율·실행한다 — 구 경로
        (HTTP 디스패치) 스케줄러는 관측성 레코드로만 볼 뿐 **절대 디스패치하지 않는다**
        (이중 실행 방지). running/done/failed 전이는 fractal 배관이 찍는다.
        """
        if getattr(job, "is_fractal", False):
            return False
        if job.status == q.QUEUED:
            return True
        if job.status == q.INTERRUPTED:
            if not job.reset_at:
                return True
            dt = _parse_iso(job.reset_at)
            if dt is None:
                return True  # 파싱 불가 → 즉시 재적격
            return self._now() >= dt
        return False

    def _pick_eligible(self, snap: dict) -> Optional[Job]:
        """스냅샷 기준으로 레포락상 dispatch 가능한 다음 잡 1개 선택(결정적 순서).

        잡 수 cap(per-user/전역)은 없다 — 자원 헤드룸은 tick()이 별도로 판단한다.
        여기서는 (a) 상태 적격 + (b) 전역 레포락 충돌 없음만 본다.
        """
        for job in self.jobs.list_jobs():  # 삽입 순서 = 결정적
            if not self._eligible_now(job):
                continue
            if not job.target_repos:
                # 전역 직렬: running이 하나도 없어야 단독 실행 가능.
                if snap["running_count"] > 0:
                    continue
            else:
                if any(repo in snap["locked_repos"] for repo in job.target_repos):
                    continue
            return job
        return None

    def _dispatch(self, job: Job) -> None:
        """잡을 running으로 표시(레포 점유). attempts 증가, reset_at 클리어."""
        self.jobs.set_status(job.ticket, q.RUNNING, attempts=job.attempts + 1)
        self.jobs.clear_fields(job.ticket, "reset_at")


# 명시적 별칭(문서/가독성).
RepoLockScheduler = Scheduler
