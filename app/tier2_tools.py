"""Tier-2 자원툴 레이어(central 전용) — Phase 3b-2.

역할:
    파일럿 Tier-2 에이전트(§4 D2 = per-user)가 **호출할 수 있는 유일한** 파이썬 함수
    집합. 설계 §5 **D3 확정**: "커스텀 파이썬 도구는 오직 **리소스 조작(resource ops)**
    전용" — 스케줄러 상태 읽기(3b-0), 사용자 컨테이너로 디스패치, 결과 fetch(3b-1).
    그 외의 능력(서브에이전트 spawn 등)은 에이전트의 **네이티브 능력**이지 파이썬 도구가
    아니다. 따라서 이 모듈은 딱 **읽기 / 디스패치 / 결과회수** 세 부류만 노출한다.

    이 레이어는 **기존 3b-0/3b-1 프리미티브의 얇은 래퍼**다 — 새 스케줄링 로직을 담지
    않는다. 강제(레포락·어드미션)는 전적으로 파이썬 스케줄러(``tick()``)의 몫이고
    (D1 = 에이전트 제안 / 파이썬 강제), 에이전트는 **순서/페이싱만 제안**한다. 여기의
    ``dispatch`` 는 "그 티켓을 지금 태워달라"는 제안일 뿐 — 레포락/자원압에 걸리면 파이썬이
    **defer**한다(반환 dict의 ``dispatched``/``deferred`` 로 드러난다).

경계(불변식):
    - **per-user 격리**: 이 툴 인스턴스는 **한 파일럿 사용자**에 바인딩된다. 다른 사용자
      소유 잡은 읽기에서 제외되고, ``dispatch`` 는 교차 사용자 티켓을 **거부**(ValueError)
      한다 → 파일럿 경로가 비파일럿 사용자를 절대 건드리지 않음을 코드로 보장.
    - **부작용 최소**: ``read_state``/``eligible_tickets``/``collect`` 는 순수 READ.
      ``dispatch`` 만 상태를 만지며, 그 경로도 기존 register_pending(부가 마킹) +
      scheduler.tick(기존 강제 경로) 조합이라 새 전이 규칙을 도입하지 않는다.

역할 소속: **central**. 구현 Phase: **Phase 3b-2**(파일럿, 피처 플래그 OFF 기본).

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

from typing import Iterable, List, Optional

from app import queue as q


class Tier2Tools:
    """한 파일럿 사용자에 바인딩된 Tier-2 자원툴(READ / DISPATCH / COLLECT).

    Dispatcher(스케줄러 + pending 레지스트리 보유)를 주입받아, 에이전트가 부를 수 있는
    최소 표면만 재노출한다. 모든 메서드는 JSON 직렬화 가능한 값을 반환한다(에이전트
    도구 계약과 정합 — SDK @tool 핸들러가 그대로 감싸 쓸 수 있다).
    """

    def __init__(self, dispatcher, user: str) -> None:
        if not user:
            raise ValueError("Tier2Tools는 파일럿 사용자에 바인딩되어야 합니다(빈 user 불가)")
        self.dispatcher = dispatcher
        self.scheduler = dispatcher.scheduler
        self.user = user

    # ------------------------------------------------------------------
    # READ (부작용 0)
    # ------------------------------------------------------------------

    def read_state(self) -> dict:
        """전역 스케줄링 상태 스냅샷(3b-0 state_snapshot 위임, 읽기 전용).

        에이전트가 순서/페이싱을 제안하려고 조회하는 상태(활성/락/적격/자원 헤드룸).
        전역 뷰를 그대로 돌려준다 — per-user 필터는 :meth:`eligible_tickets`.
        """
        return self.scheduler.state_snapshot()

    def eligible_tickets(self) -> List[dict]:
        """**이 파일럿 사용자**의 적격(dispatch 후보) 티켓만 추린 목록.

        state_snapshot의 ``eligible`` (queued | interrupted&reset_at 도래)에서 이
        사용자 소유만 필터. 각 항목: {ticket, user, target_repos, status, reason}.
        비파일럿 사용자의 티켓은 애초에 노출되지 않는다(격리).
        """
        snap = self.scheduler.state_snapshot()
        return [e for e in snap.get("eligible", []) if e.get("user") == self.user]

    def my_running(self) -> List[dict]:
        """이 사용자의 현재 활성(running) 위임 목록(진행 파악용, 읽기 전용)."""
        snap = self.scheduler.state_snapshot()
        return [r for r in snap.get("running", []) if r.get("user") == self.user]

    # ------------------------------------------------------------------
    # DISPATCH (제안 — 파이썬이 강제/deferral 판단)
    # ------------------------------------------------------------------

    def dispatch(self, ticket: str, *, correlation_id: Optional[str] = None) -> dict:
        """한 티켓을 이 사용자 컨테이너로 디스패치 **제안**하고 결과 회수용으로 등록.

        절차(모두 기존 프리미티브):
            1) 교차 사용자 방어 — 티켓 잡이 이 파일럿 사용자 소유가 아니면 ValueError.
            2) ``register_pending`` (3b-1) — 위임을 correlation id로 pending 등록
               (기본 id=티켓). 이후 :meth:`collect` 가 이 id로 결과를 회수한다.
            3) ``scheduler.tick()`` (기존 강제 경로) — 레포락/어드미션을 **파이썬이**
               판단해 적격분을 dispatch한다. 락/자원압이면 이 티켓은 **defer**된다.

        반환(JSON 직렬화 가능):
            ticket/correlation_id  등록된 티켓·상관 id
            dispatched  이 tick으로 이 티켓이 (재)dispatch됐나
            deferred    아직 running이 아닌가(레포락/자원압으로 대기)
            job_status  현재 잡 상태
            tick_dispatched  이 tick이 dispatch한 전체 티켓 id(파이썬 강제 결과)

        Raises: KeyError(미존재 잡), ValueError(교차 사용자).
        """
        job = self.scheduler.jobs.get(ticket)
        if job is None:
            raise KeyError(f"디스패치 대상 잡 없음: {ticket}")
        if job.user != self.user:
            raise ValueError(
                f"교차 사용자 디스패치 거부: 티켓 {ticket} 소유자 {job.user!r} "
                f"!= 파일럿 {self.user!r}"
            )
        cid = self.dispatcher.register_pending(correlation_id or ticket, ticket=ticket)
        tick_dispatched = list(self.scheduler.tick())
        # tick 후 최신 상태 재조회.
        job = self.scheduler.jobs.get(ticket)
        job_status = job.status if job is not None else None
        return {
            "ticket": ticket,
            "correlation_id": cid,
            "dispatched": ticket in tick_dispatched or job_status == q.RUNNING,
            "deferred": job_status != q.RUNNING,
            "job_status": job_status,
            "tick_dispatched": tick_dispatched,
        }

    # ------------------------------------------------------------------
    # COLLECT (명시적 id로 결과 회수 — 3b-1 위임, 부작용 0)
    # ------------------------------------------------------------------

    def collect(self, correlation_ids: Iterable[str]) -> dict:
        """여러 correlation id를 한 번에 폴 — {id: 결과} 매핑(각 id에 정확히 매칭).

        pending은 영속 잡 상태에서 파생되므로 재시작 후에도 동일 결과(3b-1 불변식).
        """
        return self.dispatcher.poll_results(list(correlation_ids))

    def collect_one(self, correlation_id: str) -> dict:
        """단일 correlation id의 done/pending + 결과 payload 폴(3b-1 위임)."""
        return self.dispatcher.poll_result(correlation_id)

    def pending_ids(self) -> List[str]:
        """이 사용자의 아직 미해소(pending) 위임 correlation id 목록.

        전역 pending 집합(영속 파생)에서 이 사용자 소유만 필터 → 드레인 판정 입력.
        """
        out: List[str] = []
        for cid in self.dispatcher.pending.pending_ids():
            job = self.dispatcher.pending._resolve(cid)
            if job is not None and job.user == self.user:
                out.append(cid)
        return out
