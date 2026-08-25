"""forge(코드 호스팅) 어댑터 — GitLab/GitHub 차이를 **한곳에** 모은다.

역할:
    이 시스템은 브랜치를 push 하고 변경요청(GitLab=MR / GitHub=PR)을 만든다. 그 동작이
    지금까지 **GitLab 에 하드코딩**돼 있었다 — clone/push URL 의 자격 사용자명(``oauth2:``),
    변경요청 URL 모양(``/-/merge_requests/N``), 에이전트 프롬프트의 "GitLab"·"MR" 표현,
    변경요청 닫기 API. 다른 조직은 GitHub 을 쓰므로 그대로는 **인증부터 실패**한다
    (GitHub 의 자격 사용자명은 ``x-access-token``).

    이 모듈은 그 forge 종속 지식의 **단일 원천**이다. 호출부는 forge 종류를 직접 비교하지
    말고 여기 함수를 쓴다(:mod:`app.notify` 의 notifier provider 어댑터와 같은 결).

역할 소속: **공유**(central·worker 양쪽에서 쓴다).

forge 종류 판정(:func:`kind_for`) — 우선순위와 그 이유:
    1. **URL 호스트 추론**(``github``/``gitlab`` 이 호스트명에 들어있을 때만 — 결정적일 때만)
    2. 명시 ``kind`` 인자
    3. ``config.forge.kind``
    4. :data:`DEFAULT_KIND` (=``gitlab`` — 기존 배포의 동작)

    ⚠️ 1이 2·3보다 앞서는 것이 핵심이다. 한 배포가 **여러 forge 를 섞어 쓴다** — 예를 들어
    공개 프레임워크 레포는 github.com 에 있고 사내 dlc-meta 는 사내 GitLab 에 있다
    (``config/config.example.yaml`` 의 기본값이 정확히 그 모양이다). 그러므로 "이 URL 이
    어느 forge 것인가"는 설정 전역값이 아니라 **그 URL 자신**이 가장 잘 안다. 호스트명이
    아무 힌트도 주지 않을 때(GitHub Enterprise 가 ``git.corp.example.com`` 인 경우 등)만
    설정값으로 내려간다.

시크릿 규율:
    이 모듈은 토큰 **값**을 로그·예외에 절대 남기지 않는다. :func:`with_token` 은 토큰을
    반환 URL 에만 싣고, 마스킹은 호출부(:func:`app.repos._mask`)가 :data:`CRED_USERNAMES`
    를 이용해 수행한다 — forge 를 추가하면 자격 사용자명이 여기 한 곳에서 늘어나므로
    마스킹도 자동으로 함께 넓어진다(마스킹 누락 회귀 방지).

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app.setup_schema import FORGE_KINDS

log = logging.getLogger("jad.forge")

# --- 종류 식별자(정본은 app/setup_schema.FORGE_KINDS) ------------------------
KIND_GITLAB = "gitlab"
KIND_GITHUB = "github"

#: 종류를 못 정했을 때의 기본 — 기존 배포가 전부 GitLab 이었으므로 gitlab 이다(무동작변경).
DEFAULT_KIND = KIND_GITLAB

# --- forge별 상수표 ----------------------------------------------------------

#: HTTP(S) clone/push URL 의 **자격 사용자명**(``https://<사용자명>:<토큰>@host/...``).
#: GitLab 은 PAT 를 ``oauth2`` 사용자로 받고, GitHub 은 ``x-access-token`` 을 쓴다.
_TOKEN_USERNAME = {
    KIND_GITLAB: "oauth2",
    KIND_GITHUB: "x-access-token",
}

#: 마스킹이 알아야 하는 자격 사용자명 전체(:func:`app.repos._mask` 가 소비).
CRED_USERNAMES: tuple = tuple(sorted(set(_TOKEN_USERNAME.values())))

#: 사람이 읽는 forge 이름(프롬프트·로그 문구용).
_LABEL = {KIND_GITLAB: "GitLab", KIND_GITHUB: "GitHub"}

#: 변경요청 약어/정식 명칭(프롬프트·알림 문구용).
_CHANGE_ABBR = {KIND_GITLAB: "MR", KIND_GITHUB: "PR"}
_CHANGE_TERM = {KIND_GITLAB: "Merge Request", KIND_GITHUB: "Pull Request"}

#: 호스트명에 이 조각이 들어 있으면 그 forge 로 **결정**한다(순서 = 검사 순서).
_HOST_HINTS = ((KIND_GITHUB, "github"), (KIND_GITLAB, "gitlab"))

# --- 변경요청 URL 패턴 -------------------------------------------------------
# GitLab: /-/merge_requests/N (신형) 또는 /merge_requests/N (구형·경로 변형)
_GITLAB_CHANGE_URL = re.compile(
    r"https?://\S+?/-/merge_requests/\d+"
    r"|https?://\S+?/merge_requests/\d+",
    re.IGNORECASE,
)
# GitHub: /pull/N
_GITHUB_CHANGE_URL = re.compile(r"https?://\S+?/pull/\d+", re.IGNORECASE)
# forge 를 모를 때(또는 네이티브 패턴이 안 맞을 때)의 통합 폴백.
_ANY_CHANGE_URL = re.compile(
    _GITLAB_CHANGE_URL.pattern + "|" + _GITHUB_CHANGE_URL.pattern,
    re.IGNORECASE,
)

_CHANGE_URL_PATTERN = {
    KIND_GITLAB: _GITLAB_CHANGE_URL,
    KIND_GITHUB: _GITHUB_CHANGE_URL,
}

# 스킴(scheme://) 분리 — app.repos 와 같은 규칙(중복 정의가 아니라 여기서 호스트만 뗀다).
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)(.*)$")


# --- 종류 판정 ---------------------------------------------------------------


def normalize_kind(value: Any) -> Optional[str]:
    """자유 입력을 알려진 forge 종류로 정규화(모르면 None).

    ``None`` 을 돌려주는 것이 중요하다 — 호출부가 "모른다"와 "gitlab 이다"를 구분해
    다음 폴백 단계로 내려갈 수 있게 한다.
    """
    kind = str(value or "").strip().lower()
    return kind if kind in FORGE_KINDS else None


def resolve_kind(config: Any) -> str:
    """이 설정이 쓰는 forge 종류. 설정이 없거나 모르는 값이면 :data:`DEFAULT_KIND`.

    정본은 ``config.forge.kind``(``app/config.py`` 가 채운다). ``forge`` 섹션이 없는
    **옛 설정 객체**(레거시 config·테스트 대역)면 gitlab 으로 본다 — 그 시절 유일한
    구현이 GitLab 이었고, 기존 배포의 동작을 그대로 유지하는 값이다.
    """
    fc = getattr(config, "forge", None)
    return normalize_kind(getattr(fc, "kind", "")) or DEFAULT_KIND


def _host(url: str) -> str:
    """URL(또는 scp 형식 ``git@host:path``)에서 호스트명만 소문자로 추출(못 뽑으면 "")."""
    raw = str(url or "").strip()
    if not raw:
        return ""
    m = _SCHEME.match(raw)
    rest = m.group(2) if m else raw
    if "@" in rest:                       # userinfo(oauth2:tok@ / git@) 제거
        rest = rest.split("@", 1)[1]
    # 경로/포트/scp 구분자 앞까지가 호스트.
    for sep in ("/", ":"):
        if sep in rest:
            rest = rest.split(sep, 1)[0]
    return rest.strip().lower()


def infer_kind_from_url(url: str) -> Optional[str]:
    """URL 호스트명으로 forge 종류를 **결정적일 때만** 추론(아니면 None).

    ``github.com``·``github.corp.example.com``·``gitlab.example.com`` 처럼 호스트명이
    스스로 밝히는 경우만 판정한다. ``git.corp.example.com`` 같은 중립 호스트는 None 을
    돌려 상위 폴백(명시 인자 → 설정 → 기본값)에 맡긴다 — 추측으로 인증을 깨뜨리지 않는다.
    """
    host = _host(url)
    if not host:
        return None
    for kind, hint in _HOST_HINTS:
        if hint in host:
            return kind
    return None


def kind_for(*, url: Optional[str] = None, kind: Any = None, config: Any = None) -> str:
    """이 작업의 forge 종류 확정 — URL 추론 > 명시 kind > 설정 > 기본값.

    우선순위의 근거는 모듈 docstring 참조(한 배포가 여러 forge 를 섞어 쓴다).
    """
    inferred = infer_kind_from_url(url) if url else None
    if inferred:
        return inferred
    explicit = normalize_kind(kind)
    if explicit:
        return explicit
    if config is not None:
        return resolve_kind(config)
    return DEFAULT_KIND


# --- forge별 동작 ------------------------------------------------------------


def token_username(kind: Any = None) -> str:
    """HTTP(S) 토큰 URL 의 자격 사용자명(gitlab=``oauth2`` / github=``x-access-token``).

    모르는 종류면 기본 forge 의 값을 쓴다(조용한 실패보다 오늘의 동작 유지).
    """
    return _TOKEN_USERNAME.get(normalize_kind(kind) or DEFAULT_KIND,
                               _TOKEN_USERNAME[DEFAULT_KIND])


def with_token(url: str, token: str, *, kind: Any = None, config: Any = None) -> str:
    """URL 권한부(authority) 앞에 ``<자격사용자명>:<token>@`` 을 주입(스킴 보존).

    기존 자격정보가 있으면 제거하고 재주입한다. 스킴이 없으면 원문 그대로 반환한다
    (scp 형식·상대 경로에 토큰을 억지로 끼워 깨진 URL 을 만들지 않는다).

    ⚠️ 토큰은 **반환 URL 에만** 실린다 — 이 함수는 로그를 남기지 않는다.
    """
    m = _SCHEME.match(url or "")
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    if "@" in rest:  # 기존 user[:pass]@ 제거
        rest = rest.split("@", 1)[1]
    username = token_username(kind_for(url=url, kind=kind, config=config))
    return f"{scheme}{username}:{token}@{rest}"


def label(kind: Any = None) -> str:
    """사람이 읽는 forge 이름("GitLab"|"GitHub") — 프롬프트·로그 문구용."""
    return _LABEL.get(normalize_kind(kind) or DEFAULT_KIND, _LABEL[DEFAULT_KIND])


def change_abbr(kind: Any = None) -> str:
    """변경요청 약어("MR"|"PR")."""
    return _CHANGE_ABBR.get(normalize_kind(kind) or DEFAULT_KIND, _CHANGE_ABBR[DEFAULT_KIND])


def change_term(kind: Any = None) -> str:
    """변경요청 정식 명칭("Merge Request"|"Pull Request")."""
    return _CHANGE_TERM.get(normalize_kind(kind) or DEFAULT_KIND, _CHANGE_TERM[DEFAULT_KIND])


def change_url_pattern(kind: Any = None):
    """그 forge 의 변경요청 URL 정규식(모르면 통합 패턴)."""
    k = normalize_kind(kind)
    return _CHANGE_URL_PATTERN.get(k, _ANY_CHANGE_URL) if k else _ANY_CHANGE_URL


def search_change_url(text: str, kind: Any = None) -> Optional[str]:
    """텍스트에서 변경요청(MR/PR) URL 을 찾는다 — **네이티브 우선, 통합 폴백**.

    forge 를 아는 경우 그 forge 의 패턴을 먼저 훑는다. 그래야 에이전트 로그에 다른
    forge 의 링크가 섞여 있어도 **이 배포의 산출물**이 우선 잡힌다. 네이티브가 없으면
    통합 패턴으로 한 번 더 훑어(레포별로 forge 가 다를 수 있으므로) 놓치지 않는다.
    """
    body = str(text or "")
    if not body:
        return None
    k = normalize_kind(kind)
    if k:
        m = _CHANGE_URL_PATTERN[k].search(body)
        if m:
            return m.group(0)
    m = _ANY_CHANGE_URL.search(body)
    return m.group(0) if m else None
