#!/usr/bin/env python3
"""notify_report.py — 완료-리포트를 **설정된 알림 채널**로 래핑·발송(프랙탈 P2, 설계 §3.1·§6.4).

센트럴 에이전트(상주 세션)가 사용자 서브로부터 **리치 완료-리포트**를 수신하면 이 도구를
호출해 그 리포트를 팀 채널 웹훅으로 발송한다. 완료 감지는 파이썬 stream 파싱이 아니라
**에이전트의 관찰**로 이뤄지고(설계 §5), 알림 메시지 품질 = **에이전트 리포트 품질**이다
(파이썬 템플릿이 아니다) — 그래서 이 도구는 리포트 본문을 **거의 그대로** 싣고 짧은
헤더(티켓 키)만 덧붙인다.

채널 중립(프레임워크화): 어디로·어떤 모양으로 보낼지는 ``config.notifier.provider``
(``none``|``google_chat``|``slack``|``generic_webhook``)가 정하며 이 도구는 그 판단을 하지
않는다 — provider 어댑터·웹훅 조회·POST 는 :func:`app.notify.send_text` 를 그대로 재사용
한다(재발명 금지, 설계 §6.4). best-effort — 발송 실패가 상위(센트럴)를 죽이면 안 된다.

⚠️ 이름: 이 파일은 예전에 ``gchat.py``(Google Chat 전용)였다. 리포 루트 ``gchat.py`` 는
얇은 **하위호환 별칭**으로 남아 있어 기존 프롬프트·문서의 ``python /app/gchat.py`` 호출도
그대로 동작한다(새 호출은 ``notify_report.py`` 를 쓴다).

사용(센트럴 프레임이 호출):
    python notify_report.py --ticket HAN-1 --report-file <path>   # 파일에서 리포트 읽기
    cat report.md | python notify_report.py --ticket HAN-1        # stdin 에서 리포트 읽기
    python notify_report.py --ticket HAN-1 --report "<본문>"      # 인자로 직접

종료코드: 발송 성공 0, 미발송(비활성/알 수 없는 provider/웹훅 미설정/실패) 1.
**티켓당 멱등**: 이미 상신한 티켓을 다시 부르면 재전송하지 않고 그 사실을 stdout 에 알린 뒤
성공(0)으로 반환한다(같은 완료-리포트가 채널에 중복 게시되는 것을 결정적으로 차단 —
sent-marker 는 jad-state 볼륨 ``/app/state/notify-sent/<ticket>`` 에 영속. 옛 배포의
``gchat-sent/`` 마커도 계속 읽는다 — 업그레이드 중 재전송 방지). 의도적 재전송은 ``--force``.
(⚠️ 웹훅 URL·토큰은 로그·에러에 절대 남기지 않는다 — notify 규율 계승.)

POLICY-ENCODING: 입출력 텍스트는 UTF-8 로 다룬다.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Any, Optional

from app import notify

log = logging.getLogger("jad.notify_report")


def build_report_text(report: str, *, ticket: Optional[str] = None) -> str:
    """발송 텍스트 조립(순수) — 티켓 헤더 + 리포트 본문(거의 그대로).

    리포트 본문이 곧 메시지다(에이전트 리포트 품질 = 메시지 품질). 티켓 키가 있으면
    상단에 한 줄 헤더로 덧붙여 상관(correlate)을 돕는다. 채널 문법을 쓰지 않는
    **평문**이라 provider 가 무엇이든 그대로 통한다.
    """
    body = (report or "").strip()
    t = (ticket or "").strip()
    if t:
        return f"[{t}] 오케스트레이터 완료-리포트\n{body}" if body else f"[{t}] 오케스트레이터 완료-리포트"
    return body


#: 하위호환 별칭 — 옛 이름으로 부르는 호출자(스크립트·테스트)를 위해 남긴다.
build_gchat_text = build_report_text


def send_report(
    config: Any,
    report: str,
    *,
    ticket: Optional[str] = None,
    http: Any = None,
    http_factory: Optional[Any] = None,
) -> bool:
    """완료-리포트를 **설정된 채널**로 발송(best-effort → 발송 여부 bool).

    :func:`app.notify.send_text` 한 관문을 지난다 — provider 분기(페이로드 모양)·웹훅
    조회(webhook_ref → secrets.base_dir 상대)·POST 를 거기서 재사용한다. ``provider`` 가
    ``none`` 이거나 **알 수 없는 값**이거나 웹훅 참조가 비었으면 조용히 False(알 수 없는
    provider 는 notify 가 경고 로그를 남기고 거부한다 — 오발송보다 미발송이 낫다).
    어떤 예외도 삼킨다(발송 실패가 센트럴을 죽이지 않는다). 웹훅 URL·토큰은 미로깅.
    """
    try:
        if notify.resolve_provider(config) == notify.PROVIDER_NONE:
            log.warning("notify_report: 알림 비활성(notifier.provider=none) — 발송 생략")
            return False
        if not (report or "").strip():
            log.warning("notify_report: 빈 리포트 — 발송 생략")
            return False
        text = build_report_text(report, ticket=ticket)
        return notify.send_text(
            config, text,
            ticket=str(ticket or ""),
            status="done",          # 이 도구는 완료 게이트에서만 불린다.
            event="report",
            http=http, http_factory=http_factory,
        )
    except Exception:  # noqa: BLE001 — best-effort: 발송 실패는 센트럴에 영향 주지 않는다
        # 예외 메시지에 웹훅 URL 이 섞일 수 있어 트레이스백을 남기지 않는다.
        log.warning("notify_report: 완료 리포트 발송 실패(무시)")
        return False


# ---------------------------------------------------------------------------
# 티켓당 멱등(dedup) — per-ticket sent-marker (영속: jad-state 볼륨 /app/state)
# ---------------------------------------------------------------------------
#
# 문제(지상검증): 센트럴 에이전트가 한 티켓에 이 도구를 여러 번(1차 전송 + 확인용 재전송)
# 부르면 매 호출이 새 메시지를 POST 해 같은 완료-리포트가 채널에 2~3개 뜬다. 프레임
# (central_agent_frame.md §3)이 "전 대상 레포 완료 게이트에서 정확히 1회, 확인용 재전송
# 금지"를 1차 방어로 지시하지만, 여기 도구가 **결정적 백스톱**이다 — 티켓당 sent-marker
# 를 두어 이미 보낸 티켓이면 조용히 skip(성공 exit 0)한다. --force 로 의도적 재전송 허용.

#: 마커 디렉토리(상태 볼륨 하위). provider 중립 이름.
NOTIFY_SENT_SUBDIR = "notify-sent"

#: 옛 이름(Google Chat 전용 시절). **읽기만** 한다 — 업그레이드 직후 이미 상신된 티켓이
#: 마커를 잃고 재전송되는 것을 막는 하위호환 경로다(새 마커는 위 신규 이름으로 쓴다).
LEGACY_SENT_SUBDIR = "gchat-sent"

#: 하위호환 별칭(옛 상수 이름을 참조하는 호출자용).
GCHAT_SENT_SUBDIR = LEGACY_SENT_SUBDIR


# ---------------------------------------------------------------------------
# ROLE 가드 — 워커는 통지자가 아니다(센트럴이 유일 통지자, 역할 격리 백스톱)
# ---------------------------------------------------------------------------
#
# 문제(지상검증): 센트럴(jad-central)과 워커(jad-worker-<user>)가 **둘 다** 이 도구를
# 호출해 같은 티켓의 완료-알림이 2개 뜬다. 컨테이너별 per-ticket 멱등 마커는 볼륨이
# 분리돼(센트럴/워커 별도 /app/state) 공유되지 않아 각자 1개씩 나간다. fractal 설계상
# **알림 상신은 센트럴만** 한다 — 워커는 순수 실행자(doer)로 완료를 리포트 반환으로만
# 알린다(알림 상신 아님). 프레임 규율(1차 방어)이 새더라도 워커 컨테이너에선 결코 안
# 나가도록, worker_dispatch 의 _worker_role_refusal 과 같은 취지의 **결정적 백스톱**을
# 여기 둔다: ROLE=worker 컨텍스트면 상신하지 않고 skip(로그+exit 0). ROLE 이 worker 가
# 아니면(central·미설정/테스트) None → 기존대로 정상 상신(멱등·--force 경로 불변).


def _is_worker_role(env: Optional[dict] = None) -> bool:
    """이 컨텍스트가 워커(``ROLE=worker``)인지 — 워커면 알림 통지 금지 대상이다.

    스포너가 워커 컨테이너에 ``ROLE=worker`` + ``DISPATCH_USER`` 를 주입한다
    (``app/spawner.py`` 확인). 판정은 ``ROLE`` env 만으로 결정적으로 한다(``DISPATCH_USER``
    는 참고 신호로 두지 않는다 — 오탐 방지). worker 가 아니면 False → 정상 상신.
    """
    e = env if env is not None else os.environ
    return (e.get("ROLE") or "").strip().lower() == "worker"


def _resolve_state_dir() -> str:
    """마커를 둘 상태 디렉토리(컨테이너 재생성에도 유지되는 jad-state 볼륨).

    센트럴 에이전트는 cwd=<workspace_dir>/orchestrator 에서 ``python /app/notify_report.py`` 를
    부르므로 상대 "state" 는 볼륨 마운트(/app/state)를 빗나간다. 그래서 결정적으로 앵커한다:
      1) env ``JAD_STATE_DIR`` 이 있으면 그것(배포 오버라이드/테스트 격리).
      2) 없으면 :mod:`app.state` 의 상태 디렉토리(테스트가 set_state_dir 로 절대경로 지정
         가능). 그 값이 상대경로면 컨테이너 WORKDIR(/app)에 앵커 → ``/app/state``
         (= docker-compose 의 jad-state 볼륨 마운트 지점, 영속).
    """
    d = os.environ.get("JAD_STATE_DIR")
    if d:
        return d
    base = "state"
    try:
        from app import state
        base = state.get_state_dir() or "state"
    except Exception:  # noqa: BLE001 — app.state 임포트 불가해도 기본값으로 진행
        base = "state"
    if not os.path.isabs(base):
        base = os.path.join("/app", base)
    return base


def _safe_ticket_key(ticket: str) -> str:
    """티켓 키를 파일명 안전 형태로(경로 분리자/트래버설 차단).

    티켓 키는 ``PROJ-1``·``ABC0001234-552`` 류라 영숫자·``-``·``_`` 만 허용하고 그 밖(경로
    분리자 ``/``·``\\``, 점 ``.`` 포함)은 ``_`` 로 치환한다. ``.`` 까지 막아 ``..`` 같은
    순수 트래버설 컴포넌트가 마커 경로에 끼는 것을 원천 차단한다.
    """
    key = (ticket or "").strip()
    return "".join(c if (c.isalnum() or c in "-_") else "_" for c in key)


def marker_path(ticket: str, *, state_dir: Optional[str] = None) -> str:
    """티켓의 sent-marker 절대경로(``<state>/notify-sent/<sanitized-ticket>``) — 쓰기 경로."""
    base = state_dir if state_dir is not None else _resolve_state_dir()
    return os.path.join(base, NOTIFY_SENT_SUBDIR, _safe_ticket_key(ticket))


def legacy_marker_path(ticket: str, *, state_dir: Optional[str] = None) -> str:
    """옛 배포가 남긴 마커 경로(``<state>/gchat-sent/<...>``) — **읽기 전용** 하위호환."""
    base = state_dir if state_dir is not None else _resolve_state_dir()
    return os.path.join(base, LEGACY_SENT_SUBDIR, _safe_ticket_key(ticket))


def already_sent(ticket: str, *, state_dir: Optional[str] = None) -> bool:
    """이 티켓의 완료-리포트가 이미 상신됐는지(마커 존재).

    신규 경로와 **옛 경로 둘 다** 본다 — 이름을 바꾼 버전으로 업그레이드한 직후에도
    이미 상신된 티켓이 재전송되지 않는다.
    """
    if not (ticket or "").strip():
        return False
    return (os.path.exists(marker_path(ticket, state_dir=state_dir))
            or os.path.exists(legacy_marker_path(ticket, state_dir=state_dir)))


def claim_marker(ticket: str, *, state_dir: Optional[str] = None) -> bool:
    """전송 직전 티켓을 **원자적으로 선점**한다(``O_CREAT|O_EXCL``).

    성공(True)하면 이 프로세스가 유일 발송자다. 이미 있으면 False(과거 전송/동시 호출이
    선점) → 호출자는 발송을 건너뛴다. 동시호출 경합에서도 O_EXCL 로 정확히 하나만 True 를
    받아 "한 번만 나간다". 발송이 실패하면 :func:`release_marker` 로 되돌린다(정당한 재시도
    가 막히지 않게 — 마커는 "성공적으로 나갔다"의 뜻을 유지). 티켓 키가 없으면 dedup 불가
    이므로 항상 True(발송 허용).
    """
    if not (ticket or "").strip():
        return True
    path = marker_path(ticket, state_dir=state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    try:
        os.write(fd, (_safe_ticket_key(ticket) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return True


def release_marker(ticket: str, *, state_dir: Optional[str] = None) -> None:
    """선점 마커 제거(발송 실패 롤백). 없으면 조용히 무시."""
    if not (ticket or "").strip():
        return
    try:
        os.remove(marker_path(ticket, state_dir=state_dir))
    except OSError:
        pass


def _mark_done(ticket: Optional[str]) -> None:
    """완료 게이트 통과(알림 상신)를 jobs.json 에 **done** 으로 기록(관측성 A.3).

    프랙탈 경로의 단일 완료 게이트다 — 여기서 티켓을 done 으로 마킹해 대시보드에 완료로
    뜨게 한다. 크로스프로세스 안전(:func:`app.state.record_job_event`, 내부 flock). 취소/
    이관 계열 상태는 record_job_event 의 protect 규칙이 덮어쓰지 않는다. best-effort —
    기록 실패가 상신(도구의 본 임무)을 막지 않는다.
    """
    t = (ticket or "").strip()
    if not t:
        return
    try:
        from app import state

        state.set_state_dir(state.resolve_runtime_state_dir())
        state.record_job_event(t, status="done", create_if_missing=True,
                               defaults={"status": "queued"},
                               log_summary="완료-리포트 상신(알림)")
    except Exception:  # noqa: BLE001 — 관측성 기록 실패는 상신을 막지 않는다(best-effort)
        log.warning("notify_report: 잡 done 기록 실패(무시)")


def _read_report(args: argparse.Namespace) -> str:
    """리포트 본문을 인자/파일/stdin 순으로 읽는다(UTF-8)."""
    if args.report is not None:
        return args.report
    if args.report_file:
        with open(args.report_file, "r", encoding="utf-8") as fh:
            return fh.read()
    # 인자·파일 모두 없으면 stdin.
    return sys.stdin.read()


def _load_config(config_path: str) -> Any:
    """config.yaml 로드(CLI 경로). 지연 import 로 테스트 격리."""
    from app.config import load_config

    return load_config(config_path)


def main(argv: Optional[list] = None) -> int:
    """CLI 엔트리 — 리포트를 읽어 send_report. 발송 성공 0, 아니면 1."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="완료-리포트를 설정된 알림 채널로 발송(프랙탈 P2)")
    parser.add_argument("--ticket", default=None, help="티켓 키(헤더 상관용, 선택)")
    parser.add_argument("--report", default=None, help="리포트 본문(직접 전달)")
    parser.add_argument("--report-file", default=None, help="리포트 본문 파일 경로")
    parser.add_argument("--config", default="config/config.yaml", help="config.yaml 경로")
    parser.add_argument(
        "--force", action="store_true",
        help="티켓당 멱등(dedup)을 무시하고 강제 재전송(기본은 이미 보낸 티켓이면 skip)",
    )
    args = parser.parse_args(argv)

    # --- ROLE 가드(결정적 백스톱): 워커는 통지자가 아니다 — 센트럴만 알림 상신한다. ---
    # ROLE=worker 컨텍스트(스포너가 주입)에서 호출되면 상신하지 않고 조용히 skip 한다.
    # 워커의 완료 전파는 리포트 반환으로만 이뤄지고, 그 리포트를 센트럴이 받아 알림으로
    # 상신한다. --force 여도 워커에선 나가지 않는다(중복 알림의 근본 차단). 성공(exit 0)
    # 으로 반환해 워커가 "안 보내졌나?" 오해로 재시도하지 않게 stdout 에 사실을 알린다.
    if _is_worker_role():
        print("notify_report: ROLE=worker 컨텍스트 — 상신 skip(워커는 통지자가 아님, 센트럴이 상신). "
              "워커 완료는 리포트 반환으로 전파된다.")
        log.warning("notify_report: ROLE=worker 컨텍스트 호출 — 상신하지 않음(센트럴이 유일 통지자)")
        return 0

    # 리포트 읽기·config 로드는 **트레이스백 없이** 실패를 다룬다(#3). 이 도구는 best-effort
    # 이므로, 파일 부재·config 오류(예: --config 경로 부재/파싱 실패)에서도 원시 예외를
    # 밖으로 던지지 않고 명확한 진단 한 줄만 남기고 미발송(exit 1)으로 수렴한다. (앞서
    # 지상관측된 --config 호출의 1회 traceback = 이 경로가 무방비였던 것이 원인 — 인자
    # 자체는 정상 수용된다. 시크릿·경로 값은 로깅하지 않는다.)
    try:
        report = _read_report(args)
    except OSError:
        log.warning("notify_report: 리포트 소스를 읽을 수 없음(파일 부재/읽기 오류) — 미발송")
        return 1
    try:
        config = _load_config(args.config)
    except Exception:  # noqa: BLE001 — config 부재/파싱 오류에도 트레이스백 없이 미발송
        log.warning("notify_report: config 로드 실패(--config 경로 확인) — 미발송")
        return 1

    # --- 티켓당 멱등(dedup): 이미 보낸 티켓이면 재전송하지 않는다(설계 B, 결정적 백스톱). ---
    # 에이전트가 확인용으로 다시 불러도 조용히 skip(성공 exit 0)해 채널에 같은 완료-리포트가
    # 2~3개 뜨는 것을 막는다. skip 사실은 **stdout** 에 명확히 알려(에이전트가 "안 보내졌나?"
    # 오해해 또 부르지 않도록) 성공으로 반환한다. --force 로 의도적 재전송은 허용.
    ticket = args.ticket
    if ticket and not args.force and already_sent(ticket):
        print(f"notify_report: 티켓 {ticket} 는 이미 상신됨 — 재전송 skip(멱등, 성공 exit 0). "
              f"강제 재전송은 --force.")
        _mark_done(ticket)   # 관측성(A.3): 이미 상신됨 = 이미 완료 — done 보장(멱등).
        return 0
    # 전송 직전 원자적 선점(동시호출도 정확히 하나만 발송). --force 는 선점 검사 없이 발송.
    claimed = True
    if ticket and not args.force:
        claimed = claim_marker(ticket)
        if not claimed:
            print(f"notify_report: 티켓 {ticket} 는 동시 호출이 이미 선점 — 재전송 skip(멱등, 성공 exit 0).")
            _mark_done(ticket)   # 관측성(A.3): 동시 발송자가 선점 = 완료 — done 보장(멱등).
            return 0

    ok = send_report(config, report, ticket=ticket)

    if ok:
        # 강제 재전송(--force)도 이후 dedup 기준이 되도록 마커를 보장한다(있으면 그대로 True).
        if ticket and args.force:
            claim_marker(ticket)
        _mark_done(ticket)   # 관측성(A.3): 완료 게이트 통과 — done 마킹(대시보드 완료 표시).
        return 0
    # 발송 실패 → 선점 마커 롤백(정당한 재시도가 막히지 않게; 마커=성공 발송의 의미 유지).
    if ticket and claimed and not args.force:
        release_marker(ticket)
    return 1


if __name__ == "__main__":  # pragma: no cover — 얇은 CLI 진입
    raise SystemExit(main())
