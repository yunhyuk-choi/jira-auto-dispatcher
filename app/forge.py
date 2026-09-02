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

base URL 판정(:func:`resolve_base_url`) — ⚠️ **토큰이 나갈 곳을 정하는 일이다**:
    ``forge.base_url`` 이 비어 있으면 예전에는 곧바로 SaaS 기본 엔드포인트
    (``https://gitlab.com``)로 떨어졌다. 사내 GitLab 을 쓰는 팀이 그 값을 안 적으면
    **사내 PAT 가 gitlab.com 으로 전송된다** — 진단이 실패하는 게 문제가 아니라 *토큰이
    외부로 나가는 것*이 문제다. 그래서 base URL 도 :func:`infer_kind_from_url` 과 같은
    방식으로 **설정된 레포 URL 에서 유도**하고, 유도조차 못 하면 SaaS 로 보내는 대신
    **판단 불가**를 돌려준다(부르는 쪽이 SKIP 한다 — 엉뚱한 곳에 토큰을 보내느니 검사를
    못 하는 편이 낫다).

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
from dataclasses import dataclass
from typing import Any, Optional

from app.setup_schema import FORGE_KINDS

log = logging.getLogger("jad.forge")

# --- 종류 식별자(정본은 app/setup_schema.FORGE_KINDS) ------------------------
KIND_GITLAB = "gitlab"
KIND_GITHUB = "github"

# 변경요청 닫기 API 호출 타임아웃(초) — 취소 롤백은 부수적이므로 짧게 클램프.
CLOSE_TIMEOUT_SEC = 30

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


# --- base URL 판정(토큰이 나갈 곳) --------------------------------------------

#: 각 forge 의 **SaaS 호스트**. 레포 URL 이 이 호스트면 self-hosted 가 아니라는 뜻이므로
#: base_url 은 빈 채로 두고(각 forge 의 SaaS 기본 엔드포인트를 쓴다) "확인됨"만 기록한다.
#: ⚠️ GitHub SaaS 의 API 는 ``api.github.com`` 이라 ``https://github.com`` 을 base_url 로
#: 채우면 오히려 틀린다 — 그래서 SaaS 는 "유도"가 아니라 "확인"이다.
_SAAS_HOSTS = {
    KIND_GITLAB: ("gitlab.com", "www.gitlab.com"),
    KIND_GITHUB: ("github.com", "www.github.com", "api.github.com"),
}

#: base_url 유도의 근거로 삼는 **설정된 레포 URL** 키(우선 순서 = 신뢰 순서).
#: ⚠️ ``run.orchestrator_repo_url`` 은 일부러 뺐다 — 그건 공개 프레임워크 레포의 **고정
#: 리터럴**(github.com)이라 이 조직이 어느 forge 를 쓰는지에 대한 증거가 아니다. 그걸
#: 근거로 삼으면 GitHub Enterprise 배포가 "SaaS 확인됨"으로 오판된다.
BASE_URL_SOURCE_KEYS: tuple = ("run.dlc_meta_repo_url", "run.docs_repo_url")

SOURCE_CONFIG = "config"          # forge.base_url 에 사람이 적었다
SOURCE_DERIVED = "derived"        # 레포 URL 에서 유도했다(self-hosted)
SOURCE_SAAS = "saas"              # 레포 URL 이 SaaS 호스트임을 확인했다(기본 엔드포인트)
SOURCE_UNRESOLVED = "unresolved"  # self-hosted 로 보이는데 base URL 을 뽑을 수 없다(ssh URL 등)
SOURCE_NONE = ""                  # 근거가 전혀 없다(레포 URL 이 하나도 설정되지 않았다)


@dataclass(frozen=True)
class BaseUrlResolution:
    """forge base URL 판정 결과 — **값과 그 근거를 함께** 돌려준다.

    Attributes:
        base_url: self-hosted base URL. ``""`` 는 "SaaS 기본 엔드포인트를 쓴다"(``source``
            가 :data:`SOURCE_SAAS`)이거나 **판단 불가**(그 밖)라는 뜻이다 — 부르는 쪽은
            반드시 ``source`` 를 함께 봐야 한다.
        source: :data:`SOURCE_CONFIG` | :data:`SOURCE_DERIVED` | :data:`SOURCE_SAAS` |
            :data:`SOURCE_UNRESOLVED` | :data:`SOURCE_NONE`.
        origin: 근거가 된 설정 키(``forge.base_url`` · ``run.dlc_meta_repo_url`` …).
        host: 근거 URL 의 호스트(진단 메시지용 — 토큰이 어디로 갈지 사람에게 보여준다).
    """

    base_url: str = ""
    source: str = SOURCE_NONE
    origin: str = ""
    host: str = ""

    @property
    def usable(self) -> bool:
        """이 판정으로 **실제 요청을 보내도 되는가**.

        SaaS 임이 확인됐거나 base URL 을 손에 쥐었을 때만 참이다. 근거가 없거나
        self-hosted 인데 주소를 모르면 거짓 — 그 경우 요청을 보내지 **않는** 것이 맞다.
        """
        return bool(self.base_url) or self.source == SOURCE_SAAS


def _authority(url: str) -> str:
    """URL 에서 ``호스트[:포트]`` 만(userinfo·경로 제거). 못 뽑으면 ""."""
    raw = str(url or "").strip()
    m = _SCHEME.match(raw)
    if not m:
        return ""                          # 스킴이 없으면(scp 형식 등) 주소를 지어내지 않는다
    rest = m.group(2)
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return rest.split("/", 1)[0].strip()


def base_url_from_url(url: str) -> str:
    """레포 URL → ``스킴://호스트[:포트]`` 의 base URL(못 뽑으면 "").

    :func:`infer_kind_from_url` 과 같은 결이다 — "이 배포의 forge 가 어디 있는가"는
    설정 전역값보다 **그 URL 자신**이 잘 안다. 스킴이 없는 scp 형식
    (``git@host:group/repo.git``)은 http/https 를 **추측하지 않고** "" 를 돌려준다.
    """
    raw = str(url or "").strip()
    m = _SCHEME.match(raw)
    authority = _authority(raw)
    if not m or not authority:
        return ""
    scheme = m.group(1).lower()
    if not scheme.startswith(("http://", "https://")):
        return ""                          # ssh://·git:// 는 API base URL 이 아니다
    return f"{scheme}{authority}".rstrip("/")


def is_saas_url(url: str, kind: Any = None) -> bool:
    """이 URL 의 호스트가 그 forge 의 **SaaS 호스트**인가(gitlab.com·github.com)."""
    host = _host(url)
    if not host:
        return False
    k = normalize_kind(kind) or infer_kind_from_url(url) or DEFAULT_KIND
    return host in _SAAS_HOSTS.get(k, ())


def _config_value(config: Any, dotted: str) -> str:
    """``run.dlc_meta_repo_url`` 같은 점 표기 경로를 안전하게 읽는다(없으면 "")."""
    node: Any = config
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return ""
    return str(node or "").strip()


def resolve_base_url(config: Any, *, kind: Any = None) -> BaseUrlResolution:
    """이 설정의 forge base URL 과 **그 근거**를 확정한다(순수 — I/O 없음).

    우선순위:
        1. ``forge.base_url`` 명시값 → :data:`SOURCE_CONFIG`
        2. 설정된 레포 URL(:data:`BASE_URL_SOURCE_KEYS`) 중 **이 forge 것**인 첫 URL
           - SaaS 호스트면 → :data:`SOURCE_SAAS` (base_url 은 빈 채로 — 위 ``_SAAS_HOSTS`` 주석)
           - 아니면 스킴+호스트를 유도 → :data:`SOURCE_DERIVED`
           - http(s) 가 아니라 유도할 수 없으면 → :data:`SOURCE_UNRESOLVED`
        3. 아무 근거도 없으면 → :data:`SOURCE_NONE`

    "이 forge 것"의 판정은 :func:`infer_kind_from_url` 이다. 호스트가 **다른** forge 를
    분명히 말하면(설정은 gitlab 인데 URL 은 github.com) 그 URL 은 건너뛴다 — 한 배포가
    여러 forge 를 섞어 쓰기 때문이다(모듈 docstring). 호스트가 아무 말도 안 하면
    (``git.corp.example.com``) 그건 사내 forge 일 가능성이 높으므로 근거로 받아들인다 —
    실제로 이 토큰이 그 호스트로 나가고 있는 URL 이다.
    """
    k = normalize_kind(kind) or resolve_kind(config)
    explicit = _config_value(config, "forge.base_url").rstrip("/")
    if explicit:
        # 로더가 이미 채운 값이면 그때의 **근거를 그대로 물려준다**(멱등) — 그러지 않으면
        # "레포 URL 에서 유도했다"가 두 번째 호출에서 "사람이 적었다"로 바뀐다.
        recorded = _config_value(config, "forge.base_url_source")
        origin = _config_value(config, "forge.base_url_origin") or "forge.base_url"
        return BaseUrlResolution(explicit, recorded or SOURCE_CONFIG, origin,
                                 _host(explicit))

    unresolved: Optional[BaseUrlResolution] = None
    for key in BASE_URL_SOURCE_KEYS:
        url = _config_value(config, key)
        if not url:
            continue
        inferred = infer_kind_from_url(url)
        if inferred is not None and inferred != k:
            continue                       # 다른 forge 의 레포 — 이 토큰의 근거가 아니다
        host = _host(url)
        if not host:
            continue
        if host in _SAAS_HOSTS.get(k, ()):
            return BaseUrlResolution("", SOURCE_SAAS, key, host)
        derived = base_url_from_url(url)
        if derived:
            return BaseUrlResolution(derived, SOURCE_DERIVED, key, host)
        if unresolved is None:             # self-hosted 신호는 잡았으나 주소를 못 뽑았다
            unresolved = BaseUrlResolution("", SOURCE_UNRESOLVED, key, host)
    return unresolved or BaseUrlResolution()


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


# --- 변경요청 닫기(취소 롤백 프리미티브) ------------------------------------
#
# ⚠️ **현재 파이썬 호출자는 없다.** 취소 롤백(브랜치 삭제 + 변경요청 닫기)은 워커 폴링
# 루프(app/worker.py)의 일부였고, 그 루프가 프랙탈 센트럴 세션으로 대체되며 함께 은퇴
# 했다 — 지금은 워커 컨테이너 안의 에이전트가 취소 지시를 받아 스스로 되돌린다.
# 그럼에도 이 프리미티브를 forge 어댑터에 남기는 이유는, forge 중립성(GitLab MR /
# GitHub PR 의 API 모양 차이)이 **여기 말고는 어디에도 기록돼 있지 않기** 때문이다.
# 파이썬 경로에서 롤백을 다시 하게 되면 이 함수가 그 자리다.


def _requests():
    """HTTP 클라이언트(requests 모듈). 지연 import — 테스트 격리·로드 오버헤드 회피."""
    import requests  # noqa: PLC0415

    return requests


def _close_gitlab_mr(url: str, token: str, *, http=None) -> bool:
    """GitLab MR 닫기(``PUT …/merge_requests/{iid}?state_event=close``).

    URL 모양이 GitLab MR 이 아니면 False(호출부가 best-effort 로 흡수).
    """
    import urllib.parse  # noqa: PLC0415

    # url 예: https://gitlab.example.com/group/proj/-/merge_requests/7
    marker = "/-/merge_requests/"
    if marker not in url:
        return False
    left, iid = url.split(marker, 1)
    iid = iid.strip("/").split("/")[0]
    _scheme_host, _, project_path = left.partition("://")[2].partition("/")
    if not project_path:
        return False
    base = left.split("/", 3)  # [scheme:, '', host, project_path]
    host = base[2] if len(base) >= 3 else ""
    api = (f"https://{host}/api/v4/projects/"
           f"{urllib.parse.quote_plus(project_path)}/merge_requests/{iid}")
    client = http if http is not None else _requests()
    resp = client.put(api, params={"state_event": "close"},
                      headers={"PRIVATE-TOKEN": token}, timeout=CLOSE_TIMEOUT_SEC)
    return getattr(resp, "status_code", 500) < 400


def _close_github_pr(url: str, token: str, *, http=None) -> bool:
    """GitHub PR 닫기(``PATCH /repos/{owner}/{repo}/pulls/{n}`` state=closed).

    GitHub.com 은 ``api.github.com``, GHE 는 ``<host>/api/v3`` 가 API 루트다.
    URL 모양이 GitHub PR 이 아니면 False(호출부가 best-effort 로 흡수).
    """
    # url 예: https://github.com/owner/repo/pull/7
    marker = "/pull/"
    if marker not in url:
        return False
    left, number = url.split(marker, 1)
    number = number.strip("/").split("/")[0]
    host, _, repo_path = left.partition("://")[2].partition("/")
    if not host or repo_path.count("/") < 1:
        return False
    owner, _, repo = repo_path.partition("/")
    repo = repo.split("/")[0]
    root = "https://api.github.com" if host.lower() == "github.com" else f"https://{host}/api/v3"
    api = f"{root}/repos/{owner}/{repo}/pulls/{number}"
    client = http if http is not None else _requests()
    resp = client.patch(
        api, json={"state": "closed"},
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json"},
        timeout=CLOSE_TIMEOUT_SEC,
    )
    return getattr(resp, "status_code", 500) < 400


def close_change_request(url: str, token: str, *, config: Any = None,
                         kind: Any = None, http=None) -> bool:
    """변경요청(GitLab MR / GitHub PR)을 닫는다 — best-effort → 성공 여부 bool.

    forge 판정은 **그 URL 자신**이 우선한다(:func:`kind_for`) — 이미 만들어진 링크를
    되돌리는 일이라 링크의 모양이 가장 믿을 만한 근거다. 호스트가 중립이면
    ``config.forge.kind`` 로 내려간다. 토큰이 없거나 URL 모양이 안 맞으면 False.

    ⚠️ 토큰은 헤더로만 실린다(URL·로그에 남기지 않는다).
    """
    if not (url or "").strip() or not (token or "").strip():
        return False
    if kind_for(url=url, kind=kind, config=config) == KIND_GITHUB:
        return _close_github_pr(url, token, http=http)
    return _close_gitlab_mr(url, token, http=http)
