#!/usr/bin/env python3
"""worker_dispatch.py — 워커 컨테이너로 한 티켓을 위임하는 **신뢰 하네스 도구**(프랙탈 P2).

역할(설계 §3.2, 라이브 컷오버 CRITICAL 해소):
    센트럴 세션의 사용자별 서브에이전트가 **문서화된 도구**로서 이 CLI 를 호출한다. 이
    도구는 내부에서 ``docker exec jad-worker-<user> claude -p [--session-id|--resume]
    <sid> "<지시>"`` 를 돌려(=이미 활성화된 exec 능력: DOCKER_HOST=socket-proxy, EXEC:1),
    워커의 **리치 완료-리포트**를 캡처하고 **JSON 을 stdout 으로** 돌려준다.

    ⚠️ **왜 도구인가**(근본 원인 해소): 라이브 컷오버에서 센트럴 에이전트는 "남의
    컨테이너로 docker exec 해 그 정체성으로 실행하라"는 **인젝션-형태의 원시 액션**을
    프롬프트-인젝션 공격으로 거부했다. 이 도구는 그 exec/임퍼소네이션 기계장치를
    파이썬 안에 **숨긴다**(:mod:`app.spawner` 가 라이프사이클을 숨기듯) — 에이전트는
    docker exec 를 **추론하지 않고** 문서화된 도구를 실행해 JSON 응답만 읽는다. 즉
    "에이전트가 제안/판단(어떤 티켓·병렬/직렬·후속) / 파이썬이 배관(plumbing)"이다.

    AC1(사용자당 병렬): 서브가 병렬 티켓마다 **서로 다른 --session-id** 로 이 도구를
    각각 블로킹-호출하면 그 사용자 컨테이너에서 **동시 claude 세션**이 뜬다(서로 다른
    레포는 진짜 병렬, 같은 레포는 레포락 장부로 직렬). 이 도구는 세션 id 를 만들지
    않고 **호출자가 준 값 그대로** 쓴다(sid 정책은 에이전트의 판단).

    AC2(리뷰→추가작업 루프): 서브가 반환 JSON 의 리포트를 판단해 더 필요하면 **같은 sid
    로 ``--resume`` 재호출**(같은 워커 대화를 이어간다), 진짜 완료면 상신한다.

재사용(설계 §6): 커맨드 형태의 단일 원천은 :func:`app.central_session.build_worker_exec_command`,
    리포트 추출은 :func:`app.prompts.extract_completion_report` 를 그대로 재사용한다.

사용(사용자 서브 프레임이 호출):
    python /app/worker_dispatch.py --user yhchoi --ticket HAN-1 \
        --session-id <sid> --instruction "<지시>"          # 첫 턴(새 세션)
    python /app/worker_dispatch.py --user yhchoi --ticket HAN-1 \
        --resume <sid> --instruction "<추가 지시>"          # 이어(같은 세션)

stdout 은 **JSON 한 덩어리**(에이전트가 파싱). 진단 로그는 stderr 로만 낸다.
종료코드: 워커 rc==0 이면 0, 아니면 1(에이전트는 exit code 가 아니라 JSON 을 읽는다).

POLICY-ENCODING: 입출력 텍스트는 UTF-8 로 다룬다. 시크릿 값은 로그·JSON 에 싣지 않는다
(이 경로의 인자는 티켓 키/지시 텍스트 — 비밀 아님).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
from typing import Any, Callable, Optional

from app.central_session import (
    WORKER_CONTAINER_PREFIX,
    build_worker_exec_command,
    resolve_worker_session_id,
)
from app.prompts import extract_completion_report

log = logging.getLogger("jad.worker_dispatch")


def _last_json_object(text: str) -> Optional[dict]:
    """텍스트 마지막 줄부터 거슬러 첫 유효 JSON 객체(``{...}`` 한 줄)를 찾는다(방어적).

    ``--output-format json`` 은 대개 stdout 전체가 하나의 JSON 이지만, 선행 진단 라인이
    섞여 들어와도 최종 result 객체를 놓치지 않도록 마지막 줄부터 스캔한다.
    """
    for raw in reversed((text or "").splitlines()):
        line = raw.strip()
        if len(line) >= 2 and line[0] == "{" and line[-1] == "}":
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(obj, dict):
                return obj
    return None


def _result_text_from_json(text: str) -> Optional[str]:
    """``claude --output-format json`` stdout → 최종 ``result`` 텍스트(없으면 None).

    ``-p --output-format json`` 은 **단일 result 객체**(예 ``{"type":"result",
    "result":"<최종 어시스턴트 텍스트>", ...}``)를 낸다. 그 ``result`` 필드가 곧 워커의
    최종 리포트 본문이다 — 전사(transcript)가 툴 반환 뒤에도 커지는 타이밍 아티팩트에
    영향받지 않는 결정적 최종본이다(#2).
    """
    s = (text or "").strip()
    if not s:
        return None
    obj: Optional[dict]
    try:
        obj = json.loads(s)
        if not isinstance(obj, dict):
            obj = _last_json_object(s)
    except (json.JSONDecodeError, ValueError):
        obj = _last_json_object(s)
    if isinstance(obj, dict):
        r = obj.get("result")
        if isinstance(r, str) and r.strip():
            return r
    return None


def _final_text_and_report(stdout: str) -> "tuple[str, bool]":
    """워커 stdout 에서 ``(리포트, 마커추출여부)`` 를 견고하게 뽑는다(#2).

    1) ``--output-format json`` 의 단일 result 객체를 파싱해 최종 어시스턴트 텍스트를 얻고
       (없으면 stdout 전문으로 폴백),
    2) 그 텍스트에서 BEGIN/END 리치 리포트 블록을 추출한다. 마커가 없으면 최종 텍스트
       전체를 리포트로 폴백한다(에이전트가 판단할 재료를 잃지 않게).
    """
    text = stdout or ""
    final = _result_text_from_json(text)
    source = final if final is not None else text
    extracted = extract_completion_report(source)
    if extracted:
        return extracted[1], True
    return (source or "").strip(), False


# MR/PR URL 파싱(관측성 A.2). GitLab merge_requests/<n>·GitHub pull/<n> 를 견고하게 잡는다.
# ⚠️ 한계: 리포트 본문에 URL 이 명시돼야 잡힌다(에이전트가 안 실으면 None). 커스텀 도메인/
# 포맷·여러 MR 이 있으면 **첫 매치**만 취한다(대시보드 단일 mr_url 필드). 완전 검증은 라이브.
_MR_URL_RE = re.compile(
    r"https?://[^\s<>()\[\]]+?/(?:-/)?merge_requests/\d+"
    r"|https?://[^\s<>()\[\]]+?/pull/\d+"
)


def _parse_mr_url(text: str) -> Optional[str]:
    """리포트 텍스트에서 첫 MR/PR URL 을 뽑는다(없으면 None). best-effort."""
    if not text:
        return None
    m = _MR_URL_RE.search(text)
    if not m:
        return None
    return m.group(0).rstrip(".,);]")


def _summarize(text: str, limit: int = 500) -> str:
    """리포트를 대시보드용 한 줄 요약으로 축약(공백 정규화 + 길이 제한)."""
    s = " ".join((text or "").split())
    return s[:limit] if len(s) > limit else s


def _emit_record(recorder: Optional[Callable], ticket: str, *, status: Optional[str] = None,
                 **fields: Any) -> None:
    """주입된 recorder 로 잡 라이프사이클을 기록(관측성 A.2). recorder 없으면 no-op.

    recorder 시그니처: ``recorder(ticket, *, status=None, **fields)``. 프로덕션 recorder
    (:func:`_make_state_recorder`)는 내부적으로 best-effort(예외 삼킴)다. 테스트는 호출을
    관측하는 대역을 주입한다(state 미접근 격리).
    """
    if recorder is None:
        return
    recorder(ticket, status=status, **fields)


def _default_runner(cmd: list, timeout_sec: int = 0):
    """기본 실행자 — ``docker exec ...`` 를 블로킹 실행하고 CompletedProcess 반환.

    stdout/stderr 를 캡처(UTF-8·errors=replace). ``timeout_sec>0`` 이면 그 상한을 건다
    (0/음수면 무제한 — 워커 작업은 오래 걸릴 수 있다). DOCKER_HOST 등 exec 환경은
    센트럴 컨테이너의 ambient env 를 그대로 상속한다(별도 주입 불필요).
    """
    kwargs: dict = dict(
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if timeout_sec and timeout_sec > 0:
        kwargs["timeout"] = timeout_sec
    return subprocess.run(cmd, **kwargs)  # noqa: S603 — 신뢰 하네스(커맨드는 결정적 빌더)


def _worker_role_refusal(user: str, ticket: str, env: dict) -> Optional[dict]:
    """``ROLE=worker`` 컨텍스트에서 호출됐으면 거부 결과를 만든다(self-exec 재귀 백스톱).

    ``worker_dispatch`` 는 **센트럴(ROLE=central) 전용 도구**다 — 티켓을 워커 컨테이너로
    위임(``docker exec``)하는 배관이다. 워커(ROLE=worker)는 순수 실행자(doer)이므로 이
    도구를 호출할 이유가 없다. 워커가 (프레임 오염 등으로) 이 도구를 부르면 **자기
    컨테이너로 되돌아오는 self-exec 재귀**·상호대기 라이브락이 나므로, ROLE=worker 면
    exec 하지 않고 명확히 거부한다. 이는 프레임 역할가드(1차 방어)를 보완하는 **최소
    기계 백스톱**이다(명백한 사고 방지용 — 정상 경로 판단을 억누르지 않는다).

    ROLE 이 worker 가 아니면(central·미설정/테스트) None 을 반환해 정상 진행시킨다.
    """
    role = (env.get("ROLE") or "").strip().lower()
    if role != "worker":
        return None
    return {
        "user": user,
        "ticket": ticket,
        "session_id": None,
        "mode": "refused",
        "container": None,
        "status": "refused",
        "returncode": None,
        "report_extracted": False,
        "report": (
            "[refused] worker_dispatch 는 ROLE=central 전용 도구다. 이 컨텍스트는 "
            "ROLE=worker(순수 실행자)이므로 위임하지 않는다 — 워커는 dispatch/docker "
            "exec 를 하지 않고, 위임받은 티켓의 모든 대상 레포를 직접 처리한다. "
            "(self-exec 재귀·라이브락 방지 백스톱)"
        ),
    }


# ---------------------------------------------------------------------------
# 진행중(in-progress) Jira 전이 — 워커 실제 착수 시 해야할일→진행중(런타임 발견·멱등)
# ---------------------------------------------------------------------------
#
# 설계(요구사항 1~5):
#   - 시점 = 워커 exec 직전(관측성이 running 을 찍는 그 지점) — 수신/대기(pending)가 아니라
#     실제 착수 때만 전이한다.
#   - 런타임 발견: 하드코딩 id 금지. `GET .../transitions` 로 가용 전이를 조회한다. 단
#     statusCategory=indeterminate 상태가 **둘 이상일 수 있다**(지상검증 실측: 이 워크플로우엔
#     `보류`(On Hold)와 `진행 중` 둘 다 indeterminate). "아무 indeterminate나" 고르면 `보류`로
#     잘못 갈 수 있으므로(진행중보다 나쁨), 선택 규칙은 다음 우선순위다(:func:`_find_in_progress_transition`):
#       1) **이름 우선 매칭**: 전이의 목표 상태명(또는 전이명)을 정규화(NFC·공백제거·casefold)해
#          알려진 in-progress 상태명 집합(:data:`_IN_PROGRESS_NAMES`)과 매칭되면 그걸 선택.
#       2) 이름 매칭이 없고 indeterminate 전이가 **정확히 1개**면 그걸 사용(모호성 없음).
#       3) indeterminate 가 여럿인데 이름 매칭이 없으면 → **고르지 않고 skip**(ambiguous 기록).
#          잘못 골라 보류로 보내느니 안 옮긴다(멱등·best-effort 원칙과 정합).
#   - 귀속 = **디스패치 유저 토큰**: run_dispatch 는 센트럴 컨텍스트에서 돌고 워커 컨테이너로
#     docker exec 한다. 따라서 워커 env(JIRA_EMAIL/JIRA_API_TOKEN)는 센트럴에서 직접 못 읽는다.
#     센트럴 쪽 per-user 자격 경로 = **레지스트리 레코드**(jira_email + secrets_ref.jira_token)
#     를 secrets.base_dir 로 read_secret → 그 유저 토큰으로 JiraClient 구성(작업 귀속 유지).
#   - 멱등: 현재 status **이름**이 in-progress 집합이면 재전이하지 않는다(--resume 재개·중복
#     디스패치 방어). 단 `보류`(indeterminate지만 in-progress 아님)는 in-progress 로 치지
#     않는다 — 보류 상태 티켓은 진행중으로 옮긴다.
#   - best-effort: 전이 실패가 디스패치를 죽이지 않되(예외 격리) **조용히 성공 처리하지 않는다**
#     — 결과를 로그(마스킹)와 잡 meta(jira_in_progress)에 남긴다. 토큰 값은 로깅/기록하지 않는다.
#   - 완료(done) 전이는 손대지 않는다(오케스트레이터/사용자 몫, 범위 밖).

# 알려진 in-progress 상태명(원문). 매칭은 정규화(NFC·공백제거·casefold) 후 비교하므로
# "진행 중"/"진행중"·"in progress"/"inprogress" 변형이 함께 커버된다. `보류`(On Hold)는
# 의도적으로 제외한다(indeterminate 지만 진행중이 아니다).
_IN_PROGRESS_NAMES = frozenset({
    "진행중", "진행 중", "작업중", "작업 중",
    "inprogress", "in progress", "in-progress", "doing", "started",
})


def _normalize_status_name(name: Any) -> str:
    """상태명 정규화 — NFC · 모든 공백 제거 · casefold. 이름 매칭 안정화(공백/대소문자/유니코드)."""
    s = unicodedata.normalize("NFC", str(name or ""))
    return "".join(s.split()).casefold()


# 정규화된 in-progress 이름 집합(비교용, 모듈 로드 시 1회).
_IN_PROGRESS_NORM = frozenset(_normalize_status_name(n) for n in _IN_PROGRESS_NAMES)


def _find_in_progress_transition(transitions: Any) -> "tuple":
    """가용 전이 목록에서 진행중 전이를 고른다(런타임 발견 — 하드코딩 id 금지).

    indeterminate 카테고리 상태가 여럿일 수 있으므로(`보류`+`진행 중`) 다음 우선순위로 고른다:
      1) 이름 우선 매칭(정규화)이 있으면 그 전이,
      2) 이름 매칭 없고 indeterminate 가 정확히 1개면 그것,
      3) indeterminate 여럿·이름 매칭 없음 → 고르지 않음(ambiguous).

    반환: ``(chosen|None, reason|None, candidates)``. chosen 이 있으면 reason=None. 없으면
    reason 은 ``"no-in-progress-transition"``(indeterminate 0개) 또는 ``"ambiguous-in-progress"``
    (여럿·이름 불명). candidates 는 indeterminate 후보 상태명 목록(로그/meta 가시성용).
    """
    indeterminate: list = []
    for t in transitions or []:
        if not isinstance(t, dict):
            continue
        to = t.get("to") or {}
        cat = ((to.get("statusCategory") or {}).get("key") or "").strip().lower()
        if cat == "indeterminate":
            indeterminate.append(t)
    candidates = [((t.get("to") or {}).get("name") or t.get("name") or "") for t in indeterminate]

    # 1) 이름 우선 매칭 — 목표 상태명 또는 전이명이 in-progress 집합이면 그걸 확정 선택.
    for t in indeterminate:
        to_name = _normalize_status_name((t.get("to") or {}).get("name"))
        tr_name = _normalize_status_name(t.get("name"))
        if to_name in _IN_PROGRESS_NORM or tr_name in _IN_PROGRESS_NORM:
            return t, None, candidates

    # 2) 이름 매칭 없음 + indeterminate 정확히 1개 → 모호성 없으니 사용.
    if len(indeterminate) == 1:
        return indeterminate[0], None, candidates

    # 3) 0개 → no-in-progress; 여럿 → ambiguous(고르지 않음).
    if not indeterminate:
        return None, "no-in-progress-transition", candidates
    return None, "ambiguous-in-progress", candidates


def _resolve_user_jira_client(user: str, config: Any, base_url: str):
    """디스패치 유저의 Jira 자격(레지스트리 레코드 → jira_email + secrets_ref.jira_token)으로
    :class:`JiraClient` 를 구성한다(센트럴 쪽 per-user 자격 경로 = 작업 귀속 유지).

    반환: ``(client, None)`` 또는 자격이 없으면 ``(None, reason)``. 토큰 값은 로깅하지 않는다.
    """
    from app.config import read_secret
    from app.jira_client import JiraClient
    from app.registry import Registry

    rec = Registry().get(user)
    if rec is None:
        return None, "no-registry-record"
    email = getattr(rec, "jira_email", "") or ""
    token_ref = getattr(getattr(rec, "secrets_ref", None), "jira_token", "") or ""
    base_dir = getattr(getattr(config, "secrets", None), "base_dir", "") or ""
    token = read_secret(base_dir, token_ref) or ""
    if not (email and token):
        return None, "missing-user-jira-creds"
    # 인스턴스별 값(커스텀필드 id·완료 전이)은 config 에서 주입한다(미설정 항목은 모듈 상수
    # 폴백 — 하위호환). base_url 은 이미 손에 쥔 값이라 config 보다 우선시킨다.
    return JiraClient.from_config(config, email, token, base_url=base_url), None


def _do_in_progress_transition(client: Any, ticket: str) -> dict:
    """``client``(디스패치 유저 토큰)로 ``ticket`` 을 해야할일→진행중 전이(런타임 발견·멱등).

    1) 현재 status **이름**이 in-progress 집합이면 skip(멱등). `보류`(indeterminate지만 진행중
       아님)는 in-progress 로 치지 않아 진행중으로 옮긴다.
    2) 가용 전이 중 진행중 전이를 :func:`_find_in_progress_transition` 우선순위(이름→단일→
       ambiguous)로 골라 POST. 확정 못 하면 skip(사유·후보 기록).
    반환: 결과 dict(전이/스킵 여부·사유·전이 id·목표 상태·후보). 예외는 상위(wrapper)가 잡는다.
    """
    issue = client.get_issue(ticket, fields=["status"])
    status = ((issue.get("fields") or {}).get("status") or {})
    cur_name = _normalize_status_name(status.get("name"))
    if cur_name in _IN_PROGRESS_NORM:
        return {"transitioned": False, "skipped": True, "reason": "already-in-progress"}
    chosen, reason, candidates = _find_in_progress_transition(client.list_transitions(ticket))
    if chosen is None:
        result = {"transitioned": False, "skipped": True, "reason": reason}
        if candidates:
            result["candidates"] = candidates
        return result
    tid = str(chosen.get("id"))
    client.transition(ticket, tid)
    to_name = ((chosen.get("to") or {}).get("name")) or ""
    return {
        "transitioned": True, "skipped": False,
        "transition_id": tid, "to_status": to_name,
    }


def _default_jira_transitioner(user: str, ticket: str, config: Any) -> dict:
    """프로덕션 전이자 — 디스패치 유저 토큰으로 해야할일→진행중(런타임 발견·멱등).

    config 에 jira.base_url 이 없으면(테스트/미설정) 네트워크·레지스트리 접근 없이 즉시 skip 한다.
    예외는 여기서 삼키지 않는다 — 상위 :func:`_run_in_progress_transition` 가 격리해 디스패치를
    계속시킨다(요구사항 5). 토큰 값은 로깅/기록하지 않는다.
    """
    base_url = getattr(getattr(config, "jira", None), "base_url", "") or ""
    if not base_url:
        return {"transitioned": False, "skipped": True, "reason": "no-jira-config"}
    client, reason = _resolve_user_jira_client(user, config, base_url)
    if client is None:
        return {"transitioned": False, "skipped": True, "reason": reason}
    return _do_in_progress_transition(client, ticket)


def _run_in_progress_transition(
    transitioner: Callable, user: str, ticket: str, config: Any,
    recorder: Optional[Callable],
) -> dict:
    """진행중 전이를 best-effort 로 수행하고 결과를 로그·잡 meta 에 남긴다(요구사항 5).

    전이 실패(예외 포함)가 디스패치를 죽이지 않게 격리하되 **조용히 성공 처리하지 않는다** —
    사유를 남긴다. 시크릿·토큰은 로깅/기록하지 않는다(이 경로는 티켓 키/전이 id 만 다룬다).
    """
    try:
        result = transitioner(user, ticket, config)
        if not isinstance(result, dict):
            result = {"transitioned": False, "skipped": True, "reason": "no-result"}
    except Exception as exc:  # noqa: BLE001 — 전이 실패는 디스패치를 막지 않는다(best-effort)
        result = {
            "transitioned": False, "skipped": False,
            "error": type(exc).__name__, "reason": "exception",
        }
        log.warning(
            "worker_dispatch: 진행중 Jira 전이 실패(무시, 디스패치 계속) ticket=%s err=%s",
            ticket, type(exc).__name__,
        )
    else:
        if result.get("transitioned"):
            log.info(
                "worker_dispatch: 진행중 전이 완료 ticket=%s id=%s to=%s",
                ticket, result.get("transition_id"), result.get("to_status"),
            )
        else:
            log.info(
                "worker_dispatch: 진행중 전이 skip ticket=%s reason=%s candidates=%s",
                ticket, result.get("reason"), result.get("candidates"),
            )
    # 결과를 잡 meta(jira_in_progress)에 남긴다(가시성). 알려진 필드가 아니라 meta 로 들어간다.
    _emit_record(recorder, ticket, jira_in_progress=result)
    return result


def run_dispatch(
    user: str,
    ticket: str,
    instruction: str,
    *,
    session_id: str,
    resume: bool,
    config: Any = None,
    runner: Optional[Callable] = None,
    timeout_sec: int = 0,
    env: Optional[dict] = None,
    recorder: Optional[Callable] = None,
    jira_transitioner: Optional[Callable] = None,
) -> dict:
    """한 티켓을 워커 세션으로 위임(블로킹)하고 결과 dict 를 반환(순수 배관).

    ``resume=False`` → ``--session-id <sid>``(첫 턴), ``resume=True`` → ``--resume <sid>``
    (이어). sid 는 :func:`resolve_worker_session_id`(단일 규칙)로 유효 UUID 로 해석한다
    (미지정/비UUID면 ``(user, ticket)`` 파생 — #1). 커맨드는 :func:`build_worker_exec_command`
    (단일 원천)로 짓고, 완료-리포트는 :func:`_final_text_and_report`(``--output-format json``
    의 result → BEGIN/END 마커)로 견고하게 뽑는다(#2).

    ``ROLE=worker`` 컨텍스트면 exec 하지 않고 거부한다(self-exec 재귀 백스톱, #5-실행).

    진행중 Jira 전이(착수 귀속): exec 직전(= 관측성 running 지점 = 워커 실제 착수)에 해당
    티켓을 **해야할일→진행중**으로 전이한다 — **디스패치 유저 토큰**(레지스트리 레코드의
    jira_email + secrets_ref.jira_token)으로, **런타임 발견**(statusCategory=indeterminate),
    **멱등**(이미 진행중이면 skip), **best-effort**(실패해도 디스패치 계속·결과를 로그와 잡
    meta.jira_in_progress 에 기록). 완료(done) 전이는 손대지 않는다(오케스트레이터/사용자 몫).
    ``jira_transitioner`` 로 전이자를 주입하면(테스트) 그걸, 없으면
    :func:`_default_jira_transitioner`(config.jira.base_url 없으면 네트워크 없이 skip)를 쓴다.

    관측성 뼈대(A.2): ``recorder`` 가 주어지면(:func:`_make_state_recorder` 또는 테스트
    대역) 라이프사이클을 jobs.json 에 찍는다 — exec 시작 직전 **running**, 결과가
    error/timeout/refused 면 **failed**(사유를 log_summary), ok 면 상태는 그대로 두고
    (완료 게이트=gchat) mr_url/log_summary 만 갱신한다. recorder 없으면 순수 동작(무기록).
    """
    refusal = _worker_role_refusal(user, ticket, env if env is not None else os.environ)
    if refusal is not None:
        log.warning("worker_dispatch: ROLE=worker 컨텍스트 호출 거부(self-exec 재귀 방지)")
        _emit_record(recorder, ticket, status="failed", user=user,
                     log_summary="[refused] ROLE=worker 컨텍스트 — 위임 거부(self-exec 방지)")
        return refusal
    sid = resolve_worker_session_id(session_id, user, ticket)
    first = not resume
    cmd = build_worker_exec_command(
        user, ticket, first=first, config=config,
        session_id=sid, instruction=instruction,
    )
    run = runner or _default_runner
    # 관측성(A.2): exec 시작 직전 running 으로 전이(레코드 없으면 최소 생성).
    _emit_record(recorder, ticket, status="running", user=user, session_id=sid,
                 log_summary="worker exec 시작")
    # 진행중 Jira 전이(착수 귀속): 워커 실제 착수(= 이 exec 직전 = 관측성 running 지점)에
    # 바인딩해 해야할일→진행중으로 전이한다. 디스패치 유저 토큰·런타임 발견·멱등·best-effort
    # (완료 전이는 오케스트레이터/사용자 몫 — 범위 밖).
    _run_in_progress_transition(
        jira_transitioner or _default_jira_transitioner, user, ticket, config, recorder,
    )
    try:
        completed = run(cmd, timeout_sec)
        stdout = getattr(completed, "stdout", "") or ""
        stderr = getattr(completed, "stderr", "") or ""
        rc = getattr(completed, "returncode", None)
    except subprocess.TimeoutExpired:
        _emit_record(recorder, ticket, status="failed", user=user,
                     log_summary=f"[timeout] worker_dispatch 가 {timeout_sec}s 안에 끝나지 않음")
        return {
            "user": user, "ticket": ticket, "session_id": sid,
            "mode": "resume" if resume else "session-id",
            "container": f"{WORKER_CONTAINER_PREFIX}{user}",
            "status": "timeout", "returncode": None,
            "report_extracted": False,
            "report": f"[timeout] worker_dispatch 가 {timeout_sec}s 안에 끝나지 않음",
        }

    report, report_extracted = _final_text_and_report(stdout)
    if not report and stdout.strip():
        # 빈 리포트로 **조용히 반환하지 않는다**(#2) — stdout 전문으로 폴백해 상위가
        # 판단할 재료를 남긴다(파싱 실패/예상 밖 포맷 방어).
        log.warning("worker_dispatch: 최종 리포트가 비어 stdout 전문으로 폴백(rc=%s)", rc)
        report = stdout.strip()

    status = "ok" if rc == 0 else "error"
    # 관측성(A.2): 결과 반영. ok→상태 유지(running; 완료 확정·done 은 gchat 게이트), error→failed.
    mr_url = _parse_mr_url(report)
    if status == "ok":
        _emit_record(recorder, ticket, user=user, mr_url=mr_url,
                     log_summary=_summarize(report))
    else:
        _emit_record(recorder, ticket, status="failed", user=user, mr_url=mr_url,
                     log_summary="[error] " + _summarize(report))
    result = {
        "user": user,
        "ticket": ticket,
        "session_id": sid,
        "mode": "resume" if resume else "session-id",
        "container": f"{WORKER_CONTAINER_PREFIX}{user}",
        "status": status,
        "returncode": rc,
        "report_extracted": report_extracted,
        "report": report,
    }
    # 진단 stderr 는 JSON 에 싣지 않되(잡음), 실패 시 짧게만 남긴다(로그, 시크릿 미포함 전제).
    if status != "ok" and stderr.strip():
        log.warning("worker_dispatch: 워커 exec 비정상 종료(rc=%s)", rc)
    return result


# 컨테이너 WORKDIR 앵커(Dockerfile `COPY . .` → 리포 루트가 /app). config.yaml 은
# /app/config/config.yaml 에 놓인다. gchat._resolve_state_dir 와 동일한 앵커 패턴.
_APP_ROOT = "/app"
_DEFAULT_CONFIG_REL = "config/config.yaml"


def _resolve_config_path(config_path: Optional[str]) -> str:
    """로드할 config.yaml 경로를 결정적으로 앵커한다(cwd·--config 유무와 무관).

    센트럴 서브는 cwd=``<workspace_dir>/orchestrator`` 에서 ``python /app/worker_dispatch.py``
    (``--config`` 없이) 를 부른다. 이때 상대 ``config/config.yaml`` 은 존재하지 않는
    ``<cwd>/config/config.yaml`` 로 해석돼 config 로드가 실패하고, config 의존 로직
    (예: jira 진행중 전이)이 ``no-jira-config`` 로 **비결정적으로 skip** 된다(라이브
    티켓 555 지상검증). :func:`gchat._resolve_state_dir` 와 **동일 패턴**으로 앵커한다:

      1) env ``JAD_CONFIG`` 이 있으면 그것(배포 오버라이드/테스트 격리).
      2) ``--config`` 가 **절대경로**면 그대로 존중(이미 절대경로로 주던 경로 불변).
      3) 상대경로(또는 미지정 기본값)면 컨테이너 WORKDIR(``/app``)에 앵커.
      4) 그렇게 앵커한 경로가 없으면 표준 ``/app/config/config.yaml`` 로 폴백.

    이로써 cwd·``--config`` 유무와 무관하게 항상 ``/app/config/config.yaml`` 이 로드된다.
    """
    env = os.environ.get("JAD_CONFIG")
    if env:
        return env
    path = config_path or _DEFAULT_CONFIG_REL
    if os.path.isabs(path):
        return path  # 명시 절대경로는 그대로 존중.
    anchored = os.path.join(_APP_ROOT, path)
    if not os.path.exists(anchored):
        fallback = os.path.join(_APP_ROOT, _DEFAULT_CONFIG_REL)
        if os.path.exists(fallback):
            return fallback
    return anchored


def _load_config(config_path: str) -> Any:
    """config.yaml 로드(CLI 경로). 지연 import 로 테스트 격리."""
    from app.config import load_config

    return load_config(config_path)


def _make_state_recorder() -> Callable:
    """jobs.json(= central JobQueue 와 같은 store)에 라이프사이클을 찍는 프로덕션 recorder.

    별개 프로세스에서 크로스프로세스 안전하게 갱신한다(:func:`app.state.record_job_event`,
    내부 flock). 상태 디렉토리는 호출 시점에 앵커한다(:func:`app.state.resolve_runtime_state_dir`
    → /app/state = jad-state 볼륨) — 생성 시점이 아니라 호출 시점이라, 테스트가 run_dispatch
    를 대역으로 바꿔 recorder 를 부르지 않으면 전역 상태를 건드리지 않는다. best-effort:
    기록 실패가 위임(도구의 본 임무)을 절대 막지 않는다.
    """

    def _rec(ticket: str, *, status: Optional[str] = None, **fields: Any) -> None:
        try:
            from app import state

            state.set_state_dir(state.resolve_runtime_state_dir())
            state.record_job_event(
                ticket, status=status, create_if_missing=True,
                defaults={"status": "queued"}, **fields,
            )
        except Exception:  # noqa: BLE001 — 관측성 기록 실패는 위임을 막지 않는다(best-effort)
            log.warning("worker_dispatch: 잡 상태 기록 실패(무시)")

    return _rec


def _parse_args(argv: Optional[list]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="워커 컨테이너로 한 티켓 위임(신뢰 하네스, 프랙탈 P2)"
    )
    parser.add_argument("--user", required=True, help="담당 사용자(워커 컨테이너 접미)")
    parser.add_argument("--ticket", required=True, help="티켓 키(상관용)")
    parser.add_argument("--instruction", required=True, help="워커에 줄 지시 본문")
    # 세션은 하네스가 (user, ticket)에서 결정적으로 파생한다 — 에이전트는 sid 를 만들거나
    # 추적하지 않는다(#1). --session-id/--resume 는 **선택**이며, 값이 없거나 UUID 가
    # 아니면 파생값을 쓴다. --resume 는 (값 없이도) '이어가기(resume)' 를 뜻한다.
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--session-id", dest="session_id", nargs="?", const="", default=None,
                       help="첫 턴(선택) — 유효 UUID 면 그 세션을, 아니면 (user,ticket) 파생 sid 로 새 세션")
    group.add_argument("--resume", dest="resume", nargs="?", const="", default=None,
                       help="이어(선택) — 같은 (user,ticket) 파생(또는 준) sid 로 워커 대화 계속")
    parser.add_argument("--config", default="config/config.yaml",
                        help="config.yaml 경로(상대경로/미지정이면 /app 앵커 → /app/config/config.yaml)")
    parser.add_argument("--timeout-sec", type=int, default=0,
                        help="실행 상한(초, 0=무제한). 워커 작업은 오래 걸릴 수 있다")
    return parser.parse_args(argv)


def main(argv: Optional[list] = None) -> int:
    """CLI 엔트리 — 위임 실행 후 결과 JSON 을 stdout 으로. rc==0 이면 0, 아니면 1."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(message)s")
    args = _parse_args(argv)

    resume = args.resume is not None
    raw_sid = args.resume if resume else args.session_id
    # 플래그만 주고 값이 없으면(const="") '미지정' 으로 취급 → run_dispatch 가 파생한다.
    sid = raw_sid or None

    config = None
    try:
        # cwd·--config 유무와 무관하게 /app 앵커링(상대 경로가 cwd 를 빗나가 config 로드가
        # 실패 → jira 전이가 no-jira-config 로 비결정적 skip 되던 것을 근본 해소).
        config = _load_config(_resolve_config_path(args.config))
    except Exception:  # noqa: BLE001 — config 부재/오류여도 claude_bin 기본값으로 진행 가능
        log.warning("worker_dispatch: config 로드 실패 — 기본값으로 진행")

    result = run_dispatch(
        args.user, args.ticket, args.instruction,
        session_id=sid, resume=resume, config=config, timeout_sec=args.timeout_sec,
        recorder=_make_state_recorder(),
    )
    # stdout 은 JSON 한 덩어리만(에이전트 파서 계약).
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":  # pragma: no cover — 얇은 CLI 진입
    raise SystemExit(main())
