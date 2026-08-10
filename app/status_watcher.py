"""상태 감시축 — 취소/외부완료/재오픈 감시 루프(중앙 전용) — RECURSIVE-DISPATCH §10.

역할:
    §2의 폴러가 **전진축**(created 워터마크로 신규 티켓 감시)이라면, 이 워처는
    **상태 감시축**이다. 활성/추적 중인 잡의 티켓 상태를 재조회해 다음을 처리한다.

        - **취소 감지** — `status = "취소됨" AND updated >= <cancel_watermark>` 로 최근
          취소된 티켓을 찾아, 중앙이 추적 중인(활성/대기) 잡을 abort시킨다
          (scheduler.cancel_job). 실행 중이면 worker가 롤백 후 회신, 대기 중이면 즉시
          드롭. `완료`(정상)와 `취소됨`(중단)은 statusCategory가 같으므로 **상태 이름**
          으로만 구분한다.
        - **외부 완료 감지** — 활성 잡의 티켓이 (중앙 아닌) 남에 의해 `완료`로 끝나
          있으면 정상 종료 처리(롤백 X).
        - **재오픈 감지** — 취소 확정(cancelled) 잡의 티켓이 `해야 할 일`로 돌아오면
          재-claim + 재-enqueue(같은 티켓 다시 작업). 별도 updated 워터마크 유지.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다).

구현: 폴링 기반(웹훅은 후속). 예외 격리 루프(폴러와 동일 규율).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Optional

from app import queue as q
from app.poller import build_job

log = logging.getLogger("jad.status_watcher")


def _jql_time(iso: str) -> str:
    """ISO updated → JQL용 'yyyy-MM-dd HH:mm'(분 단위; 초 손실은 멱등이 흡수)."""
    v = (iso or "").strip()
    if not v:
        return v
    try:
        norm = v
        if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":
            norm = v[:-2] + ":" + v[-2:]
        dt = datetime.fromisoformat(norm.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return v[:16].replace("T", " ")


class StatusWatcher:
    """취소/외부완료/재오픈 상태 감시 루프."""

    CANCELLED_STATUS = "취소됨"    # 중단 신호(롤백 O)
    DONE_STATUS = "완료"           # 정상 종료(롤백 X)
    REOPEN_STATUS = "해야 할 일"   # 재오픈 액션가능 상태

    # 취소 신호를 실제로 반영할 대상 잡 상태(이미 종결/취소중이면 건너뜀 — 멱등).
    _CANCELABLE = frozenset({q.QUEUED, q.RUNNING, q.INTERRUPTED})

    _WATCH_FIELDS = ["status", "updated", "assignee", "summary", "components", "labels"]

    def __init__(self, config, jira_client, gate, registry, dispatcher,
                 now_provider=None) -> None:
        """의존성 주입(설정·Jira·게이트·레지스트리·디스패처)."""
        self.config = config
        self.jira = jira_client
        self.gate = gate
        self.registry = registry
        self.dispatcher = dispatcher
        self.scheduler = dispatcher.scheduler
        self._stop = threading.Event()
        self._now = now_provider
        from app import state

        self._state = state
        self.cancel_watermark: Optional[str] = state.load_cancel_watermark()
        self.reopen_watermark: Optional[str] = state.load_reopen_watermark()

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------

    def poll_once(self) -> dict:
        """1회 감시 — 취소·외부완료·재오픈을 순서대로 처리. 처리 건수 dict 반환."""
        result = {"cancelled": 0, "done": 0, "reopened": 0}
        self._detect_cancellations(result)
        self._detect_external_done(result)
        self._detect_reopens(result)
        return result

    # ------------------------------------------------------------------
    # 취소 감지
    # ------------------------------------------------------------------

    def _project_clause(self) -> str:
        project = getattr(self.config.jira, "project", "")
        return f"project = {project} AND " if project else ""

    def _detect_cancellations(self, result: dict) -> None:
        """`취소됨` + updated 워터마크 JQL → 추적 중인 잡을 cancel_job."""
        clauses = f'{self._project_clause()}status = "{self.CANCELLED_STATUS}"'
        if self.cancel_watermark:
            clauses += f' AND updated >= "{_jql_time(self.cancel_watermark)}"'
        jql = clauses + " ORDER BY updated ASC"

        res = self.jira.search_jql(jql, fields=self._WATCH_FIELDS)
        issues = res.get("issues", []) or []
        max_updated = self.cancel_watermark
        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            updated = ((issue.get("fields") or {}).get("updated")) or ""
            if updated and (max_updated is None or updated > max_updated):
                max_updated = updated

            job = self.scheduler.jobs.get(key)
            if job is None or job.status not in self._CANCELABLE:
                continue  # 추적 안 하거나 이미 취소중/종결 → 스킵(멱등)
            try:
                self.scheduler.cancel_job(key)
                result["cancelled"] += 1
                log.info("취소 감지 → abort: %s (상태=%s)", key, job.status)
            except KeyError:
                pass

        if max_updated and max_updated != self.cancel_watermark:
            self.cancel_watermark = max_updated
            self._state.save_cancel_watermark(self.cancel_watermark)

    # ------------------------------------------------------------------
    # 외부 완료 감지(롤백 없음)
    # ------------------------------------------------------------------

    def _detect_external_done(self, result: dict) -> None:
        """실행 중 잡의 티켓이 남에 의해 `완료`면 정상 종료 처리(롤백 X)."""
        running = [j for j in self.scheduler.jobs.list_jobs() if j.status == q.RUNNING]
        if not running:
            return
        keys = [j.ticket for j in running]
        ids = ", ".join(f'"{k}"' for k in keys)
        jql = f'key in ({ids}) AND status = "{self.DONE_STATUS}"'
        res = self.jira.search_jql(jql, fields=self._WATCH_FIELDS)
        for issue in res.get("issues", []) or []:
            key = issue.get("key")
            if not key:
                continue
            try:
                self.scheduler.on_complete(key, q.DONE)  # 롤백 없이 정상 종료
                result["done"] += 1
                log.info("외부 완료 감지 → 정상 종료(롤백 X): %s", key)
            except KeyError:
                pass

    # ------------------------------------------------------------------
    # 재오픈 감지
    # ------------------------------------------------------------------

    def _resolve_user(self, issue: dict):
        """이슈 담당자 account_id → enabled 등록 사용자(없으면 None)."""
        fields = (issue or {}).get("fields", {}) or {}
        assignee = fields.get("assignee") or {}
        account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
        if not account_id:
            return None
        return self.registry.get_by_account_id(account_id)

    def _detect_reopens(self, result: dict) -> None:
        """취소 확정 잡의 티켓이 `해야 할 일`로 오면 재-claim + 재-enqueue(§10.4)."""
        cancelled = [j for j in self.scheduler.jobs.list_jobs() if j.status == q.CANCELLED]
        if not cancelled:
            return
        keys = [j.ticket for j in cancelled]
        ids = ", ".join(f'"{k}"' for k in keys)
        clauses = f'key in ({ids}) AND status = "{self.REOPEN_STATUS}"'
        if self.reopen_watermark:
            clauses += f' AND updated >= "{_jql_time(self.reopen_watermark)}"'
        jql = clauses + " ORDER BY updated ASC"

        res = self.jira.search_jql(jql, fields=self._WATCH_FIELDS)
        issues = res.get("issues", []) or []
        max_updated = self.reopen_watermark
        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            updated = ((issue.get("fields") or {}).get("updated")) or ""
            if updated and (max_updated is None or updated > max_updated):
                max_updated = updated

            user = self._resolve_user(issue)
            if user is None:
                log.info("재오픈 매핑 실패(미등록/비활성) — skip: %s", key)
                continue
            # 취소 확정 시 dedup가 풀렸으므로 재-claim(멱등; 결과 무시).
            self.gate.claim(key)
            job = build_job(self.config, key, issue, user)
            self.scheduler.reopen(job)
            result["reopened"] += 1
            log.info("재오픈 → 재-enqueue: %s → user=%s", key, user.username)

        if max_updated and max_updated != self.reopen_watermark:
            self.reopen_watermark = max_updated
            self._state.save_reopen_watermark(self.reopen_watermark)

    # ------------------------------------------------------------------
    # 백그라운드 루프
    # ------------------------------------------------------------------

    def run_forever(self) -> None:
        """poll_interval_sec 간격 감시 루프(백그라운드 스레드 진입점, 예외 격리)."""
        interval = int(getattr(self.config.jira, "poll_interval_sec", 60))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 감시 스레드는 죽지 않아야 한다
                log.exception("status_watcher poll_once 실패(다음 주기 재시도)")
            self._stop.wait(interval)

    def stop(self) -> None:
        """루프 정지 신호."""
        self._stop.set()
