"""사용자당 지속 세션 매니저 — 프랙탈 P1(컨테이너/워커 계층) 신경로.

역할(설계 §3.3·§4·§5·§9 P1):
    워커 컨테이너(=한 사용자, ``DISPATCH_USER``)에서, **티켓마다 claude 프로세스를
    새로 스폰하던 기존 모델**(app/agent_runner.run_job → _consume: 티켓당 소유 후
    프로세스 kill) 대신, **사용자당 지속 세션 하나**를 유지한다:

      - **스폰-원스**: 첫 티켓에서 지속 세션(``build_command`` 지속형)을 한 번 띄우고,
        컨테이너 에이전트 operating frame(§3.3)을 핵심 지시 앞에 **한 번** 주입한다.
      - **재사용-if-up 주입**: 세션이 살아 있으면 새로 띄우지 않고 **같은 세션 핸들**에
        다음 티켓을 stdin 으로 **이어 주입**한다(``_encode_user_message`` 재사용).
      - **stdout → 로그파일**: 백그라운드 리더 스레드가 stdout 를 계속 흘려 로그파일에
        적재(파이프 데드락 방지). 라이브 "파싱해서 kill" 리더가 아니다(설계 §5).
      - **관찰형 완료**: 티켓 완료는 상위가 관찰할 **리치 완료-리포트**로 전파된다
        (설계 §5). P1(센트럴 미구축)에서는 이 매니저가 리포트를 **캡처·로그**해
        상위(P2 센트럴)가 소비할 수 있는 자리에 둔다. 채널 F(진행중/완료) 회신은
        기존 워커 루프가 그대로 담당(비파괴 병행) — 매니저는 티켓당 AgentResult 를
        돌려 그 브리지를 유지한다.
      - **drain-종료**: 더 이상 주입할 잡이 없으면(큐 비면) 세션이 정상 종료된다
        (:meth:`drain`). 유휴 = 프로세스 없음(설계 §4). 좀비 방지는 기존 tini/killpg
        백스톱을 그대로 재사용한다(:func:`app.agent_runner._terminate_proc`).

역할 소속: **worker**(신경로). 기본 OFF 피처 플래그(``run.fractal_worker``) 뒤에서만
동작한다 — OFF면 이 모듈은 아예 인스턴스화되지 않고 기존 per-ticket 경로가 byte-for-byte
그대로 돈다(app/worker.py).

⚠️ 재사용 원칙(설계 §6): 주입 프리미티브(``_encode_user_message``)·지속형
``build_command``·좀비 teardown(``_terminate_proc``/killpg)·스트림 파싱 유틸은 이미
:mod:`app.agent_runner` 에 있다 — **재발명하지 않고 재사용**한다.

⚠️ 시크릿 규율: 토큰 값은 로그/리포트/AgentResult 에 노출하지 않는다. 값은
``build_env`` 로만 읽고, 캡처 텍스트는 :func:`app.agent_runner._redact` 로 마스킹한다.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from typing import Any, Callable, Optional

from app import agent_runner, forge
from app.agent_runner import (
    AgentResult,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_HANDOFF,
    STATUS_INTERRUPTED,
    UserCreds,
    _bg_pending_from_event,
    _close_stdin,
    _encode_user_message,
    _killpg,
    _normalize_control,
    _process_group_id,
    _redact,
    _terminate_proc,
    _write_stdin,
    build_command,
    build_env,
    build_prompt,
    detect_limit_and_reset,
    extract_mr_url,
    extract_session_id,
    parse_stream_event,
)
from app.prompts import compose, container_agent_frame, extract_completion_report

log = logging.getLogger("jad.session_manager")

# 리더 스레드 조인/드레인 유예(초).
_DRAIN_JOIN_SEC = 5

# run_ticket 대기 폴 간격(초) — cancel/timeout/EOF 를 이 간격으로 재검사.
_WAIT_POLL_SEC = 0.5


def fractal_enabled(config: Any) -> bool:
    """프랙탈 신경로(사용자당 지속 세션) 사용 여부 — ``run.fractal_worker``(기본 False).

    기본 OFF: 설정에 값이 없거나 falsy 면 신경로를 쓰지 않고 기존 per-ticket 경로가
    byte-for-byte 그대로 돈다. 지속 세션 자체가 꺼져 있으면(``persistent_session``
    False / 스트림 포맷 불일치) 신경로도 성립하지 않으므로 함께 False 로 본다.
    """
    run = getattr(config, "run", None)
    if run is None:
        return False
    if not bool(getattr(run, "fractal_worker", False)):
        return False
    # 신경로는 지속(양방향 stream-json) 세션을 전제로 한다 — 아니면 성립 불가.
    return agent_runner.persistent_enabled(config)


def _session_log_dir(config: Any) -> str:
    """세션 stdout 로그/리포트를 적재할 디렉토리(retrievable place).

    ``run.workspace_dir`` 하위 ``.jad-sessions`` 를 우선, 없으면 시스템 temp 하위.
    """
    ws = getattr(getattr(config, "run", None), "workspace_dir", "") or ""
    base = os.path.join(ws, ".jad-sessions") if ws else os.path.join(
        tempfile.gettempdir(), "jad-sessions"
    )
    return base


class _Await:
    """run_ticket 한 건의 대기 컨텍스트(리더 스레드가 채우고 run_ticket 가 소비)."""

    __slots__ = ("ticket", "done", "status", "final_text", "mr_url",
                 "reset_at", "report", "session_id")

    def __init__(self, ticket: str) -> None:
        self.ticket = ticket
        self.done = False
        self.status = STATUS_FAILED
        self.final_text = ""
        self.mr_url: Optional[str] = None
        self.reset_at: Optional[str] = None
        self.report: Optional[str] = None
        self.session_id: Optional[str] = None


class UserSession:
    """한 사용자의 지속 claude 세션(스폰-원스·재사용주입·drain-종료·리포트캡처).

    스레드 안전: 세션 상태는 ``_lock``/``_cond`` 로 보호한다. 리더 스레드가 stdout 를
    소비하며 완료-리포트를 캡처하고, run_ticket 스레드(워커 루프)가 주입 후 완료를 대기한다.
    """

    def __init__(
        self,
        config: Any,
        creds: UserCreds,
        *,
        user: str = "",
        popen_factory: Optional[Callable] = None,
        provision_fn: Optional[Callable] = None,
        ensure_repos_fn: Optional[Callable] = None,
        base_env: Optional[dict] = None,
        log_dir: Optional[str] = None,
    ) -> None:
        self.config = config
        self.creds = creds
        self.user = user or getattr(creds, "user", "") or "user"
        self._popen_factory = popen_factory or agent_runner._default_popen
        # 프로비저닝: 기본은 run_job 과 동일한 _provision_repos(세션 스폰 전 1회).
        self._provision_fn = provision_fn if provision_fn is not None else agent_runner._provision_repos
        self._ensure_repos_fn = ensure_repos_fn
        self._base_env = base_env
        self._log_dir = log_dir or _session_log_dir(config)

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._proc = None
        self._reader: Optional[threading.Thread] = None
        self._reader_done = False
        self._log_fh = None
        self._frame_injected = False
        self._session_id: Optional[str] = None
        self._pending_bg: set = set()
        self._secret_values: list = []
        self._await: Optional[_Await] = None
        # 캡처한 티켓 리포트(retrievable place, P2 가 소비). ticket -> report text.
        self.reports: dict = {}
        self._spawns = 0   # 스폰 횟수(테스트 관찰용 — 재사용이면 증가하지 않는다).

    # -- 상태 조회 --------------------------------------------------------

    def is_alive(self) -> bool:
        """세션 프로세스가 살아 있는지(재사용-if-up 판정 근거)."""
        with self._lock:
            proc = self._proc
        if proc is None:
            return False
        poll = getattr(proc, "poll", None)
        if poll is None:
            return True
        try:
            return poll() is None
        except Exception:  # noqa: BLE001 — 대역/이미 종료
            return True

    @property
    def spawn_count(self) -> int:
        """지금까지 실제 프로세스를 새로 띄운 횟수(재사용 검증용)."""
        return self._spawns

    def get_report(self, ticket: str) -> Optional[str]:
        """티켓의 캡처된 리치 완료-리포트(없으면 None)."""
        return self.reports.get(ticket)

    # -- 세션 스폰/주입 ---------------------------------------------------

    def _ensure_session(self, config: Any) -> Optional[AgentResult]:
        """세션이 없으면 스폰(프로비저닝 → Popen → 리더 스레드). 있으면 재사용(no-op).

        치명 프로비저닝 실패(orchestrator_repo 부재)면 그 AgentResult 를 반환한다.
        """
        if self.is_alive():
            return None  # 재사용-if-up: 스폰하지 않는다.

        # 세션 스폰 전 1회 프로비저닝(run_job 과 동일 fresh=True). best-effort.
        if self._provision_fn is not None:
            fatal = self._provision_fn(self.creds, config, self._ensure_repos_fn, fresh=True)
            if fatal is not None:
                return fatal

        # 지속형 커맨드(사용자 고정 session-id). build_command 재사용 — pseudo-ticket 으로
        # 사용자별 결정적 session-id 를 얻는다(티켓별이 아니라 세션별).
        session_job = {"ticket": f"fractal-session:{self.user}"}
        cmd = build_command(session_job, config, resume=False)
        env, secret_values = build_env(session_job, self.creds, config, base_env=self._base_env)
        self._secret_values = secret_values
        cwd = getattr(getattr(config, "run", None), "orchestrator_repo", "") or ""

        proc = self._popen_factory(cmd, cwd=cwd, env=env)

        with self._lock:
            self._proc = proc
            self._frame_injected = False
            self._reader_done = False
            self._pending_bg = set()
            self._session_id = None
            self._spawns += 1
            self._open_log()
            self._reader = threading.Thread(
                target=self._read_loop, name=f"jad-session-reader-{self.user}", daemon=True
            )
            self._reader.start()
        return None

    def _open_log(self) -> None:
        """세션 stdout 로그파일 오픈(UTF-8·LF). 실패는 무해(로그 없이 진행)."""
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            path = os.path.join(self._log_dir, f"session-{self.user}-{ts}.log")
            self._log_fh = open(path, "a", encoding="utf-8", newline="\n")
        except OSError:  # 로그 못 열어도 세션은 진행(파이프는 리더가 계속 drain).
            self._log_fh = None

    def _inject(self, core_instruction: str) -> bool:
        """핵심 지시를 세션 stdin 에 주입. 첫 주입엔 컨테이너 프레임을 앞에 조합한다."""
        with self._lock:
            proc = self._proc
            if not self._frame_injected:
                msg = compose(container_agent_frame(), core_instruction)
                self._frame_injected = True
            else:
                msg = core_instruction
        return _write_stdin(proc, _encode_user_message(msg))

    # -- 리더 스레드(stdout → 로그 + 완료-리포트 캡처) --------------------

    def _read_loop(self) -> None:
        """proc.stdout 를 계속 소비 → 로그파일 적재 + 완료-리포트 관찰(파이프 데드락 방지).

        라이브 "파싱해서 kill" 이 아니다 — 세션은 drain 때만 닫는다. 각 라인을 로그에
        흘리고, 현재 대기 중인 티켓이 있으면 완료(terminal result)를 감지해 대기자를 깨운다.
        """
        proc = self._proc
        stdout = getattr(proc, "stdout", None)
        try:
            if stdout is not None:
                for raw in stdout:
                    self._on_line(raw)
        except Exception:  # noqa: BLE001 — 리더 실패는 세션 종료로 수렴(대기자 깨움)
            log.warning("세션 리더 스레드 예외(격리)")
        finally:
            with self._cond:
                self._reader_done = True
                self._cond.notify_all()

    def _on_line(self, raw: str) -> None:
        """stdout 한 줄 처리 — 로그 적재 + 이벤트 파싱 + 완료 감지."""
        # 1) 로그파일 적재(파이프 데드락 방지의 실제 지점).
        if self._log_fh is not None:
            try:
                self._log_fh.write(raw if raw.endswith("\n") else raw + "\n")
                self._log_fh.flush()
            except (OSError, ValueError):
                pass

        event = parse_stream_event(raw)
        if event is None:
            return

        with self._cond:
            sid = extract_session_id(event)
            if sid:
                self._session_id = sid
            aw = self._await
            # forge 네이티브 변경요청 URL(MR/PR) 우선 추출 — 세션 config 로 종류 판정.
            url = extract_mr_url(event, forge.resolve_kind(self.config))
            if url and aw is not None:
                aw.mr_url = url
            bg = _bg_pending_from_event(event)
            if bg is not None:
                self._pending_bg = bg

            if event.get("type") != "result":
                return

            # --- result 이벤트: 완료(terminal) 판정 ---
            is_lim, reset_at = detect_limit_and_reset(event)
            final = event.get("result") or event.get("error") or ""
            final_text = final.strip() if isinstance(final, str) else ""
            is_err = bool(event.get("is_error")) or str(
                event.get("subtype") or ""
            ).startswith("error")

            terminal = is_lim or is_err or (not self._pending_bg)
            if not terminal or aw is None:
                return  # 백그라운드 대기 중(pending_bg>0)인 result 는 비종결.

            # 대기 중인 티켓 완료 → 캡처하고 대기자를 깨운다(세션은 살려둔다).
            aw.final_text = final_text
            aw.session_id = self._session_id
            if is_lim:
                aw.status = STATUS_INTERRUPTED
                aw.reset_at = reset_at
            elif is_err:
                aw.status = STATUS_FAILED
            else:
                aw.status = STATUS_DONE
            # 리치 완료-리포트 추출(마커 블록 우선, 없으면 최종 멘트 전체).
            extracted = extract_completion_report(final_text)
            aw.report = extracted[1] if extracted else final_text
            aw.done = True
            self._await = None
            self._cond.notify_all()

    # -- 티켓 실행(주입 후 완료 대기) -------------------------------------

    def run_ticket(
        self,
        job: Any,
        creds: UserCreds,
        config: Any,
        *,
        cancel_check: Optional[Callable[[], Any]] = None,
        resume_hint: bool = False,
        **_kw: Any,
    ) -> AgentResult:
        """티켓 1건을 **지속 세션에 주입**하고 완료-리포트를 관찰해 AgentResult 로 환원.

        기존 :func:`app.agent_runner.run_job` 의 시그니처와 호환(워커 ``_process_job`` 이
        ``run(job, creds, config, cancel_check=...)`` 로 호출) — 다만 프로세스를 새로
        띄우지 않고(있으면) 같은 세션에 이어 주입한다. **세션은 완료 후에도 살려둔다**
        (drain 때만 종료). cancel/handoff 신호가 오면 세션을 정리하고 그 상태로 회신한다.
        """
        ticket = str(agent_runner._job_field(job, "ticket", "") or "")

        # 세션 보장(스폰-원스 / 재사용-if-up).
        fatal = self._ensure_session(config)
        if fatal is not None:
            return fatal

        core = build_prompt(job, config)
        if resume_hint:
            core = (
                "이전 턴이 토큰 한도로 중단됐다가 재개됐다. 진행 중이던 작업을 이어서 "
                "완성하라(새로 시작하지 말라).\n\n" + core
            )

        with self._cond:
            aw = _Await(ticket)
            self._await = aw

        injected = self._inject(core)
        if not injected:
            # 주입 실패(파이프 깨짐 등) — 세션을 정리하고 실패 회신.
            with self._cond:
                self._await = None
            self._teardown()
            return AgentResult(
                status=STATUS_FAILED,
                session_id=self._session_id,
                log_summary=_redact("[inject-error] 세션 stdin 주입 실패", self._secret_values),
            )

        return self._wait_for(aw, config, cancel_check=cancel_check, job=job, creds=creds)

    def resume_ticket(
        self,
        job: Any,
        session_id: str,
        creds: UserCreds,
        config: Any,
        *,
        cancel_check: Optional[Callable[[], Any]] = None,
        **_kw: Any,
    ) -> AgentResult:
        """한도(interrupted) 재개 — 살아 있으면 같은 세션에, 아니면 재스폰 후 이어 주입.

        워커 ``_process_job`` 의 ``resume(job, sid, creds, config, cancel_check=...)`` 계약
        호환. session_id 는 세션-관리 모델에선 참고용이며, 결과에 없으면 채워 돌려준다.
        """
        result = self.run_ticket(job, creds, config, cancel_check=cancel_check, resume_hint=True)
        if not result.session_id:
            result.session_id = session_id
        return result

    def _wait_for(
        self,
        aw: _Await,
        config: Any,
        *,
        cancel_check: Optional[Callable[[], Any]],
        job: Any,
        creds: UserCreds,
    ) -> AgentResult:
        """주입한 티켓의 완료를 대기(cancel/EOF/timeout 폴). 세션은 완료 후 살려둔다."""
        max_sec = agent_runner._session_max_sec(config)
        deadline = time.monotonic() + max_sec if max_sec > 0 else None

        while True:
            with self._cond:
                if aw.done:
                    break
                if self._reader_done:
                    # 세션이 완료 리포트 없이 죽음(EOF/행) — 정직하게 FAILED.
                    aw.status = STATUS_FAILED
                    aw.final_text = aw.final_text or "[session-ended] 완료-리포트 없이 세션 종료"
                    aw.session_id = self._session_id
                    break
                self._cond.wait(_WAIT_POLL_SEC)

            # 제어(취소/핸드오프) 폴 — 락 밖에서.
            if cancel_check is not None:
                sig = _normalize_control(cancel_check())
                if sig == "cancel":
                    return self._abort(STATUS_CANCELLED, "[cancelled] 취소 신호로 세션 중단")
                if sig == "handoff":
                    return self._abort(STATUS_HANDOFF, "[handoff] 담당자 변경 신호로 세션 중단")

            if deadline is not None and time.monotonic() > deadline:
                return self._abort(
                    STATUS_FAILED, f"[timeout] 세션 티켓 백스톱 초과({max_sec}s)"
                )

        # 완료(또는 EOF) — 리포트 캡처(retrievable place)하고 AgentResult 환원. 세션 유지.
        report = aw.report or aw.final_text
        if aw.ticket and report:
            self.reports[aw.ticket] = _redact(report, self._secret_values)
            self._write_report(aw.ticket, self.reports[aw.ticket])

        summary = _redact(
            f"[fractal-session] ticket={aw.ticket} status={aw.status}"
            + (f"\n{report}" if report else ""),
            self._secret_values,
        )[-agent_runner._SUMMARY_MAX_CHARS:]

        return AgentResult(
            status=aw.status,
            session_id=aw.session_id or self._session_id,
            mr_url=aw.mr_url,
            reset_at=aw.reset_at,
            log_summary=summary,
            final_text=_redact(aw.final_text, self._secret_values),
        )

    def _abort(self, status: str, note: str) -> AgentResult:
        """취소/핸드오프/타임아웃 — 세션을 정리(좀비 방지)하고 그 상태로 회신."""
        with self._cond:
            sid = self._session_id
            self._await = None
        self._teardown()
        return AgentResult(
            status=status,
            session_id=sid,
            log_summary=_redact(note, self._secret_values),
        )

    def _write_report(self, ticket: str, report: str) -> None:
        """캡처한 티켓 리포트를 파일로 적재(P2 가 소비할 retrievable place). best-effort."""
        try:
            rdir = os.path.join(self._log_dir, "reports")
            os.makedirs(rdir, exist_ok=True)
            safe = ticket.replace("/", "_").replace("\\", "_") or "unknown"
            with open(os.path.join(rdir, f"{safe}.md"), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(report)
        except OSError:
            pass

    # -- drain / teardown -------------------------------------------------

    def drain(self) -> None:
        """더 이상 주입할 잡이 없을 때 세션을 정상 종료(drain-종료, 설계 §4).

        stdin 을 닫아(EOF) 세션이 스스로 끝나게 하고, 남으면 그룹 강제 종료(좀비 방지).
        멱등 — 이미 없으면 no-op.
        """
        self._teardown()

    def _teardown(self) -> None:
        """세션 프로세스/리더/로그 정리(좀비 방지 백스톱 재사용). 멱등."""
        with self._lock:
            proc = self._proc
            reader = self._reader
            log_fh = self._log_fh
            self._proc = None
            self._reader = None
            self._log_fh = None
        if proc is None:
            return

        # 1) EOF 로 정상 종료 유도.
        _close_stdin(proc)
        # 2) reap 전 pgid 캡처(reap 후 getpgid 실패 가능) → wait → 그룹 손자 sweep.
        pgid = _process_group_id(proc)
        try:
            proc.wait(timeout=_DRAIN_JOIN_SEC)
        except Exception:  # noqa: BLE001 — 유예 초과/대역
            # 정상 EOF 로 안 죽으면 그룹 강제 종료(좀비 방지, tini 백스톱과 이중).
            _terminate_proc(proc)
        if pgid is not None:
            _killpg(pgid, agent_runner._SIGKILL)

        if reader is not None and reader is not threading.current_thread():
            try:
                reader.join(timeout=_DRAIN_JOIN_SEC)
            except Exception:  # noqa: BLE001
                pass
        if log_fh is not None:
            try:
                log_fh.close()
            except (OSError, ValueError):
                pass


__all__ = ["UserSession", "fractal_enabled"]
