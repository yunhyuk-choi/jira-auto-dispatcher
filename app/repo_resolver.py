"""central LLM 레포 리졸버 — 신규 티켓이 닿는 레포를 dlc-meta REPO-MAP으로 판단.

역할:
    폴러가 새 티켓을 발견하면, central이 ``claude`` 에게 "이 티켓 작업이 어느
    레포(들)에 닿는가"를 묻고 **REPO-MAP.md 슬러그**로 검증해 ``target_repos`` 를
    채운다. 스케줄러는 이 값으로 **서로 다른 레포=병렬**(global cap), **같은
    레포=직렬**을 판단한다(RECURSIVE-DISPATCH §4). 정적 config.repo_map 룩업이
    대개 빈 리스트를 돌려 "미해석=전역 직렬"로 접히던 문제를 대체한다.

    정적 룩업(``poller.resolve_target_repos``)은 **폴백/오버라이드**로만 남는다.

역할 소속: **central** (워커와 같은 이미지라 ``claude`` CLI 보유).

설계 원칙:
    - **best-effort** — claude 실패/타임아웃/파싱오류면 조용히 폴백(정적 매핑
      있으면 그걸로, 없으면 ``[]``)하고 사유만 로깅한다(시크릿 미노출). 폴러
      스레드를 절대 죽이지 않는다.
    - **유휴=0 유지** — 리졸버는 *신규 티켓이 있을 때만* claude를 부른다. 폴링
      자체(빈 결과)엔 claude 호출이 없다(호출부 poll_once가 게이트).
    - **순수 함수 분리** — ``build_prompt`` / ``parse_repos`` /
      ``extract_valid_slugs`` 는 I/O 없는 순수 함수(테스트 대상). claude 실행은
      주입 가능한 ``runner`` 로 격리한다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from typing import Any, Callable, List, Optional, Tuple

log = logging.getLogger("jad.repo_resolver")

# 작은 판단 호출이라 짧은 상한. config.run.repo_resolver_timeout_sec로 오버라이드.
DEFAULT_TIMEOUT_SEC = 60


# ---------------------------------------------------------------------------
# central 자기 토큰 레이트/사용량 한도(축1) — 별개 예외 + 보수적 신호 탐지
# ---------------------------------------------------------------------------


class CentralAIRateLimited(Exception):
    """central *자기* Claude 토큰의 레이트/사용량 한도 신호(축1).

    일반 리졸브 실패(정적/``[]`` 폴백)와 **구분되는** 예외다. claude 호출이 429/
    rate limit/usage limit/overloaded/quota 같은 **명확한 한도 신호**를 보였을 때만
    올린다 — 그 외 실패(타임아웃·파싱오류·기타 rc)는 기존 best-effort 폴백을 유지한다.

    ``reset_at`` 이 파싱되면(예: ``retry-after: N`` → now+N) 쿨다운 만료 epoch(초)로
    전달한다. 없으면 호출부가 기본 쿨다운을 건다. **시크릿/토큰 값은 절대 싣지 않는다.**
    """

    def __init__(self, reset_at: Optional[float] = None, detail: str = "") -> None:
        super().__init__(detail or "central AI rate/usage limited")
        self.reset_at = reset_at


# 레이트/사용량 한도로 볼 **명확한** 신호만(보수적). 대소문자·구분자(-,_,공백) 관대.
_RATE_LIMIT_PATTERNS = (
    re.compile(r"(?<!\d)429(?!\d)"),            # HTTP 429(앞뒤가 숫자면 제외 — 오탐 방지)
    re.compile(r"rate[\s_-]*limit", re.IGNORECASE),
    re.compile(r"usage[\s_-]*limit", re.IGNORECASE),
    re.compile(r"overloaded", re.IGNORECASE),
    re.compile(r"quota", re.IGNORECASE),
)
# retry-after / retry after: N(초) — 있으면 상대 시간을 절대 epoch로 환산.
_RETRY_AFTER = re.compile(r"retry[\s_-]*after[\"'\s:=]*(\d+)", re.IGNORECASE)


def detect_central_rate_limit(
    text: str, *, now_fn: Callable[[], float] = time.time
) -> Tuple[bool, Optional[float]]:
    """텍스트(claude JSON result/stderr/에러 메시지)에서 한도 신호를 보수적으로 탐지.

    Returns:
        ``(limited, reset_at)`` — 한도 신호가 있으면 ``(True, reset_at|None)``,
        없으면 ``(False, None)``. ``reset_at`` 은 ``retry-after`` 초를 ``now_fn()`` 에
        더한 절대 epoch(파싱 실패/부재면 None).
    """
    if not text:
        return (False, None)
    if not any(p.search(text) for p in _RATE_LIMIT_PATTERNS):
        return (False, None)
    reset_at: Optional[float] = None
    m = _RETRY_AFTER.search(text)
    if m:
        try:
            reset_at = float(now_fn()) + float(m.group(1))
        except (ValueError, TypeError, OverflowError):
            reset_at = None
    return (True, reset_at)


def _output_rate_limit(
    output: str, *, now_fn: Callable[[], float] = time.time
) -> Tuple[bool, Optional[float]]:
    """claude가 **정상 종료(exit 0)** 했지만 출력이 한도 에러 봉투인지 판별(보수적).

    정상 result(슬러그 배열)를 한도로 오탐하지 않도록, JSON 봉투는 ``is_error`` 가
    참일 때만 신호를 본다. JSON이 아니면(순수 텍스트 에러) 전체를 본다.
    """
    text = (output or "").strip()
    if not text:
        return (False, None)
    try:
        env = json.loads(text)
    except (ValueError, TypeError):
        env = None
    if isinstance(env, dict):
        if not env.get("is_error"):
            return (False, None)  # 정상 봉투 → 오탐 방지
        inner = env.get("result") if isinstance(env.get("result"), str) else ""
        subtype = str(env.get("subtype", ""))
        return detect_central_rate_limit(f"{inner} {subtype}", now_fn=now_fn)
    return detect_central_rate_limit(text, now_fn=now_fn)

# REPO-MAP 표의 첫 컬럼(슬러그) 추출용. 예: ``| `portal-frontend` | ... |``
# 백틱 유무 모두 허용하고, 헤더/구분선(---) 행은 걸러낸다.
_TABLE_ROW = re.compile(r"^\s*\|(?P<first>[^|]*)\|")
_SLUG_CLEAN = re.compile(r"[`*_\s]")

# 텍스트에 섞인 JSON 배열(중첩 없는 단순 문자열 배열) 추출용.
_ARRAY = re.compile(r"\[[^\[\]]*\]")


# ---------------------------------------------------------------------------
# 순수 함수 (테스트 대상)
# ---------------------------------------------------------------------------


def extract_valid_slugs(repo_map_md: str) -> List[str]:
    """REPO-MAP.md 마크다운 표에서 **슬러그(첫 컬럼)** 목록을 추출.

    표의 각 데이터 행 첫 컬럼(백틱/강조 제거)을 슬러그로 본다. 헤더 행
    (``슬러그`` 라벨)·구분선(``---``)·빈 셀은 제외한다. 순서 보존·중복 제거.
    """
    if not repo_map_md:
        return []
    slugs: List[str] = []
    seen: set = set()
    for line in repo_map_md.splitlines():
        m = _TABLE_ROW.match(line)
        if not m:
            continue
        cell = m.group("first")
        slug = _SLUG_CLEAN.sub("", cell).strip()
        if not slug:
            continue
        # 헤더/구분선 행 제외.
        if slug in ("슬러그", "slug", "Slug"):
            continue
        if set(slug) <= set("-:"):  # ``---`` 류 구분선
            continue
        if slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def build_prompt(issue: dict, repo_map_md: str) -> str:
    """티켓 + REPO-MAP 표 → claude 판단 프롬프트(순수).

    티켓 key·summary·description·components·labels 와 REPO-MAP.md 표를 주고,
    "작업이 닿는 레포 **슬러그**를 빠짐없이 JSON 배열로" 응답하도록 지시한다.
    카테고리·범위 표현('전체 프론트엔드' 등)은 REPO-MAP 태그·설명 근거로 전개한다.
    """
    fields = (issue or {}).get("fields", {}) or {}
    key = (issue or {}).get("key", "") or ""
    summary = fields.get("summary", "") or ""
    description = _text_of(fields.get("description")) or ""

    comps = []
    for comp in fields.get("components", []) or []:
        name = comp.get("name") if isinstance(comp, dict) else comp
        if name:
            comps.append(str(name))
    labels = [str(x) for x in (fields.get("labels", []) or []) if x]

    # description은 과하면 절삭(판단엔 앞부분이면 충분, 토큰 절약).
    if len(description) > 2000:
        description = description[:2000] + " …(생략)"

    lines = [
        "너는 이슈 트래커 티켓을 읽고 그 작업이 **어느 코드 레포에 닿는지**를",
        "판단하는 라우터다. 아래 REPO-MAP(레포 인벤토리)과 티켓을 보고, 이 티켓",
        "작업이 실제 코드 변경을 일으킬 레포를 **빠짐없이 모두** 고른다.",
        "central이 티켓의 전체 범위를 락 걸어야 하므로, 티켓이 닿는 레포를 하나도",
        "빠뜨리면 안 된다(대표 1개만 고르는 것은 오류다).",
        "",
        "규칙:",
        "- 반드시 REPO-MAP 표에 있는 슬러그만 사용한다(표에 없는 이름 금지).",
        "- 이 티켓 작업이 실제 코드 변경을 일으킬 레포를 **빠짐없이 모두** 고른다.",
        "  여러 레포에 걸치는 게 명백하면 그 레포를 전부 넣는다(1개로 줄이지 않는다).",
        "- **카테고리/범위 전개**: 티켓이 '전체/모든 프론트엔드 레포', '모든 백엔드',",
        "  '프론트 전부' 같이 카테고리나 범위를 지정하면, REPO-MAP의 **태그·설명**을",
        "  근거로 그 카테고리에 해당하는 레포를 **전부** 포함한다.",
        "  예: '전체 프론트엔드 레포' → REPO-MAP에서 frontend/web/shell 태그를 가진",
        "  레포 전부(portal-frontend, marketplace-frontend, metapage-frontend 등).",
        "  단, 문서/스토리보드성 레포(예: *-screens, docs 태그)는 실제 코드 변경",
        "  대상이 아니면 제외한다.",
        "- 범위가 진짜로 불명확하면(어느 레포인지 특정 불가) 빈 배열 []을 반환한다",
        "  (추측 금지). 단 이는 '범위 불명'일 때이지, '여러 레포에 걸침이 명백'할",
        "  때가 아니다 — 명백하면 해당 레포를 모두 넣는다.",
        "- 설명·주석·코드펜스 없이 **JSON 배열 한 줄만** 출력한다.",
        '  예: ["portal-frontend"]  또는  ["portal-frontend","marketplace-frontend","metapage-frontend"]  또는  []',
        "",
        "=== REPO-MAP ===",
        repo_map_md.strip(),
        "",
        "=== 티켓 ===",
        f"key: {key}",
        f"summary: {summary}",
        f"components: {', '.join(comps) if comps else '(없음)'}",
        f"labels: {', '.join(labels) if labels else '(없음)'}",
        "description:",
        description if description else "(없음)",
        "",
        "=== 출력(JSON 배열 한 줄) ===",
    ]
    return "\n".join(lines)


def parse_repos(claude_output: str, valid_slugs: List[str]) -> List[str]:
    """claude 출력에서 슬러그 JSON 배열을 파싱하고 REPO-MAP 슬러그로 검증.

    관용:
        - ``claude -p --output-format json`` 봉투(``{"result": "..."}``)면
          ``result`` 텍스트를 먼저 꺼낸다.
        - 잡음이 섞인 텍스트여도 첫 JSON 배열(``[...]``)만 뽑아 파싱한다.
        - 배열 원소 중 **valid_slugs에 없는 값은 제거**(미지 슬러그·환각 방어).
        - 순서 보존·중복 제거. 파싱 불가면 ``[]`` (호출부가 폴백 판단).
    """
    valid = set(valid_slugs or [])
    text = (claude_output or "").strip()
    if not text:
        return []

    # 1) claude json 봉투 언랩(있으면).
    try:
        env = json.loads(text)
        if isinstance(env, dict):
            inner = env.get("result")
            if isinstance(inner, str):
                text = inner.strip()
        elif isinstance(env, list):
            # 이미 순수 배열이면 바로 사용.
            return _filter_slugs(env, valid)
    except (ValueError, TypeError):
        pass

    # 2) 텍스트에서 첫 JSON 배열 추출 후 파싱.
    m = _ARRAY.search(text)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except (ValueError, TypeError):
        return []
    if not isinstance(arr, list):
        return []
    return _filter_slugs(arr, valid)


def _filter_slugs(arr: Any, valid: set) -> List[str]:
    """리스트 원소를 문자열화·정리 후 valid 슬러그만 남긴다(순서·중복 제거)."""
    out: List[str] = []
    seen: set = set()
    for item in arr:
        if not isinstance(item, str):
            continue
        slug = _SLUG_CLEAN.sub("", item).strip()
        if not slug or slug in seen:
            continue
        if valid and slug not in valid:
            continue  # 미지 슬러그(환각) 제거
        seen.add(slug)
        out.append(slug)
    return out


def _text_of(value: Any) -> str:
    """description이 ADF(dict)·문자열 어느 쪽이든 텍스트로 평탄화(best-effort)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    # Atlassian Document Format(dict) — text 노드만 재귀 수집.
    parts: List[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            t = node.get("text")
            if isinstance(t, str):
                parts.append(t)
            for child in node.get("content", []) or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# claude 실행 (주입 가능한 runner로 격리)
# ---------------------------------------------------------------------------


def _default_runner(cmd: List[str], timeout: int) -> str:
    """기본 runner — ``subprocess.run`` 으로 claude를 실행하고 stdout 반환.

    실패(비-0 종료·타임아웃)면 예외를 올린다(호출부가 폴백). stdin은 닫는다.
    """
    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if (cp.returncode or 0) != 0:
        detail = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(f"claude rc={cp.returncode}: {detail[:200]}")
    return cp.stdout or ""


def resolve_target_repos_llm(
    issue: dict,
    repo_map_md: str,
    *,
    runner: Optional[Callable[[List[str], int], str]] = None,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    claude_bin: str = "claude",
    fallback: Optional[List[str]] = None,
    now_fn: Callable[[], float] = time.time,
) -> List[str]:
    """티켓 → target_repos 를 claude로 판단(best-effort).

    흐름: valid_slugs 추출 → 프롬프트 빌드 → ``claude -p --output-format json
    --dangerously-skip-permissions`` 실행 → parse_repos.

    best-effort 폴백:
        - REPO-MAP이 비었거나 슬러그가 없으면 → claude 호출 없이 ``fallback``.
        - claude 실패/타임아웃/파싱오류(예외)면 → ``fallback`` + 사유 로깅.
        - claude가 정상 응답했으면 그 결과(빈 배열 포함)를 그대로 반환한다
          (빈 배열 = "불명확" 이라는 유효한 판단 → 스케줄러가 전역 직렬 처리).

    ⚠️ **예외 — central 자기 토큰 한도(축1):** claude 호출이 429/rate limit/usage
    limit/overloaded/quota 같은 **명확한 한도 신호**를 보이면 폴백 대신
    :class:`CentralAIRateLimited` 를 올린다(호출부가 쿨다운/pending 처리). 그 외
    실패는 기존 best-effort 폴백 그대로다(한도가 아닌 오류를 스로틀로 오인하지 않는다).

    Args:
        issue: Jira 이슈 dict(fields.summary/description/components/labels 사용).
        repo_map_md: dlc-meta REPO-MAP.md 원문.
        runner: ``(cmd, timeout) -> stdout`` 실행자(테스트 주입). 기본 subprocess.
        timeout: claude 호출 상한(초).
        claude_bin: claude 실행 바이너리 경로(config.run.claude_bin).
        fallback: 실패/미해석 시 돌려줄 정적 목록(없으면 ``[]``).
        now_fn: 한도 ``retry-after`` → 절대 epoch 환산용 시계(주입, 기본 time.time).

    Raises:
        CentralAIRateLimited: claude가 자기 토큰 레이트/사용량 한도 신호를 보인 경우.
    """
    fb = list(fallback or [])
    valid_slugs = extract_valid_slugs(repo_map_md)
    if not valid_slugs:
        # REPO-MAP 없음/파싱 실패 → claude 부르지 않고 정적 폴백.
        log.info("repo_resolver: REPO-MAP 슬러그 없음 → 정적 폴백 사용")
        return fb

    prompt = build_prompt(issue, repo_map_md)
    cmd = [
        claude_bin,
        "-p",
        prompt,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
    ]

    run = runner or _default_runner
    key = (issue or {}).get("key", "?")
    try:
        output = run(cmd, timeout)
    except CentralAIRateLimited:
        raise  # runner가 직접 한도로 판단해 올린 경우 그대로 전파(호출부 쿨다운).
    except Exception as exc:  # noqa: BLE001 — best-effort: 폴러 스레드 보호
        # 한도 신호(429/rate limit/…)면 별개 예외로 전파해 쿨다운을 걸게 한다.
        # 그 외 실패는 기존대로 정적 폴백(예외 메시지엔 프롬프트/시크릿 미노출).
        detail = str(exc)
        limited, reset_at = detect_central_rate_limit(detail, now_fn=now_fn)
        if limited:
            log.warning("repo_resolver: central AI 레이트/사용량 한도 감지(%s) → 쿨다운", key)
            raise CentralAIRateLimited(reset_at=reset_at, detail=type(exc).__name__) from None
        log.warning(
            "repo_resolver: claude 판단 실패(%s: %s) → 정적 폴백",
            key, type(exc).__name__,
        )
        return fb

    # exit 0 이지만 출력이 한도 에러 봉투(is_error + overloaded 등)일 수도 있다.
    limited, reset_at = _output_rate_limit(output, now_fn=now_fn)
    if limited:
        log.warning("repo_resolver: central AI 한도 감지(출력 봉투, %s) → 쿨다운", key)
        raise CentralAIRateLimited(reset_at=reset_at, detail="output signal")

    repos = parse_repos(output, valid_slugs)
    log.info("repo_resolver: %s → target_repos=%s", key, repos)
    return repos


# ---------------------------------------------------------------------------
# dlc-meta REPO-MAP 로딩 (best-effort 신선화)
# ---------------------------------------------------------------------------


def _repo_map_path(run: Any) -> str:
    """central이 REPO-MAP을 읽을 **공유 워크스페이스** dlc-meta 경로.

    우선순위: ``run.repo_map_path`` → 없으면 ``<run.workspace_dir>/dlc-meta``.
    공유 워크스페이스(jad-workspace 볼륨) 안 단일 클론을 가리킨다(설계 §4).
    """
    import os

    explicit = (getattr(run, "repo_map_path", "") if run else "") or ""
    if explicit:
        return explicit
    ws = (getattr(run, "workspace_dir", "") if run else "") or ""
    return os.path.join(ws, "dlc-meta") if ws else ""


def load_repo_map_md(
    config: Any,
    forge_token: Optional[str],
    *,
    provision_fn: Optional[Callable[..., str]] = None,
    runner: Callable = subprocess.run,
) -> str:
    """dlc-meta를 clone/pull(best-effort)한 뒤 ``<dlc-meta>/REPO-MAP.md`` 를 읽는다.

    클론/pull 대상은 **공유 워크스페이스 안의 단일 클론**이다(설계 §4 "공유
    워크스페이스 하나 = 단일 pull 지점"). 경로는 config ``run.repo_map_path``
    (기본 ``<run.workspace_dir>/dlc-meta``), 리모트는 ``run.dlc_meta_repo_url``.
    리졸브 **전에** 신선화(pull)하되, 실패해도 예외를 올리지 않고 이미 체크아웃된
    파일(있으면 stale)을 읽는다. 파일이 없으면 ``""``.

    Args:
        config: ``config.run.*`` 를 갖는 AppConfig.
        forge_token: central forge 토큰(없으면 provision skip, 기존 파일만 읽음).
        provision_fn: ``(path, url, token, runner=..., forge_kind=...) -> str``
            (테스트 주입). 기본은 :func:`app.repos.provision_one`.
        runner: git 실행자(기본 subprocess.run).
    """
    import os

    run = getattr(config, "run", None)
    path = _repo_map_path(run)
    url = (getattr(run, "dlc_meta_repo_url", "") if run else "") or ""

    if path and url and forge_token:
        from app import forge  # 지연 import(순환 방지)

        fn = provision_fn
        if fn is None:
            from app.repos import provision_one as fn  # 지연 import(순환 방지)
        try:
            # forge_kind: 중립 호스트(GHE 등)에서 토큰 URL 자격 사용자명을 맞추는 힌트.
            status = fn(path, url, forge_token, runner=runner,
                        forge_kind=forge.resolve_kind(config))
            log.info("repo_resolver: dlc-meta 신선화: %s", status)
        except Exception as exc:  # noqa: BLE001 — best-effort, stale라도 읽는다
            log.warning("repo_resolver: dlc-meta 신선화 실패(%s) → 기존 파일 사용",
                        type(exc).__name__)
    elif not forge_token:
        log.info("repo_resolver: central forge 토큰 없음 → dlc-meta pull 생략(기존 파일만)")

    if not path:
        return ""
    repo_map_path = os.path.join(path, "REPO-MAP.md")
    if not os.path.exists(repo_map_path):
        log.info("repo_resolver: REPO-MAP.md 없음(%s) → 정적 폴백 경로", repo_map_path)
        return ""
    try:
        with open(repo_map_path, "r", encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        log.warning("repo_resolver: REPO-MAP.md 읽기 실패(%s)", type(exc).__name__)
        return ""
