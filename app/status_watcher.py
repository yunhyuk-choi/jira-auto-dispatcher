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
from app import scope as scope_mod
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

    # ``project`` 를 함께 받는다 — 재오픈 재-디스패치의 범위 게이트가 티켓의 프로젝트
    # 키를 응답에서 직접 읽게 하기 위해서다(:func:`app.scope.project_key_of`).
    _WATCH_FIELDS = ["status", "updated", "assignee", "summary", "components",
                     "labels", "project"]

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
        """1회 감시 — 취소·추적제외·외부완료·재오픈을 순서대로 처리. 처리 건수 dict 반환."""
        result = {"cancelled": 0, "optout": 0, "done": 0, "reopened": 0}
        self._detect_cancellations(result)
        self._detect_optout(result)
        self._detect_external_done(result)
        self._detect_reopens(result)
        return result

    # ------------------------------------------------------------------
    # 역-트리거 공통(취소 상태 / opt-out 라벨) — 폴러와 동일 config 원천
    # ------------------------------------------------------------------

    def _cancel_statuses(self) -> list:
        """config.match.cancel_statuses(기본 ['취소됨']). 하드코딩 대체(폴러 공유)."""
        return list(getattr(self.config.match, "cancel_statuses", [self.CANCELLED_STATUS]) or [])

    def _optout_labels(self) -> list:
        """config.match.optout_labels(기본 ['자동화_추적_해제']). 추적 해제 라벨."""
        return list(getattr(self.config.match, "optout_labels", ["자동화_추적_해제"]) or [])

    @staticmethod
    def _is_tracking_disabled(fields: dict, optout_labels) -> bool:
        """이슈 라벨 ∩ optout_labels ≠ ∅ 이면 True(추적 해제 라벨이 붙음)."""
        if not optout_labels:
            return False
        labels = set((fields or {}).get("labels", []) or [])
        return bool(labels & set(optout_labels))

    # ------------------------------------------------------------------
    # 취소 감지
    # ------------------------------------------------------------------

    def _project_clause(self) -> str:
        """감시 JQL 의 project 절 — **폴러와 같은 합집합**(:mod:`app.scope`).

        ⚠️ 예전에는 ``jira.project`` 하나만 걸었다. per-user scope 가 여러 프로젝트를
        허용하게 된 지금 그대로 두면, 기본 프로젝트가 아닌 곳의 티켓이 취소돼도 감시축이
        그것을 보지 못해 추적 잡이 계속 돈다. 합집합이 비면 예전처럼 빈 문자열을 돌려
        (프로젝트 무관 감시) 기존 동작을 보존한다 — 이 경로는 **이미 추적 중인 잡**에만
        작용하므로 새 티켓을 긁어 오지 않는다.
        """
        clause = scope_mod.project_clause(scope_mod.union_projects(self.registry, self.config))
        return f"{clause} AND " if clause else ""

    def _detect_cancellations(self, result: dict) -> None:
        """`취소 상태`(config) + updated 워터마크 JQL → 추적 중인 잡을 cancel_job."""
        cancels = self._cancel_statuses()
        if not cancels:
            return  # 취소 감지 비활성(config가 명시 빈 리스트)
        st = ", ".join(f'"{s}"' for s in cancels)
        clauses = f'{self._project_clause()}status in ({st})'
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
                self.scheduler.cancel_job(key, reason=q.CANCEL_STATUS_CANCELLED)
                result["cancelled"] += 1
                log.info("취소 감지 → abort: %s (상태=%s)", key, job.status)
            except KeyError:
                pass

        if max_updated and max_updated != self.cancel_watermark:
            self.cancel_watermark = max_updated
            self._state.save_cancel_watermark(self.cancel_watermark)

    # ------------------------------------------------------------------
    # opt-out 라벨 감지(추적 제외 백스톱 — 웹훅 놓쳤을 때 대비)
    # ------------------------------------------------------------------

    def _detect_optout(self, result: dict) -> None:
        """추적 중인 잡의 티켓에 opt-out 라벨이 붙었으면 cancel_job(백스톱).

        1차 경로는 웹훅(reconcile → cancel_job)이고, 이것은 웹훅을 놓쳤을 때를 위한
        폴링 백스톱이다. ``_detect_cancellations`` 패턴을 준용하되, 워터마크 대신
        **추적 중(cancelable)인 잡의 키**로 대상을 좁혀 그 티켓의 현재 라벨을 확인한다.
        """
        optout = self._optout_labels()
        if not optout:
            return
        tracked = [j for j in self.scheduler.jobs.list_jobs()
                   if j.status in self._CANCELABLE]
        if not tracked:
            return
        keys = [j.ticket for j in tracked]
        ids = ", ".join(f'"{k}"' for k in keys)
        labels_in = ", ".join(f'"{l}"' for l in optout)
        jql = f'key in ({ids}) AND labels in ({labels_in})'

        res = self.jira.search_jql(jql, fields=self._WATCH_FIELDS)
        for issue in res.get("issues", []) or []:
            key = issue.get("key")
            if not key:
                continue
            job = self.scheduler.jobs.get(key)
            if job is None or job.status not in self._CANCELABLE:
                continue  # 추적 안 하거나 이미 취소중/종결 → 스킵(멱등)
            try:
                self.scheduler.cancel_job(key, reason=q.CANCEL_UNTRACKED_OPTOUT)
                result["optout"] += 1
                log.info("opt-out 라벨 감지 → abort: %s (상태=%s)", key, job.status)
            except KeyError:
                pass

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

    def _resolve_user(self, key: str, issue: dict):
        """담당자 account_id → enabled 등록 사용자 **+ 프로젝트 범위 게이트**.

        재오픈은 **재-디스패치**다 — 폴러·웹훅과 같은 게이트를 통과해야 한다
        (:func:`app.scope.resolve_user_in_scope`). 그러지 않으면 "취소됐다 되살아난
        티켓"이 per-user scope 를 우회하는 뒷문이 된다.
        """
        user, _reason = scope_mod.resolve_user_in_scope(
            self.registry, self.config, key, issue)
        return user

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

            # opt-out 재개 백스톱: 라벨이 **아직** 붙어 있으면 재-enqueue하지 않는다.
            # 라벨이 제거되면(updated 전진) 이 게이트를 통과해 재-enqueue된다 — 즉,
            # 라벨 제거→재개의 폴링 백스톱이 이 재오픈 경로로 수렴한다(REOPEN_STATUS
            # = match 상태일 때). 웹훅이 1차, 이 폴링이 2차.
            fields = (issue or {}).get("fields", {}) or {}
            if self._is_tracking_disabled(fields, self._optout_labels()):
                log.info("재오픈 skip(opt-out 라벨 잔존): %s", key)
                continue

            user = self._resolve_user(key, issue)
            if user is None:
                log.info("재오픈 매핑 실패(미등록/비활성/범위 밖) — skip: %s", key)
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
