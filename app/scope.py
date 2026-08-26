"""프로젝트 범위(scope) — "이 티켓을 이 사람이 받아도 되는가"의 단일 원천(중앙 전용).

역할:
    Jira 티켓을 등록 사용자에게 라우팅하는 **모든 경로**(폴러 전진축·웹훅·상태 감시축)가
    같은 두 가지 판단을 공유하게 한다.

        1. **JQL 을 어느 프로젝트에 던질 것인가** — 전 사용자 유효 scope 의 **합집합**.
        2. **수신한 티켓을 이 담당자에게 줘도 되는가** — 그 티켓의 프로젝트 키가 그
           담당자의 유효 scope 안에 있는가(합집합 통과 ≠ 개인 통과).

왜 합집합 + 수신 후 게이트인가(설계 결정):
    사용자별 ``OR`` 절을 조합하면 JQL 이 사용자 수에 비례해 커지고(``(assignee = X AND
    project in (…)) OR (assignee = Y AND project in (…)) OR …``) 디버깅이 어려워진다.
    합집합으로 **한 번** 던지고, 이미 존재하는 *담당자 accountId → 등록 사용자* 매핑
    단계에서 한 번 더 거르면 JQL 은 단순한 채로 per-user scope 가 정확히 작동한다.
    비용은 "남의 프로젝트 티켓을 한 번 받아서 버리는 것"뿐이고, 그건 dedup 게이트를
    잡기 **전** 이라 부작용이 없다.

⚠️ 이 모듈이 **유일한 매핑 지점**이다:
    :func:`resolve_user_in_scope` 는 담당자 accountId 추출 → 레지스트리 조회(enabled) →
    범위 게이트를 한 함수에 묶는다. 폴러·웹훅·워처가 각자 ``registry.get_by_account_id``
    를 부르던 시절에는 한 곳만 고치면 다른 경로로 범위 밖 티켓이 새어 들어왔다. 그래서
    ``app/`` 안에서 ``get_by_account_id`` 를 부르는 곳은 **여기뿐**이며,
    ``tests/test_scope.py`` 가 그것을 기계적으로 지킨다.

빈 scope 의 의미 — "제한 없음"이 아니라 **"인스턴스 기본값 상속"**:
    ``scope.projects`` 가 비어 있으면 인스턴스 기본값(:func:`instance_projects` —
    ``jira.project`` + ``jira.projects``)을 상속한다. 이 필드가 없던 시절에 등록된
    ``registry.json`` 이 그대로 동작하기 위한 규칙이다(그 사용자들은 지금까지 정확히
    인스턴스 기본 프로젝트만 받아 왔다).

⚠️ JQL 인젝션:
    프로젝트 키는 **설정 파일과 온보딩 폼**에서 온다 — 즉 JQL 문자열로 흘러 들어가는
    외부 입력이다. :data:`PROJECT_KEY_RE` 를 통과하지 못한 값은 조립 전에 **버린다**
    (이스케이프가 아니라 거부다 — 따옴표·괄호·``OR`` 가 들어간 값은 애초에 Jira 프로젝트
    키가 아니다). 그래서 :func:`project_clause` 가 만드는 절에는 구조 문자가 남을 수 없다.

역할 소속: **central** (worker 는 Jira 를 직접 보지 않는다).

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger("jad.scope")

#: 허용하는 Jira 프로젝트 키 모양 — 영문자로 시작하는 영숫자/밑줄.
#:
#: Jira Cloud 의 실제 규칙은 이보다 좁지만(대문자 2~10자), 조직마다 예외가 있고 좁게
#: 잡아 정상 키를 버리는 쪽이 더 나쁜 실패다. 여기서 필요한 것은 "JQL 구조를 깰 수 있는
#: 문자가 하나도 없다"는 보장이고 이 패턴이 그것을 준다(따옴표·괄호·공백·백슬래시 전부 불가).
PROJECT_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")


# ---------------------------------------------------------------------------
# 정규화 — 문자열/리스트 → 검증된 프로젝트 키 목록
# ---------------------------------------------------------------------------

def is_valid_project_key(value: Any) -> bool:
    """JQL 에 그대로 실어도 안전한 프로젝트 키 모양인가."""
    return bool(isinstance(value, str) and PROJECT_KEY_RE.match(value.strip()))


def normalize_projects(raw: Any, *, source: str = "") -> list:
    """``"A, B"`` / ``["A", "B"]`` → 검증된 키 목록(중복 제거, 입력 순서 유지).

    모양이 어긋난 값은 **버리고 경고를 남긴다** — 조용히 통과시키면 JQL 이 깨지거나(400)
    더 나쁘게는 의도치 않은 범위로 넓어진다. 비교는 대소문자 무시로 하되 **표기는 입력
    그대로** 둔다(운영자가 적은 대로 로그·JQL 에 보이는 편이 추적하기 쉽다).

    Args:
        raw: 문자열(쉼표구분) · 리스트/튜플 · None.
        source: 경고에 붙일 출처 표시(예: ``"jira.project"``, ``"user:yh"``).
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = [x for x in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        items = list(raw)
    else:
        items = [raw]

    out: list = []
    seen: set = set()
    bad: list = []
    for item in items:
        text = str(item).strip() if item is not None else ""
        if not text:
            continue
        if not is_valid_project_key(text):
            bad.append(text)
            continue
        if text.upper() in seen:
            continue
        seen.add(text.upper())
        out.append(text)
    if bad:
        # 값 자체를 싣는다 — 시크릿이 아니라 프로젝트 키이고, 무엇이 거부됐는지 보이지
        # 않으면 "왜 내 티켓이 안 잡히지"를 추적할 수 없다.
        log.warning("프로젝트 키 모양이 아니어서 무시했습니다%s: %s",
                    f" ({source})" if source else "", ", ".join(repr(b) for b in bad))
    return out


# ---------------------------------------------------------------------------
# 인스턴스 기본값 / 사용자 유효 범위 / 합집합
# ---------------------------------------------------------------------------

def instance_projects(config: Any) -> list:
    """인스턴스 기본 프로젝트 목록 — ``jira.project``(대표) + ``jira.projects``(추가).

    ``jira.project`` 는 예전부터 **문자열 하나**였고 지금도 그렇다(대표 프로젝트 —
    discover·doctor·온보딩 안내가 스칼라로 소비한다). 복수는 ``jira.projects`` 로 받으며
    대표 프로젝트는 **항상 포함**된다. 옛 config 객체(``projects`` 속성 없음)도 그대로
    동작한다.
    """
    jira = getattr(config, "jira", None)
    if jira is None:
        return []
    primary = normalize_projects(getattr(jira, "project", ""), source="jira.project")
    extra = normalize_projects(getattr(jira, "projects", None), source="jira.projects")
    return normalize_projects([*primary, *extra])


def user_projects(user: Any, defaults: list) -> list:
    """이 사용자의 **유효** 프로젝트 범위 — 비어 있으면 인스턴스 기본값을 상속.

    ⚠️ 빈 scope 는 "제한 없음"이 아니다. 기존 배포(scope 필드가 없던 시절의
    ``registry.json``)가 지금까지 받아 온 것이 정확히 인스턴스 기본 프로젝트이므로,
    빈 값은 그 동작을 그대로 유지하는 **상속**으로 해석한다.
    """
    own = normalize_projects(
        getattr(getattr(user, "scope", None), "projects", None),
        source=f"user:{getattr(user, 'username', '?')}",
    )
    return own if own else normalize_projects(defaults)


def union_projects(registry: Any, config: Any) -> list:
    """enabled 사용자 전원의 유효 범위 **합집합**(폴러 JQL 의 project 절 원천).

    ``jira_account_id`` 가 없는 사용자는 애초에 담당자로 매칭될 수 없으므로 제외한다
    (:meth:`app.poller.Poller._enabled_account_ids` 와 같은 기준).
    """
    defaults = instance_projects(config)
    merged: list = []
    for user in registry.list_users():
        if not getattr(user, "enabled", False) or not getattr(user, "jira_account_id", ""):
            continue
        merged.extend(user_projects(user, defaults))
    return normalize_projects(merged)


# ---------------------------------------------------------------------------
# 티켓 → 프로젝트 키 / 범위 게이트
# ---------------------------------------------------------------------------

def project_key_of(key: str = "", issue: Optional[dict] = None) -> str:
    """티켓의 프로젝트 키 — ``fields.project.key`` 우선, 없으면 이슈 키 접두사.

    Jira 응답에 ``project`` 필드를 요청했으면 그것이 정확하다. 요청하지 않았거나 응답이
    옛 모양이면 이슈 키(``ABC-123``)의 앞부분으로 폴백한다 — 이슈 키 접두사는 곧 프로젝트
    키라는 것이 Jira 의 불변식이다. 둘 다 없으면 빈 문자열(호출부가 "판정 불가"로 다룬다).
    """
    fields = (issue or {}).get("fields") or {}
    project = fields.get("project")
    if isinstance(project, dict) and project.get("key"):
        return str(project["key"]).strip()
    raw = str(key or (issue or {}).get("key") or "").strip()
    return raw.split("-", 1)[0] if "-" in raw else ""


def in_user_scope(key: str, issue: Optional[dict], user: Any, config: Any) -> bool:
    """이 티켓의 프로젝트가 이 사용자의 유효 범위 안에 있는가(대소문자 무시).

    범위를 확정할 수 없으면 **False** 다:
        - 유효 범위가 비었다(= 사용자 scope 도 인스턴스 기본값도 없다) → 무엇을 받아야
          할지 아무도 말하지 않았다. 이때 통과시키면 **전 프로젝트**를 긁는다.
        - 티켓의 프로젝트 키를 못 읽었다 → 판정 근거가 없다.
    둘 다 "조용히 넓어지느니 조용하지 않게 좁힌다"는 선택이며, 호출부가 이유를 로그로 남긴다.
    """
    allowed = user_projects(user, instance_projects(config))
    if not allowed:
        return False
    pk = project_key_of(key, issue)
    if not pk:
        return False
    return pk.upper() in {a.upper() for a in allowed}


def resolve_user_in_scope(registry: Any, config: Any, key: str, issue: Optional[dict]):
    """담당자 accountId → enabled 등록 사용자 → **범위 게이트**까지의 단일 수렴점.

    폴러(전진축·웹훅 트리거·회복 드레인)·얇은 웹훅 엔드포인트·상태 감시축(재오픈)이 전부
    이 함수를 부른다. 매핑과 게이트를 갈라 놓으면 한쪽 경로만 고쳐지고 다른 경로로 범위
    밖 티켓이 새어 들어온다(그게 이 함수가 존재하는 이유다).

    Returns:
        (사용자, 사유) 튜플. 통과하면 ``(UserRecord, "")``, 아니면 ``(None, 사유)``.
        사유는 ``"no-assignee"`` / ``"unmapped-or-disabled"`` / ``"out-of-scope"``.
    """
    fields = (issue or {}).get("fields") or {}
    assignee = fields.get("assignee") or {}
    account_id = assignee.get("accountId") if isinstance(assignee, dict) else None
    if not account_id:
        return None, "no-assignee"
    user = registry.get_by_account_id(account_id)
    if user is None:
        return None, "unmapped-or-disabled"
    if not in_user_scope(key, issue, user, config):
        log.info(
            "범위 밖 티켓 — skip: %s (프로젝트=%s, user=%s, 유효 scope=%s)",
            key or (issue or {}).get("key", ""),
            project_key_of(key, issue) or "(판정 불가)",
            user.username,
            user_projects(user, instance_projects(config)) or "(비어 있음)",
        )
        return None, "out-of-scope"
    return user, ""


# ---------------------------------------------------------------------------
# JQL 조립
# ---------------------------------------------------------------------------

def project_clause(projects: list) -> str:
    """``project in ("A", "B")`` 절(빈 목록이면 빈 문자열).

    원소는 :func:`normalize_projects` 로 한 번 더 거른다 — 이 함수만 보고도 "JQL 에 구조
    문자가 실릴 수 없다"가 참이어야 한다(호출부의 성실함에 기대지 않는다).
    """
    keys = normalize_projects(projects)
    if not keys:
        return ""
    return "project in (" + ", ".join(f'"{k}"' for k in keys) + ")"


# ---------------------------------------------------------------------------
# 온보딩 — "기본 프로젝트를 포함할지 + 추가할 프로젝트"
# ---------------------------------------------------------------------------

class ScopeChoiceError(ValueError):
    """온보딩 범위 선택이 아무 프로젝트도 남기지 않을 때(사람에게 되물어야 한다)."""


def resolve_onboarding_projects(defaults: list, include_default: bool, extra: Any) -> list:
    """온보딩 답변(포함 여부 + 추가 목록) → 레지스트리에 저장할 ``scope.projects``.

    네 경우:
        - 기본 포함 ✓, 추가 없음 → ``[]`` (**상속**). 빈 값으로 두는 것이 의도다 —
          나중에 인스턴스 기본 프로젝트가 늘어나면 이 사용자도 자동으로 따라간다.
        - 기본 포함 ✓, 추가 있음 → ``기본 + 추가`` (명시 목록). 이 경우에만 기본값이
          레코드에 **고정**된다 — 기본이 바뀌면 이 사용자는 따라가지 않는다.
        - 기본 제외 ✗, 추가 있음 → ``추가`` 만.
        - 기본 제외 ✗, 추가 없음 → :class:`ScopeChoiceError` (받을 티켓이 없는 등록).

    Raises:
        ScopeChoiceError: 결과가 빈 범위가 되는데 상속으로도 해석할 수 없을 때.
    """
    extras = normalize_projects(extra, source="onboarding.scope")
    if include_default:
        if not extras:
            return []          # 상속 — 인스턴스 기본값을 따라간다
        return normalize_projects([*normalize_projects(defaults), *extras])
    if not extras:
        raise ScopeChoiceError(
            "받을 프로젝트가 하나도 없습니다 — 기본 프로젝트를 포함하거나 "
            "추가 프로젝트 키를 적어 주세요."
        )
    return extras
