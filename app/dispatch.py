"""디스패치 — 사용자별 잡 큐 + central↔worker HTTP 프로토콜(중앙 전용).

역할:
    폴러가 매핑한 잡을 "사용자별" 큐에 넣고(enqueue), 그 사용자의 worker가
    HTTP로 다음 잡을 가져가고(GET .../next) 실행 상태·로그를 회신(POST .../status)
    하는 중앙 측 디스패치 허브. worker는 Jira를 직접 보지 않고 오직 이 HTTP로만
    잡을 주고받는다.

역할 소속: **central**.

구현 Phase: **Phase 3** (레지스트리 + 디스패치 큐/HTTP).

HTTP 계약(dispatch_bp, main.py가 등록):
    GET  /dispatch/<user>/next
        worker 폴링 진입점. 그 사용자의 다음 queued 잡 1건을 반환하고 running
        으로 전이(원자적 클레임). 없으면 204/빈 응답.
    POST /dispatch/<user>/<job>/status
        worker가 상태/로그/결과를 회신. 본문: {status, log?, reset_at?, branch?,
        session_id?, mr_url?, error?}. 상태머신(queue.py)에 반영.

잡 상태머신은 queue.py(Job/상태상수)를 재사용한다:
    queued → running → (interrupted[reset_at]) → done/failed
    interrupted는 스케줄러가 reset_at에 재-enqueue(worker가 --resume로 이어감).

영속:
    - 사용자별 큐는 state.py로 영속(JOBS_FILE, 사용자 키 포함) → 재시작 복원.
    - concurrency_per_worker(기본 1) 초과 클레임 방지.

참고:
    - worker 인증: 최소한 사용자 스코프 토큰/공유 시크릿으로 next/status를 보호
      (Phase 3에서 결정). 사내망 한정 전제이나 사용자 교차 클레임은 막는다.
"""

from __future__ import annotations

from typing import Optional

from flask import Blueprint

from app.queue import Job

# 라우트는 main.py(central)에서 등록한다(Phase 3).
dispatch_bp = Blueprint("dispatch", __name__)


class Dispatcher:
    """사용자별 잡 큐 + HTTP 핸들러(스텁)."""

    def __init__(self, registry, job_queue) -> None:
        """의존성 주입(레지스트리·잡 스토어).

        TODO(Phase 3): 참조 보관 + 사용자별 큐 인덱스 준비(락 포함).
        """
        self.registry = registry
        self.queue = job_queue

    def enqueue(self, user: str, job: Job) -> None:
        """사용자 user의 큐에 잡을 queued로 등록(폴러가 호출).

        TODO(Phase 3): 사용자 스코프로 job 태깅 → queue.enqueue → 영속.
        """
        raise NotImplementedError("TODO(Phase 3): enqueue")

    def next_job(self, user: str) -> Optional[Job]:
        """사용자 user의 다음 queued 잡을 반환하고 running 전이(원자적).

        TODO(Phase 3): concurrency_per_worker 확인 → queued 1건 클레임 →
        running 전이 → 반환. 없으면 None.
        """
        raise NotImplementedError("TODO(Phase 3): next_job")

    def report_status(self, user: str, job_id: str, payload: dict) -> None:
        """worker 회신(status/log/reset_at 등)을 상태머신에 반영.

        TODO(Phase 3): payload.status 검증 → queue.set_status → interrupted면
        reset_at 기록(스케줄러가 재개 예약) → 영속.
        """
        raise NotImplementedError("TODO(Phase 3): report_status")

    def list_jobs(self, user: Optional[str] = None) -> list:
        """잡 현황 목록(전 사용자 또는 특정 사용자 — 관리 UI용).

        TODO(Phase 3): user 필터 적용해 잡 스냅샷 반환.
        """
        raise NotImplementedError("TODO(Phase 3): list_jobs")


# --- HTTP 핸들러(라우트 본체는 main.py에서 dispatch_bp에 배선) ---


def handle_next(dispatcher: Dispatcher, user: str):
    """GET /dispatch/<user>/next — 다음 잡 1건 반환(running 전이).

    TODO(Phase 3): dispatcher.next_job(user) → JSON. 없으면 204.
    """
    raise NotImplementedError("TODO(Phase 3): handle_next")


def handle_status(dispatcher: Dispatcher, user: str, job: str, request):
    """POST /dispatch/<user>/<job>/status — worker 상태/로그 회신 수신.

    TODO(Phase 3): request.json 파싱 → dispatcher.report_status → 2xx.
    """
    raise NotImplementedError("TODO(Phase 3): handle_status")
