"""High-watermark JQL 폴러(중앙 전용).

역할:
    poll_interval_sec 마다 Jira에 JQL을 던져 "등록(enabled) 사용자들에게 새로
    할당된 트리거 상태" 티켓을 찾아, dedup 게이트를 통과시킨 뒤(gate.claim),
    담당자 account_id를 레지스트리로 사용자에 매핑하고(enabled만) 그 사용자
    큐에 디스패치한다(dispatcher.enqueue → 스케줄러). high-watermark(마지막
    created 커서)를 state에 영속해 중복/누락을 줄인다(중복은 게이트가 흡수).

역할 소속: **central** (worker는 Jira를 직접 보지 않는다).

구현 Phase: **Phase 4** (폴러 + 웹훅).

흐름:
    1. watermark 로드
    2. JQL: project = <project> AND assignee in (<enabled account_id...>)
       AND status in (<match.statuses>) [AND created > "<watermark>"]
       ORDER BY created ASC
    3. 각 이슈: gate.claim(key) → resolve_user(enabled) → target_repos 해석 →
       Job 생성 → dispatcher.enqueue(user, job)
    4. watermark 전진(처리한 최대 created) 후 영속

참고:
    - 백그라운드 스레드로 상시 구동(main.py의 central 분기가 기동).
    - 웹훅과 동일하게 반드시 gate를 통과한 뒤 매핑/디스패치(직접 큐잉 금지).
    - 매핑 실패/미등록/비활성 사용자면 enqueue하지 않고 로그만 남긴다(가역성을
      위해 gate.release로 claim을 되돌린다 — 나중에 enabled 되면 재트리거 가능).
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from app.queue import Job

log = logging.getLogger("jad.poller")

# watermark 초기화 tz 폴백(config.resume.timezone 미설정/조회불가 시).
_DEFAULT_TIMEZONE = "Asia/Seoul"


def resolve_target_repos(issue: dict, repo_map: dict) -> list:
    """티켓의 components/labels를 REPO-MAP으로 레포 목록에 매핑(중복 제거·정렬).

    repo_map 값은 문자열(단일 레포) 또는 리스트(다중) 모두 허용. 매핑되는 키가
    하나도 없으면 빈 리스트(→ 스케줄러가 "미해석=전역 직렬"로 보수 처리).
    """
    if not repo_map:
        return []
    fields = (issue or {}).get("fields", {}) or {}
    keys: list = []
    for comp in fields.get("components", []) or []:
        name = comp.get("name") if isinstance(comp, dict) else comp
        if name:
            keys.append(str(name))
    for label in fields.get("labels", []) or []:
        if label:
            keys.append(str(label))

    repos: set = set()
    for key in keys:
        mapped = repo_map.get(key)
        if not mapped:
            continue
        if isinstance(mapped, str):
            repos.add(mapped)
        else:
            repos.update(str(r) for r in mapped)
    return sorted(repos)


def _mode_for(user, target_repos: list) -> str:
    """job-level autonomy_mode 결정(per_repo 오버라이드가 일치하면 우선)."""
    overrides = {user.per_repo.get(r) for r in target_repos if r in (user.per_repo or {})}
    overrides.discard(None)
    if len(overrides) == 1:
        return overrides.pop()
    return user.autonomy_mode


def build_job(config, key: str, issue: dict, user) -> Job:
    """티켓/이슈/사용자 → 채널 E Job(폴러·웹훅 공용 빌더)."""
    target_repos = resolve_target_repos(issue, getattr(config, "repo_map", {}))
    branch_prefix = getattr(config.git, "branch_prefix", "auto/")
    run = getattr(config, "run", None)
    return Job(
        ticket=key,
        user=user.username,
        target_repos=target_repos,
        autonomy_mode=_mode_for(user, target_repos),
        branch=f"{branch_prefix}{key}",
        context_refs={
            "runs": f"runs/{key}/",
            "dlc_meta": getattr(run, "dlc_meta_repo", "") if run else "",
            "dataspace_docs": getattr(run, "dataspace_docs_repo", "") if run else "",
        },
        meta={"per_repo": dict(user.per_repo or {})},
    )


class Poller:
    """high-watermark JQL 폴러."""

    def __init__(self, config, jira_client, gate, registry, dispatcher,
                 *, clock: Optional[Callable[[], datetime]] = None) -> None:
        """의존성 주입(설정·Jira 클라이언트·게이트·레지스트리·디스패처).

        ``clock``: tz-aware ``datetime`` 을 반환하는 주입식 시계(테스트용).
        미지정이면 ``datetime.now(resume.timezone)``.
        """
        self.config = config
        self.jira = jira_client
        self.gate = gate
        self.registry = registry
        self.dispatcher = dispatcher
        self._clock = clock
        self._stop = threading.Event()
        from app import state

        self._state = state
        self.watermark: Optional[str] = state.load_watermark()
        # 하드닝: 최초 실행(watermark 부재)이면 "now"로 초기화해 영속한다. 이러지
        # 않으면 build_jql에 created 하한 절이 빠져 **기존 To-Do 티켓 전부**가
        # 한꺼번에 트리거된다(stampede). now 이후 생성분만 트리거되도록 시드한다.
        if self.watermark is None:
            self.watermark = self._initial_watermark()
            self._state.save_watermark(self.watermark)
            log.info("watermark 최초 초기화(now 시드) — 기존 To-Do stampede 방지")

    # ------------------------------------------------------------------

    def _resume_tzinfo(self):
        """config.resume.timezone → tzinfo(조회 실패 시 UTC 폴백)."""
        tzname = (
            getattr(getattr(self.config, "resume", None), "timezone", "")
            or _DEFAULT_TIMEZONE
        )
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(tzname)
        except Exception:  # noqa: BLE001 — tzdata 부재 등 → UTC 폴백(안전)
            return timezone.utc

    def _now(self) -> datetime:
        """현재 시각(tz-aware). 주입 clock 우선, 없으면 resume.timezone 기준 now."""
        if self._clock is not None:
            return self._clock()
        return datetime.now(self._resume_tzinfo())

    def _initial_watermark(self) -> str:
        """최초 실행 watermark 초기값 = now(resume.timezone)의 ISO8601 문자열."""
        return self._now().isoformat()

    # ------------------------------------------------------------------

    def _enabled_account_ids(self) -> list:
        return [
            u.jira_account_id
            for u in self.registry.list_users()
            if u.enabled and u.jira_account_id
        ]

    def build_jql(self) -> Optional[str]:
        """트리거 조건 + watermark로 JQL 구성. enabled 사용자가 없으면 None."""
        account_ids = self._enabled_account_ids()
        if not account_ids:
            return None
        clauses = []
        project = getattr(self.config.jira, "project", "")
        if project:
            clauses.append(f"project = {project}")
        ids = ", ".join(f'"{a}"' for a in account_ids)
        clauses.append(f"assignee in ({ids})")
        statuses = list(getattr(self.config.match, "statuses", []) or [])
        if statuses:
            st = ", ".join(f'"{s}"' for s in statuses)
            clauses.append(f"status in ({st})")
        if self.watermark:
            clauses.append(f'created > "{self._jql_time(self.watermark)}"')
        return " AND ".join(clauses) + " ORDER BY created ASC"

    @staticmethod
    def _jql_time(iso: str) -> str:
        """ISO created → JQL용 'yyyy-MM-dd HH:mm'(분 단위; 초 손실은 dedup이 흡수)."""
        v = iso.strip()
        # Jira는 '+0900' 형태의 오프셋을 줄 수 있어 fromisoformat 호환으로 보정.
        try:
            norm = v
            if len(v) >= 5 and (v[-5] in "+-") and v[-3] != ":":
                norm = v[:-2] + ":" + v[-2:]
            dt = datetime.fromisoformat(norm.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return v[:16].replace("T", " ")

    def resolve_user(self, issue: dict):
        """이슈 담당자 account_id → enabled 등록 사용자(없으면 None)."""
        fields = (issue or {}).get("fields", {}) or {}
        assignee = fields.get("assignee") or {}
        account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
        if not account_id:
            return None
        return self.registry.get_by_account_id(account_id)

    def _make_job(self, key: str, issue: dict, user) -> Job:
        return build_job(self.config, key, issue, user)

    def poll_once(self) -> int:
        """1회 폴링 — claim → 매핑 → 디스패치. 처리(enqueue)한 건수 반환."""
        jql = self.build_jql()
        if jql is None:
            log.info("enabled 사용자가 없어 폴링 skip")
            return 0

        result = self.jira.search_jql(
            jql,
            fields=["assignee", "status", "created", "summary", "components", "labels"],
        )
        issues = result.get("issues", []) or []
        enqueued = 0
        max_created = self.watermark

        for issue in issues:
            key = issue.get("key")
            if not key:
                continue
            created = ((issue.get("fields") or {}).get("created")) or ""
            if created and (max_created is None or created > max_created):
                max_created = created

            if not self.gate.claim(key):
                continue  # 중복(폴러/웹훅 겹침) — 흡수

            user = self.resolve_user(issue)
            if user is None:
                # 미등록/비활성 → claim 되돌림(나중에 enabled 되면 재트리거 가능)
                log.info("매핑 실패(미등록/비활성) — skip & release: %s", key)
                self.gate.release(key)
                continue

            job = self._make_job(key, issue, user)
            self.dispatcher.enqueue(user.username, job)
            enqueued += 1
            log.info("dispatch: %s → user=%s repos=%s", key, user.username, job.target_repos)

        if max_created and max_created != self.watermark:
            self.watermark = max_created
            self._state.save_watermark(self.watermark)

        return enqueued

    def run_forever(self) -> None:
        """poll_interval_sec 간격 루프(백그라운드 스레드 진입점, 예외 격리)."""
        interval = int(getattr(self.config.jira, "poll_interval_sec", 60))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 폴러 스레드는 죽지 않아야 한다
                log.exception("poll_once 실패(다음 주기 재시도)")
            self._stop.wait(interval)

    def stop(self) -> None:
        """루프 정지 신호."""
        self._stop.set()
