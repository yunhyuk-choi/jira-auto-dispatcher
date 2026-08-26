"""central 부팅 자가진단 — doctor 를 **컨테이너 자신이** 돌린다(중앙 전용).

왜 이게 필요한가:
    :mod:`app.setup_doctor` 는 보통 ``docker compose up`` **전에 호스트에서** 돈다. 그런데
    설정 값의 상당수는 **컨테이너 관점**이다(``deploy.secrets_base_dir: /run/secrets``,
    ``deploy.docker_host: tcp://socket-proxy:2375`` — compose 네트워크 안에서만 해석되는
    이름). 호스트에서는 그것들이 전부 ``SKIP`` 으로 나오고, 완전한 판정을 보려면 사람이
    기동 후에 ``docker compose exec central python -m app.setup doctor`` 를 **한 번 더**
    돌려야 했다. central 은 어차피 그 컨테이너 안에서 뜬다 — 거기서 스스로 돌리면 두
    번째 실행이 필요 없다.

설계 제약(둘 다 지킨다):
    - **기동을 막지 않는다.** 진단이 실패했다고 부팅을 거부하면, 설정을 고칠 관리 UI 도
      같이 안 뜨는 자충수가 된다. 이 모듈은 결과를 **알릴 뿐** 부팅에 관여하지 않는다.
    - **부팅을 느리게 하지 않는다.** 네트워크 검사가 여럿이고(HTTP 15초 · git 20초 ·
      docker ping) 합쳐서 수십 초가 될 수 있다. 그래서 데몬 스레드에서 돌고, 결과는
      캐시된다 — ``/api/doctor`` 는 매 요청마다 재실행하지 않는다. 수동 재실행 경로는
      :meth:`DoctorRuntime.refresh` (엔드포인트 ``POST /api/doctor/refresh``)다.

**worker 에서는 돌지 않는다** — 이 모듈은 central 조립 경로에서만 만들어진다
(:func:`app.main.build_central_components`). worker 는 config 를 env 로 받고 Jira·forge 를
직접 보지 않으므로 여기서 검사할 것이 없다.

온보딩 게이트:
    :data:`BLOCKING_CHECKS` 의 검사가 ``FAIL`` 이면 :mod:`app.onboarding` 이 신규 사용자
    등록(``POST /onboard``)을 거부한다. 잘못된 설정으로 워커를 띄우면 **조용히 실패하는
    잡만 쌓이기** 때문이다. 고르는 기준은 그 상수의 주석 참조.

운영 중 실측 반영:
    진단은 부팅 때 한 번 돈다. 그런데 **토큰은 운영 중에 만료·회수된다** — 그때부터
    폴러는 아무 티켓도 못 읽으면서 "할 일 없음"처럼 조용히 돈다(Jira Cloud 는 자격이
    틀려도 JQL 검색에 200 + 빈 배열을 준다). 폴러가 그것을 감지하면
    :meth:`DoctorRuntime.note_jira_auth` 로 여기 실어, 부팅 진단이 아니라 **지금 사실**이
    ``/api/doctor`` 와 관리 UI 배너에 뜨게 한다.

시크릿 규율:
    :class:`app.setup_doctor.CheckResult` 는 이미 값이 아니라 존재·응답코드·마스킹된
    출력만 담는다. 이 모듈은 그것을 **그대로** 실어 나르며 아무 것도 덧붙이지 않는다 —
    관리 UI 에는 인증이 없으므로(SECURITY.md) 여기서 값이 새면 그대로 유출이다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

from app import setup_doctor

log = logging.getLogger("jad.doctor")

#: ``FAIL`` 이면 **사용자 온보딩을 막는** 검사들.
#:
#: 고르는 기준은 하나다 — *이 상태로 워커를 띄우면 잡이 **조용히** 실패하는가?*
#:     - ``config``          동의 미승인 · 남의 조직 예시 자리표시자(= 이 배포의 값이 아니다)
#:     - ``secrets``         서비스 토큰 부재 → 폴러·dlc-meta 가 인증 없이 돈다
#:     - ``jira_auth``       401 → 아무 티켓도 못 읽는다
#:     - ``jira_search``     프로젝트 키 오류 → 조용히 아무 일도 안 한다
#:     - ``docker``          워커를 아예 띄울 수 없다
#:
#: 일부러 **빼는** 것들(막지 않고 UI 경고로만 보여 준다):
#:     - ``forge_token``·``dlc_meta`` — central 자신의 git 경로다. 깨지면 사이클로그 push·
#:       REPO-MAP 갱신이 degrade 되지만 잡은 돌고 MR/PR 도 나온다. 그리고 사설망/방화벽
#:       조합에서 오탐이 나기 쉬운 자리라, 여기서 막으면 정상 배포가 설치 불가가 된다.
#:     - ``notifier`` — 애초에 FAIL 이 아니라 WARN/SKIP 으로만 난다.
#:
#: ⚠️ 목록에 **관리 UI 로 고칠 수 있는 것은 없다** — 전부 config.yaml·시크릿 파일·호스트
#: 환경이라 UI 밖에서 고친다. 그래서 "고칠 방법이 UI 뿐인데 UI 를 막는" 자충수가 아니다.
BLOCKING_CHECKS: tuple = (
    "config", "secrets", "jira_auth", "jira_search", "docker",
)

#: 진단 상태.
STATE_PENDING = "pending"   # 아직 한 번도 돌지 않았다
STATE_RUNNING = "running"   # 지금 돌고 있다(결과가 있으면 그건 직전 회차의 것)
STATE_READY = "ready"       # 결과가 있다


class DoctorRuntime:
    """doctor 실행기 + 결과 캐시(스레드 안전).

    한 번에 한 회차만 돈다 — 중복 요청은 조용히 무시하고 현재 스냅샷을 돌려준다
    (관리 UI 가 5초마다 폴링하므로 여기서 막지 않으면 진단이 겹쳐 쌓인다).
    """

    def __init__(self, cfg: Any, *, config_path: str = "", project_dir: str = ".",
                 run_checks: Optional[Callable] = None) -> None:
        """Args:
            cfg: 로드된 :class:`app.config.AppConfig`.
            config_path: 원본 config.yaml 경로(자리표시자 검사에 쓴다).
            project_dir: 이 배포 디렉토리(호스트 폴백 경로 계산 기준).
            run_checks: 검사 실행기 주입(테스트·CI 는 여기로 대역을 준다 —
                네트워크·docker 에 절대 붙지 않는다).
        """
        self._cfg = cfg
        self._config_path = config_path
        self._project_dir = project_dir or "."
        self._run_checks = run_checks if run_checks is not None else setup_doctor.run_checks
        self._lock = threading.Lock()
        self._results: Optional[list] = None
        #: :attr:`_results` 가 **실제로 갱신된** 시각. ``_finished_at`` 과 구분한다 —
        #: 진단이 예외로 끝나도 ``_finished_at`` 은 전진하는데, 그걸 기준으로 삼으면
        #: *터진 회차*가 폴러의 관측을 밀어내 버린다(새 사실은 아무것도 없었는데).
        self._results_at: float = 0.0
        #: 운영 중 **실측된** 검사 결과 덮어쓰기 ``{이름: (CheckResult, 기록 시각)}``.
        #: 폴러처럼 상시 도는 축이 진단보다 **더 최근** 사실을 알게 되는 자리가 있다
        #: (:meth:`note_jira_auth`). 시각을 함께 들고 있다가 마지막 회차보다 새로운
        #: 것만 덮어쓴다 — 그래야 사람이 설정을 고치고 '다시 진단'을 눌렀을 때 낡은
        #: 런타임 관측이 그 결과를 가리지 않는다(그리고 그 반대도 성립한다).
        self._runtime: dict = {}
        self._running = False
        self._started_at: Optional[float] = None
        self._finished_at: Optional[float] = None
        self._duration_sec: Optional[float] = None
        self._error = ""

    # -- 실행 -------------------------------------------------------------

    def run_once(self) -> list:
        """검사를 **동기로** 한 회차 돌리고 결과를 캐시한다(중복 실행은 no-op).

        Returns:
            이번 회차의 :class:`app.setup_doctor.CheckResult` 목록. 이미 다른 스레드가
            돌고 있었으면 빈 목록(그쪽이 캐시를 채운다).
        """
        with self._lock:
            if self._running:
                return []
            self._running = True
            self._started_at = time.time()
            self._error = ""
        results: list = []
        try:
            results = list(self._run_checks(
                self._cfg, config_path=self._config_path, project_dir=self._project_dir))
        except Exception as exc:  # noqa: BLE001 — 진단 사고가 central 을 죽이면 안 된다
            log.exception("부팅 자가진단 실행 실패(격리)")
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return []
        finally:
            with self._lock:
                self._running = False
                self._finished_at = time.time()
                if self._started_at is not None:
                    self._duration_sec = self._finished_at - self._started_at
                if results:
                    self._results = results
                    self._results_at = self._finished_at or time.time()
        self._log(results)
        return results

    def start(self) -> bool:
        """검사를 **백그라운드 데몬 스레드**로 돌린다(부팅을 막지도 늦추지도 않는다).

        Returns:
            스레드를 띄웠으면 True, 이미 돌고 있어 건너뛰었으면 False.
        """
        with self._lock:
            if self._running:
                return False
        threading.Thread(target=self.run_once, name="jad-doctor", daemon=True).start()
        return True

    #: 수동 재실행 — ``start`` 와 같은 동작이되 의도를 이름으로 드러낸다(엔드포인트용).
    refresh = start

    # -- 조회 -------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    def snapshot(self) -> dict:
        """현재 캐시된 진단 결과(기계가 읽는 형태 — ``/api/doctor`` 응답 본문).

        ⚠️ 재실행하지 않는다. 매 요청마다 돌리면 관리 UI 폴링이 그대로 진단 폭주가 된다.
        """
        results = self._effective_results()
        with self._lock:
            running = self._running
            started_at = self._started_at
            finished_at = self._finished_at
            duration = self._duration_sec
            error = self._error
        if results:
            state = STATE_RUNNING if running else STATE_READY
        else:
            state = STATE_RUNNING if running else STATE_PENDING
        payload = setup_doctor.results_to_dict(results) if results else {
            "ok": None, "checks": [], "counts": {},
        }
        blocking = _blocking_failures(results)
        payload.update({
            "state": state,
            "running": running,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_sec": round(duration, 3) if duration is not None else None,
            "age_sec": round(time.time() - finished_at, 1) if finished_at else None,
            "blocking": blocking,
            "onboarding_blocked": bool(blocking),
            "blocking_checks": list(BLOCKING_CHECKS),
            "error": error,
        })
        return payload

    def blocking_failures(self) -> list:
        """온보딩을 막아야 하는 FAIL 검사 이름들(아직 안 돌았으면 빈 목록).

        ⚠️ **"모르면 막지 않는다."** 부팅 직후 진단이 끝나기 전에는 결과가 없고, 그때
        온보딩을 막으면 진단이 느린 환경에서 관리 UI 가 이유 없이 잠긴다.
        """
        return _blocking_failures(self._effective_results())

    # -- 운영 중 실측 반영 -------------------------------------------------

    def note_jira_auth(self, ok: bool, detail: str = "") -> None:
        """폴러가 **폴링 중 실측한** Jira 자격 상태를 ``jira_auth`` 검사에 반영한다.

        왜 여기로 오는가:
            자격이 만료·회수돼도 JQL 검색은 200 + 빈 배열이라(모듈 :mod:`app.poller`
            축0 참조) 시스템은 겉보기에 "할 일 없음"으로 조용히 돈다. 폴러가 그것을
            감지해도 알릴 곳이 로그뿐이면 아무도 안 본다. ``/api/doctor`` 는 관리 UI 가
            이미 5초마다 읽는 자리라, 여기 실으면 그대로 배너가 된다.

        온보딩 게이트에 미치는 영향(의도된 것):
            ``jira_auth`` 는 :data:`BLOCKING_CHECKS` 다. 폴링 중 자격이 깨지면 신규
            사용자 온보딩(``POST /onboard``)이 409 로 막힌다 — 티켓을 하나도 못 읽는
            상태에서 워커를 새로 띄워 봐야 조용히 노는 컨테이너만 는다. 자격이
            회복되면 폴러가 곧바로 PASS 를 기록해 게이트도 풀린다.

        ⚠️ ``detail`` 에는 상태코드·예외 타입만 온다(폴러가 응답 본문을 싣지 않는다) —
        진단 응답은 인증 없이 읽히므로 여기로 값이 새면 그대로 유출이다.
        """
        if ok:
            result = setup_doctor.CheckResult(
                "jira_auth", setup_doctor.STATUS_PASS,
                f"폴링 중 자격 확인됨({detail})" if detail else "폴링 중 자격 확인됨")
        else:
            result = setup_doctor.CheckResult(
                "jira_auth", setup_doctor.STATUS_FAIL,
                f"폴링 중 자격 거부 감지({detail or '사유 불명'}) — 검색은 200 + 빈 "
                f"결과를 돌려주므로 '할 일 없음'처럼 보이지만 티켓을 하나도 읽지 "
                f"못하고 있습니다",
                "jira.watcher_token_file 이 가리키는 토큰을 재발급하고 "
                "jira.watcher_email 과 짝이 맞는지 확인한 뒤 '다시 진단' 하세요.")
        self.note_runtime_check(result)

    def note_runtime_check(self, result: Any) -> None:
        """운영 중 실측된 :class:`app.setup_doctor.CheckResult` 하나를 덮어쓴다.

        진단 회차 전체를 다시 돌리지 않고 **한 검사만** 최신 사실로 갈아 끼운다.
        마지막 회차보다 나중에 기록된 것만 효력을 갖는다(:meth:`_effective_results`).
        """
        with self._lock:
            self._runtime[result.name] = (result, time.time())

    def _effective_results(self) -> list:
        """캐시된 회차 결과 + (그보다 새로운) 런타임 관측을 합친 목록.

        런타임 관측이 **회차보다 오래됐으면 버린다** — 사람이 설정을 고치고 '다시 진단'
        을 눌렀는데 낡은 관측이 결과를 덮으면 고쳤다는 사실이 화면에 영영 안 뜬다.
        반대로 회차가 끝난 뒤 폴러가 감지한 실패는 회차 결과를 이긴다(그쪽이 최신이다).

        기준은 ``_finished_at`` 이 아니라 :attr:`_results_at` 이다 — 진단이 **터져서**
        아무 결과도 못 남긴 회차는 새 사실을 가져오지 않았으므로 폴러의 관측을 밀어낼
        자격이 없다.
        """
        with self._lock:
            results = list(self._results or [])
            runtime = dict(self._runtime)
            floor = self._results_at
        fresh = {name: res for name, (res, ts) in runtime.items() if ts >= floor}
        if not fresh:
            return results
        merged = [fresh.pop(r.name, r) for r in results]
        # 아직 한 회차도 안 돈 상태에서 들어온 관측도 버리지 않는다(그것만이 아는 사실이다).
        merged.extend(fresh.values())
        return merged

    # -- 내부 -------------------------------------------------------------

    def _log(self, results: list) -> None:
        """결과를 로그로 남긴다(FAIL 은 힌트까지 — 로그만 보고 고칠 수 있어야 한다)."""
        counts = {s: sum(1 for r in results if r.status == s)
                  for s in (setup_doctor.STATUS_PASS, setup_doctor.STATUS_FAIL,
                            setup_doctor.STATUS_WARN, setup_doctor.STATUS_SKIP)}
        blocking = _blocking_failures(results)
        log.info("부팅 자가진단 완료 — 통과 %d · 실패 %d · 경고 %d · 건너뜀 %d (%.1fs)",
                 counts[setup_doctor.STATUS_PASS], counts[setup_doctor.STATUS_FAIL],
                 counts[setup_doctor.STATUS_WARN], counts[setup_doctor.STATUS_SKIP],
                 self._duration_sec or 0.0)
        for r in results:
            if r.status == setup_doctor.STATUS_FAIL:
                log.error("자가진단 FAIL [%s] %s%s", r.name, r.message,
                          f" ↳ {r.hint}" if r.hint else "")
            elif r.status == setup_doctor.STATUS_WARN:
                log.warning("자가진단 WARN [%s] %s", r.name, r.message)
        if blocking:
            log.error(
                "⚠️ 위 실패 때문에 사용자 온보딩(POST /onboard)을 차단합니다: %s — "
                "잘못된 설정으로 워커를 띄우면 조용히 실패하는 잡만 쌓입니다. "
                "고친 뒤 관리 UI 의 '다시 진단' 을 누르거나 POST /api/doctor/refresh 하세요.",
                ", ".join(blocking))


def _blocking_failures(results: list) -> list:
    """결과 목록에서 온보딩을 막는 FAIL 이름만 추린다(:data:`BLOCKING_CHECKS` 순서)."""
    failed = {r.name for r in results if r.status == setup_doctor.STATUS_FAIL}
    return [name for name in BLOCKING_CHECKS if name in failed]
