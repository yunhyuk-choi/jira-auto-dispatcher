"""High-watermark JQL 폴러(중앙 전용) — 전진 트리거축.

역할:
    poll_interval_sec 마다 Jira에 JQL을 던져 "등록(enabled) 사용자에게 새로
    올라온 작업" 티켓을 찾아, dedup 게이트를 통과시킨 뒤(gate.claim), 담당자
    account_id를 레지스트리로 사용자에 매핑하고(enabled만) 그 사용자 큐에
    디스패치한다(dispatcher.enqueue → 스케줄러).

    **전진 트리거 = (A) 신규 생성 OR (B) 담당자가 enabled로 변경.** 둘 다
    `status in (match.statuses)` + `assignee in (enabled ids)` 조건 하에서다.
    상태만 바뀌거나 댓글만 달린 건은 트리거하지 않는다 — 그래서 (B)는 updated
    기준이 아니라 changelog의 `assignee CHANGED TO (...)` 를 본다.

    두 축은 각자의 커서를 갖는다:
        - **created 워터마크**(A) — 마지막으로 처리한 max created. (기존 그대로.)
        - **assignee 워터마크**(B) — 폴 시각 커서. 매 폴 사이클 now로 전진하고
          최초 실행 시 now로 시드해 과거 담당자-변경 이력 스탬피드를 막는다.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다).

구현 Phase: **Phase 4** (폴러 + 웹훅).

흐름:
    1. 두 워터마크 로드(부재 시 now 시드)
    2. (A) JQL: project in (<전 사용자 scope 합집합>) AND assignee in (<enabled ids>)
       AND status in (<match.statuses>) [AND created > "<created_wm>"]
       ORDER BY created ASC
       (B) JQL: project in (<전 사용자 scope 합집합>) AND assignee in (<enabled ids>)
       AND status in (<match.statuses>)
       AND assignee CHANGED TO (<enabled ids>) [AFTER "<assignee_wm>"]
       ORDER BY updated ASC
       ⚠️ 합집합이 비면 **JQL 을 던지지 않는다**(None) — 예전처럼 project 절만 빠져
       전 프로젝트를 긁는 일이 없도록. 왜 안 도는지는 WARNING 으로 남는다.
    3. (A)·(B) 결과를 **티켓 키로 합집합·중복제거** 후, 각 유니크 이슈:
       gate.claim(key) → resolve_user(enabled + **per-user 프로젝트 범위 게이트**) →
       target_repos 해석 → Job 생성 → _emit(user, job)(프랙탈 센트럴 세션 주입)
       (JQL 은 합집합이라 남의 프로젝트 티켓이 섞여 온다 — 담당자 매핑 단계에서
       :func:`app.scope.resolve_user_in_scope` 가 그것을 거른다.)
    4. created 워터마크는 처리한 max created로, assignee 워터마크는 now로 전진 후 영속

참고:
    - **자격 생존 확인(축0)**: 인증이 깨져도 Jira Cloud 의 JQL 검색은 200 + 빈 배열을
      돌려주므로 "매칭 티켓 없음"과 구별되지 않는다. 빈 폴에서 주기적으로
      ``GET /myself`` 를 때려 그 위장을 벗긴다(:meth:`Poller._verify_auth_if_due`,
      ``jira.auth_recheck_sec``). 감지되면 로그 + ``/api/doctor`` 로 드러난다.
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

from app import central_dispatch
from app import queue as q
from app import scheduler as sched
from app import scope as scope_mod
from app.central_dispatch import CentralInjectFailed  # noqa: F401 — 공용 seam 으로 이관, 재-export(하위호환)
from app.queue import Job
from app.repo_resolver import CentralAIRateLimited

log = logging.getLogger("jad.poller")

# watermark 초기화 tz 폴백(config.resume.timezone 미설정/조회불가 시).
_DEFAULT_TIMEZONE = "Asia/Seoul"

# 한 드레인 사이클에서 해석·디스패치할 pending 티켓 상한(한꺼번에 몰아치지 않도록).
_DRAIN_BATCH_DEFAULT = 10


class AICooldown:
    """central *자기* Claude 토큰의 레이트/사용량 한도 쿨다운(축1).

    한도(429/rate limit/usage limit/overloaded/quota)가 감지되면 일정 시간 claude
    호출을 멈춘다. 연속 한도에는 백오프가 **완만히 증가**(default × 2^(n-1), max로 캡)
    하고, 한 번이라도 해석에 성공하면 성장 카운터를 리셋한다.

    ⚠️ 이 쿨다운은 **claude 레포-해석 단계만** 막는다 — 이미 해석된 잡의 dispatch·
    자원 어드미션·레포락/완료 관리(스케줄러)는 이와 무관하게 계속 돈다.

    시각은 epoch(초) 부동소수로 다룬다(호출부 poller가 주입 시계 ``_now()`` 의
    ``.timestamp()`` 를 넘긴다 — 테스트 결정성). 쿨다운은 프로세스 내 상태로만 두고
    영속하지 않는다 — 재시작하면 즉시 드레인을 시도(회복 낙관)하고, 여전히 한도면
    첫 해석이 다시 쿨다운을 건다(pending은 영속되므로 유실 없음).
    """

    def __init__(self, default_sec: float = 120.0, max_sec: float = 900.0) -> None:
        self.default_sec = float(default_sec)
        self.max_sec = float(max_sec)
        self.until: float = 0.0        # 쿨다운 만료 epoch(0=해제)
        self._consecutive: int = 0     # 연속 한도 히트 수(백오프 성장 지수)

    def is_throttled(self, now: float) -> bool:
        """지금(now, epoch초) 쿨다운 중인가."""
        return now < self.until

    def note_rate_limited(self, now: float, reset_at: Optional[float] = None) -> float:
        """한도 감지 반영 — 쿨다운을 건다. 만료 epoch 반환.

        ``reset_at`` (응답의 reset/retry-after 절대 epoch)이 미래면 그 값을 쓰고,
        없으면 백오프(default × 2^(연속-1))를 now에 더한 값을 max로 캡해 건다.
        """
        self._consecutive += 1
        if reset_at is not None and reset_at > now:
            self.until = float(reset_at)
        else:
            backoff = self.default_sec * (2 ** (self._consecutive - 1))
            self.until = now + min(backoff, self.max_sec)
        return self.until

    def note_success(self) -> None:
        """해석 성공 반영 — 백오프 성장 리셋 + 쿨다운 해제."""
        self._consecutive = 0
        self.until = 0.0


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


def build_job(config, key: str, issue: dict, user, target_repos: Optional[list] = None) -> Job:
    """티켓/이슈/사용자 → 채널 E Job(폴러·웹훅 공용 빌더).

    ``target_repos`` 가 주어지면 그대로 쓴다(폴러의 LLM 리졸버 결과 주입). None이면
    정적 config.repo_map 룩업으로 폴백한다(웹훅·status_watcher 경로 기존 동작 유지).
    """
    if target_repos is None:
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
            # 설계 문서 레포(선택). 신규 키 ``docs``. 옛 config 객체가 넘어와도 값이
            # 비지 않도록 레거시 속성으로 폴백한다(프롬프트 빌더는 두 키 모두 읽는다).
            "docs": ((getattr(run, "docs_repo", "")
                      or getattr(run, "dataspace_docs_repo", "")) if run else ""),
        },
        meta={"per_repo": dict(user.per_repo or {})},
    )


class Poller:
    """high-watermark JQL 폴러."""

    def __init__(self, config, jira_client, gate, registry, dispatcher,
                 *, clock: Optional[Callable[[], datetime]] = None,
                 repo_map_loader: Optional[Callable[[], str]] = None,
                 llm_runner: Optional[Callable] = None,
                 central_sink: Optional[object] = None,
                 job_queue: Optional[object] = None) -> None:
        """의존성 주입(설정·Jira 클라이언트·게이트·레지스트리·디스패처).

        ``clock``: tz-aware ``datetime`` 을 반환하는 주입식 시계(테스트용).
        미지정이면 ``datetime.now(resume.timezone)``.

        ``repo_map_loader``: dlc-meta REPO-MAP.md 원문을 반환하는 주입식 로더
        (테스트 격리 — 라이브 git/파일 접근 대체). 미지정이면 config 기반 기본
        로더가 공유 워크스페이스 dlc-meta를 pull·read 한다.
        ``llm_runner``: repo_resolver의 claude 실행자 주입(테스트 격리).

        ``central_sink``(프랙탈, 기본 None): 상주 센트럴 라이브 세션 핸들
        (``inject_event(job)`` 을 노출하는 :class:`app.central_session.CentralSession`).
        해석된 티켓은 이 핸들을 통해 센트럴 세션에 이벤트로 주입된다(설계 §3.1) — 이것이
        **유일 실행 경로**다. 미주입(오설정 배포)이면 방출을 조용히 건너뛴다(레거시
        ``dispatcher.enqueue`` 폴백은 은퇴했다 — 그 소비자가 더 이상 없다). main.py 가
        지속 세션이 성립할 때만 이 핸들을 주입한다.
        """
        self.config = config
        self.jira = jira_client
        self.gate = gate
        self.registry = registry
        self.dispatcher = dispatcher
        self._central_sink = central_sink
        # 프랙탈 관측성(A.1): 프랙탈 경로에서 잡을 JobQueue(같은 store)에 queued 로
        # 기록해 대시보드에 뜨게 하는 핸들. 미주입이면 dispatcher.scheduler.jobs 로
        # 폴백한다(같은 인스턴스). 잡에 meta.fractal 표식을 달아 구 경로 스케줄러 tick 이
        # 이를 running 으로 올리지 않게 한다. None 이면 기록을 건너뛴다.
        self._job_queue = job_queue
        if self._job_queue is None:
            self._job_queue = getattr(getattr(dispatcher, "scheduler", None), "jobs", None)
        self._clock = clock
        self._repo_map_loader = repo_map_loader
        self._llm_runner = llm_runner
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

        # (B) 담당자-변경축 폴 시각 커서(created 워터마크와 별개). 최초 실행 시
        # now로 시드해 **과거 담당자-변경 이력**이 한꺼번에 트리거되는 스탬피드를
        # 막는다(now 이후 enabled로 바뀐 것만 트리거). poll_once가 매 폴 사이클
        # now로 전진시킨다. created 워터마크와 동일 포맷터·resume.timezone 재사용.
        self.assignee_watermark: Optional[str] = state.load_assignee_watermark()
        if self.assignee_watermark is None:
            self.assignee_watermark = self._initial_watermark()
            self._state.save_assignee_watermark(self.assignee_watermark)
            log.info("assignee_watermark 최초 초기화(now 시드) — 과거 담당자-변경 stampede 방지")

        # central 자기 토큰 레이트/사용량 한도 쿨다운(축1). run.ai_cooldown_* 로 조율.
        run = getattr(config, "run", None)
        default_sec = float(getattr(run, "ai_cooldown_default_sec", 120)) if run else 120.0
        max_sec = float(getattr(run, "ai_cooldown_max_sec", 900)) if run else 900.0
        self._ai_cd = AICooldown(default_sec=default_sec, max_sec=max_sec)
        # 미해석 대기 티켓(축1) — 쿨다운 중 수신은 됐으나 아직 레포 해석을 못 한 티켓들.
        # dedup claim은 유지된 채로 여기 보관·영속(재시작에도 유실 없음)했다가 드레인.
        # 각 원소: {"key": ..., "issue": {...}}. 메모리 미러 + 변경 시 영속(state).
        # thread-safety: 웹훅 데몬 스레드 다중(trigger_ticket→_add/_reconcile→_drop) +
        # 폴 스레드(poll_once/drain)가 동시에 _pending을 변이·영속하므로 전용 leaf 락으로
        # **모든 읽기·쓰기·persist**를 보호한다(항목 손실·파일 경합 방지). RLock인 이유는
        # _add/_drop/drain이 락을 쥔 채 _persist_pending을 재호출(같은 스레드 재획득)하기
        # 때문. 락 안에서는 리스트 변이 + persist(빠른 원자적 쓰기)만 하고, 느린 작업
        # (claude 해석·enqueue)은 스냅샷을 들고 **락 밖에서** 수행한다(데드락·정체 방지).
        self._pending_lock = threading.RLock()
        self._pending: list = list(state.load_pending_resolution([]) or [])

        # --- Jira 자격 생존 확인(축0) ---------------------------------------
        # 인증이 깨져도 JQL 검색은 200 + 빈 배열이라 "할 일 없음"과 구별되지 않는다.
        # _verify_auth_if_due 가 주기적으로 /myself 를 때려 그 위장을 벗긴다.
        self._auth_reporter: Optional[Callable] = None   # 진단에 알리는 콜백(주입)
        self._auth_checked_ts: float = 0.0               # 마지막 확인 시각(epoch, 0=미확인)
        self._auth_broken: bool = False                  # 직전 확인이 실패였나(로그 소음 억제)

    # ------------------------------------------------------------------

    def set_auth_reporter(self, reporter: Optional[Callable]) -> None:
        """Jira 자격 상태를 알릴 콜백을 주입한다 — ``reporter(ok: bool, detail: str)``.

        central 조립부(:func:`app.main.build_central_components`)가 부팅 자가진단
        (:meth:`app.doctor_runtime.DoctorRuntime.note_jira_auth`)을 물려, 폴링 중 감지한
        인증 실패가 ``/api/doctor`` → 관리 UI 배너로 드러나게 한다. 미주입이면 로그만
        남는다(폴러는 진단 모듈을 몰라도 된다 — 결합 최소화).
        """
        self._auth_reporter = reporter

    def _auth_recheck_sec(self) -> float:
        """자격 재확인 최소 간격(초). 0 이하면 재확인 안 함(``jira.auth_recheck_sec``)."""
        return float(getattr(self.config.jira, "auth_recheck_sec", 1800) or 0)

    def _note_auth(self, ok: bool, detail: str = "") -> None:
        """자격 확인 결과를 로그 + 진단에 반영(상태가 바뀔 때만 로그를 남긴다)."""
        if ok:
            if self._auth_broken:
                log.info("Jira 자격 회복 확인 — 폴링이 다시 티켓을 읽을 수 있습니다")
        elif not self._auth_broken:
            log.error(
                "⚠️ Jira 자격이 거부됩니다(%s) — JQL 검색은 오류가 아니라 **빈 결과**를 "
                "돌려주므로 겉으로는 '할 일 없음'처럼 보이지만 실제로는 아무 티켓도 "
                "읽지 못합니다. jira.watcher_token_file 의 토큰과 jira.watcher_email 을 "
                "갱신하세요.", detail or "사유 불명",
            )
        self._auth_broken = not ok
        reporter = self._auth_reporter
        if reporter is None:
            return
        try:
            reporter(ok, detail)
        except Exception:  # noqa: BLE001 — 보고 실패가 폴 루프를 죽이지 않게 격리
            log.warning("Jira 자격 상태 보고 실패(격리)")

    def _verify_auth_if_due(self, now_ts: float, *, saw_issues: bool) -> None:
        """빈 폴이 **인증 실패의 위장**인지 주기적으로 확인한다.

        Jira Cloud 실측:
            ``GET  /rest/api/3/myself``     → 401
            ``POST /rest/api/3/search/jql`` → 200 ``{"issues": [], "isLast": true}``

        즉 자격이 틀려도 검색은 성공한 척한다. 응답 **형태**로는 구별할 수 없으므로
        (:mod:`app.jira_client` 가 이미 잡는 "200 + HTML" 과 달리 이건 정상 JSON 이다)
        별도의 읽기 요청으로만 벗길 수 있다.

        빈도 판단(``jira.auth_recheck_sec``, 기본 30분):
            - **티켓이 하나라도 돌아온 폴은 그 자체가 자격 증거**다 → 요청 없이 OK 로 기록.
            - 빈 폴에서만, 그것도 재확인 주기가 지났을 때만 ``/myself`` 를 부른다. 매 폴
              (기본 60초)마다 부르면 하루 1,440회가 늘지만 30분 간격이면 48회다. 그
              대가는 최악의 감지 지연 30분인데, 지금은 **영원히 감지되지 않는다.**

        예외는 전부 삼킨다 — 이 확인은 *진단*이지 폴링의 전제 조건이 아니다.
        """
        if saw_issues:
            self._auth_checked_ts = now_ts
            self._note_auth(True, "검색 결과로 확인(티켓 수신)")
            return
        interval = self._auth_recheck_sec()
        if interval <= 0:
            return
        if self._auth_checked_ts and (now_ts - self._auth_checked_ts) < interval:
            return
        myself = getattr(self.jira, "myself", None)
        if myself is None:
            return                      # 이 클라이언트로는 확인할 수단이 없다(대역 등)
        self._auth_checked_ts = now_ts
        try:
            myself()
        except Exception as exc:  # noqa: BLE001 — 진단 호출이 폴 루프를 죽이지 않게 격리
            # ⚠️ 예외 **본문**을 싣지 않는다(응답에 뭐가 섞여 올지 모른다). 상태코드만.
            code = getattr(exc, "status_code", None)
            self._note_auth(False, f"HTTP {code}" if code else type(exc).__name__)
        else:
            self._note_auth(True, "/myself 확인")

    # ------------------------------------------------------------------

    def _emit(self, user, job) -> None:
        """해석된 잡을 프랙탈 센트럴 라이브 세션에 이벤트로 주입(유일 실행 경로).

        프랙탈 주입 seam(설계 §3.1): 공용 :mod:`app.central_dispatch` seam 으로 상주 센트럴
        라이브 세션에 이벤트를 주입한다(센트럴 에이전트가 레포락 consult → 사용자별 서브
        스폰/이어위임 → 완료-리포트 수신 시 알림 채널 상신). 재오픈/재배정/rerun 도 같은
        seam 으로 수렴한다(진입점 통일).

        ⚠️ 레거시 은퇴 완료: 프랙탈이 **유일 실행 경로**다(fractal-OFF 폴백 제거됨).
        주입이 실패해도(파이프 깨짐 등) 구 경로(``dispatcher.enqueue`` → 스케줄러 → 워커
        폴링)로 **폴백하지 않는다** — 그 소비자는 이제 존재하지 않으며, 폴백은 이중-체인·
        이중-통지의 근원이었다. 실패면 dedup claim 을 되돌리고 :class:`CentralInjectFailed`
        를 올려 폴 루프가 다음 사이클에 재시도하게 한다(잡 유실 없음 — 재트리거 가능).

        ``central_active`` 가드는 이 배포의 프랙탈-ON 게이트(sink 주입 + 지속 세션 성립)이며
        항상 참이다 — 유일하게 거짓일 수 있는 오설정 배포에선 방출을 조용히 건너뛴다(다음
        폴에서 재시도).
        """
        if central_dispatch.central_active(self.config, self._central_sink):
            central_dispatch.emit_to_central(
                self.config, self._central_sink, self._job_queue, self.gate, job)
            log.info("central-inject: %s → user=%s repos=%s",
                     getattr(job, "ticket", ""), user.username, job.target_repos)

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

    def _scope_union(self) -> list:
        """enabled 사용자 전원의 유효 프로젝트 범위 합집합(:mod:`app.scope`)."""
        return scope_mod.union_projects(self.registry, self.config)

    def _project_clause_or_none(self, where: str) -> Optional[str]:
        """``project in (...)`` 절. 합집합이 비면 **None** + 왜 안 도는지 로그.

        합집합이 비었다는 것은 "아무도 프로젝트를 지정하지 않았고 인스턴스 기본값
        (``jira.project``)도 없다"는 뜻이다. 예전에는 이때 project 절이 통째로 빠져
        **등록 사용자에게 할당된 전 프로젝트** 티켓을 긁었다 — 조용히 넓어지는, 가장 나쁜
        실패다. 지금은 JQL 을 아예 만들지 않고 이유를 남긴다(조용히 아무것도 안 하면
        원인 추적이 불가능하다).
        """
        clause = scope_mod.project_clause(self._scope_union())
        if not clause:
            log.warning(
                "%s: 감시할 프로젝트가 없어 JQL 을 만들지 않습니다 — 등록(enabled) 사용자의 "
                "scope.projects 가 모두 비어 있고 인스턴스 기본값 jira.project 도 비어 "
                "있습니다. config.yaml 의 jira.project(또는 jira.projects)를 채우거나 "
                "관리 UI 온보딩에서 사용자 범위를 지정하세요.", where)
            return None
        return clause

    def build_jql(self) -> Optional[str]:
        """트리거 조건 + watermark로 JQL 구성.

        enabled 사용자가 없거나 **전 사용자 scope 합집합이 비면** None(폴링 안 함).
        """
        account_ids = self._enabled_account_ids()
        if not account_ids:
            return None
        project_clause = self._project_clause_or_none("build_jql")
        if project_clause is None:
            return None
        clauses = [project_clause]
        ids = ", ".join(f'"{a}"' for a in account_ids)
        clauses.append(f"assignee in ({ids})")
        statuses = list(getattr(self.config.match, "statuses", []) or [])
        if statuses:
            st = ", ".join(f'"{s}"' for s in statuses)
            clauses.append(f"status in ({st})")
        if self.watermark:
            clauses.append(f'created > "{self._jql_time(self.watermark)}"')
        optout = self._optout_exclusion_clause()
        if optout:
            clauses.append(optout)   # opt-out 라벨 티켓은 애초에 안 집음
        return " AND ".join(clauses) + " ORDER BY created ASC"

    def build_assignee_change_jql(self) -> Optional[str]:
        """(B) 담당자-변경 트리거 JQL — assignee가 enabled로 *바뀐* 티켓만.

        (A)의 created와 달리 changelog의 ``assignee CHANGED TO (<enabled ids>)
        AFTER "<assignee_watermark>"`` 를 본다. 이렇게 하면 상태 변경·댓글 등
        다른 수정에는 트리거되지 않는다(updated 기준을 쓰지 않는 이유). status
        in / assignee in 조건은 (A)와 동일하게 건다. enabled 사용자가 없으면 None.
        """
        account_ids = self._enabled_account_ids()
        if not account_ids:
            return None
        project_clause = self._project_clause_or_none("build_assignee_change_jql")
        if project_clause is None:
            return None
        ids = ", ".join(f'"{a}"' for a in account_ids)
        clauses = [project_clause]
        clauses.append(f"assignee in ({ids})")
        statuses = list(getattr(self.config.match, "statuses", []) or [])
        if statuses:
            st = ", ".join(f'"{s}"' for s in statuses)
            clauses.append(f"status in ({st})")
        changed = f"assignee CHANGED TO ({ids})"
        if self.assignee_watermark:
            changed += f' AFTER "{self._jql_time(self.assignee_watermark)}"'
        clauses.append(changed)
        optout = self._optout_exclusion_clause()
        if optout:
            clauses.append(optout)   # opt-out 라벨 티켓은 재배정으로도 안 집음
        # ORDER BY updated ASC — 변경 이벤트축은 updated로 정렬(전진은 폴 시각 now).
        return " AND ".join(clauses) + " ORDER BY updated ASC"

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

    # ------------------------------------------------------------------
    # 역-트리거 공통(취소 상태 / opt-out 라벨) — "이 티켓 작업 안 함" 수렴
    # ------------------------------------------------------------------

    def _cancel_statuses(self) -> list:
        """config.match.cancel_statuses(기본 ['취소됨']). 워처와 동일 원천."""
        return list(getattr(self.config.match, "cancel_statuses", ["취소됨"]) or [])

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

    @staticmethod
    def _escape_jql_value(value: str) -> str:
        r"""JQL 문자열 리터럴 이스케이프(``\`` → ``\\``, ``"`` → ``\"``)."""
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    def _optout_exclusion_clause(self) -> str:
        """opt-out 라벨 제외 JQL 절 — 라벨 없는 티켓은 포함(EMPTY OR NOT IN).

        ⚠️ ``labels NOT IN (...)`` 단독은 **라벨이 아예 없는 티켓을 제외**할 수 있어
        (Jira JQL null 처리) 정상 티켓을 통째로 놓친다. ``labels IS EMPTY`` 를 OR로
        묶어 무라벨 티켓을 반드시 포함시킨다. optout_labels 비면 빈 문자열.
        """
        labels = self._optout_labels()
        if not labels:
            return ""
        esc = ", ".join(f'"{self._escape_jql_value(l)}"' for l in labels)
        return f"(labels is EMPTY OR labels not in ({esc}))"

    def _reconcile_untrack(self, key: str, *, reason: str) -> None:
        """'이 티켓 작업 안 함' 수렴 — 추적 잡 취소 + pending/claim 정리(공용).

        취소 상태 또는 opt-out 라벨 감지 시 호출된다. 기존 ``scheduler.cancel_job``
        을 재사용해 큐 대기=즉시 취소+dedup 해제 / 실행 중=CANCELLING(worker 회신
        대기)로 수렴한다. ⚠️ cancel_job은 **추적 잡이 없으면 KeyError** 를 던지므로
        (종결만 no-op) 추적 여부를 먼저 확인하고 감싼다. 미해석 대기(pending)에만
        걸린 티켓이면 드롭 + claim 되돌림(회복 드레인이 다시 집지 않도록)."""
        scheduler = self.dispatcher.scheduler
        # 내부 reason 문자열('*-optout' / '*-cancel')을 잡에 남길 cancel_reason으로 매핑 —
        # 취소됨(상태)과 추적해제(라벨)를 반드시 구별한다(관리 UI 핵심 요구).
        cancel_reason = (q.CANCEL_UNTRACKED_OPTOUT if "optout" in reason
                         else q.CANCEL_STATUS_CANCELLED)
        job = scheduler.jobs.get(key)
        if job is not None and job.status not in q.TERMINAL_STATUSES:
            try:
                scheduler.cancel_job(key, reason=cancel_reason)
                log.info("reconcile(%s) → cancel_job: %s (상태=%s)", reason, key, job.status)
            except KeyError:
                pass
        # pending(미해석 대기)에만 걸린 티켓 → 드롭 + claim 되돌림(드레인 재픽 방지).
        if key in self.pending_keys():
            self._drop_pending(key)
            self.gate.release(key)
            log.info("reconcile(%s) → pending 드롭 + claim 해제: %s", reason, key)

    def resolve_user(self, issue: dict, key: str = ""):
        """이슈 담당자 account_id → enabled 등록 사용자 **+ 프로젝트 범위 게이트**.

        ⚠️ 매핑과 게이트는 **한 함수 안에** 있다(:func:`app.scope.resolve_user_in_scope`).
        JQL 은 전 사용자 scope 의 *합집합* 으로 던지므로, 남의 프로젝트 티켓이 결과에
        섞여 들어오는 것은 정상이다 — 그것을 걸러 내는 자리가 바로 여기다. 폴러·웹훅·
        워처가 전부 이 수렴점을 쓴다.

        ``key`` 는 이슈에 최상위 ``key`` 가 없는 호출(웹훅 재검증 응답 등)을 위한 보조
        입력이다. 범위 밖/미등록/비활성이면 None.
        """
        user, _reason = scope_mod.resolve_user_in_scope(
            self.registry, self.config, key or (issue or {}).get("key", ""), issue)
        return user

    @staticmethod
    def _assignee_account_id(issue: dict) -> Optional[str]:
        """이슈의 현재 담당자 account_id(없으면 None) — 재배정 감지용."""
        fields = (issue or {}).get("fields", {}) or {}
        assignee = fields.get("assignee") or {}
        return assignee.get("accountId") if isinstance(assignee, dict) else None

    def _handle_reassignment(self, key: str, issue: dict) -> bool:
        """dedup claim이 이미 잡혀 있을 때 **담당자 변경(핸드오프/재배정)** 인지 판정·라우팅.

        폴러·웹훅 양 경로의 공용 수렴점(DRY). claim이 걸려 있다는 것은 이 티켓에
        이미 잡이 있다는 뜻이다. 그 잡의 소유자 X와 이슈의 **현재 담당자** Y를 비교해:

            - 추적 잡 없음/종결, 담당자 해석 불가, 또는 Y==X → **False**(일반 dedup 스킵,
              기존 동작 보존).
            - Y≠X → scheduler.reassign_or_handoff로 라우팅(큐 대기=재배정/park, 실행 중=
              롤백 없는 핸드오프). **True** 반환(호출부는 스킵 대신 처리됨으로 로그).

        Y의 가용성(enabled)/모드는 레지스트리에서 해석해 스케줄러에 주입한다. Y가
        비활성/미등록이어도(park 대상) **감지는 한다** — 그래야 실행 중 잡을 checkpoint로
        보존하고 park할 수 있다. 그래서 enabled-only인 resolve_user가 아니라
        find_by_account_id(enabled 무관)로 Y를 찾는다.
        """
        scheduler = self.dispatcher.scheduler
        job = scheduler.jobs.get(key)
        if job is None or job.status in q.TERMINAL_STATUSES:
            return False
        account_id = self._assignee_account_id(issue)
        if not account_id:
            return False
        new_rec = self.registry.find_by_account_id(account_id)
        if new_rec is None or not new_rec.username:
            return False  # 미등록 담당자 → 재배정 판정 불가(일반 스킵)
        if new_rec.username == job.user:
            return False  # 같은 소유자(중복 트리거) → 일반 스킵
        # 담당자 변경 감지 — 핸드오프/재배정으로 라우팅.
        # ⚠️ 새 담당자 Y 가 enabled 라도 **이 티켓의 프로젝트가 Y 의 범위 밖**이면 Y 에게
        # 실행을 넘기지 않는다 — 비활성과 동일하게 다뤄 park 시킨다(실행 중 잡은
        # checkpoint 로 보존된다). 그러지 않으면 재배정이 per-user scope 를 우회하는
        # 구멍이 된다.
        in_scope = scope_mod.in_user_scope(key, issue, new_rec, self.config)
        if new_rec.enabled and not in_scope:
            log.info("재배정 대상이 범위 밖 — park 로 처리: %s (user=%s, 프로젝트=%s)",
                     key, new_rec.username, scope_mod.project_key_of(key, issue) or "?")
        enabled = bool(new_rec.enabled) and in_scope
        mode = _mode_for(new_rec, job.target_repos)
        # REDISPATCH(큐 대기분 재-소유) 는 정상 폴링 티켓과 **동일 프랙탈 seam** 으로 방출한다
        # (재-소유된 슬롯을 센트럴 세션에 주입 — 구 tick 으로 running 만들어 스턱나지 않게).
        # fractal-OFF 폴백 은퇴 완료: 훅을 항상 주입한다(레거시 tick 재-dispatch 분기 제거).
        signal = scheduler.reassign_or_handoff(
            key, new_rec.username, enabled=enabled, autonomy_mode=mode,
            on_redispatch=self._central_redispatch)
        if signal == sched.REASSIGN_SAME_OWNER or signal == sched.REASSIGN_NO_JOB:
            return False  # 경계 재확인(잡 소멸 등) → 일반 스킵
        log.info("담당자 변경 감지: %s (X=%s → Y=%s, enabled=%s) → %s",
                 key, job.user, new_rec.username, enabled, signal)
        return True

    def _central_redispatch(self, ticket: str) -> None:
        """재배정(REDISPATCH)로 Y에게 재-소유된 큐 대기 슬롯을 프랙탈 센트럴 세션에 주입.

        ``scheduler.reassign_or_handoff`` 이 락 밖에서 호출하는 훅. 슬롯은 이미
        ``jobs.reassign`` 으로 queued 재초기화됐다. 리셋~주입 윈도우에서 구 경로 tick 이 이
        잡을 비-프랙탈로 오인해 dispatch 하지 못하게 **먼저 fractal 표식**을 찍고
        (mark_fractal), 정상 폴링 티켓과 동일 seam(emit_to_central)으로 주입한다. 주입 실패는
        :class:`CentralInjectFailed` 로 전파돼 상위 폴/웹훅 루프가 재시도한다.

        ``central_active`` 가 거짓인 오설정 배포에서는 조용히 건너뛴다 — 방출 seam
        (:meth:`_emit`)·재오픈 seam 과 같은 게이트다. 여기서만 예외를 던지면 재배정이
        오설정 배포에서 폴 루프를 깨뜨린다(잡은 큐 대기로 남아 다음 기회를 기다린다).
        """
        if not central_dispatch.central_active(self.config, self._central_sink):
            return
        central_dispatch.mark_fractal(self._job_queue, ticket)
        job = self.dispatcher.scheduler.jobs.get(ticket)
        if job is None:
            return
        central_dispatch.emit_to_central(
            self.config, self._central_sink, self._job_queue, self.gate, job)

    def _make_job(self, key: str, issue: dict, user, target_repos: Optional[list] = None) -> Job:
        return build_job(self.config, key, issue, user, target_repos=target_repos)

    # ------------------------------------------------------------------
    # 레포 해석(LLM 플래너 — 신규 티켓 → target_repos)
    # ------------------------------------------------------------------

    def _resolution_mode(self) -> str:
        """config.run.repo_resolution ('llm'|'static'). 기본 'llm'."""
        run = getattr(self.config, "run", None)
        return (getattr(run, "repo_resolution", "llm") if run else "llm") or "llm"

    def _central_forge_token(self) -> Optional[str]:
        """central forge 토큰(dlc-meta pull용) — secrets.base_dir 상대 참조로만 읽음.

        참조 이름은 forge 중립 접근자가 고른다(:func:`app.config.central_forge_token_ref`)
        — 신규 ``forge.token_ref`` / 중립 별칭 / 레거시 ``run.repo_resolver_gitlab_token_ref``.
        """
        from app.config import central_forge_token_ref, read_secret

        ref = central_forge_token_ref(self.config)
        if not ref:
            return None
        base_dir = getattr(getattr(self.config, "secrets", None), "base_dir", "") or ""
        return read_secret(base_dir, ref)

    def _load_repo_map_md(self) -> str:
        """dlc-meta REPO-MAP.md 원문 로드(주입 로더 우선, 없으면 config 기반 기본).

        기본 로더는 공유 워크스페이스의 dlc-meta를 best-effort로 pull한 뒤 읽는다.
        **신규 티켓이 있을 때만** 호출된다(유휴=0 — poll_once가 게이트).
        """
        if self._repo_map_loader is not None:
            return self._repo_map_loader() or ""
        from app import repo_resolver

        return repo_resolver.load_repo_map_md(self.config, self._central_forge_token())

    def _resolve_repos(self, issue: dict, repo_map_md: str) -> list:
        """단일 티켓 → target_repos. LLM 판단(best-effort) + 정적 폴백.

        ⚠️ central 자기 토큰 한도면 :class:`CentralAIRateLimited` 를 **전파**한다
        (best-effort 폴백으로 흡수하지 않는다) — 호출부가 쿨다운/pending 처리.
        """
        from app import repo_resolver

        static_fb = resolve_target_repos(issue, getattr(self.config, "repo_map", {}))
        run = getattr(self.config, "run", None)
        timeout = int(getattr(run, "repo_resolver_timeout_sec", 60)) if run else 60
        claude_bin = (getattr(run, "claude_bin", "claude") if run else "claude") or "claude"
        return repo_resolver.resolve_target_repos_llm(
            issue, repo_map_md,
            runner=self._llm_runner,
            timeout=timeout,
            claude_bin=claude_bin,
            fallback=static_fb,
            now_fn=lambda: self._now().timestamp(),
        )

    # ------------------------------------------------------------------
    # central 자기 토큰 한도(축1) — 쿨다운 판정 + pending(미해석 대기) 관리
    # ------------------------------------------------------------------

    def _now_ts(self) -> float:
        """주입 시계 기준 현재 epoch(초) — 쿨다운 비교/백오프 환산용(테스트 결정성)."""
        return self._now().timestamp()

    def is_ai_throttled(self, now: Optional[float] = None) -> bool:
        """central 자기 Claude 토큰이 지금 쿨다운(한도) 중인가."""
        return self._ai_cd.is_throttled(self._now_ts() if now is None else now)

    def _persist_pending(self) -> None:
        # _pending_lock 하에서 스냅샷(copy)을 떠 원자적 쓰기에 넘긴다. 호출자가 이미
        # 락을 보유한 경우(RLock 재획득)와 단독 호출 모두 안전하다.
        with self._pending_lock:
            self._state.save_pending_resolution(list(self._pending))

    def pending_keys(self) -> list:
        """현재 미해석 대기(pending) 티켓 키 목록(관측/테스트용)."""
        with self._pending_lock:
            return [r.get("key") for r in self._pending if r.get("key")]

    def _add_pending(self, key: str, issue: dict) -> None:
        """티켓을 미해석 대기 집합에 보관(dedup claim은 유지). 키 기준 dedup·영속."""
        with self._pending_lock:
            for r in self._pending:
                if r.get("key") == key:
                    r["issue"] = issue  # 최신 스냅샷으로 갱신
                    self._persist_pending()
                    return
            self._pending.append({"key": key, "issue": issue})
            self._persist_pending()

    def _drop_pending(self, key: str) -> None:
        """미해석 대기 집합에서 티켓 제거(해석·디스패치 완료 또는 매핑 소멸 시)."""
        with self._pending_lock:
            before = len(self._pending)
            self._pending = [r for r in self._pending if r.get("key") != key]
            if len(self._pending) != before:
                self._persist_pending()

    def poll_once(self) -> int:
        """1회 폴링 — (A)신규 ∪ (B)담당자-변경 → claim → 매핑 → 디스패치.

        전진 트리거 = **신규 생성 OR 담당자 enabled로 변경**. 상태/댓글 변경은
        트리거하지 않는다. assignee_watermark now 시드/전진으로 과거 담당자-변경
        이력을 무시하고, **dedup 게이트(ticket 키) + 착수 후 상태 이탈**(match
        상태를 벗어나 두 쿼리 어디에도 안 걸림)이 루프/재디스패치를 막는다.

        처리(enqueue)한 건수 반환.
        """
        # (A)·(B) 공통 게이트: enabled 사용자가 하나도 없으면 어느 쪽도 못 만든다.
        if not self._enabled_account_ids():
            log.info("enabled 사용자가 없어 폴링 skip")
            return 0

        # 두 번째 공통 게이트: 감시할 프로젝트가 하나도 없으면 **JQL 을 던지지 않는다**
        # (예전에는 project 절 없이 전 프로젝트를 긁었다). 이유는 로그로 드러난다.
        if self._project_clause_or_none("poll_once") is None:
            return 0

        # assignee 워터마크 전진 기준(폴 시각). 폴 *시작* 시각으로 고정해, 폴 도중
        # 발생한 담당자-변경은 반드시 다음 폴에서 잡히게 한다(_jql_time 분 단위
        # 절삭이 경계를 과거로 당겨 gap 대신 overlap → dedup가 흡수).
        poll_now = self._now()

        # ``project`` 를 함께 받는다 — 범위 게이트가 티켓의 프로젝트 키를 이슈 키 접두사
        # 추정이 아니라 응답에서 직접 읽게 하기 위해서다(:func:`app.scope.project_key_of`).
        fields = ["assignee", "status", "created", "updated", "summary",
                  "components", "labels", "project"]

        # (A) 신규-티켓 경로 — created 워터마크 + ORDER BY created ASC (현행 유지).
        jql_created = self.build_jql()
        issues_a = (self.jira.search_jql(jql_created, fields=fields).get("issues", [])
                    or []) if jql_created else []

        # (B) 담당자-변경 경로 — assignee CHANGED TO enabled AFTER assignee 워터마크.
        jql_assignee = self.build_assignee_change_jql()
        issues_b = (self.jira.search_jql(jql_assignee, fields=fields).get("issues", [])
                    or []) if jql_assignee else []

        # 두 쿼리 결과를 **티켓 키로 합집합·중복제거**(먼저 만난 이슈 우선). 한
        # 티켓이 (A)·(B) 양쪽에 걸려도 여기서 1건으로 접히고, dedup 게이트가 2차
        # 방어를 한다(같은 티켓이 여러 번 재할당돼도 재디스패치 안 됨).
        unique: dict = {}
        for issue in (*issues_a, *issues_b):
            key = issue.get("key")
            if key and key not in unique:
                unique[key] = issue

        # 이 폴 사이클 기준 쿨다운 판정 시각(epoch초). 폴 시작 시각으로 고정한다.
        now_ts = poll_now.timestamp()

        # 축0: 빈 결과가 "할 일 없음"인지 "자격 만료"인지 주기적으로 가른다(위 메서드 참조).
        self._verify_auth_if_due(now_ts, saw_issues=bool(issues_a or issues_b))
        throttled = self._resolution_mode() == "llm" and self._ai_cd.is_throttled(now_ts)

        # LLM 레포 해석용 REPO-MAP은 **신규 티켓이 있을 때만** 한 번 로드한다
        # (유휴=0 — 빈 폴에선 pull/claude 호출이 전혀 없다). static 모드거나 신규
        # 티켓이 없으면 로드 자체를 건너뛴다. **쿨다운 중이면** claude를 부르지 않을
        # 것이므로 REPO-MAP pull도 생략한다(모두 pending으로 보관될 것).
        repo_map_md = ""
        if unique and self._resolution_mode() == "llm" and not throttled:
            try:
                repo_map_md = self._load_repo_map_md()
            except Exception:  # noqa: BLE001 — 로드 실패해도 폴러는 죽지 않음(정적 폴백)
                log.exception("REPO-MAP 로드 실패 → 정적 폴백")
                repo_map_md = ""

        enqueued = 0
        max_created = self.watermark

        for key, issue in unique.items():
            # created 워터마크 전진은 (A) 기준 그대로 — 유니크 이슈의 max created.
            created = ((issue.get("fields") or {}).get("created")) or ""
            if created and (max_created is None or created > max_created):
                max_created = created

            # 방어적 opt-out 게이트(JQL 제외의 백업): 라벨이 붙었으면 신규 착수하지
            # 않고, 이미 추적 중이면 취소로 수렴한다("이 티켓 작업 안 함").
            fields_o = (issue.get("fields") or {})
            if self._is_tracking_disabled(fields_o, self._optout_labels()):
                self._reconcile_untrack(key, reason="poll-optout")
                continue

            # 루프 방지 ①: dedup 게이트가 ticket 키로 막는다.
            if not self.gate.claim(key):
                # claim이 이미 걸림 = 이 티켓에 잡이 있다. 담당자가 X→Y로 바뀌었으면
                # 실행 중=핸드오프(롤백X)/큐 대기=재배정으로 라우팅한다(재배정 ≠ 취소).
                # 같은 소유자·해석불가면 기존대로 dedup 흡수.
                self._handle_reassignment(key, issue)
                continue

            user = self.resolve_user(issue, key)
            if user is None:
                # 미등록/비활성/범위 밖 → claim 되돌림(나중에 enabled·범위 포함되면 재트리거).
                log.info("매핑 실패(미등록/비활성/범위 밖) — skip & release: %s", key)
                self.gate.release(key)
                continue

            # target_repos 해석: llm 모드면 REPO-MAP+티켓으로 판단(best-effort,
            # 실패 시 정적 폴백), static 모드면 build_job이 정적 룩업.
            # ⚠️ central 자기 토큰 한도(축1): 쿨다운 중이면 claude를 부르지 않고 티켓을
            #    pending으로 보관한다(claim 유지·미디스패치 — 유실 없음). 해석을 시도하다
            #    한도가 감지되면(CentralAIRateLimited) 쿨다운을 걸고 그 티켓도 pending으로.
            target_repos = None
            if self._resolution_mode() == "llm":
                if self._ai_cd.is_throttled(now_ts):
                    self._add_pending(key, issue)
                    log.info("central AI 쿨다운 중 → pending 보관(미해석·미디스패치): %s", key)
                    continue
                try:
                    target_repos = self._resolve_repos(issue, repo_map_md)
                except CentralAIRateLimited as exc:
                    self._ai_cd.note_rate_limited(now_ts, exc.reset_at)
                    self._add_pending(key, issue)
                    log.warning("central AI 한도 감지 → 쿨다운 + pending 보관: %s", key)
                    continue
                else:
                    self._ai_cd.note_success()
            job = self._make_job(key, issue, user, target_repos=target_repos)
            self._emit(user, job)
            enqueued += 1
            log.info("dispatch: %s → user=%s repos=%s", key, user.username, job.target_repos)

        # 루프 방지 ②: 착수(진행 중 이동) 시 status가 트리거 상태를 벗어나 두 쿼리
        # 어디에도 매칭되지 않는다. created 워터마크는 max created로(기존대로),
        # assignee 워터마크는 폴 시각 now로 전진 — 그 사이 담당자-변경은 다음 폴에서.
        if max_created and max_created != self.watermark:
            self.watermark = max_created
            self._state.save_watermark(self.watermark)

        self.assignee_watermark = poll_now.isoformat()
        self._state.save_assignee_watermark(self.assignee_watermark)

        return enqueued

    def trigger_ticket(self, key: str, event: Optional[str] = None) -> bool:
        """웹훅 등 외부 이벤트로 **단일 티켓**을 즉시 트리거/재조정(폴링과 동일 수렴점).

        폴링 경로(poll_once)와 동일한 트리거 시맨틱을 강제한다:
            get_issue(재검증) → **역-트리거 재조정**(취소 상태/opt-out 라벨 → cancel_job)
            → 상태 게이트(match.statuses) → dedup 게이트(gate.claim) → 담당자 매핑(enabled)
            → target_repos 해석 → Job → dispatcher.enqueue.

        웹훅은 **즉시 트리거**만 담당하고, *무엇을 할지는 현재 상태 재조회로 판단*한다
        (이벤트 문자열은 신뢰하지 않는다 — ``event`` 는 로그로만 남긴다). 취소·추적제외는
        "이 티켓 작업 안 함"으로 수렴하므로 신규 착수 로직보다 **먼저** 재조정한다.

        ``run_forever`` 가 동시에 폴링 중이어도 안전하다 — 게이트/레지스트리/
        디스패처만 만지고(폴 루프와 동일 스레드-세이프 수준) **워터마크는 건드리지
        않는다**(공유 가변 상태 미도입). 별도 데몬 스레드에서 호출 가능.

        Returns:
            디스패치했으면 True, (미조회/상태 불일치/중복/미매핑/취소·추적제외) 스킵이면 False.
        """
        # ``project`` 를 함께 받는다 — 범위 게이트가 티켓의 프로젝트 키를 이슈 키 접두사
        # 추정이 아니라 응답에서 직접 읽게 하기 위해서다(:func:`app.scope.project_key_of`).
        fields = ["assignee", "status", "created", "updated", "summary",
                  "components", "labels", "project"]
        try:
            issue = self.jira.get_issue(key, fields=fields)
        except Exception as exc:  # noqa: BLE001 — 재검증 실패는 스킵(폴러 백스톱이 흡수)
            log.warning("webhook 트리거 이슈 조회 실패(%s): %s", key, exc)
            return False
        if not issue:
            log.warning("webhook 트리거 이슈 없음/빈 응답 — skip: %s", key)
            return False

        # 트리거 상태 게이트: match.statuses가 있으면 그 안에 있어야만 **신규** 트리거한다
        # (진행 중/완료로 이미 넘어간 티켓은 신규 디스패치하지 않는다).
        statuses = list(getattr(self.config.match, "statuses", []) or [])
        fields_d = (issue or {}).get("fields", {}) or {}
        status_obj = fields_d.get("status") or {}
        status_name = status_obj.get("name") if isinstance(status_obj, dict) else None
        status_ok = (not statuses) or (status_name in statuses)

        # --- 역-트리거 재조정(신규 착수보다 먼저) — event 미신뢰, 현재 상태로 판단 ---
        # 현재 상태가 취소 상태이거나 opt-out 라벨이 붙었으면 "이 티켓 작업 안 함"으로
        # 수렴한다: 추적 잡이 있으면 취소(cancel_job), 없으면 no-op. 웹훅의 "취소 즉시화"
        # + "라벨-중 잡 취소" + "라벨 있으면 신규 착수 안 함"이 이 한 분기로 처리된다.
        if status_name in self._cancel_statuses() or \
                self._is_tracking_disabled(fields_d, self._optout_labels()):
            reason = "webhook-cancel" if status_name in self._cancel_statuses() else "webhook-optout"
            log.info("webhook 재조정(%s, event=%s): %s", reason, event, key)
            self._reconcile_untrack(key, reason=reason)
            return False

        # 루프 방지 ①: dedup 게이트(폴러와 공용). claim이 이미 걸림 = 이 티켓에 라이브 잡
        # 존재. 담당자 변경(핸드오프/재배정)이면 스킵 대신 라우팅한다 — ⚠️ 상태 게이트와
        # **무관**하게 확인한다(실행 중 잡의 티켓은 이미 '진행 중'이라 match.statuses 밖일
        # 수 있으나 재배정은 감지해야 한다). 같은 소유자·해석불가면 기존대로 dedup 흡수.
        if not self.gate.claim(key):
            if self._handle_reassignment(key, issue):
                log.info("webhook 담당자 변경 처리: %s", key)
                return True
            log.info("webhook 트리거 skip(이미 claim): %s", key)
            return False

        # 여기부터는 새로 claim = 이 티켓에 라이브 잡 없음 → **신규** dispatch. 상태 게이트
        # 적용(match 밖이면 방금 얻은 claim 되돌리고 스킵 — 신규 트리거 대상 아님).
        if not status_ok:
            self.gate.release(key)
            log.info("webhook 트리거 skip(상태 불일치 %s): %s", status_name, key)
            return False

        user = self.resolve_user(issue, key)
        if user is None:
            # 미등록/비활성/범위 밖 → claim 되돌림(나중에 enabled·범위 포함되면 재트리거).
            log.info("webhook 트리거 매핑 실패(미등록/비활성/범위 밖) — release: %s", key)
            self.gate.release(key)
            return False

        # target_repos 해석: llm 모드면 REPO-MAP+티켓으로 판단(best-effort, 실패 시
        # 정적 폴백), static 모드면 build_job이 정적 룩업(target_repos=None).
        # ⚠️ central 자기 토큰 한도(축1): 쿨다운 중이면 claude 미호출·pending 보관
        #    (claim 유지·미디스패치 — 유실 없음). True 반환(수신·추적은 됨). 해석 중
        #    한도 감지 시에도 쿨다운을 걸고 pending으로 보관한다.
        target_repos = None
        if self._resolution_mode() == "llm":
            now_ts = self._now_ts()
            if self._ai_cd.is_throttled(now_ts):
                self._add_pending(key, issue)
                log.info("webhook: central AI 쿨다운 중 → pending 보관(미해석·미디스패치): %s", key)
                return True
            try:
                repo_map_md = self._load_repo_map_md()
            except Exception:  # noqa: BLE001 — 로드 실패해도 죽지 않음(정적 폴백)
                log.exception("webhook REPO-MAP 로드 실패 → 정적 폴백")
                repo_map_md = ""
            try:
                target_repos = self._resolve_repos(issue, repo_map_md)
            except CentralAIRateLimited as exc:
                self._ai_cd.note_rate_limited(now_ts, exc.reset_at)
                self._add_pending(key, issue)
                log.warning("webhook: central AI 한도 감지 → 쿨다운 + pending 보관: %s", key)
                return True
            else:
                self._ai_cd.note_success()

        job = self._make_job(key, issue, user, target_repos=target_repos)
        self._emit(user, job)
        log.info("webhook dispatch: %s → user=%s repos=%s", key, user.username, job.target_repos)
        return True

    def drain_pending_resolution(self, *, limit: int = _DRAIN_BATCH_DEFAULT) -> int:
        """쿨다운 회복 후 pending(미해석 대기) 티켓을 해석·디스패치(축1 드레인).

        쿨다운 중이거나 pending이 없으면 no-op(0). llm 모드에서만 의미가 있다.
        한 사이클 처리량은 ``limit`` 으로 제한한다(한꺼번에 몰아치지 않음). 각 티켓:
        저장된 issue로 담당자 재해석(현 레지스트리 기준) → 레포 해석(claude) → 잡
        생성 → enqueue → pending 제거. 담당자가 더 이상 enabled 아니면 claim 되돌리고
        pending에서 제거한다. 해석 중 한도가 **재발**하면 즉시 쿨다운을 재-무장하고
        나머지는 pending으로 남긴다(루프 중단). 이 사이클에 디스패치한 건수 반환.
        """
        if self._resolution_mode() != "llm":
            return 0
        if self._ai_cd.is_throttled(self._now_ts()):
            return 0
        # 락 안에서 처리 대상 스냅샷(copy)만 뜨고 즉시 락을 푼다 — 이후 느린 작업
        # (claude 해석·enqueue)은 락 밖에서 수행한다(leaf 락 유지·데드락 방지).
        with self._pending_lock:
            batch = list(self._pending)[:limit]
        if not batch:
            return 0

        # REPO-MAP은 드레인 배치당 한 번만 로드(pull/read). 실패해도 정적 폴백.
        try:
            repo_map_md = self._load_repo_map_md()
        except Exception:  # noqa: BLE001 — 로드 실패해도 드레인은 정적 폴백으로 진행
            log.exception("drain: REPO-MAP 로드 실패 → 정적 폴백")
            repo_map_md = ""

        drained = 0
        for record in batch:
            # 배치 도중 한도가 재발했으면(아래 note_rate_limited) 즉시 중단.
            if self._ai_cd.is_throttled(self._now_ts()):
                break
            key = record.get("key")
            issue = record.get("issue") or {}
            if not key:
                with self._pending_lock:
                    self._pending = [r for r in self._pending if r is not record]
                    self._persist_pending()
                continue

            user = self.resolve_user(issue, key)
            if user is None:
                # 담당자가 더 이상 등록/enabled 아니거나 티켓이 그 사람 범위 밖 → claim
                # 되돌리고 pending 제거(나중에 다시 enabled·범위 포함되면 재트리거 가능).
                log.info("drain: 매핑 실패(미등록/비활성/범위 밖) — release+drop: %s", key)
                self.gate.release(key)
                self._drop_pending(key)
                continue

            try:
                target_repos = self._resolve_repos(issue, repo_map_md)
            except CentralAIRateLimited as exc:
                # 한도 재발 → 쿨다운 재-무장, 나머지 pending 유지(다음 회복 사이클에).
                self._ai_cd.note_rate_limited(self._now_ts(), exc.reset_at)
                log.warning("drain: central AI 한도 재발 → 재-무장, pending 유지: %s", key)
                break
            else:
                self._ai_cd.note_success()

            job = self._make_job(key, issue, user, target_repos=target_repos)
            self._emit(user, job)
            self._drop_pending(key)
            drained += 1
            log.info("drain dispatch: %s → user=%s repos=%s",
                     key, user.username, job.target_repos)
        return drained

    def run_forever(self) -> None:
        """poll_interval_sec 간격 루프(백그라운드 스레드 진입점, 예외 격리).

        매 사이클: poll_once(전진 트리거) → drain_pending_resolution(축1 회복 드레인).
        드레인은 쿨다운이 풀렸을 때만 실제 동작하며(그 외 no-op), 결정적 작업
        (스케줄러 tick)은 별도 루프에서 이 쿨다운과 무관하게 계속 돈다(main.py).
        """
        interval = int(getattr(self.config.jira, "poll_interval_sec", 60))
        while not self._stop.is_set():
            try:
                self.poll_once()
            except Exception:  # noqa: BLE001 - 폴러 스레드는 죽지 않아야 한다
                log.exception("poll_once 실패(다음 주기 재시도)")
            try:
                self.drain_pending_resolution()
            except Exception:  # noqa: BLE001 - 드레인 실패도 폴러 스레드를 죽이지 않는다
                log.exception("drain_pending_resolution 실패(다음 주기 재시도)")
            self._stop.wait(interval)

    def stop(self) -> None:
        """루프 정지 신호."""
        self._stop.set()
