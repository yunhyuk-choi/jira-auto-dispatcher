"""명시적 id 요청/응답 pending 레지스트리(central 전용) — Phase 3b-1.

역할:
    미래 Tier-2 중앙 서브에이전트는 티켓을 **그 사용자 컨테이너에 위임**(delegate)하고
    나중에 그 결과를 **명시적 correlation id로 회수**한다(§5 D4 확정 = 폴+콜백 위 명시적
    id, NOT await/future, NOT SSE). 이 모듈은 그 **트랜스포트 토대**만 얹는다 — 티어
    에이전트는 아직 없다(3b-1). 위임 하나는 correlation id로 태깅되고(기본 = 티켓 id),
    워커의 terminal 회신이 그 id를 되싣어 오면 central이 **결과↔요청을 명시적 id로 매칭**한다.
    동시 위임이 여럿이어도 각 결과가 정확한 id에 매칭된다(명시적 id의 존재 이유).

    ⚠️ **재시작 안전(restart-safe)이 핵심 불변식**: pending 집합과 해소 결과는
    **영속 잡 상태(jobs.json)에서 파생**된다 — in-memory 상태를 별도로 들고 있지 않는다.
        - dispatch됐지만 non-terminal 잡 = **pending**.
        - terminal 잡 = **resolvable**(저장된 결과 필드로 결과 payload 조립).
    따라서 central이 재시작해 jobs.json에서 :class:`app.queue.JobQueue` 를 새로 만들고
    그 위에 새 PendingRegistry를 얹어도 pending/결과가 그대로 재구성된다(진실원 = 잡 상태).

역할 소속: **central**.

구현 Phase: **Phase 3b-1** (명시적 id 요청/응답 트랜스포트 토대, 하위호환·무동작변경).

하위호환:
    - 이 레지스트리는 **읽기 + 부가 마킹만** 한다 — 스케줄러/디스패치 로직을 한 줄도
      바꾸지 않는다. 위임되지 않은(순수 폴러) 잡은 correlation_id field가 None이라
      티켓으로 폴백하며, register_pending을 부르지 않는 한 delegation 마커도 없다.
    - 결과 payload의 ``cycle_log_path`` 는 dispatch.report_status가 terminal 회신 시
      이미 잡에 영속한 값(dlc_meta_writer 산출, dispatch.py:118-129)을 읽어 surfacing한다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from app import queue as q
from app.queue import Job, JobQueue

# 잡 meta에 delegation(위임) 등록 여부를 남기는 키. 순수 부가 메타 — 스케줄링에 무영향.
# pending 집합 열거(pending_ids)는 이 마커 + 잡 상태에서 파생된다(영속 → 재시작 안전).
DELEGATION_META_KEY = "delegation"

# 결과 payload에 실을 잡 필드(스칼라/컨테이너). cycle_log_path는 meta에서 별도로 읽는다.
_RESULT_FIELDS = ("mr_url", "branch", "log_summary", "audit_refs", "attempts", "user")


class PendingRegistry:
    """영속 잡 상태 위에 얹는 **파생형** pending/결과 레지스트리(central).

    별도 영속 구조를 두지 않는다 — :class:`app.queue.JobQueue` (jobs.json 백엔드)를
    진실원으로 삼아 pending/resolvable을 매 조회마다 재계산한다. 그래서 인스턴스는
    상태를 들고 있지 않고, 재시작 후 새 JobQueue로 새 인스턴스를 만들면 그대로 복원된다.
    """

    def __init__(self, job_queue: JobQueue) -> None:
        self.jobs = job_queue

    # ------------------------------------------------------------------
    # 요청측(위임 등록)
    # ------------------------------------------------------------------

    def register_pending(self, correlation_id: str, *, ticket: Optional[str] = None) -> str:
        """correlation id로 위임을 pending 등록한다(미래 Tier-2 위임 시점 호출).

        기본 correlation id = 티켓 id(``ticket`` 미지정 시 correlation_id를 티켓으로 간주).
        잡에 correlation_id를 **명시 영속**하고 delegation 마커를 남긴다 — 둘 다 jobs.json에
        저장돼 재시작 후에도 이 위임을 correlation id로 다시 찾을 수 있다(restart-safe).
        멱등(같은 correlation을 여러 번 등록해도 안전).

        Raises: KeyError(대응 잡 없음). Returns: 등록된 correlation id.
        """
        tkt = ticket or correlation_id
        job = self.jobs.get(tkt)
        if job is None:
            raise KeyError(f"위임 등록 실패 — 알 수 없는 잡: {tkt}")
        # correlation_id(field)와 delegation 마커(meta)를 함께 영속. update는 None을
        # 건너뛰므로 명시 문자열을 넘긴다. delegation 마커는 hasattr False → meta로 간다.
        self.jobs.update(tkt, correlation_id=str(correlation_id),
                         **{DELEGATION_META_KEY: True})
        return str(correlation_id)

    # ------------------------------------------------------------------
    # 응답측(결과 폴링/콜백)
    # ------------------------------------------------------------------

    def poll_result(self, correlation_id: str) -> dict:
        """한 correlation id의 상태/결과를 조회(폴).

        반환(JSON 직렬화 가능):
            correlation_id  조회한 id
            found           대응 잡 존재 여부
            ticket          대응 잡 티켓(없으면 None)
            job_status      잡 내부 상태(running/done/failed/...) 또는 None
            terminal        잡이 terminal(해소됨)인가
            status          "done"(terminal) | "pending"(non-terminal) | "unknown"(없음)
            result          terminal이면 결과 payload(dict), 아니면 None
        """
        job = self._resolve(correlation_id)
        if job is None:
            return {
                "correlation_id": correlation_id, "found": False, "ticket": None,
                "job_status": None, "terminal": False, "status": "unknown", "result": None,
            }
        terminal = job.status in q.TERMINAL_STATUSES
        return {
            "correlation_id": correlation_id,
            "found": True,
            "ticket": job.ticket,
            "job_status": job.status,
            "terminal": terminal,
            "status": "done" if terminal else "pending",
            "result": self._result_payload(job) if terminal else None,
        }

    def poll_results(self, correlation_ids: Iterable[str]) -> dict:
        """여러 correlation id를 한 번에 폴 — {id: poll_result(id)} 매핑 반환.

        동시 위임이 여럿일 때 각 결과가 **정확한 id에 매칭**됨을 보장한다(명시적 id의 요점).
        """
        return {cid: self.poll_result(cid) for cid in correlation_ids}

    # ------------------------------------------------------------------
    # 파생 집합(영속 잡 상태에서 재계산 → 재시작 안전)
    # ------------------------------------------------------------------

    def pending_ids(self) -> List[str]:
        """현재 pending(등록됐고 아직 non-terminal)인 위임의 correlation id 목록.

        영속 잡 상태에서 파생 — delegation 마커가 있고 상태가 non-terminal인 잡들.
        """
        out: List[str] = []
        for j in self.jobs.list_jobs():
            if self._is_delegation(j) and j.status not in q.TERMINAL_STATUSES:
                out.append(j.corr_id)
        return out

    def resolved_ids(self) -> List[str]:
        """해소된(등록됐고 terminal) 위임의 correlation id 목록(영속 상태에서 파생)."""
        return [j.corr_id for j in self.jobs.list_jobs()
                if self._is_delegation(j) and j.status in q.TERMINAL_STATUSES]

    def is_pending(self, correlation_id: str) -> bool:
        """이 correlation이 pending(대응 잡 존재 + non-terminal)인가."""
        job = self._resolve(correlation_id)
        return bool(job and job.status not in q.TERMINAL_STATUSES)

    # ------------------------------------------------------------------
    # 내부
    # ------------------------------------------------------------------

    @staticmethod
    def _is_delegation(job: Job) -> bool:
        return bool((job.meta or {}).get(DELEGATION_META_KEY))

    def _resolve(self, correlation_id: str) -> Optional[Job]:
        """correlation id → 잡. 명시 correlation_id field 매칭 우선, 없으면 티켓 폴백.

        job.corr_id(= correlation_id or ticket)로 매칭하므로 명시적 위임과 티켓-기본
        폴백을 모두 커버한다. 결정적 순서(잡 삽입 순서)로 첫 매치를 취한다.
        """
        if not correlation_id:
            return None
        for j in self.jobs.list_jobs():
            if j.corr_id == correlation_id:
                return j
        return None

    def _result_payload(self, job: Job) -> dict:
        """terminal 잡에서 결과 payload를 조립(영속 필드만 사용 → 재시작 후에도 동일).

        ``cycle_log_path`` 는 dispatch.report_status가 terminal 회신 시 잡 meta에 영속한
        dlc_meta_writer 산출 경로(dispatch.py:118-129)를 읽는다 — 없으면 생략.
        """
        payload = {
            "correlation_id": job.corr_id,
            "ticket": job.ticket,
            "status": job.status,
        }
        for name in _RESULT_FIELDS:
            payload[name] = getattr(job, name, None)
        cycle_log_path = (job.meta or {}).get("cycle_log_path")
        if cycle_log_path:
            payload["cycle_log_path"] = cycle_log_path
        return payload
