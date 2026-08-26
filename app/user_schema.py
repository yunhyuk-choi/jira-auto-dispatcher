"""합류자(per-user) 온보딩 스키마 — "팀원에게 무엇을 묻고, 그 값을 어디서 구하는가".

이 시스템의 사용자는 **둘**이다:

    1. **팀 최초 설치자** — 인스턴스를 세운다. 그 사람이 답할 것은
       :mod:`app.setup_schema` 가 선언하고 설치 관문 CLI(``python -m app.setup``)가
       강제한다.
    2. **이후 합류하는 팀원** — 이미 도는 인스턴스에 **자기 자격증명을 등록**한다.
       그게 관리 UI 온보딩(``POST /onboard``)의 몫이고, 이 모듈이 그 선언이다.

왜 별도 스키마인가:
    두 사람이 답하는 것은 **필드 집합이 다르다**(인스턴스 설정 vs 개인 자격증명).
    스키마를 억지로 하나로 합치면 어느 쪽에도 맞지 않는 필드가 생긴다. 그래서 스키마는
    둘로 두되 **같은 자료구조**(:class:`app.setup_schema.SchemaSection` /
    :class:`~app.setup_schema.SchemaField`)로 선언한다 — 그래야 검증
    (:func:`app.setup_validate.validate_answers`)·렌더(관리 UI 폼)·안내(준비물 카드)가
    한 벌의 기계로 돈다. 검증기가 두 벌이 되는 순간 규칙은 갈라진다.

이 모듈이 하는 일 / 하지 않는 일:
    - **한다**: 필드 선언, 폼 입력값 정규화(:func:`coerce_answers`), 검증 호출
      (:func:`validate_user_answers`), **인스턴스 설정에서 유도한 준비물 안내**
      (:func:`build_guide`).
    - **하지 않는다**: 시크릿 저장·레지스트리 기록·HTTP. 그건 :mod:`app.onboarding` 이다.
      이 모듈은 I/O 도 전역 상태도 없다(``config`` 객체를 **읽기만** 한다).

안내를 하드코딩하지 않는다 — 무엇이 설정에서 유도되는가:
    - forge 종류(:func:`app.forge.resolve_kind`) → GitLab PAT / GitHub PAT **한쪽만**
      보여 준다. 토큰 발급 페이지 URL 도 ``forge.base_url`` 유도값
      (:func:`app.forge.resolve_base_url`)으로 조립한다 — 사내 GitLab 을 쓰는 팀에게
      gitlab.com 링크를 주지 않기 위해서다.
    - Jira 사이트 URL(``jira.base_url``) → 프로필 URL·API 토큰 발급 링크·
      ``accountId`` 확인 경로(:data:`app.jira_client.MYSELF_PATH`)를 그 사이트 기준으로.
    - dlc-meta / 프레임워크 레포 URL(``run.*``) → 2단 절차 안내의 실제 주소.
    필연적으로 **정적일 수밖에 없는 것**은 각 서비스의 화면 경로(예: GitLab 의
    "아바타 → Edit profile → Access Tokens")다. 그건 설정이 알 수 있는 정보가 아니라
    그 제품의 UI 사실이므로 :data:`FORGE_TOKEN_GUIDE` 같은 **forge 별 표**에 두고,
    설정은 *어느 표를 보여줄지*를 정한다.

⚠️ 시크릿 규율:
    이 스키마의 토큰 필드는 ``secret=True`` 다 — 값 자체가 시크릿이라는 선언이다.
    설치 스키마와 달리 **여기서는 값을 실제로 받는다**(합류자가 붙여넣는다). 대신
    :mod:`app.onboarding` 이 그 값을 ``secrets.base_dir/<user>/`` 아래 0600 파일로 쓰고
    레지스트리에는 **참조만** 남긴다. 그래서 검증기를 부를 때
    ``allow_secret_values=True`` 를 준다(그러지 않으면 "시크릿 값은 설정에 담지 않는다"
    규칙이 발동한다 — 그 규칙은 config.yaml 로 흘러가는 설치 답변용이다).
    **안내 문구·응답·로그에는 시크릿 값을 절대 싣지 않는다.**

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Mapping, Optional

from app import forge
from app import scope as scope_mod
from app import setup_schema as S
from app import setup_validate as V
from app.jira_client import MYSELF_PATH

# ---------------------------------------------------------------------------
# 키 상수 — 소비처(온보딩 API·UI·테스트)가 문자열을 다시 타이핑하지 않게
# ---------------------------------------------------------------------------

#: 풀 퍼미션 동의 항목(합류자 **본인**의 동의 — 설치자의 동의로 갈음하지 않는다).
CONSENT_KEY = "consent_full_permissions"

#: 동의 시각(감사 흔적). ⚠️ **서버가 수신 시각으로 채운다** — 클라이언트가 보낸 시각은
#: 신뢰하지 않는다(감사 흔적의 의미가 사라진다). 그래서 폼에 렌더하지 않는다.
CONSENT_AT_KEY = "consent_accepted_at"

#: 폼에 입력칸을 만들지 **않는** 필드(서버가 채운다).
SERVER_DERIVED_KEYS: frozenset = frozenset({CONSENT_AT_KEY})

#: 작업 범위 — **추가** 프로젝트 키 목록(쉼표구분). 비면 인스턴스 기본값을 상속한다.
SCOPE_KEY = "scope"

#: 작업 범위 — 인스턴스 기본 프로젝트를 포함할지(기본 True).
#:
#: 왜 두 필드인가: 예전에는 빈 칸에 ``<PROJECT_KEY>`` 자리표시자만 있어서 합류자는 무엇을
#: 적어야 하는지 알 수 없었다. 이 인스턴스가 실제로 감시하는 키를 **보여 주고**(안내는
#: :func:`build_guide` 가 설정에서 렌더한다) "그걸 포함할지 + 더 받을 게 있는지"만 물으면
#: 답할 수 있다.
SCOPE_INCLUDE_DEFAULT_KEY = "scope_include_default"

#: :func:`_field_card` 의 "예시를 덮지 않았다" 표식(``None`` 은 유효한 오버라이드 값이다).
_KEEP = object()


# ---------------------------------------------------------------------------
# forge 별 개인 토큰 안내 — "설정이 고르고, 표가 말한다"
# ---------------------------------------------------------------------------

#: forge 종류 → 개인 액세스 토큰 발급 안내.
#:
#: ``path`` 는 그 제품의 화면 경로(설정이 알 수 없는 UI 사실), ``scope`` 는 **이 시스템이
#: 실제로 하는 일**(브랜치 push + 변경요청 생성)에 필요한 최소 스코프,
#: ``settings_path`` 는 base URL 뒤에 붙여 링크를 만들 상대 경로다.
#:
#: ⚠️ 이 표는 :data:`app.setup_schema.FORGE_KINDS` 를 **전부** 덮어야 한다
#: (테스트가 지킨다) — forge 를 추가하고 안내를 빠뜨리면 그 배포의 합류자는 무엇을
#: 발급해야 하는지 알 수 없다.
FORGE_TOKEN_GUIDE: dict = {
    forge.KIND_GITLAB: {
        "path": "아바타 → Edit profile → Access Tokens → Add new token",
        "scope": "api",
        "scope_why": "브랜치 push 와 Merge Request 생성에 모두 필요하다(read_api 로는 부족).",
        "settings_path": "/-/user_settings/personal_access_tokens",
    },
    forge.KIND_GITHUB: {
        "path": "Settings → Developer settings → Personal access tokens → Generate new token",
        "scope": "repo",
        "scope_why": "브랜치 push 와 Pull Request 생성에 모두 필요하다(public_repo 로는 부족).",
        "settings_path": "/settings/tokens",
    },
}

#: Atlassian API 토큰 발급 페이지(계정 단위라 Jira 사이트 URL 과 무관한 **고정 주소**).
ATLASSIAN_TOKEN_URL = "https://id.atlassian.com/manage-profile/security/api-tokens"

#: 프레임워크 레포 URL 을 설정에서 못 읽었을 때의 폴백(공개 프레임워크 — 리터럴).
DEFAULT_FRAMEWORK_REPO_URL = "https://github.com/yunhyuk-choi/ai-dlc-orchestrator"


# ---------------------------------------------------------------------------
# 스키마 선언
# ---------------------------------------------------------------------------

_IDENTITY = S.SchemaSection(
    name="identity",
    title="신원",
    description=(
        "이 인스턴스 안에서 당신을 가리키는 이름들. 시크릿이 아니며 관리 UI 목록·컨테이너"
        "·커밋 author 에 쓰인다."
    ),
    fields=(
        S.SchemaField(
            key="username",
            type=S.FieldType.STRING,
            required=True,
            description=(
                "내부 식별자(공백 없이). worker 컨테이너 이름(``jad-worker-<username>``)과 "
                "시크릿 폴더명(``secrets/<username>/``)이 여기서 나온다. 등록 후에는 바꿀 "
                "수 없다."
            ),
            example="yhchoi",
        ),
        S.SchemaField(
            key="display_name",
            type=S.FieldType.STRING,
            default="",
            description="관리 UI 목록·알림에 보일 표시 이름. 비우면 username 을 쓴다.",
            example="최윤혁",
        ),
        S.SchemaField(
            key="git_name",
            type=S.FieldType.STRING,
            default="",
            description=(
                "커밋 author 이름. worker 가 당신 이름으로 커밋한다. forge 계정 이름과 "
                "같을 필요는 없다."
            ),
            example="Yunhyuk Choi",
        ),
        S.SchemaField(
            key="git_email",
            type=S.FieldType.STRING,
            default="",
            description=(
                "커밋 author 이메일. ⚠️ forge 계정에 **인증된 이메일**이어야 커밋이 당신 "
                "계정에 연결된다(아니면 커밋이 익명으로 남는다)."
            ),
            example="you@example.com",
        ),
        S.SchemaField(
            key="notify_user_id",
            type=S.FieldType.STRING,
            default="",
            legacy_keys=("google_chat_user_id",),
            description=(
                "(선택) 완료 알림 @멘션용 **알림 채널의 사용자 id**. 모양은 채널마다 다르다"
                "(Google Chat=숫자 userId / Slack=U…). 비우면 이름만 표시된다. "
                "알림을 안 쓰는 배포면 비워 둔다."
            ),
            example="1234567890",
        ),
    ),
)

_JIRA = S.SchemaSection(
    name="jira",
    title="Jira 자격증명",
    description=(
        "worker 안의 에이전트가 **당신 정체성으로** 티켓을 읽고 상태를 전이하고 코멘트를 "
        "남길 때 쓰는 자격. 감시 계정(central)의 자격과 별개다."
    ),
    fields=(
        S.SchemaField(
            key="jira_account_id",
            type=S.FieldType.STRING,
            required=True,
            description=(
                "당신의 Jira 계정 식별자. **폴러가 티켓 담당자를 당신에게 매핑하는 키**라 "
                "이게 틀리면 당신 티켓은 영원히 감지되지 않는다(에러도 안 난다). "
                f"확인: 이 사이트에서 ``GET {MYSELF_PATH}`` 의 ``accountId``, 또는 Jira "
                "프로필 URL 의 ``/people/<accountId>`` 뒷부분."
            ),
            example="557058:1a2b3c4d-5e6f-7a8b-9c0d-1e2f3a4b5c6d",
        ),
        S.SchemaField(
            key="jira_email",
            type=S.FieldType.STRING,
            required=True,
            description=(
                "당신의 Atlassian 계정 이메일. Jira Cloud 의 인증은 **(이메일, API 토큰) "
                "쌍**이라 토큰만으로는 인증되지 않는다 — 아래 토큰을 발급받은 그 계정의 "
                "이메일이어야 한다."
            ),
            example="you@example.com",
        ),
        S.SchemaField(
            key="jira_token",
            type=S.FieldType.STRING,
            required=True,
            secret=True,
            description=(
                "당신의 Jira API 토큰. Atlassian 계정 → Security → API tokens 에서 발급한다"
                f"({ATLASSIAN_TOKEN_URL}). 값은 서버에 0600 파일로만 저장되고 응답·로그·"
                "레지스트리에는 참조만 남는다."
            ),
        ),
    ),
)

_FORGE = S.SchemaSection(
    name="forge",
    title="코드 호스팅(forge) 개인 토큰",
    description=(
        "브랜치를 push 하고 변경요청(MR/PR)을 **당신 이름으로** 만들 때 쓰는 개인 토큰. "
        "⚠️ 이 값이 없으면 워커는 커밋까지는 하고 **변경요청을 만들지 못한다** — 에러 없이 "
        "반쪽만 도는, 이 시스템에서 가장 비싼 실패 모드다. 그래서 **필수**다."
    ),
    fields=(
        S.SchemaField(
            key="forge_token",
            type=S.FieldType.STRING,
            required=True,
            secret=True,
            legacy_keys=("gitlab_token",),
            description=(
                "개인 코드 호스팅 액세스 토큰(GitLab PAT / GitHub PAT). 발급 경로와 필요한 "
                "스코프는 이 인스턴스의 forge 종류에 따라 다르며, 관리 UI 의 준비물 안내가 "
                "그 배포에 맞는 쪽만 보여 준다(:data:`FORGE_TOKEN_GUIDE`)."
            ),
        ),
    ),
)

_CLAUDE = S.SchemaSection(
    name="claude",
    title="Claude 인증",
    description=(
        "worker 안에서 도는 코딩 에이전트가 쓸 **당신의** Claude 자격. 팀 공용 키가 아니라 "
        "개인 구독 토큰이다."
    ),
    fields=(
        S.SchemaField(
            key="claude_setup_token",
            type=S.FieldType.STRING,
            required=True,
            secret=True,
            description=(
                "**본인 머신**의 터미널에서 ``claude setup-token`` 을 실행해 나온 장기 토큰"
                "(Max 구독). worker 가 ``CLAUDE_CODE_OAUTH_TOKEN`` 으로 쓴다. "
                "⚠️ 재발급하면 기존 토큰이 무효화되니, 이미 쓰고 있다면 그 값을 재사용한다."
            ),
        ),
    ),
)

_WORK = S.SchemaSection(
    name="work",
    title="작업 모드",
    description="당신에게 배정된 티켓을 워커가 어떻게 처리할지.",
    fields=(
        S.SchemaField(
            key="autonomy_mode",
            type=S.FieldType.ENUM,
            choices=("A", "B"),
            default="B",
            description=(
                "A = 완전자율(코드 완성 + 변경요청 초안까지) / "
                "B = 경량 1차(브랜치·스캐폴딩·1차 시도까지 하고 변경요청은 만들지 않는다). "
                "처음에는 B 를 권한다. 두 모드 모두 티켓을 '완료'로 전이하지는 않는다."
            ),
            example="B",
        ),
        S.SchemaField(
            key="permission_level",
            type=S.FieldType.ENUM,
            choices=("bypass",),
            default="bypass",
            description=(
                "worker 컨테이너의 사전 인가 레벨. 헤드리스 자율 실행에는 ``bypass`` 가 "
                "필요하며 현재 유일한 값이다(SECURITY.md). 아래 동의 항목이 가리키는 "
                "바로 그 권한이다."
            ),
            example="bypass",
        ),
        S.SchemaField(
            key=SCOPE_INCLUDE_DEFAULT_KEY,
            type=S.FieldType.BOOL,
            default=True,
            description=(
                "이 인스턴스의 기본 프로젝트를 내 작업 범위에 포함한다. 대부분은 켠 채로 "
                "두면 된다 — 끄면 아래 '추가 프로젝트'에 적은 것만 받는다(둘 다 비면 "
                "받을 티켓이 없으므로 등록이 통과하지 않는다)."
            ),
        ),
        S.SchemaField(
            key=SCOPE_KEY,
            type=S.FieldType.STRING_LIST,
            default=[],
            description=(
                "**추가로** 받을 Jira 프로젝트 키(쉼표구분). 기본 프로젝트 외에 더 받을 게 "
                "없으면 비워 둔다 — 비워 두면 인스턴스 기본값을 그대로 상속하며, 나중에 "
                "기본 프로젝트가 늘어나도 자동으로 따라간다. ⚠️ 여기 적힌 프로젝트의 "
                "티켓만 당신 워커로 간다(폴러가 담당자 매핑 단계에서 범위를 확인한다)."
            ),
            example=["PROJ"],
        ),
    ),
)

_CONSENT = S.SchemaSection(
    name="consent",
    title="풀 퍼미션 동의 (본인)",
    description=(
        "⚠️ 등록하면 이 시스템은 **당신의** Jira·forge·Claude 자격증명으로 코딩 에이전트를 "
        "``--dangerously-skip-permissions`` 로 실행한다 — 사람의 매 단계 승인 없이 셸 실행·"
        "파일 쓰기·git push·티켓 전이가 일어나고, 그 흔적은 **당신 계정**에 남는다. "
        "설치자가 한 동의로 갈음하지 않는다. 본인의 명시 동의 없이는 등록이 통과하지 않는다."
    ),
    fields=(
        S.SchemaField(
            key=CONSENT_KEY,
            type=S.FieldType.BOOL,
            required=True,
            default=False,
            description=(
                "내 자격증명으로 자율 에이전트가 실행되는 것에 동의한다. 무엇에 동의하는지의 "
                "정본은 SECURITY.md 다."
            ),
        ),
        S.SchemaField(
            key=CONSENT_AT_KEY,
            type=S.FieldType.STRING,
            default="",
            description=(
                "동의 시각(ISO-8601). ⚠️ **서버가 수신 시각으로 기록한다** — 클라이언트가 "
                "보낸 값은 쓰지 않는다(그러면 감사 흔적이 아니다). 폼에 입력칸이 없다."
            ),
            example="2026-08-26T09:00:00+09:00",
        ),
    ),
)

#: 합류자 온보딩이 물어야 하는 항목 **전체**(선언 순서 = 폼 표시 순서).
USER_SCHEMA: tuple = (_IDENTITY, _JIRA, _FORGE, _CLAUDE, _WORK, _CONSENT)

#: 토큰을 붙여넣는 사람이 알아야 하는 **보관 규율**(필드 하나가 아니라 폼 전체에 걸린다).
#: 사실만 적는다 — 여기 적힌 것은 :func:`app.onboarding._write_secret` 과
#: :class:`app.registry.SecretsRef` 가 실제로 지키는 동작이다.
SECRETS_NOTE = (
    "붙여넣은 토큰 **값**은 서버의 secrets.base_dir/<username>/ 아래 0600 파일로만 "
    "저장되고, 레지스트리·응답·로그에는 **참조 경로만** 남는다. 등록은 안전 기본 "
    "enabled=false 로 시작하며, 운영자가 검토 후 활성화해야 워커가 뜬다."
)

#: 동의 미승인 오류의 힌트(합류자용 — 설치자용과 문구가 다르다).
CONSENT_HINT = (
    "이 등록은 당신의 Jira·forge·Claude 자격증명으로 자율 에이전트를 돌리는 것에 대한 "
    "동의입니다. 관리 UI 의 동의 체크박스를 켜고 다시 제출하세요(SECURITY.md)."
)


# ---------------------------------------------------------------------------
# 조회 헬퍼
# ---------------------------------------------------------------------------


def iter_fields():
    """선언 순서대로 모든 필드를 순회한다."""
    return S.iter_fields_in(USER_SCHEMA)


def get_field(key: str) -> Optional[S.SchemaField]:
    """키로 필드 조회(없으면 None)."""
    return S.get_field_in(USER_SCHEMA, key)


def required_keys() -> tuple:
    """무조건 필수인 필드 키들(선언 순서)."""
    return tuple(f.key for f in iter_fields() if f.required)


def secret_keys() -> tuple:
    """값 자체가 시크릿인 필드 키들 — 로그·응답에 절대 싣지 않을 대상."""
    return tuple(f.key for f in iter_fields() if f.secret)


# ---------------------------------------------------------------------------
# 입력 정규화 — 폼은 전부 문자열로 온다
# ---------------------------------------------------------------------------

#: 문자열로 온 참/거짓의 해석(HTML 체크박스는 ``on``, 옛 스크립트는 ``1``·``true``).
_TRUE_WORDS = frozenset({"true", "1", "on", "yes", "y"})
_FALSE_WORDS = frozenset({"false", "0", "off", "no", "n", ""})


def _coerce_one(f: S.SchemaField, value: Any) -> Any:
    """선언 타입에 맞춰 값 하나를 정규화(못 바꾸면 **원본 그대로** — 검증기가 잡는다).

    ⚠️ 모르는 모양을 억지로 통과시키지 않는다. 예컨대 ``consent_full_permissions: "네"``
    는 True 로 바꾸지 않고 그대로 둬서 검증기가 타입 오류로 잡게 한다 — 동의 값을
    관대하게 해석하는 것은 동의를 받지 않는 것과 같다.
    """
    t = f.type
    if isinstance(value, str):
        raw = value.strip()
        if t is S.FieldType.BOOL:
            low = raw.lower()
            if low in _TRUE_WORDS:
                return True
            if low in _FALSE_WORDS:
                return False
            return raw
        if t is S.FieldType.INT:
            try:
                return int(raw)
            except ValueError:
                return raw
        if t is S.FieldType.STRING_LIST:
            return [x.strip() for x in raw.split(",") if x.strip()]
        return raw
    if t is S.FieldType.STRING_LIST and isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return value


def coerce_answers(raw: Mapping) -> dict:
    """관리 UI 폼/JSON 답변을 **선언 타입으로** 정규화한 평평한 dict 로 만든다.

    폼 인코딩은 모든 값을 문자열로 준다(체크박스는 ``"on"``, 목록은 쉼표 문자열).
    검증기는 타입에 엄격하므로(그게 게이트의 값이다) 그 사이를 여기서 메운다.
    레거시 필드 이름(``gitlab_token``·``google_chat_user_id``)도 그대로 통과시킨다 —
    :func:`app.setup_validate.resolve_values` 가 ``legacy_keys`` 선언을 보고 신규 키로
    수렴시키며 경고를 남긴다.

    ⚠️ 선언되지 않은 키도 버리지 않고 그대로 남긴다(호출부가 자기 목적으로 쓸 수 있다).
    """
    out: dict = {}
    for key, value in dict(raw or {}).items():
        f = get_field(str(key))
        if f is None:
            # 레거시 별칭이면 그 필드의 타입으로 정규화한다.
            target = S.legacy_key_map_in(USER_SCHEMA).get(str(key))
            f = get_field(target) if target else None
        out[str(key)] = _coerce_one(f, value) if f is not None else value
    return out


def now_iso() -> str:
    """현재 시각을 로컬 오프셋 포함 ISO-8601 문자열로(동의 시각 기록용).

    ⚠️ 로케일 의존 포맷을 쓰지 않는다(POLICY-ENCODING) — 항상 ISO-8601 이다.
    """
    return _dt.datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# 검증 — 설치 관문과 **같은 검증기**를 스키마만 갈아 끼워 부른다
# ---------------------------------------------------------------------------


def validate_user_answers(raw: Mapping) -> V.ValidationResult:
    """합류자 답변을 :data:`USER_SCHEMA` 에 대고 검증한다(**공개 진입점**).

    :func:`app.setup_validate.validate_answers` 를 그대로 쓴다 — 필수·조건부 필수·타입·
    허용값·동의 게이트의 판정 논리가 설치 관문과 **한 벌**로 남게 하기 위해서다.
    다른 점은 셋뿐이며 전부 인자로 표현된다:

        - ``sections=USER_SCHEMA`` — 필드 집합이 다르다.
        - ``allow_secret_values=True`` — 여기서는 토큰 **값**을 실제로 받는다(모듈 상단
          시크릿 규율 참조).
        - ``consent_key=CONSENT_KEY`` — 동의하는 사람이 설치자가 아니라 **본인**이다.

    ``closed_sections=()`` 인 이유: 이 스키마의 키는 점 표기가 아니라 평평한 폼 필드
    이름이라 "섹션 안의 오타"라는 개념이 없다. 그리고 관리 UI 는 스키마에 없는 값
    (예: 향후 확장 필드)을 함께 보낼 수 있어야 한다.

    Returns:
        :class:`app.setup_validate.ValidationResult` — ``findings[].key`` 가 폼 입력칸
        이름과 **같으므로** UI 가 칸별 오류로 그대로 매핑할 수 있다.
    """
    return V.validate_answers(
        raw,
        sections=USER_SCHEMA,
        consent_key=CONSENT_KEY,
        consent_hint=CONSENT_HINT,
        accepted_at_key=CONSENT_AT_KEY,
        allow_secret_values=True,
        closed_sections=(),
    )


def missing_keys(result: V.ValidationResult) -> list:
    """검증 결과에서 **누락**으로 잡힌 키만(옛 응답 형식 ``missing`` 하위호환)."""
    return [f.key for f in result.errors
            if f.code in (V.CODE_MISSING_REQUIRED, V.CODE_MISSING_REQUIRED_IF)]


# ---------------------------------------------------------------------------
# 준비물 안내 — **인스턴스 설정에서 렌더**한다
# ---------------------------------------------------------------------------


def _cfg(config: Any, dotted: str, default: str = "") -> str:
    """``jira.base_url`` 같은 점 표기 경로를 안전하게 읽는다(없으면 default).

    테스트·임베드 조립은 ``SimpleNamespace(secrets=...)`` 처럼 **일부 섹션만** 있는
    config 를 준다 — 안내는 그런 환경에서도 절대 예외를 내지 않아야 한다(안내가 못 뜨는
    것과 관리 UI 가 500 으로 죽는 것은 다르다).
    """
    node: Any = config
    for part in dotted.split("."):
        node = getattr(node, part, None)
        if node is None:
            return default
    return str(node or "").strip() or default


def forge_guide(config: Any) -> dict:
    """이 배포의 forge 에 맞는 **개인 토큰 발급 안내**(설정이 고르고 표가 말한다).

    Returns:
        ``{kind, label, change_abbr, path, scope, scope_why, url, base_url, base_url_source}``.
        ``url`` 은 토큰 발급 페이지 링크 — self-hosted 면 그 호스트로, SaaS 면 공개
        호스트로 조립한다. 근거를 못 잡으면 빈 문자열이며(추측한 주소로 사람을 보내지
        않는다) UI 는 경로 설명만 보여 준다.
    """
    kind = forge.resolve_kind(config)
    table = FORGE_TOKEN_GUIDE.get(kind, {})
    resolution = forge.resolve_base_url(config, kind=kind)
    if resolution.base_url:
        origin = resolution.base_url
    elif resolution.source == forge.SOURCE_SAAS:
        origin = "https://gitlab.com" if kind == forge.KIND_GITLAB else "https://github.com"
    else:
        origin = ""
    settings_path = str(table.get("settings_path", ""))
    return {
        "kind": kind,
        "label": forge.label(kind),
        "change_abbr": forge.change_abbr(kind),
        "change_term": forge.change_term(kind),
        "path": str(table.get("path", "")),
        "scope": str(table.get("scope", "")),
        "scope_why": str(table.get("scope_why", "")),
        "url": (origin + settings_path) if (origin and settings_path) else "",
        "base_url": resolution.base_url,
        "base_url_source": resolution.source,
    }


def jira_guide(config: Any) -> dict:
    """이 배포의 Jira 사이트에 맞는 **개인 자격 안내**.

    Returns:
        ``{base_url, project, projects, myself_url, profile_hint, token_url}``.
        ``projects`` 는 이 인스턴스의 감시 프로젝트 전체 목록이다. ``base_url`` 이
        비어 있으면(설정 미완) 사이트 의존 링크는 빈 문자열로 남긴다.
    """
    base_url = _cfg(config, "jira.base_url").rstrip("/")
    return {
        "base_url": base_url,
        # 대표 프로젝트(스칼라 — 기존 소비처 그대로).
        "project": _cfg(config, "jira.project"),
        # 이 인스턴스가 실제로 감시하는 **전체** 프로젝트 키. 온보딩 범위 질문이 이걸
        # 보여 준다(하드코딩 금지 — 설정에서 렌더한다).
        "projects": scope_mod.instance_projects(config),
        "myself_url": (base_url + MYSELF_PATH) if base_url else "",
        "myself_path": MYSELF_PATH,
        "profile_hint": (
            "Jira 우상단 아바타 → 프로필 → 주소창의 ``/people/<accountId>`` 뒷부분"
        ),
        "token_url": ATLASSIAN_TOKEN_URL,
    }


def join_steps(config: Any) -> list:
    """합류자가 거쳐야 하는 **2단 절차**(로컬 + 웹) — 설정에서 실제 주소를 채운다.

    ⚠️ 이 시스템에 합류한다는 것은 웹 등록만이 아니다. 워커 안의 에이전트는
    ``ai-dlc-orchestrator`` 프레임워크의 오케스트레이터 정체성으로 동작하고, 그 정체성은
    **그 사람의 로컬 ``dlc-meta`` 클론**을 전제한다. 웹만 하고 끝내면 로컬 오케스트레이터가
    dlc-meta 도 정체성도 없는 상태로 남는다 — 어디에도 적혀 있지 않던 사실이라 여기에
    적는다.

    프레임워크 쪽 절차의 **정본은 프레임워크 레포**다. 그래서 여기서는 확실한 것만
    말하고(클론한 뒤 그 디렉토리에서 ``claude`` 를 띄우면 SETTER 가 합류 모드로 분기한다),
    세부는 그 레포의 안내를 따르라고 한다 — 남의 레포 절차를 여기서 복제하면 갈라진다.
    """
    framework_url = _cfg(config, "run.orchestrator_repo_url", DEFAULT_FRAMEWORK_REPO_URL)
    dlc_meta_url = _cfg(config, "run.dlc_meta_repo_url")
    return [
        {
            "id": "local",
            "order": 1,
            "title": "로컬 — 프레임워크 합류(SETTER)",
            "where": "본인 머신",
            "why": (
                "당신의 로컬 오케스트레이터가 정체성을 갖고 dlc-meta(사이클로그·REPO-MAP)에 "
                "닿게 하는 단계다. 이걸 건너뛰면 웹 등록이 끝나도 로컬에는 dlc-meta 도 "
                "정체성도 없다."
            ),
            "how": (
                f"프레임워크 레포({framework_url})를 clone 하고 그 디렉토리에서 ``claude`` 를 "
                "띄운다. 아직 부트스트랩되지 않은 환경이면 SETTER 가 **합류 모드**로 "
                "분기한다(기존 공유 원격 clone). 상세 절차는 그 레포의 안내를 따른다."
            ),
            "repo_url": framework_url,
            "dlc_meta_url": dlc_meta_url,
        },
        {
            "id": "web",
            "order": 2,
            "title": "웹 — 이 대시보드에 자격증명 등록",
            "where": "이 화면",
            "why": (
                "central 이 당신 워커를 띄우고, 당신에게 배정된 티켓을 당신 정체성으로 "
                "실행하게 하는 단계다."
            ),
            "how": (
                "아래 준비물을 손에 쥔 뒤 온보딩 폼을 제출한다. 등록은 안전을 위해 "
                "``enabled=false`` 로 시작하며, 운영자가 검토 후 활성화하면 워커가 뜬다."
            ),
        },
    ]


def _field_card(f: S.SchemaField, note: str = "", link: str = "",
                link_label: str = "", example: Any = _KEEP) -> dict:
    """필드 선언 하나 → UI 가 그대로 그리는 안내 카드(값은 절대 담지 않는다).

    ``example`` 을 주면 선언된 예시 대신 그것을 싣는다 — 스키마가 알 수 없는 **인스턴스
    의존 예시**(이 배포가 실제로 감시하는 프로젝트 키 등)를 위해서다. 시크릿 필드에는
    어떤 예시도 싣지 않는다(그 규칙이 이 오버라이드보다 우선한다).
    """
    return {
        "key": f.key,
        "type": f.type.value,
        "required": bool(f.required),
        "required_if": f.required_if.describe() if f.required_if else "",
        "secret": bool(f.secret),
        "choices": list(f.choices),
        "default": f.default,
        "example": None if f.secret else (
            f.example if example is _KEEP else example),  # 시크릿은 예시조차 두지 않는다
        "description": f.description,
        "legacy_keys": list(f.legacy_keys),
        "input": f.key not in SERVER_DERIVED_KEYS,
        "note": note,
        "link": link,
        "link_label": link_label,
    }


def build_guide(config: Any = None) -> dict:
    """관리 UI 가 폼과 준비물 안내를 **그대로 그릴 수 있는** 페이로드(순수 — I/O 없음).

    구성:
        ``steps``       2단 절차(로컬 SETTER + 웹 등록) — :func:`join_steps`.
        ``sections``    스키마 섹션 → 필드 카드. UI 는 이걸로 폼을 만든다.
        ``forge``       이 배포의 forge 에 맞는 토큰 안내 — :func:`forge_guide`.
        ``jira``        이 배포의 Jira 사이트에 맞는 안내 — :func:`jira_guide`.
        ``consent_key`` 동의 항목 키(UI 가 특별히 다루는 유일한 필드).
        ``secrets_note`` 토큰 보관 규율(:data:`SECRETS_NOTE`).

    ⚠️ **시크릿은 한 톨도 담지 않는다.** 담는 것은 스키마 선언과 config 의 **비-시크릿**
    값(사이트 URL·프로젝트 키·레포 URL)뿐이다 — 관리 UI 에는 인증이 없다(SECURITY.md).
    """
    fg = forge_guide(config)
    jg = jira_guide(config)
    #: 이 인스턴스가 실제로 감시하는 프로젝트 키 — 범위 질문을 **설정에서 렌더**한다.
    default_projects = list(jg.get("projects") or [])
    default_projects_text = ", ".join(default_projects) or "(설정 미완 — 운영자에게 문의)"

    #: 필드별 **인스턴스 의존 부연**(설정에서 유도된 것만 — 문구를 복제하지 않는다).
    notes: dict = {
        "jira_account_id": (
            f"확인 방법 ①  아래 '내 accountId 조회' 버튼(이메일+토큰으로 "
            f"{jg['myself_path']} 를 대신 호출한다). "
            f"②  {jg['profile_hint']}."
        ),
        "jira_email": (
            f"이 인스턴스가 보는 Jira 사이트: {jg['base_url'] or '(설정 미완)'}"
        ),
        "jira_token": "발급: Atlassian 계정 → Security → API tokens → Create API token.",
        "forge_token": (
            f"{fg['label']}: {fg['path']} — 스코프 `{fg['scope']}`. {fg['scope_why']}"
            if fg.get("path") else
            "이 배포의 forge 종류를 설정에서 읽지 못했습니다 — 운영자에게 문의하세요."
        ),
        "claude_setup_token": (
            "본인 머신 터미널에서 `claude setup-token` 실행 → 출력된 토큰을 붙여넣는다."
        ),
        SCOPE_INCLUDE_DEFAULT_KEY: (
            f"이 인스턴스의 기본 프로젝트: {default_projects_text}. "
            f"켜 두면 이 프로젝트(들)의 티켓 중 당신이 담당자인 것을 받는다."
        ),
        SCOPE_KEY: (
            f"비워 두면 기본 프로젝트({default_projects_text})만 받는다. "
            f"여기 적은 키는 기본 프로젝트에 **더해진다**"
            f"(위 체크를 끄면 여기 적은 것만 받는다)."
        ),
    }
    #: 필드별 **인스턴스 의존 예시**(placeholder). 선언된 예시로는 말할 수 없는 것만.
    examples: dict = {
        # 지어낸 프로젝트 키를 placeholder 로 두면 그것을 그대로 적는 사람이 생긴다.
        # 여기서는 *형식*만 말하고, 실제 키는 위 note 가 설정에서 읽어 보여 준다.
        SCOPE_KEY: "추가할 프로젝트 키를 쉼표로 (없으면 비워 두기)",
    }
    links: dict = {
        "jira_token": (jg["token_url"], "API 토큰 발급"),
        "forge_token": (fg["url"], f"{fg['label']} 토큰 발급"),
        "jira_account_id": (jg["myself_url"], "myself 응답 열기"),
    }

    sections = []
    for section in S.iter_sections_in(USER_SCHEMA):
        sections.append({
            "name": section.name,
            "title": section.title,
            "description": section.description,
            "optional": bool(section.optional),
            "fields": [
                _field_card(f, note=notes.get(f.key, ""),
                            link=links.get(f.key, ("", ""))[0],
                            link_label=links.get(f.key, ("", ""))[1],
                            example=examples.get(f.key, _KEEP))
                for f in section.fields
            ],
        })

    return {
        "steps": join_steps(config),
        "sections": sections,
        "forge": fg,
        "jira": jg,
        "consent_key": CONSENT_KEY,
        "secrets_note": SECRETS_NOTE,
        "required": list(required_keys()),
    }
