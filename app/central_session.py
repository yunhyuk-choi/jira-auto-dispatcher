"""상주 센트럴 라이브 세션 — 프랙탈 P2(센트럴 계층) 신경로.

역할(설계 §3.1·§4·§5·§9 P2):
    센트럴 컨테이너에서, 신규/갱신 Jira 티켓을 **스케줄러 큐(dispatcher.enqueue)** 로
    넣는 대신 **상주 센트럴 라이브 세션 하나**(=ai-dlc-orchestrator 에이전트)에 이벤트로
    **라이브 stdin 주입**한다. 그 세션이 센트럴 operating frame(§3.1)에 따라:

      - 레포락 상태를 **읽어(consult)** 존중하며 무엇을·누구에게 보낼지 **제안**하고
        (상호배제 **강제**는 파이썬 RepoLockScheduler 의 몫 — 설계 §1.5),
      - **사용자별 서브에이전트를 네이티브로 스폰**(이미 돌면 이어위임)해, 그 서브가
        **신뢰 하네스 도구**(``python /app/worker_dispatch.py ...``)를 호출해 워커
        컨테이너 세션을 구동하게 한다. exec/임퍼소네이션 기계장치는 그 파이썬 안에
        **숨는다**(에이전트는 docker exec 를 추론하지 않고 문서화된 도구만 부른다 —
        라이브 컷오버의 인젝션-형태 액션 거부 근본 해소),
      - 워커의 **리치 완료-리포트를 관찰**(도구가 반환한 JSON)해 진짜 완료면
        ``python /app/notify_report.py`` 로 알림 채널에 래핑·발송하고,
      - 모든 사용자 서브가 drain 되면 정상 종료한다(설계 §4).

    운영 규약(신뢰 정본): 센트럴 operating protocol 은 이 레포의 ``CLAUDE.md`` 에 실린
    "central 런타임 세션 운영규약" 섹션이 정본이다. 컨테이너에서 ``COPY . .`` 로 리포
    루트가 /app 에 놓여 ``/app/CLAUDE.md`` 가 되고, cwd(``run.orchestrator_repo`` =
    ``<workspace_dir>/orchestrator``)의 **상위 디렉토리** 이므로 세션 시작 시 신뢰
    문서로 자동 로드된다. 세션 스폰 시 ``--append-system-prompt`` 로는 그 CLAUDE.md
    섹션을 따르라는 **짧은 포인터**만 전달한다(운영 규약 전문을 신뢰되지 않는 채널로
    싣지 않는다 — 정본은 CLAUDE.md). cwd 는 오케스트레이터 프레임워크 레포(정체성
    원천) 그대로 유지한다(변경/편집 금지).

    파이썬으로 남는 최소 기계장치(설계 §6): 세션 기동/종료(좀비 방지 teardown 재사용),
    **라이브 이벤트 주입 핸들**(stdin), stdout→로그(파이프 데드락 방지). 완료 감지·알림 상신
    호출·서브 스폰은 전부 **에이전트**가 프롬프트-구동으로 한다(파이썬 stream 파싱 아님).

역할 소속: **central**(신경로). 기본 OFF 피처 플래그(``run.fractal_central``) 뒤에서만
동작한다 — OFF면 이 모듈은 아예 인스턴스화되지 않고 기존 poller→scheduler→worker 경로가
byte-for-byte 그대로 돈다(app/poller.py 의 dispatcher.enqueue).

⚠️ 재사용 원칙(설계 §6): 지속형 ``build_command``·주입 프리미티브
(``_encode_user_message``/``_write_stdin``)·좀비 teardown(``_terminate_proc``/killpg)·
스트림 파싱 유틸(``parse_stream_event``/``extract_session_id``)은 이미 :mod:`app.agent_runner`
에 있다 — **재발명하지 않고 재사용**한다. P1 의 동기 ``UserSession.run_ticket`` 은
오버로드하지 않는다(설계 결정 A: 형제 클래스).

⚠️ 시크릿 규율: 토큰 값은 로그에 노출하지 않는다. 센트럴 세션은 자기(central) 토큰으로
동작하며, 이벤트 주입 텍스트는 티켓 키/사용자/레포 목록(비밀 아님)만 담는다.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import uuid
from typing import Any, Callable, List, Optional

from app import agent_runner, naming
from app.agent_runner import (
    _close_stdin,
    _encode_user_message,
    _killpg,
    _process_group_id,
    _terminate_proc,
    _write_stdin,
    build_command,
    deterministic_session_id,
    extract_session_id,
    parse_stream_event,
)

log = logging.getLogger("jad.central_session")

# 리더 스레드 조인/드레인 유예(초).
_DRAIN_JOIN_SEC = 5

#: 워커 컨테이너 이름 접두(사용자별) — **기본 인스턴스의 값**(``jad-worker-``).
#:
#: ⚠️ 접두어의 단일 원천은 :func:`app.naming.worker_container_prefix` 이며 인스턴스 이름
#: (``deploy.instance``)에서 파생된다 — spawner 가 그 이름으로 컨테이너를 만들기 때문에
#: 여기서 다르게 지으면 ``docker exec`` 가 **없는 컨테이너**를 때린다. 이 상수는 config 를
#: 손에 쥐지 못한 호출부(대역·옛 시그니처)의 폴백으로만 남는다.
WORKER_CONTAINER_PREFIX = "jad-worker-"

# 검증 가능한 신뢰 하네스(trusted plumbing)의 **안정 절대경로**. 프레임(§3.1·§3.2)이
# 이 경로를 명시하며, 컨테이너에서 `COPY . .`(Dockerfile)로 리포 루트가 /app 에 놓이므로
# 리포 루트의 worker_dispatch.py/notify_report.py 가 각각 이 경로에 존재한다(에이전트가 실재를
# 확인해도 있다 — 라이브 컷오버에서 "cwd 에 없는 기계장치" 거부의 근본 해소).
WORKER_DISPATCH_PATH = "/app/worker_dispatch.py"
NOTIFY_CLI_PATH = "/app/notify_report.py"
# 하위호환 별칭(옛 이름 — Google Chat 전용이던 시절). 리포 루트 ``gchat.py`` 는 얇은 shim
# 으로 남아 있어 구 프롬프트의 ``/app/gchat.py`` 호출도 그대로 동작한다.
GCHAT_PATH = NOTIFY_CLI_PATH
# 관측성(살) 도구 — 프랙탈 잡 라이프사이클을 대시보드에 세만틱하게 기록(track.py). 뼈대가
# 기본 가시성을 보장하므로 이건 부가정보(per-repo 진행·MR·세만틱 이벤트)용이다(관측성 B).
TRACK_PATH = "/app/track.py"

# 스폰 시 --append-system-prompt 로 전달하는 **짧은 포인터**. 운영 규약 전문은 신뢰
# 정본인 /app/CLAUDE.md("central 런타임 세션 운영규약" 섹션, cwd 상위 디렉토리로 자동
# 로드)에 있다 — 여기서는 그 섹션을 따르라는 지시만 준다(신뢰되지 않는 채널에 규약
# 전문을 싣지 않는다). 자기-정당화 메타-코멘트 없음.
CLAUDE_MD_POINTER = (
    "이 세션에서 자동 로드된 /app/CLAUDE.md 의 "
    "\"central 런타임 세션 운영규약 (프랙탈-센트럴)\" 섹션에 따라 이 상주 센트럴 "
    "라이브 세션을 운영하라. 신규/갱신 Jira 티켓은 user 메시지 이벤트로 하나씩 도착한다."
)


def central_fractal_enabled(config: Any) -> bool:
    """센트럴 신경로(상주 센트럴 라이브 세션) 사용 여부 — ``run.fractal_central``(기본 False).

    기본 OFF: 설정에 값이 없거나 falsy 면 신경로를 쓰지 않고 기존 poller→scheduler→worker
    경로가 byte-for-byte 그대로 돈다. 지속(양방향 stream-json) 세션이 꺼져 있으면
    (``persistent_session`` False / 스트림 포맷 불일치) 센트럴 라이브 세션도 성립하지
    않으므로 함께 False 로 본다(worker 게이트 ``session_manager.fractal_enabled`` 와 대칭).
    """
    run = getattr(config, "run", None)
    if run is None:
        return False
    if not bool(getattr(run, "fractal_central", False)):
        return False
    # 라이브 이벤트 주입은 지속(양방향 stream-json) 세션을 전제로 한다 — 아니면 성립 불가.
    return agent_runner.persistent_enabled(config)


def user_session_id(user: str) -> str:
    """한 사용자 워커 세션의 **결정적** session-id(--session-id/--resume 키의 안정성 근거).

    사용자별 서브가 `docker exec … claude -p --session-id/--resume <이 값>` 로 그 사용자
    워커 세션의 대화 맥락을 잇는다. 티켓별이 아니라 **사용자별**로 고정한다(한 사용자
    워커 세션 = 여러 티켓을 순차 턴으로).
    """
    return deterministic_session_id(f"fractal-user:{user}")


def worker_session_id(user: str, ticket: str) -> str:
    """워커 세션의 **결정적** session-id(uuid5) — ``(사용자, 티켓)`` 조합 기준.

    ``claude --session-id``/``--resume`` 은 **UUID 만** 허용한다(임의 문자열, 예
    ``alice-PROJ-548`` 을 넘기면 워커가 ``Invalid session ID`` 로 하드에러 낸다).
    이 함수가 ``(user, ticket)`` 에서 **안정적 UUID** 를 파생해 그 문제를 없앤다:

      - **병렬 티켓끼리 서로 다른 sid**(AC1) — ticket 이 다르면 uuid5 도 다르다.
      - **같은 티켓의 리뷰→추가작업 재개**(AC2) — 같은 ``(user, ticket)`` → 같은 sid 로
        ``--resume`` 이 항상 같은 워커 대화를 잇는다(매번 랜덤이면 재개가 깨진다).

    호출자(에이전트/서브)는 sid 를 만들거나 추적할 필요가 없다 — 티켓만 넘기면 하네스가
    세션을 알아서 잇는다.
    """
    return deterministic_session_id(f"fractal-worker:{user}:{ticket}")


def _is_uuid(value: Any) -> bool:
    """``value`` 가 유효한 UUID 문자열이면 True(claude ``--session-id`` 요건)."""
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        uuid.UUID(value.strip())
    except (ValueError, AttributeError, TypeError):
        return False
    return True


def resolve_worker_session_id(
    session_id: Optional[str], user: str, ticket: str
) -> str:
    """워커 세션에 쓸 **유효 UUID** sid 로 해석(하네스 단일 규칙).

    명시 ``session_id`` 가 이미 유효 UUID 면 그대로 존중한다(호출자가 세션을 핀 고정한
    경우). 미지정이거나 UUID 가 아니면(예: 티켓 기반 문자열) ``(user, ticket)`` 에서
    결정적으로 파생한다(:func:`worker_session_id`). 이로써 ``--session-id``/``--resume``
    가 항상 UUID 요건을 만족하고, 같은 티켓 재개가 항상 같은 sid 로 이어진다(#1 해소).
    """
    if _is_uuid(session_id):
        return session_id.strip()  # type: ignore[union-attr]
    return worker_session_id(user, ticket)


def build_worker_exec_command(
    user: str,
    ticket: str,
    *,
    first: bool,
    config: Any = None,
    session_id: Optional[str] = None,
    instruction: Optional[str] = None,
    container_prefix: Optional[str] = None,
) -> List[str]:
    """워커 세션을 구동/이어주입할 ``docker exec`` 커맨드 구성(순수, 결정적).

    ⚠️ 이 exec/임퍼소네이션 기계장치는 **신뢰 하네스**(:mod:`worker_dispatch`) 안에
    **숨는다** — 프레임(에이전트)은 이 커맨드를 직접 짓지 않고 문서화된 도구
    (``python /app/worker_dispatch.py ...``)를 호출할 뿐이다. 이 빌더는 그 도구가
    내부에서 쓰는 커맨드 형태의 **단일 원천**이자 단위테스트 대상이다.

    - **첫 턴**(``first=True``): ``--session-id <sid>`` 를 **딱 한 번** 쓴다.
    - **이후 턴**(``first=False``): 항상 ``--resume <sid>`` (같은 --session-id 재사용은
      "already in use" 하드에러 → 반드시 --resume 로 이어간다).

    sid 규칙은 :func:`resolve_worker_session_id` 단일 원천을 따른다: 명시 ``session_id``
    가 유효 UUID 면 그대로, 아니면(미지정/티켓기반 문자열) ``(user, ticket)`` 파생 UUID.
    병렬 티켓은 ticket 이 달라 서로 다른 sid 로 동시 세션을 연다(AC1); 같은 티켓 재개는
    같은 sid 로 이어진다(AC2). ``instruction`` 이 주어지면 워커에 줄 지시 본문을, 없으면
    ``ticket`` 을 프롬프트로 쓴다.

    stdout 은 ``--output-format json`` 으로 **단일 최종 result 객체**를 내보낸다 — 하네스
    (:mod:`worker_dispatch`)가 전사(transcript) 성장 레이스 없이 최종 리포트를 결정적으로
    파싱하기 위함이다(#2 해소).
    """
    run = getattr(config, "run", None)
    claude_bin = getattr(run, "claude_bin", "claude") if run else "claude"
    sid = resolve_worker_session_id(session_id, user, ticket)
    # 컨테이너 이름 접두어는 **spawner 가 실제로 지은 이름**과 같아야 한다 —
    # 인스턴스 이름(deploy.instance)에서 파생하는 단일 원천(app/naming.py)을 쓴다.
    # 명시 인자는 존중한다(호출부가 이미 이름을 알고 있는 경우·테스트).
    prefix = (container_prefix if container_prefix is not None
              else naming.worker_container_prefix(config))
    container = f"{prefix}{user}"
    session_flag = ["--session-id", sid] if first else ["--resume", sid]
    prompt = instruction if instruction is not None else ticket
    return [
        "docker", "exec", container, claude_bin, "-p",
        "--output-format", "json", *session_flag, prompt,
    ]


def format_event(job: Any) -> str:
    """센트럴 세션에 주입할 **Jira 이벤트 텍스트**(핵심 지시) 조립(순수).

    ``job`` 은 폴러/웹훅이 만든 Job(ticket/user/target_repos/autonomy_mode 를 담음).
    이벤트는 비밀이 아닌 필드(티켓 키/담당 사용자/레포 목록/autonomy_mode)만 담는다.
    autonomy_mode(A/리뷰 vs B/이어작업)를 실어야 센트럴이 알림 완료-리포트에 모드별
    "다음 동작(로컬 오케스트레이터)" 블록을 쓸 수 있다. 센트럴 프레임(§3.1)이 이 이벤트를
    받아 레포락 consult → 사용자별 서브 스폰/이어위임 → 완료-리포트 수신 시 알림을 한다.
    """
    ticket = str(_field(job, "ticket", "") or "")
    user = str(_field(job, "user", "") or "")
    repos = _field(job, "target_repos", None)
    repos_txt = ", ".join(repos) if isinstance(repos, (list, tuple)) and repos else "(미해석=전역 직렬)"
    # autonomy_mode 를 이벤트에 실어 센트럴이 알림 완료-리포트의 "다음 동작(로컬
    # 오케스트레이터)" 블록을 모드별로 쓸 수 있게 한다(A=리뷰 모드, B=이어작업 모드).
    # 순수 additive — 기존 필드/형식은 그대로 두고 한 줄만 덧붙인다(구 파서 불변).
    mode = str(_field(job, "autonomy_mode", "B") or "B").upper()
    if mode == "A":
        mode_txt = "A(완전자율) — 리뷰 모드: 로컬 오케스트레이터가 MR 을 리뷰·머지하고 티켓을 Done 으로 전이한다."
    else:
        mode_txt = (
            f"{mode}(경량 1차) — 이어작업 모드: 로컬 오케스트레이터가 원격 push 된 브랜치를 "
            "fetch 해 남은 작업을 이어서 진행한다(MR 없음)."
        )
    lines = [
        "# Jira 이벤트 — 신규/갱신 티켓",
        f"- 티켓: {ticket}",
        f"- 담당 사용자: {user}",
        f"- target_repos(힌트/출발점, 최종 확정 아님): {repos_txt}",
        f"- autonomy_mode: {mode_txt}",
        "",
        "이 이벤트를 로드된 /app/CLAUDE.md 의 \"central 런타임 세션 운영규약\" 섹션에 따라 "
        "처리하라: target_repos 는 얇은 리졸버의 힌트이므로 최종본으로 얼리지 말고 티켓 본문을 "
        "읽어 영향 레포 전체 집합을 검증·확장하라(멀티레포면 전 레포가 커버되기 전엔 완료 금지). "
        "레포락 공유 장부를 존중하고, 담당 사용자의 서브에이전트를 네이티브로 스폰(이미 돌면 "
        "이어위임)해 위임하며(서브는 하네스 도구 worker_dispatch.py 를 호출한다), 진짜 완료-리포트를 "
        "받으면 dlc-meta 에 저널·사이클로그를 기록(센트럴=단일 라이터)한 뒤 notify_report.py 도구로 상신한다. "
        "알림 완료-리포트에는 위 autonomy_mode 에 맞는 \"다음 동작(로컬 오케스트레이터)\" 블록을 "
        "포함하라(central_agent_frame §3.3 규약).",
    ]
    return "\n".join(lines)


def _field(job: Any, name: str, default: Any = None) -> Any:
    """job(dict 또는 객체)에서 필드 접근."""
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)


def _session_log_dir(config: Any) -> str:
    """센트럴 세션 stdout 로그를 적재할 디렉토리(retrievable place).

    ``run.workspace_dir`` 하위 ``.jad-sessions`` 를 우선, 없으면 시스템 temp 하위.
    (워커 세션과 같은 규약 — session_manager._session_log_dir 대칭.)
    """
    ws = getattr(getattr(config, "run", None), "workspace_dir", "") or ""
    base = os.path.join(ws, ".jad-sessions") if ws else os.path.join(
        tempfile.gettempdir(), "jad-sessions"
    )
    return base


class CentralSession:
    """상주 센트럴 라이브 세션(스폰-원스·라이브 이벤트 주입·drain-종료).

    설계 결정 A(형제 클래스): P1 :class:`app.session_manager.UserSession` 의 동기
    ``run_ticket``(주입→완료 대기→AgentResult)을 **오버로드하지 않는다**. 센트럴은 이벤트를
    주입하고 **기다리지 않는다** — 완료 관찰·알림은 세션(에이전트)이 프롬프트-구동으로
    비동기 수행한다. 파이썬은 주입 핸들 + stdout 드레인 + teardown 만 담당한다.

    스레드 안전: ``_lock`` 으로 세션 상태를 보호한다. 리더 스레드가 stdout 를 소비(로그
    적재 + session_id 캡처)하고, 폴러/웹훅 스레드(들)가 ``inject_event`` 로 주입한다.
    """

    def __init__(
        self,
        config: Any,
        *,
        popen_factory: Optional[Callable] = None,
        base_env: Optional[dict] = None,
        log_dir: Optional[str] = None,
    ) -> None:
        self.config = config
        self._popen_factory = popen_factory or agent_runner._default_popen
        self._base_env = base_env
        self._log_dir = log_dir or _session_log_dir(config)

        self._lock = threading.RLock()
        self._proc = None
        self._reader: Optional[threading.Thread] = None
        self._reader_done = False
        self._log_fh = None
        self._session_id: Optional[str] = None
        self._spawns = 0   # 스폰 횟수(테스트 관찰용 — 재사용이면 증가하지 않는다).

    # -- 상태 조회 --------------------------------------------------------

    def is_alive(self) -> bool:
        """센트럴 세션 프로세스가 살아 있는지(재사용-if-up 판정 근거)."""
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

    @property
    def session_id(self) -> Optional[str]:
        """관측된 센트럴 세션 id(init 이벤트에서 캡처, 없으면 None)."""
        return self._session_id

    # -- 세션 스폰/주입 ---------------------------------------------------

    def _ensure_session(self) -> None:
        """세션이 없으면 스폰(Popen → 리더 스레드). 있으면 재사용(no-op).

        센트럴 세션은 **자기(central) 정체성**으로 돈다 — 사용자별 git/토큰 주입이 없다
        (그건 각 워커 컨테이너의 몫). 따라서 env 는 base_env(기본 os.environ)를 그대로
        쓴다(CLAUDE_CODE_OAUTH_TOKEN 은 central 환경에 이미 있음).
        """
        if self.is_alive():
            return  # 재사용-if-up: 스폰하지 않는다.

        # 지속형 커맨드(센트럴 고정 session-id). build_command 재사용 — pseudo-ticket 으로
        # 결정적 session-id 를 얻는다(티켓별이 아니라 센트럴 세션 하나).
        session_job = {"ticket": "fractal-central-session"}
        cmd = build_command(session_job, self.config, resume=False)
        # 운영 규약의 신뢰 정본은 /app/CLAUDE.md("central 런타임 세션 운영규약" 섹션)이며,
        # cwd(=<workspace_dir>/orchestrator)의 상위 디렉토리 /app 에서 세션 시작 시 자동
        # 로드된다. --append-system-prompt 로는 그 섹션을 따르라는 **짧은 포인터**만 준다
        # (규약 전문을 신뢰되지 않는 채널로 싣지 않는다 — POINTER 는 자기-정당화 메타 없음).
        cmd = cmd + ["--append-system-prompt", CLAUDE_MD_POINTER]
        env = dict(self._base_env if self._base_env is not None else os.environ)
        cwd = getattr(getattr(self.config, "run", None), "orchestrator_repo", "") or ""

        proc = self._popen_factory(cmd, cwd=cwd, env=env)

        with self._lock:
            self._proc = proc
            self._reader_done = False
            self._session_id = None
            self._spawns += 1
            self._open_log()
            self._reader = threading.Thread(
                target=self._read_loop, name="jad-central-session-reader", daemon=True
            )
            self._reader.start()

    def _open_log(self) -> None:
        """센트럴 세션 stdout 로그파일 오픈(UTF-8·LF). 실패는 무해(로그 없이 진행)."""
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            path = os.path.join(self._log_dir, f"central-session-{ts}.log")
            self._log_fh = open(path, "a", encoding="utf-8", newline="\n")
        except OSError:  # 로그 못 열어도 세션은 진행(파이프는 리더가 계속 drain).
            self._log_fh = None

    def inject_event(self, job: Any) -> bool:
        """Jira 이벤트(job)를 센트럴 세션 stdin 에 **라이브 주입**한다(스폰-원스/재사용-if-up).

        센트럴 운영 규약은 신뢰 정본인 ``/app/CLAUDE.md``("central 런타임 세션 운영규약"
        섹션, cwd 상위로 자동 로드)에 이미 있고, 스폰 시 짧은 포인터만 딸려 있다 — 따라서
        여기서는 **티켓 이벤트만** user 메시지로 주입한다. 완료를 **기다리지 않는다** —
        주입만 하고 즉시 반환한다(완료 관찰·알림은 세션이 비동기 수행). 주입 성공
        여부(bool)를 반환한다.
        """
        self._ensure_session()
        event = format_event(job)
        with self._lock:
            proc = self._proc
        ok = _write_stdin(proc, _encode_user_message(event))
        if not ok:
            # 주입 실패(파이프 깨짐 등) — 세션을 정리(다음 이벤트가 재스폰 유도).
            log.warning("센트럴 세션 stdin 주입 실패 — 세션 정리(다음 이벤트에 재스폰)")
            self._teardown()
        return ok

    # -- 리더 스레드(stdout → 로그 + session_id 캡처) --------------------

    def _read_loop(self) -> None:
        """proc.stdout 를 계속 소비 → 로그파일 적재 + session_id 캡처(파이프 데드락 방지).

        라이브 "파싱해서 완료 판정" 이 아니다(설계 §5) — 완료 감지는 에이전트가 리포트
        파일을 관찰해서 한다. 파이썬 리더는 파이프를 비워 데드락만 막고 진단 로그를 남긴다.
        """
        with self._lock:
            proc = self._proc
        stdout = getattr(proc, "stdout", None)
        try:
            if stdout is not None:
                for raw in stdout:
                    self._on_line(raw)
        except Exception:  # noqa: BLE001 — 리더 실패는 세션 종료로 수렴
            log.warning("센트럴 세션 리더 스레드 예외(격리)")
        finally:
            with self._lock:
                self._reader_done = True

    def _on_line(self, raw: str) -> None:
        """stdout 한 줄 처리 — 로그 적재 + session_id 캡처."""
        if self._log_fh is not None:
            try:
                self._log_fh.write(raw if raw.endswith("\n") else raw + "\n")
                self._log_fh.flush()
            except (OSError, ValueError):
                pass
        event = parse_stream_event(raw)
        if event is None:
            return
        sid = extract_session_id(event)
        if sid:
            with self._lock:
                self._session_id = sid

    # -- drain / teardown -------------------------------------------------

    def drain(self) -> None:
        """센트럴 세션을 정상 종료(drain-종료, 설계 §4). 멱등 — 이미 없으면 no-op."""
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
        # 2) reap 전 pgid 캡처 → wait → 그룹 손자 sweep(좀비 방지, tini 백스톱과 이중).
        pgid = _process_group_id(proc)
        try:
            proc.wait(timeout=_DRAIN_JOIN_SEC)
        except Exception:  # noqa: BLE001 — 유예 초과/대역
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


__all__ = [
    "CentralSession",
    "GCHAT_PATH",          # 하위호환 별칭 — 정본은 NOTIFY_CLI_PATH
    "NOTIFY_CLI_PATH",
    "TRACK_PATH",
    "WORKER_DISPATCH_PATH",
    "build_worker_exec_command",
    "central_fractal_enabled",
    "format_event",
    "resolve_worker_session_id",
    "user_session_id",
    "worker_session_id",
]
