#!/usr/bin/env python3
"""track.py — 프랙탈 잡 라이프사이클을 대시보드에 세만틱하게 기록하는 도구(프랙탈 P2, 관측성 B).

역할(관측성 설계 B — 뼈대 위의 "살"):
    센트럴 에이전트(및 사용자 서브)가 티켓 처리를 진행하며 **세만틱 진행 상황**(트리아지
    후 영향 레포 확정 / 레포별 커버·브랜치·MR / 완료 등)을 jobs.json(= central 의 JobQueue
    와 **같은 store**)에 남기는 문서화된 도구다. 이 기록은 관리 대시보드에 그대로 뜬다.

    ⚠️ 이건 **부가정보(살)**일 뿐이다 — 결정적 뼈대(poller 의 queued 생성, worker_dispatch
    의 running/failed, gchat 의 done)가 이미 기본 가시성을 보장한다. 에이전트가 track 을
    깜빡해도 대시보드엔 잡이 뜬다. track 은 per-repo 진행·MR·세만틱 이벤트로 그 위에 덧칠한다.

크로스프로세스 안전(관측성 C): :func:`app.state.record_job_event`(내부 flock)로 갱신하므로
    central 의 JobQueue·worker_dispatch·gchat 와 서로 덮어쓰지 않는다. 존재하지 않는 티켓이면
    최소 레코드를 만든 뒤 갱신한다(뼈대가 이미 만들었을 가능성이 높다).

사용(센트럴/사용자 서브 프레임이 호출):
    python /app/track.py --ticket <T> --event triaged --user <U> --repo <R> \
        --status running --mr <URL> --branch <B> --detail '<한 줄 진행>'

--event 는 자유 세만틱 라벨(triaged/repo_started/repo_covered/completed 등)이다. --status
가 주어지면 잡 상태(queued/running/done/failed/…)로 정규화해 반영한다(취소/이관 상태는
record_job_event 의 protect 규칙이 되돌리지 않는다). --repo 가 있으면 per-repo 진행을
meta.repos 아래에 누적한다.

종료코드: 기록 성공 0, 실패 1(best-effort — 실패해도 상위 흐름을 막지 않는 게 원칙).
POLICY-ENCODING: 입출력 텍스트는 UTF-8. 시크릿은 인자·기록에 싣지 않는다(티켓/레포/이벤트만).
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("jad.track")


def _now_iso() -> str:
    """이벤트 시각(ISO-8601 UTC)."""
    return datetime.now(timezone.utc).isoformat()


def record_event(
    ticket: str,
    *,
    event: str,
    user: Optional[str] = None,
    repo: Optional[str] = None,
    status: Optional[str] = None,
    mr: Optional[str] = None,
    detail: Optional[str] = None,
    branch: Optional[str] = None,
    store: Optional[Callable] = None,
) -> Optional[dict]:
    """세만틱 이벤트를 잡 레코드에 반영(순수 조립 + 주입 store 호출).

    ``store`` 는 ``record_job_event`` 호환 콜러블(테스트가 대역을 주입해 state 미접근 격리).
    None 이면 실제 :func:`app.state.record_job_event` 를 상태 디렉토리 앵커 후 사용한다.
    반환: store 의 반환(갱신된 레코드 사본) 또는 None.
    """
    ticket = (ticket or "").strip()
    if not ticket:
        return None

    now = _now_iso()
    # meta: last_event(스칼라 덮어씀) + per-repo 진행(누적 병합).
    meta: dict = {"last_event": event, "last_event_at": now}
    if repo:
        meta["repos"] = {repo: {"event": event, "at": now,
                                **({"detail": detail} if detail else {})}}

    # log_summary(대시보드 표시): detail 우선, 없으면 이벤트(+레포) 요약.
    summary = detail or (f"{event} · {repo}" if repo else event)

    fields: dict[str, Any] = {"log_summary": summary}
    if user:
        fields["user"] = user
    if mr:
        fields["mr_url"] = mr
    if branch:
        fields["branch"] = branch

    if store is None:
        try:
            from app import queue as _q
            from app import state

            state.set_state_dir(state.resolve_runtime_state_dir())
            norm_status = _q.normalize_status(status) if status else None
            return state.record_job_event(
                ticket, status=norm_status, create_if_missing=True,
                defaults={"status": "queued"}, meta=meta, **fields,
            )
        except Exception:  # noqa: BLE001 — best-effort: 기록 실패가 상위를 막지 않는다
            log.warning("track: 잡 이벤트 기록 실패(무시): %s", ticket)
            return None

    # 주입 store(테스트): 정규화는 호출측 계약에 맡기지 않고 여기서 최소만 넘긴다.
    return store(ticket, status=status, create_if_missing=True,
                 defaults={"status": "queued"}, meta=meta, **fields)


def _parse_args(argv: Optional[list]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="프랙탈 잡 라이프사이클을 대시보드에 기록(관측성 살, 프랙탈 P2)"
    )
    parser.add_argument("--ticket", required=True, help="티켓 키")
    parser.add_argument("--event", required=True, help="세만틱 이벤트 라벨(triaged/repo_covered/…)")
    parser.add_argument("--user", default=None, help="담당 사용자(선택)")
    parser.add_argument("--repo", default=None, help="이 이벤트가 가리키는 레포(per-repo 진행, 선택)")
    parser.add_argument("--status", default=None,
                        help="잡 상태(queued/running/done/failed/… 선택; 정규화됨)")
    parser.add_argument("--mr", default=None, help="MR/PR URL(선택)")
    parser.add_argument("--branch", default=None, help="브랜치(선택)")
    parser.add_argument("--detail", default=None, help="한 줄 진행 요약(대시보드 log_summary)")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """CLI 엔트리 — 이벤트를 기록. 성공 0, 실패 1."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    args = _parse_args(argv)
    rec = record_event(
        args.ticket, event=args.event, user=args.user, repo=args.repo,
        status=args.status, mr=args.mr, detail=args.detail, branch=args.branch,
    )
    if rec is None:
        return 1
    print(f"track: {args.ticket} ← {args.event}"
          + (f" ({args.repo})" if args.repo else "")
          + (f" [{args.status}]" if args.status else ""))
    return 0


if __name__ == "__main__":  # pragma: no cover — 얇은 CLI 진입
    raise SystemExit(main())
