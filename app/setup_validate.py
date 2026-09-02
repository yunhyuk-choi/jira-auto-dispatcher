"""온보딩 답변 검증 — **강제성의 기계적 원천**(CLI·웹 온보딩 공용 라이브러리).

역할:
    :mod:`app.setup_schema` 는 "무엇을 물어야 하는가"를 **선언**만 한다 — ``required``·
    ``required_if``·``choices`` 가 적혀 있어도 그것을 **강제하는 주체가 없었다.** 그래서
    설치자가 필수 항목을 빠뜨려도, 심지어 풀 퍼미션 동의(``consent.full_permissions``)를
    켜지 않아도 시스템이 부팅됐다. 이 모듈이 그 자리를 채운다.

    강제성의 원천은 "지시를 잘 따르는 것"이 아니라 **기계적 게이트**다. 이 모듈을 부르는
    쪽(``python -m app.setup validate`` / 웹 온보딩)이 무엇이든, 검증에 실패하면 설정은
    산출되지 않고 CLI 는 non-zero 로 끝난다. 대화형 온보딩 에이전트가 붙어도 그 에이전트는
    **값을 캐내는 인터페이스일 뿐**이고 판정은 여기서 한다.

설계 원칙 — **검증기는 하나, 소비처는 둘**:
    이 모듈은 I/O·전역 상태·부작용이 **전혀 없는 순수 라이브러리**다. 소비처는 실제로 둘이다:

        1. **설치 관문 CLI**(:mod:`app.setup`) — 인스턴스를 세우는 사람의 답변.
           스키마는 :data:`app.setup_schema.SETUP_SCHEMA`.
        2. **웹 온보딩**(:mod:`app.onboarding`) — 이미 도는 인스턴스에 합류하는 팀원의
           per-user 자격증명. 스키마는 :data:`app.user_schema.USER_SCHEMA`.

    둘은 **필드 집합이 다르다**(설치 설정 vs 개인 자격증명). 그래서 스키마를 억지로 합치지
    않고, :func:`validate_answers` 의 ``sections`` 인자로 스키마만 갈아 끼운다 — 규칙
    (필수·조건부 필수·타입·허용값·동의 게이트)은 한 벌로 남는다. 검증 로직이 두 벌이 되는
    순간 둘은 갈라지고, 갈라진 게이트는 게이트가 아니다.

    결과(:class:`ValidationResult`)는 사람이 읽는 형태
    (:meth:`ValidationResult.format_text`)와 기계가 읽는 형태
    (:meth:`ValidationResult.to_dict`) 둘 다로 내보낸다. 후자의 ``findings[].key`` 가
    **필드별 키**이므로 웹 온보딩은 그걸로 입력칸을 짚어 오류를 표시한다.

첫 오류에서 멈추지 않는다:
    누락·조건부 누락·허용값 위반·타입 불일치를 **전부 모아서** 보고한다. 하나씩 알려주면
    설치자가 왕복을 여러 번 하게 된다(그리고 그 왕복이 설치 포기의 주된 이유다).

파서(:mod:`app.config`)와 같은 것을 본다:
    - 레거시 키(``match.statuses``·``notify.enabled``·``spawn.docker_host`` …)로 준 값도
      config.py 가 **실제로 읽으므로** 여기서도 신규 키를 채운 것으로 인정한다(경고만).
    - 우선순위도 동일하다: 신규 키 > 레거시 키 > 스키마 기본값.
    두 곳이 갈라지면 "검증은 통과했는데 부팅은 실패"가 나온다 — 그게 가장 나쁘다.

시크릿 규율:
    이 시스템은 시크릿 **값**을 config.yaml 에 담지 않는다(참조만). 그래서 참조 자리
    (``secret_ref``)에 **값처럼 생긴 것**이 오면 오류다(:data:`CODE_SECRET_VALUE`).
    ⚠️ 그 오류 메시지에는 **문제의 값을 절대 싣지 않는다** — 키 이름과 이유만 말한다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app import setup_schema as S

# ---------------------------------------------------------------------------
# 심각도 · 오류 코드(기계가 읽는 안정 식별자 — 문구가 바뀌어도 코드는 유지한다)
# ---------------------------------------------------------------------------

LEVEL_ERROR = "error"      # 게이트를 막는다(→ non-zero exit, 렌더 거부)
LEVEL_WARNING = "warning"  # 막지는 않지만 알아야 한다

CODE_MISSING_REQUIRED = "missing_required"        # 무조건 필수인데 비었다
CODE_MISSING_REQUIRED_IF = "missing_required_if"  # 조건부 필수 조건이 켜졌는데 비었다
CODE_BAD_CHOICE = "bad_choice"                    # choices 밖의 값
CODE_BAD_TYPE = "bad_type"                        # 선언 타입과 불일치
CODE_BAD_FORMAT = "bad_format"                    # 선언 모양(pattern) 위반
CODE_SECRET_VALUE = "secret_value"                # 참조 자리에 시크릿 "값"이 왔다
CODE_CONSENT_REQUIRED = "consent_required"        # 풀 퍼미션 동의 미승인
CODE_PLACEHOLDER = "placeholder_value"            # <PROJECT_KEY> 같은 예시 자리표시자
CODE_LEGACY_KEY = "legacy_key"                    # (경고) 옛 키로 줬다 — 읽히긴 한다
CODE_UNKNOWN_KEY = "unknown_key"                  # (경고) 스키마에 없는 키
CODE_UNSUBSTITUTED_ENV = "unsubstituted_env"      # (경고) ${VAR} 가 그대로 남았다
CODE_BAD_TIMESTAMP = "bad_timestamp"              # (경고) 동의 시각이 ISO-8601 아님

#: "스키마가 이 섹션 전체를 안다"고 볼 수 있는 최상위 섹션들. 이 섹션 **안**의 모르는 키는
#: 오타일 가능성이 높으므로 경고한다. ``run``·``spawn``·``server`` 등은 스키마가 일부만
#: 묻고 나머지는 config.py 가 자기 기본값으로 읽으므로(예: ``run.workspace_dir``) 경고하지
#: 않는다 — 그러지 않으면 완전한 config.yaml 을 검증할 때마다 경고가 쏟아진다.
CLOSED_SECTIONS: tuple = ("forge", "notifier", "jira", "deploy", "consent")

#: 예시 파일의 자리표시자(``<PROJECT_KEY>``·``<your-group>`` …). 그대로 복사해 온 설정을
#: "채워졌다"고 오인하지 않기 위한 검사 — 이건 경고가 아니라 **오류**다(반드시 틀린 값).
_PLACEHOLDER = re.compile(r"<[^<>\s][^<>]*>")

#: 미치환 env 토큰(``${SECRETS_DIR}``). config.py 가 로드시 치환하므로 그 자체는 정상
#: 사용법이지만, env 가 없으면 부팅이 실패하므로 알려는 준다.
_ENV_TOKEN = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")

#: 대표적인 토큰 접두사 — 참조 자리에 이런 게 오면 **값을 붙여넣은 것**이다.
_TOKEN_PREFIXES: tuple = (
    "glpat-",        # GitLab PAT
    "gldt-",         # GitLab deploy token
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_",  # GitHub PAT/OAuth
    "github_pat_",   # GitHub fine-grained PAT
    "ATATT",         # Atlassian API token
    "xoxb-", "xoxp-", "xapp-",  # Slack
    "sk-",           # 일반 API 키 관례
)

#: ISO-8601 (날짜 또는 날짜시각). 엄밀한 파싱이 아니라 "형태가 그럴듯한가"만 본다.
_ISO8601 = re.compile(
    r"^\d{4}-\d{2}-\d{2}"
    r"(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)


# ---------------------------------------------------------------------------
# 결과 자료구조
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    """검증 결과 항목 하나(오류 또는 경고).

    Attributes:
        level: :data:`LEVEL_ERROR` | :data:`LEVEL_WARNING`.
        key: 문제가 있는 **점 표기 키 경로**(웹 온보딩이 이 값으로 해당 입력칸을 짚는다).
        code: 안정 식별자(``CODE_*``) — 문구가 바뀌어도 소비처 로직은 안 깨진다.
        message: 무엇이 잘못됐는지(사람이 읽는 한 줄).
        hint: 어떻게 고치는지. 비어 있을 수 있다.
    """

    level: str
    key: str
    code: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict:
        """JSON 직렬화용 dict."""
        return {
            "level": self.level,
            "key": self.key,
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
        }

    def format_line(self) -> str:
        """사람이 읽는 한 줄(들여쓴 힌트는 호출부가 붙인다)."""
        mark = "✖" if self.level == LEVEL_ERROR else "⚠"
        return f"{mark} {self.key}: {self.message}"


@dataclass
class ValidationResult:
    """검증 결과 — 소비처(CLI·웹 온보딩·대화형 에이전트) 공용 반환값.

    Attributes:
        findings: 오류·경고 전부(선언 순서 → 발견 순서).
        values: 기본값·레거시 키까지 **해석된** 최종 값 표(점 표기 키 → 값).
            렌더러·doctor 가 아니라 프로그래밍 소비처를 위한 것이다.
        explicit: 설치자가 **실제로 답한** 항목 + 그 답에서 **기계적으로 파생되는**
            항목(스키마 dataclass 기본값은 제외, 레거시 키는 신규 키로 정규화).
            :func:`app.setup_render.render_config` 가 이걸 쓴다 — 답하지 않은 항목까지
            써 버리면 프로파일 설계가 무력화되고, 반대로 파생값을 빼면
            ``deploy.profile`` 과 **모순되는** config.yaml 이 나온다
            (:func:`_apply_profile_derived`).
    """

    findings: list = field(default_factory=list)
    values: dict = field(default_factory=dict)
    explicit: dict = field(default_factory=dict)

    @property
    def errors(self) -> list:
        """오류만(게이트를 막는 것)."""
        return [f for f in self.findings if f.level == LEVEL_ERROR]

    @property
    def warnings(self) -> list:
        """경고만."""
        return [f for f in self.findings if f.level == LEVEL_WARNING]

    @property
    def ok(self) -> bool:
        """오류가 하나도 없으면 True(= 게이트 통과)."""
        return not self.errors

    def to_dict(self) -> dict:
        """기계가 읽는 출력(``--json``·웹 온보딩 응답).

        ⚠️ **값은 싣지 않는다** — 검증 대상에는 시크릿 참조가 섞여 있고, 진단 출력이
        조용히 값을 흘리는 것이 이 리포가 가장 피하려는 실패 모드다. 값이 필요한 파이썬
        소비처는 :attr:`values`/:attr:`explicit` 를 직접 읽으면 된다.
        """
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }

    def format_text(self) -> str:
        """사람이 읽는 출력(CLI 기본)."""
        lines: list = []
        for group, title in ((self.errors, "오류"), (self.warnings, "경고")):
            if not group:
                continue
            lines.append(f"[{title} {len(group)}건]")
            for f in group:
                lines.append("  " + f.format_line())
                if f.hint:
                    lines.append(f"      ↳ {f.hint}")
            lines.append("")
        if self.ok:
            lines.append("설정 답변 검증 통과 — render 로 config.yaml 을 생성할 수 있습니다.")
        else:
            lines.append(
                f"설정 답변 검증 실패 — 오류 {len(self.errors)}건을 고친 뒤 다시 실행하세요."
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# 답변 정규화(중첩 dict ↔ 점 표기 둘 다 받는다)
# ---------------------------------------------------------------------------


def _sections(sections: Optional[tuple]) -> tuple:
    """기준 스키마를 정한다 — 주지 않으면 설치 스키마(:data:`S.SETUP_SCHEMA`).

    이 한 줄이 "검증기는 하나, 소비처는 둘"의 이음매다. 소비처는 스키마를 갈아 끼울 뿐
    규칙(필수·조건부·타입·허용값·동의)은 전부 이 모듈 것을 쓴다.
    """
    return S.SETUP_SCHEMA if sections is None else tuple(sections)


def flatten_answers(raw: Mapping, sections: Optional[tuple] = None) -> dict:
    """수집된 답변을 ``{점 표기 키: 값}`` 으로 평탄화한다.

    두 입력 모양을 **모두** 받는다 — 웹 폼은 평평한 점 표기를 주고, config.yaml 을 그대로
    던지는 쪽은 중첩 매핑을 준다:

        ``{"jira": {"project": "PROJ"}}``  ≡  ``{"jira.project": "PROJ"}``

    ⚠️ **스키마를 보며 내려간다**: 경로가 선언된 필드에 닿으면 거기서 멈춘다. 그래야
    ``jira.custom_fields`` 같은 STRING_MAP 값(dict)을 자식 키로 쪼개지 않는다.

    Args:
        raw: 답변(중첩 또는 점 표기).
        sections: 기준 스키마. 생략하면 설치 스키마(:data:`app.setup_schema.SETUP_SCHEMA`).
    """
    sections = _sections(sections)
    out: dict = {}

    def walk(node: Mapping, prefix: str) -> None:
        for raw_key, value in node.items():
            key = f"{prefix}.{raw_key}" if prefix else str(raw_key)
            if S.get_field_in(sections, key) is not None:
                out[key] = value          # 선언된 필드 — 값을 통째로 받는다
            elif isinstance(value, Mapping) and value:
                walk(value, key)          # 아직 섹션 — 더 내려간다
            else:
                out[key] = value          # 모르는 키(또는 빈 섹션) — 그대로 기록

    walk(dict(raw or {}), "")
    return out


def resolve_values(flat: Mapping, sections: Optional[tuple] = None) -> tuple:
    """평탄화된 답변 → ``(해석된 값 표, 명시적으로 답한 값 표, 경고 목록)``.

    해석 규칙은 :mod:`app.config` 의 ``_pick``/``_build_deploy`` 와 **같아야 한다**:
        신규 키 > 레거시 키 > **프로파일 파생**(:func:`_apply_profile_derived`) > 스키마 기본값

    레거시 ``notify.enabled`` 만은 값의 모양이 달라(bool → provider 이름) 파서와 같은
    특례를 둔다: ``true`` 면 ``google_chat``(그 시절 유일한 구현), 아니면 ``none``.

    Args:
        flat: 평탄화된 답변.
        sections: 기준 스키마. 생략하면 설치 스키마.
    """
    values: dict = {}
    explicit: dict = {}
    notes: list = []

    for f in S.iter_fields_in(_sections(sections)):
        if f.key in flat:
            values[f.key] = flat[f.key]
            explicit[f.key] = flat[f.key]
            continue
        legacy_hit = None
        for old in f.legacy_keys:
            if old in flat:
                legacy_hit = old
                break
        if legacy_hit is not None:
            raw_value = flat[legacy_hit]
            if f.key == "notifier.provider":
                # notify.enabled(bool) → provider 이름. config.py _build_notifier 와 동일.
                raw_value = "google_chat" if bool(raw_value) else "none"
            values[f.key] = raw_value
            explicit[f.key] = raw_value
            notes.append(Finding(
                LEVEL_WARNING, f.key, CODE_LEGACY_KEY,
                f"레거시 키 {legacy_hit!r} 로 값을 받았습니다(계속 읽히지만 이름이 옛것입니다).",
                f"새 설정은 {f.key} 를 쓰세요.",
            ))
            continue
        if f.default is not None:
            values[f.key] = copy.deepcopy(f.default)

    _apply_profile_derived(values, explicit, _sections(sections))
    return values, explicit, notes


def _apply_profile_derived(values: dict, explicit: dict, sections: tuple) -> None:
    """``deploy.profile`` 파생값을 **답한 값에 합친다**(제자리 변경).

    왜 ``explicit`` 에까지 넣는가:
        렌더러는 "답한 것만" 쓴다 — 그 규칙 자체는 옳다(스키마 dataclass 기본값을 파일에
        박으면 프로파일 설계가 무력화된다). 하지만 **프로파일 파생값은 dataclass 기본값이
        아니라 답의 일부**다. 프로파일을 고른 순간 함께 정해진 값이므로, 렌더에 반영되지
        않으면 예시 파일의 *다른 프로파일 값*이 그대로 남아 프로파일과 모순되는
        config.yaml 이 나온다(실측 결함).

    우선순위(:func:`app.config._build_deploy` 와 동일):
        명시 답변 > 레거시 키 > **프로파일 파생** > 스키마 기본값.
        앞의 두 경우는 이미 ``explicit`` 에 들어와 있으므로, 여기 없는 키만 채운다.
    """
    if S.get_field_in(sections, "deploy.profile") is None:
        return                      # 이 스키마는 배포 프로파일을 묻지 않는다(사용자 스키마 등)
    for key, value in S.profile_derived_values(values.get("deploy.profile")).items():
        if key in explicit:         # 명시 답변·레거시 키가 이긴다
            continue
        if S.get_field_in(sections, key) is None:
            continue
        values[key] = value
        explicit[key] = value


# ---------------------------------------------------------------------------
# 개별 규칙(순수 술어 — 각각 독립적으로 테스트된다)
# ---------------------------------------------------------------------------


def is_empty(value: Any) -> bool:
    """"비어 있다"의 단일 정의.

    ⚠️ bool/int/float 는 **비어 있지 않다** — ``False``·``0`` 은 명시적인 답이다. 그래서
    ``consent.full_permissions: false`` 는 "누락"이 아니라 "동의하지 않음"으로 잡히고,
    그건 :func:`_check_consent` 의 몫이다(누락 규칙으로 잡으면 메시지가 엉뚱해진다).
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


#: ``{id, name}`` 매핑에서 허용하는 키. 그 밖의 키는 오타이거나 다른 스키마의 값이다.
_NAMED_REF_KEYS = frozenset({"id", "name"})


def _named_ref_error(item: Any) -> Optional[str]:
    """:data:`app.setup_schema.FieldType.NAMED_REF_LIST` 원소 하나의 문제, 없으면 None.

    ``"완료"`` 처럼 이름만 줘도 되고(하위호환), ``{"id": "41", "name": "완료"}`` 처럼
    id 를 함께 줘도 된다. **name 은 언제나 필요하다** — 이름이 없으면 JQL 에도 레거시
    ``match.*`` 미러에도 실을 수 없고, 파서가 그 항목을 조용히 버린다.
    """
    if isinstance(item, str):
        return None
    if isinstance(item, Mapping):
        unknown = sorted(set(item) - _NAMED_REF_KEYS)
        if unknown:
            return f"매핑에 모르는 키가 있습니다: {', '.join(unknown)} (허용: id, name)"
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return "매핑 항목에는 비어 있지 않은 문자열 name 이 필요합니다"
        if "id" in item and not isinstance(item.get("id"), str):
            return f"id 는 문자열이어야 합니다(받은 타입: {type(item.get('id')).__name__})"
        return None
    return (f"원소는 이름 문자열이거나 {{id, name}} 매핑이어야 합니다"
            f"(받은 타입: {type(item).__name__})")


def _type_error(f: S.SchemaField, value: Any) -> Optional[str]:
    """선언 타입과 맞지 않으면 사람이 읽는 이유, 맞으면 None."""
    t = f.type
    if t in (S.FieldType.STRING, S.FieldType.ENUM):
        if not isinstance(value, str):
            return f"문자열이어야 합니다(받은 타입: {type(value).__name__})"
        return None
    if t is S.FieldType.BOOL:
        if not isinstance(value, bool):
            return f"true/false 여야 합니다(받은 타입: {type(value).__name__})"
        return None
    if t is S.FieldType.INT:
        # bool 은 int 의 서브클래스다 — 정수 자리에 true 가 오는 것은 오설정이다.
        if isinstance(value, bool) or not isinstance(value, int):
            return f"정수여야 합니다(받은 타입: {type(value).__name__})"
        return None
    if t is S.FieldType.FLOAT:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"숫자여야 합니다(받은 타입: {type(value).__name__})"
        return None
    if t is S.FieldType.STRING_LIST:
        if not isinstance(value, (list, tuple)):
            return f"문자열 목록이어야 합니다(받은 타입: {type(value).__name__})"
        bad = [v for v in value if not isinstance(v, str)]
        if bad:
            return f"목록의 원소는 전부 문자열이어야 합니다({len(bad)}개가 아닙니다)"
        return None
    if t is S.FieldType.NAMED_REF_LIST:
        if not isinstance(value, (list, tuple)):
            return (f"목록이어야 합니다 — 원소는 이름 문자열이거나 {{id, name}} 매핑입니다"
                    f"(받은 타입: {type(value).__name__})")
        for item in value:
            reason = _named_ref_error(item)
            if reason:
                return reason
        return None
    if t is S.FieldType.STRING_MAP:
        if not isinstance(value, Mapping):
            return f"문자열→문자열 매핑이어야 합니다(받은 타입: {type(value).__name__})"
        bad = [k for k, v in value.items()
               if not isinstance(k, str) or not isinstance(v, str)]
        if bad:
            return f"매핑의 키·값은 전부 문자열이어야 합니다(어긋난 키 {len(bad)}개)"
        return None
    return None


#: 형식 위반을 **값 없이** 설명할 때 이름 붙일 수 있는 위험 요소.
#:
#: 왜 카테고리인가: 어긋난 문자를 그대로 되비추면 (1) 관리 UI 는 무인증이고 응답을
#: 화면에 그리며(templates/index.html), (2) 서버 로그에 개행·제어문자가 그대로 섞인다.
#: 그래서 "무엇이 들어 있었는가"를 **분류로만** 말한다 — 고칠 수 있을 만큼은 알려 주되
#: 공격자가 넣은 바이트를 그대로 되돌려주지 않는다.
_FORMAT_HAZARDS = (
    ("경로 구분자(/ 또는 \\)", lambda v: "/" in v or "\\" in v),
    ("경로 상위 표기(..)", lambda v: ".." in v),
    ("공백", lambda v: any(ch.isspace() for ch in v)),
    ("제어문자", lambda v: any(ord(ch) < 32 or ord(ch) == 127 for ch in v)),
    ("ASCII 밖의 문자", lambda v: any(ord(ch) > 127 for ch in v)),
)


def describe_format_violation(value: str) -> str:
    """모양 위반을 **입력값을 싣지 않고** 설명한다(길이 + 위험 요소 분류).

    ⚠️ 반환 문자열에 값의 어떤 조각도 넣지 않는다 — 호출부(관리 UI·로그)가 이것을 그대로
    출력하기 때문이다. :func:`looks_like_secret_value` 와 같은 규율이다.
    """
    v = value or ""
    hits = [name for name, hit in _FORMAT_HAZARDS if hit(v)]
    detail = f"길이 {len(v)}자"
    if hits:
        detail += ", 포함: " + " · ".join(hits)
    return detail


def looks_like_secret_value(value: str) -> str:
    """참조 자리에 온 값이 **시크릿 값처럼** 보이면 그 이유, 아니면 "".

    참조는 ``secrets.base_dir`` **상대 경로**(예: ``service/jira-token``)다. 여기에
    토큰이나 웹훅 URL 자체가 들어오면 config.yaml 이 시크릿을 품게 된다 — 이 리포의
    규율("값이 아니라 참조")을 정면으로 깨는 일이라 검증에서 막는다.

    ⚠️ 반환 문자열에 **값을 넣지 않는다**(호출부가 그대로 출력하기 때문).
    """
    v = (value or "").strip()
    if not v:
        return ""
    if "://" in v:
        return "URL 로 보입니다 — 웹훅 URL·엔드포인트는 시크릿 '값'입니다"
    if v.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", v):
        return "절대경로입니다 — 참조는 secrets.base_dir 기준 **상대경로**여야 합니다"
    if any(v.startswith(p) for p in _TOKEN_PREFIXES):
        return "토큰 값으로 보입니다(알려진 토큰 접두사)"
    if v != value:
        return "앞뒤 공백이 있습니다 — 참조 경로에 공백을 두지 마세요"
    if any(ch.isspace() for ch in v):
        return "공백이 포함돼 있습니다 — 참조는 공백 없는 상대경로여야 합니다"
    # 경로 구분자도 확장자도 없는 아주 긴 한 덩어리 = 파일명이라기보다 토큰이다.
    # (오탐이 곧 설치 차단이므로 문턱을 넉넉히 둔다 — 알려진 접두사는 위에서 이미 잡는다.)
    if "/" not in v and "." not in v and len(v) >= 32:
        return "긴 단일 토큰 문자열로 보입니다 — 참조가 아니라 값을 붙여넣은 것 같습니다"
    return ""


def _placeholder_hits(value: Any) -> bool:
    """값(문자열·목록·매핑) 어딘가에 ``<...>`` 자리표시자가 남아 있는가."""
    if isinstance(value, str):
        return bool(_PLACEHOLDER.search(value))
    if isinstance(value, (list, tuple)):
        return any(_placeholder_hits(v) for v in value)
    if isinstance(value, Mapping):
        return any(_placeholder_hits(v) for v in value.values())
    return False


def _env_token_hits(value: Any) -> bool:
    """값 어딘가에 미치환 ``${VAR}`` 이 남아 있는가."""
    if isinstance(value, str):
        return bool(_ENV_TOKEN.search(value))
    if isinstance(value, (list, tuple)):
        return any(_env_token_hits(v) for v in value)
    if isinstance(value, Mapping):
        return any(_env_token_hits(v) for v in value.values())
    return False


# ---------------------------------------------------------------------------
# 검증 본체
# ---------------------------------------------------------------------------


def _check_consent(values: Mapping, key: str, hint: str) -> list:
    """풀 퍼미션 동의 게이트 — **이 시스템의 전제**.

    이 소프트웨어는 사람의 매 단계 승인 없이 도구 권한(파일 쓰기·셸·git push)을 가진
    코딩 에이전트를 헤드리스로 돌린다. 그걸 알고 감수한다는 명시 동의 없이 통과시키면
    안 된다 — 그래서 값이 **정확히 true** 가 아니면 오류다(누락·false·"true" 문자열
    전부 불통과).

    ⚠️ :mod:`app.config` 의 부팅 검증은 이걸 강제하지 **않는다**(동의 키가 없던 기존
    배포를 깨지 않으려고 경고만 남긴다). 강제는 관문인 여기의 몫이다.

    동의는 **누가 무엇에 동의하는가**에 따라 키가 다르다 — 설치자는
    ``consent.full_permissions``(설치 스키마), 합류자는 ``consent_full_permissions``
    (per-user 온보딩 스키마). 판정 논리는 하나고 키만 주입한다.
    """
    value = values.get(key)
    if value is True:
        return []
    if value is None:
        detail = "동의 값이 없습니다"
    elif value is False:
        detail = "동의하지 않음(false)입니다"
    else:
        detail = f"true/false 가 아닙니다(받은 타입: {type(value).__name__})"
    return [Finding(
        LEVEL_ERROR, key, CODE_CONSENT_REQUIRED,
        f"풀 퍼미션 실행 동의가 필요합니다 — {detail}.", hint,
    )]


#: 설치자용 동의 힌트(기본값). per-user 소비처는 자기 문구를 주입한다.
CONSENT_HINT_SETUP = (
    "이 시스템은 사람 승인 없이 셸·파일 쓰기·git push 를 하는 에이전트를 헤드리스로 "
    "실행합니다(SECURITY.md). 무엇에 동의하는지 읽고 consent.full_permissions 를 "
    "true 로, consent.accepted_at 을 동의 시각(ISO-8601)으로 채우세요."
)


def _check_unknown_keys(flat: Mapping, sections: tuple, closed: tuple) -> list:
    """스키마에 없는 키 경고(오타 잡기). 레거시 키는 :func:`resolve_values` 가 이미 다뤘다.

    경고 범위를 ``closed``(기본 :data:`CLOSED_SECTIONS`)로 좁히는 이유는 모듈 상단 주석
    참조 — 완전한 config.yaml 을 그대로 검증해도 경고가 쏟아지지 않게 하기 위해서다.
    ``closed`` 가 비면 이 검사는 아무것도 하지 않는다(모르는 키를 자유롭게 받는 소비처용).
    """
    if not closed:
        return []
    legacy = S.legacy_key_map_in(sections)
    out: list = []
    for key in flat:
        if S.get_field_in(sections, key) is not None or key in legacy:
            continue
        section = key.split(".", 1)[0]
        if section not in closed:
            continue
        out.append(Finding(
            LEVEL_WARNING, key, CODE_UNKNOWN_KEY,
            f"{section} 섹션에 선언되지 않은 항목입니다 — 오타이거나 무시됩니다.",
            f"허용 항목: " + ", ".join(
                f.key for f in S.iter_fields_in(sections)
                if f.key.startswith(section + ".")
            ),
        ))
    return out


def validate_answers(
    raw: Mapping,
    *,
    sections: Optional[tuple] = None,
    consent_key: str = "consent.full_permissions",
    consent_hint: str = "",
    accepted_at_key: str = "consent.accepted_at",
    allow_secret_values: bool = False,
    closed_sections: Optional[tuple] = None,
) -> ValidationResult:
    """수집된 답변을 스키마 선언에 대고 검증한다(**공개 진입점**).

    Args:
        raw: 중첩 매핑 또는 점 표기 매핑(둘 다 받는다 — :func:`flatten_answers`).
        sections: 기준 스키마. 생략하면 설치 스키마
            (:data:`app.setup_schema.SETUP_SCHEMA`). per-user 온보딩처럼 **필드 집합이
            다른** 소비처는 자기 스키마를 같은 자료구조로 선언해 여기에 넣는다 —
            스키마를 합치지 않고 **검증 메커니즘만** 공유하기 위한 자리다.
        consent_key: 풀 퍼미션 동의 항목의 키(누가 동의하는가에 따라 다르다).
        consent_hint: 동의 미승인 오류의 힌트 문구(생략하면 설치자용 문구).
        accepted_at_key: 동의 시각 항목의 키(ISO-8601 형식 경고 대상).
        allow_secret_values: 시크릿 **값**을 답변으로 받아도 되는가. 기본 False —
            설치 답변은 config.yaml 로 흘러가므로 값이 오면 오류다. per-user 온보딩은
            토큰 값을 받아 **시크릿 파일로** 저장하고 레지스트리엔 참조만 남기므로
            True 로 부른다(그 규율은 :func:`app.onboarding._write_secret` 이 지킨다).
        closed_sections: 모르는 키를 경고할 섹션 목록. 생략하면 :data:`CLOSED_SECTIONS`.
            빈 튜플을 주면 모르는 키를 경고하지 않는다.

    Returns:
        :class:`ValidationResult`. ``result.ok`` 가 False 면 호출부는 **반드시** 실패로
        끝내야 한다(CLI 는 non-zero, 웹은 4xx, 렌더러는 거부).

    검사 항목(전부 모아서 보고 — 첫 오류에서 멈추지 않는다):
        1. 필수(``required``) 누락
        2. 조건부 필수(``required_if``) 위반
        3. ``choices`` 밖의 값
        3.5. 선언 모양(``pattern``) 위반 — ⚠️ 오류 메시지에 **값을 싣지 않는다**
        4. 선언 타입 불일치
        5. 참조 자리에 시크릿 **값**(값이 아니라 참조 규율)
        6. 예시 자리표시자(``<PROJECT_KEY>``)가 그대로 남음
        7. 풀 퍼미션 동의 미승인
        8. (경고) 레거시 키 사용 · 모르는 키 · 미치환 ``${VAR}`` · 동의 시각 형식
    """
    sections = _sections(sections)
    closed = CLOSED_SECTIONS if closed_sections is None else tuple(closed_sections)
    flat = flatten_answers(raw, sections)
    values, explicit, findings = resolve_values(flat, sections)

    for f in S.iter_fields_in(sections):
        value = values.get(f.key)
        empty = is_empty(value)

        # --- 1·2. 필수 / 조건부 필수 -------------------------------------
        if empty:
            if f.required:
                findings.append(Finding(
                    LEVEL_ERROR, f.key, CODE_MISSING_REQUIRED,
                    f"필수 항목이 비어 있습니다. {f.description.strip().splitlines()[0]}",
                    _example_hint(f),
                ))
            elif f.required_if is not None:
                cond = f.required_if
                if cond.matches(values.get(cond.key)):
                    findings.append(Finding(
                        LEVEL_ERROR, f.key, CODE_MISSING_REQUIRED_IF,
                        f"조건부 필수 항목이 비어 있습니다({cond.describe()}). "
                        f"현재 {cond.key}={values.get(cond.key)!r}.",
                        _example_hint(f),
                    ))
            # 비어 있으면 이후 값 검사(타입·choices)는 의미가 없다.
            continue

        # --- 4. 타입 -------------------------------------------------------
        type_reason = _type_error(f, value)
        if type_reason:
            findings.append(Finding(
                LEVEL_ERROR, f.key, CODE_BAD_TYPE,
                f"타입이 맞지 않습니다 — {type_reason}.", _example_hint(f),
            ))
            continue  # 타입이 틀리면 choices 비교도 무의미

        # --- 3. choices ----------------------------------------------------
        if f.choices and value not in f.choices:
            findings.append(Finding(
                LEVEL_ERROR, f.key, CODE_BAD_CHOICE,
                f"허용값이 아닙니다: {value!r}.",
                "허용: " + " | ".join(str(c) for c in f.choices),
            ))

        # --- 3.5. 모양(pattern) ---------------------------------------------
        # 전체 일치로 본다 — 부분 일치를 허용하면 "앞부분만 맞는" 값이 통과하고, 그 값이
        # 경로·컨테이너 이름 자리로 흘러가면 그게 취약점이다(app/user_schema.py username).
        #
        # ⚠️ 이 오류만은 **값을 되비추지 않는다.** choices 위반은 스키마가 정한 좁은
        # 집합에서 벗어난 오타라 값을 보여 주는 편이 낫지만, 모양 위반은 임의 입력이고
        # 이 시스템의 관리 UI 는 무인증이다 — 서버가 받은 바이트를 화면·로그로 그대로
        # 되돌려주지 않는다. 대신 무엇을 고쳐야 하는지는 ``pattern_hint`` 가 말한다.
        if f.pattern and isinstance(value, str) and not re.fullmatch(f.pattern, value):
            findings.append(Finding(
                LEVEL_ERROR, f.key, CODE_BAD_FORMAT,
                "허용된 모양이 아닙니다"
                f"({describe_format_violation(value)}). "
                "보안상 입력한 값은 그대로 되돌려 보여 주지 않습니다.",
                f.pattern_hint or _example_hint(f),
            ))
            continue  # 모양이 틀린 값에 자리표시자 검사까지 겹쳐 알릴 이유가 없다

        # --- 5. 시크릿 값 규율 ---------------------------------------------
        if f.secret and not allow_secret_values:
            # 스키마에 값 시크릿은 없지만(테스트가 지킨다), 생기더라도 config.yaml 로는
            # 절대 못 가게 여기서 막는다.
            findings.append(Finding(
                LEVEL_ERROR, f.key, CODE_SECRET_VALUE,
                "시크릿 값은 설정에 담지 않습니다 — 시크릿 파일로 저장하고 참조만 두세요.",
                "값은 secrets.base_dir 아래 0600 파일에 두고 설정에는 상대 경로만 씁니다.",
            ))
        elif f.secret_ref and isinstance(value, str):
            reason = looks_like_secret_value(value)
            if reason:
                findings.append(Finding(
                    LEVEL_ERROR, f.key, CODE_SECRET_VALUE,
                    f"시크릿 참조 자리에 값이 온 것 같습니다 — {reason}.",
                    f"값은 <secrets.base_dir>/{f.example or '<경로>'} 파일에 0600 으로 "
                    f"저장하고, 여기에는 그 상대 경로만 적으세요(예: {f.example!r}).",
                ))

        # --- 6. 자리표시자 --------------------------------------------------
        # ⚠️ 시크릿 **값**에는 돌리지 않는다 — 토큰 안에 우연히 ``<...>``·``${...}`` 가
        # 들어 있을 수 있고, 그 오탐은 붙여넣은 사람이 고칠 방법이 없다(값은 발급기관이
        # 정한다). 참조·일반 값에만 적용한다.
        if f.secret:
            continue
        if _placeholder_hits(value):
            findings.append(Finding(
                LEVEL_ERROR, f.key, CODE_PLACEHOLDER,
                "예시 자리표시자(<...>)가 그대로 남아 있습니다 — 실제 값으로 바꾸세요.",
                _example_hint(f),
            ))

        # --- 8. 경고: 미치환 env 토큰 ---------------------------------------
        if _env_token_hits(value):
            findings.append(Finding(
                LEVEL_WARNING, f.key, CODE_UNSUBSTITUTED_ENV,
                "미치환 ${VAR} 토큰이 있습니다 — 런타임 env 로 채워집니다.",
                "그 env 가 실제 실행 환경에 없으면 부팅이 실패합니다(config.py 가 치환).",
            ))

    # --- 7. 동의 게이트 ------------------------------------------------------
    findings.extend(_check_consent(values, consent_key,
                                   consent_hint or CONSENT_HINT_SETUP))

    # --- 8. 경고: 동의 시각 형식 ---------------------------------------------
    accepted = values.get(accepted_at_key)
    if isinstance(accepted, str) and accepted.strip() and not _ISO8601.match(accepted.strip()):
        findings.append(Finding(
            LEVEL_WARNING, accepted_at_key, CODE_BAD_TIMESTAMP,
            "ISO-8601 형태로 보이지 않습니다(감사 흔적으로 쓰입니다).",
            "예: 2026-08-25T09:00:00+09:00",
        ))

    # --- 8. 경고: 모르는 키 ---------------------------------------------------
    findings.extend(_check_unknown_keys(flat, sections, closed))

    return ValidationResult(findings=findings, values=values, explicit=explicit)


def _example_hint(f: S.SchemaField) -> str:
    """필드의 예시/허용값을 한 줄 힌트로(없으면 설명 첫 줄)."""
    if f.choices:
        return "허용: " + " | ".join(str(c) for c in f.choices)
    if f.example is not None:
        return f"예: {f.example!r}"
    return f.description.strip().splitlines()[0]
