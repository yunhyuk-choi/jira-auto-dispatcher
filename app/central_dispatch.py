"""central_dispatch — 모든 디스패치 진입점이 수렴하는 프랙탈 센트럴 세션 방출 seam.

폴러(신규/갱신 티켓)·상태워처(재오픈)·재배정(REDISPATCH)·수동 rerun 은 모두 이 한
seam 으로 해석된 잡을 **상주 센트럴 라이브 세션**에 이벤트로 주입(``inject_event``)하고,
관측성 레코드(``meta.fractal=True``, queued)를 같은 JobQueue store 에 남긴다 — 구 경로
스케줄러는 이 표식을 보고 디스패치하지 않는다(이중 실행 방지, ``scheduler._eligible_now``).
정상 폴링 티켓이 타는 경로(``poller._emit`` → ``inject_event`` → central_session →
worker_dispatch)와 **동일 메커니즘**이며, 재오픈/재배정/rerun 도 이 seam 으로 통일한다.

주입 실패 시 dedup claim 을 되돌리고 :class:`CentralInjectFailed` 를 올려 호출부가 다음
사이클에 재시도하게 한다(레거시 폴백 없음 — P3 유일-실행-경로 규율).

⚠️ fractal-OFF 은퇴 완료(chore/remove-legacy-serving): 호출부(poller/status_watcher/main)의
레거시 구 경로(scheduler enqueue/reopen/rerun 재-dispatch) 폴백은 삭제됐다. :func:`central_active`
는 이제 이 배포의 프랙탈-ON 게이트(sink 주입 + 플래그)로만 남으며 항상 참이다 — 유일하게
거짓일 수 있는 오설정 배포에선 호출부가 방출을 건너뛴다(레거시 실행 경로 없음).

역할 소속: **central**.
"""

from __future__ import annotations

import logging
from typing import Any

from app import queue as q

log = logging.getLogger("jad.central_dispatch")


class CentralInjectFailed(RuntimeError):
    """프랙탈 센트럴 세션 이벤트 주입 실패 — 레거시 enqueue 폴백 없이 재시도 신호(P3).

    프랙탈이 유일 실행 경로이므로, 주입이 실패해도 구 경로(scheduler enqueue → 워커
    폴링)로 방출하지 않는다(이중-체인 근본 제거). 대신 이 예외를 올려 폴/워처 루프가
    다음 사이클에 재시도하게 한다(dedup claim 은 방출부에서 되돌린다 — 재트리거 가능).
    """


def central_active(config: Any, central_sink: Any) -> bool:
    """이 방출이 프랙탈 센트럴 세션으로 가야 하는지 — sink 주입 + 플래그/지속세션 ON.

    ``central_sink`` 미주입(오설정 배포)이면 False. 그렇지 않으면
    :func:`app.central_session.central_fractal_enabled`(``run.fractal_central`` +
    지속 stream-json 세션 성립)에 위임한다. 이 배포는 영구 프랙탈-ON 이라 항상 참이며,
    거짓이면 호출부는 방출을 건너뛴다(레거시 구 경로 폴백 없음 — fractal-OFF 은퇴 완료).
    """
    if central_sink is None:
        return False
    from app.central_session import central_fractal_enabled

    return central_fractal_enabled(config)


def emit_to_central(config: Any, central_sink: Any, job_queue: Any, gate: Any, job: Any) -> None:
    """해석된 ``job`` 을 센트럴 세션에 주입 + 관측성 레코드(meta.fractal) 기록.

    호출 전 :func:`central_active` 로 게이팅한다 — 이 함수는 프랙탈 활성 전제로 동작한다.

    - 주입 성공 → JobQueue 에 프랙탈 레코드 기록(:func:`record_fractal_job`).
    - 주입 False/예외 → dedup claim 되돌림 + :class:`CentralInjectFailed`.
    """
    try:
        injected = central_sink.inject_event(job)
    except Exception as exc:  # noqa: BLE001 — 주입 예외: claim 되돌리고 재시도(레거시 폴백 없음)
        _release_claim(gate, job)
        log.exception("central-inject 예외 — 재시도(레거시 폴백 없음): %s", _ticket(job))
        raise CentralInjectFailed(_ticket(job)) from exc
    if not injected:
        _release_claim(gate, job)
        log.error("central-inject 실패 — 재시도(레거시 폴백 없음): %s", _ticket(job))
        raise CentralInjectFailed(_ticket(job))
    record_fractal_job(job_queue, job)


def record_fractal_job(job_queue: Any, job: Any) -> None:
    """프랙탈 잡을 JobQueue(같은 store)에 queued + ``meta.fractal`` 로 기록/표식(관측성 A.1).

    ``meta.fractal=True`` 표식을 달아 구 경로 스케줄러가 이 잡을 디스패치하지 않게 한다
    (관측성 레코드일 뿐 — 실행은 상주 센트럴 세션이 조율). 신규 티켓이면 ``enqueue``(티켓
    멱등), **이미 store 에 있는 티켓**(재오픈/재배정/rerun 리셋 후)이면 표식만 갱신한다 —
    ``enqueue`` 는 티켓 멱등이라 기존 레코드를 덮지 않기 때문. best-effort — 기록 실패가
    방출/폴 루프를 죽이지 않는다.
    """
    jq = job_queue
    if jq is None:
        return
    try:
        job.meta[q.FRACTAL_META_KEY] = True
        if not job.status:
            job.status = q.QUEUED
        if jq.get(job.ticket) is None:
            jq.enqueue(job)
        else:
            jq.update(job.ticket, **{q.FRACTAL_META_KEY: True})
    except Exception:  # noqa: BLE001 — 관측성 기록 실패가 방출/폴을 막지 않는다
        log.warning("프랙탈 잡 레코드 생성/표식 실패(격리): %s", _ticket(job))


def mark_fractal(job_queue: Any, ticket: str) -> None:
    """기존 store 레코드에 ``meta.fractal`` 표식만 붙인다(구 경로 스케줄러 디스패치 제외).

    재오픈/재배정/rerun 이 슬롯을 리셋(queued)하기 **전에** 호출해, 리셋~주입 사이의 짧은
    윈도우에서 스케줄러 tick 이 이 잡을 비-프랙탈로 오인해 running 으로 dispatch(→ 실행자
    없어 스턱)하지 못하게 한다. 레코드 부재면 무해 no-op(주입부의 record_fractal_job 이 뒤
    이어 표식을 확정한다). best-effort.
    """
    if job_queue is None:
        return
    try:
        job_queue.update(ticket, **{q.FRACTAL_META_KEY: True})
    except KeyError:
        pass
    except Exception:  # noqa: BLE001 — 표식 실패가 방출을 막지 않는다(record_fractal_job 이 백스톱)
        log.warning("프랙탈 표식 선-마킹 실패(격리): %s", ticket)


def _release_claim(gate: Any, job: Any) -> None:
    """주입 실패 시 dedup claim 을 되돌린다(best-effort) — 다음 폴에서 재트리거 가능.

    claim 이 걸린 채 방출이 실패하면 티켓이 stuck 되므로, ``gate.release`` 로 되돌려
    재시도 경로를 연다. release 실패는 격리(재시도 신호는 예외로 이미 올라간다).
    """
    ticket = _ticket(job)
    if not ticket:
        return
    try:
        gate.release(ticket)
    except Exception:  # noqa: BLE001 — claim 되돌리기 실패가 재시도 신호를 가리지 않게 격리
        log.warning("inject 실패 후 claim 되돌리기 실패(격리): %s", ticket)


def _ticket(job: Any) -> str:
    return getattr(job, "ticket", "") or ""
