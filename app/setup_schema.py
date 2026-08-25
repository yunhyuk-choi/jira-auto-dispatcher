"""온보딩 설정 스키마 — "설치자에게 무엇을 물어야 하는가"의 **기계가 읽는 단일 원천**.

역할:
    이 시스템을 처음 세우는 사람이 채워야 하는 설정 항목 **전체**를 파이썬 자료구조로
    선언한다. 지금까지 그 지식은 ``config/config.example.yaml`` 의 주석과 사람의 머릿속에
    흩어져 있었고, 특정 조직에 묶인 값(GitLab 전제·Google Chat 전제·특정 Jira 인스턴스의
    커스텀필드 id·특정 프로젝트의 설계문서 레포)이 코드에 하드코딩돼 있었다. 오픈소스로
    공개하려면 그 값들이 **설정**이어야 하고, 설정이려면 "무엇을 물어야 하는가"가 먼저
    기계가 읽을 수 있는 형태로 있어야 한다 — 이 모듈이 그 선언이다.

이 모듈이 하는 일 / 하지 않는 일:
    - **한다**: 항목의 키 경로·타입·필수여부·조건부 필수·기본값·설명·시크릿 여부·
      하위호환 레거시 키를 선언한다. 조회 헬퍼(:func:`iter_fields` 등)만 제공한다.
    - **하지 않는다**: 검증·렌더링·진단을 구현하지 않는다. 이 모듈은 **순수 선언**이며
      I/O·전역 상태·부작용이 없다. 그 선언을 **강제**하는 쪽은 따로 있다:
      :mod:`app.setup_validate`(검증) · :mod:`app.setup_render`(config.yaml 생성) ·
      :mod:`app.setup_doctor`(실측 진단) · :mod:`app.setup`(얇은 CLI 껍데기).
      (대화형 온보딩 에이전트·웹 온보딩 확장은 여전히 후속이며, 그것들도 판정은
      위 라이브러리에 위임한다 — 게이트가 두 벌이 되면 반드시 갈라진다.)

정본 관계:
    - 이 스키마 = "무엇을 묻는가"의 정본.
    - :mod:`app.config` = "그 답을 런타임에 어떻게 읽는가"의 정본(dataclass·파서).
    - ``config/config.example.yaml`` = 사람이 읽는 예시(주석 포함).
    세 곳은 **같은 키 경로**를 공유한다. :attr:`SchemaField.key` 가 그 점 표기 경로다
    (예: ``forge.token_ref`` → YAML 의 ``forge:`` 밑 ``token_ref:``).

시크릿 규율(중요):
    이 시스템은 시크릿 **값**을 config.yaml 에 담지 않는다. 값은 ``secrets.base_dir``
    아래 0600 파일로 두고 설정에는 **참조(상대 경로)만** 둔다. 그래서 필드는 두 가지
    시크릿 성격을 구분한다:
        - :attr:`SchemaField.secret` — 값 자체가 시크릿. config.yaml 에 **절대 쓰지 않는다**
          (온보딩이 받아 시크릿 파일로 저장하고, 설정에는 참조만 남긴다).
        - :attr:`SchemaField.secret_ref` — 값이 "시크릿 파일 참조". 설정에 기록해도 된다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterator, Optional, Tuple


class FieldType(str, Enum):
    """설정 항목의 값 타입(온보딩 렌더러·검증기가 입력 위젯/파싱을 고르는 근거)."""

    STRING = "string"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    ENUM = "enum"                 # choices 중 하나
    STRING_LIST = "string_list"   # 문자열 리스트(YAML 시퀀스)
    STRING_MAP = "string_map"     # 문자열→문자열 매핑(YAML 매핑)


@dataclass(frozen=True)
class RequiredIf:
    """조건부 필수 — "다른 항목이 이러이러하면 이 항목은 필수".

    예) forge 종류가 정해지면 그 forge 토큰 참조는 필수다::

        RequiredIf("forge.kind", truthy=True)

    예) notifier 를 실제로 켰으면 웹훅 참조는 필수다::

        RequiredIf("notifier.provider", equals=("google_chat", "slack", "generic_webhook"))

    해석 규칙(둘 다 주면 ``equals`` 가 우선):
        - ``equals`` 가 비어 있지 않으면 → 대상 값이 그 중 하나일 때 필수.
        - 아니면 ``truthy`` → 대상 값이 truthy 일 때 필수.
    """

    key: str                 # 조건을 보는 **다른 항목**의 키 경로
    equals: Tuple = ()       # 이 값들 중 하나면 조건 충족
    truthy: bool = False     # (equals 미지정 시) 값이 truthy 면 조건 충족

    def matches(self, value: Any) -> bool:
        """대상 항목의 값이 조건을 충족하면 True(순수 술어 — 후속 검증기용)."""
        if self.equals:
            return value in self.equals
        if self.truthy:
            return bool(value)
        return False

    def describe(self) -> str:
        """사람이 읽는 조건 설명(온보딩 안내 문구용)."""
        if self.equals:
            return f"{self.key} 가 {' | '.join(str(v) for v in self.equals)} 이면 필수"
        return f"{self.key} 가 설정되면 필수"


@dataclass(frozen=True)
class SchemaField:
    """온보딩이 물어야 할 설정 항목 하나의 선언.

    Attributes:
        key: config.yaml 안의 **점 표기 키 경로**(정본). :mod:`app.config` 파서와
            ``config/config.example.yaml`` 이 같은 경로를 쓴다.
        type: 값 타입(:class:`FieldType`).
        description: 사람이 읽는 설명 — 온보딩 질문 문구의 원천. "무엇을/왜"를 담는다.
        required: 무조건 필수.
        required_if: 조건부 필수(:class:`RequiredIf`). ``required`` 가 True 면 무의미.
        default: 설치자가 답하지 않았을 때의 값. ``None`` 이면 기본 없음.
        choices: ``type == ENUM`` 일 때 허용값.
        secret: 값 자체가 시크릿 → **config.yaml 에 쓰지 않는다**(시크릿 파일로).
        secret_ref: 값이 시크릿 **파일 참조**(``secrets.base_dir`` 상대) → 기록해도 된다.
        legacy_keys: 하위호환으로 계속 **읽는** 옛 키 경로들. 신규 키가 있으면 신규 우선.
        example: 예시 값(온보딩 placeholder·example.yaml 용).
    """

    key: str
    type: FieldType
    description: str
    required: bool = False
    required_if: Optional[RequiredIf] = None
    default: Any = None
    choices: Tuple = ()
    secret: bool = False
    secret_ref: bool = False
    legacy_keys: Tuple = ()
    example: Any = None


@dataclass(frozen=True)
class SchemaSection:
    """설정 항목의 묶음(온보딩 화면/단계 하나에 대응).

    ``name`` 은 **UX 묶음 이름**이고, 실제 YAML 경로는 각 필드의 :attr:`SchemaField.key`
    가 정한다(대부분 같지만 ``docs_repo`` 처럼 다를 수 있다 — 그 섹션의 필드는 기존
    ``run.*`` 아래 산다).
    """

    name: str
    title: str
    description: str
    fields: Tuple
    optional: bool = False   # 섹션 전체를 건너뛸 수 있으면 True(예: docs_repo)


# ---------------------------------------------------------------------------
# 파생 기본값 테이블 — 배포 프로파일
# ---------------------------------------------------------------------------

#: ``deploy.profile`` 하나만 고르면 나머지 배포 값이 여기서 파생된다(설치자 인지부하 축소).
#: :mod:`app.config` 가 이 테이블을 **그대로 재사용**한다(단일 원천 — 값이 두 곳으로
#: 갈라지지 않게). 우선순위는 config 파서 문서 참조:
#: 명시 ``deploy.*`` > 레거시 키 > 프로파일 파생 > 코드 기본값.
PROFILE_DEFAULTS: dict = {
    # 개발자 노트북 — central 도 워커도 같은 로컬 도커. 시크릿은 로컬 디렉토리(env SECRETS_DIR).
    "local": {
        "docker_host": "unix:///var/run/docker.sock",
        "host_deploy_dir": "",
        "secrets_base_dir": "",
        "workspace_volume": "jad-workspace",
    },
    # 클라우드 VM(EC2/GCE 등) — compose 로 socket-proxy 경유(도커 소켓 직결 금지).
    "cloud_vm": {
        "docker_host": "tcp://socket-proxy:2375",
        "host_deploy_dir": "",
        "secrets_base_dir": "/run/secrets",
        "workspace_volume": "jad-workspace",
    },
    # 사내 온프렘 서버 — cloud_vm 과 같은 형태(다른 것은 네트워크·정책이지 이 값들이 아니다).
    "onprem_server": {
        "docker_host": "tcp://socket-proxy:2375",
        "host_deploy_dir": "",
        "secrets_base_dir": "/run/secrets",
        "workspace_volume": "jad-workspace",
    },
}

#: 허용 배포 프로파일(선언 순서 유지).
DEPLOY_PROFILES: Tuple = tuple(PROFILE_DEFAULTS.keys())

#: Jira 커스텀필드의 **논리 키 → (설명, 오늘의 기본 id)**.
#: 커스텀필드 id 는 Jira 인스턴스마다 다르다(``customfield_10015`` 는 우리 인스턴스의 값일
#: 뿐이다). ``jira.custom_fields`` 는 이 논리 키로 매핑을 받는다 — 온보딩은 이 테이블로
#: 항목별 질문을 렌더하면 된다. 값이 비면 :mod:`app.jira_client` 의 모듈 상수를 그대로
#: 쓴다(하위호환 — 기존 배포는 아무것도 안 바꿔도 오늘과 동일하게 동작).
JIRA_CUSTOM_FIELD_KEYS: Tuple = (
    ("start_date", "착수 시 필수인 '시작날짜' 필드 id", "customfield_10015"),
    ("due_date", "착수 시 필수인 '마감일' 필드 id", "duedate"),
    ("actual_start", "완료 전이 시 필수인 '실제 시작일' 필드 id", "customfield_10187"),
    ("actual_end", "완료 전이 시 필수인 '실제 종료일' 필드 id", "customfield_10186"),
)

#: 지원 forge 종류.
FORGE_KINDS: Tuple = ("gitlab", "github")

#: 지원 알림 채널. ``none`` 이 기본 — 알림 없이도 시스템은 완전히 동작한다.
NOTIFIER_PROVIDERS: Tuple = ("none", "google_chat", "slack", "generic_webhook")


# ---------------------------------------------------------------------------
# 스키마 선언
# ---------------------------------------------------------------------------

_FORGE = SchemaSection(
    name="forge",
    title="코드 호스팅(forge)",
    description=(
        "브랜치를 push 하고 MR/PR 을 만들 코드 호스팅. 지금까지 GitLab 이 코드에 전제돼 "
        "있었으나(변수명·URL 조립), 이제 종류를 골라 쓴다. ⚠️ 사용자 **개인** forge 토큰은 "
        "여기가 아니라 관리 UI 온보딩이 받아 레지스트리에 참조로 둔다 — 이 섹션은 central "
        "서비스 자신이 쓰는 토큰(dlc-meta pull/push·참고 레포 fetch)이다."
    ),
    fields=(
        SchemaField(
            key="forge.kind",
            type=FieldType.ENUM,
            choices=FORGE_KINDS,
            required=True,
            default="gitlab",
            description="코드 호스팅 종류. 브랜치/MR·PR API 어댑터 선택에 쓰인다.",
            example="gitlab",
        ),
        SchemaField(
            key="forge.base_url",
            type=FieldType.STRING,
            default="",
            description=(
                "self-hosted forge 의 base URL(예: 사내 GitLab, GitHub Enterprise). "
                "비우면 각 forge 의 SaaS 기본 엔드포인트를 쓴다."
            ),
            example="https://gitlab.example.com",
        ),
        SchemaField(
            key="forge.token_ref",
            type=FieldType.STRING,
            secret_ref=True,
            required_if=RequiredIf("forge.kind", truthy=True),
            default="",
            legacy_keys=("run.repo_resolver_gitlab_token_ref",),
            description=(
                "central 서비스 forge 토큰이 담긴 **파일의 secrets.base_dir 상대 참조**"
                "(값이 아니다). 그 파일에 토큰을 0600 으로 저장한다."
            ),
            example="service/forge-token",
        ),
    ),
)

_NOTIFIER = SchemaSection(
    name="notifier",
    title="완료 알림",
    description=(
        "잡이 끝났을 때 어디로 알릴지. best-effort 이며 기본은 **끔**(none) — 알림 없이도 "
        "시스템은 완전히 동작한다. 지금까지 Google Chat 이 코드에 전제돼 있었다."
    ),
    fields=(
        SchemaField(
            key="notifier.provider",
            type=FieldType.ENUM,
            choices=NOTIFIER_PROVIDERS,
            required=True,
            default="none",
            legacy_keys=("notify.enabled",),
            description=(
                "알림 채널. ``none`` 이면 알림을 보내지 않는다(기본). 레거시 "
                "``notify.enabled: true`` 만 있는 설정은 ``google_chat`` 으로 해석한다. "
                "채널별 차이는 **페이로드 모양과 멘션 문법**뿐이다: ``google_chat``·"
                "``slack`` 은 ``{\"text\": ...}`` 에 각각 ``<users/{id}>``·``<@{id}>`` 멘션, "
                "``generic_webhook`` 은 고정 JSON 스키마(source/event/ticket/status/"
                "mention_id/text)를 POST 한다. 목록 밖 값이면 **발송하지 않는다**"
                "(경고 후 미발송 — 모양을 모르는 페이로드를 남의 채널로 쏘지 않는다)."
            ),
            example="none",
        ),
        SchemaField(
            key="notifier.webhook_ref",
            type=FieldType.STRING,
            secret_ref=True,
            required_if=RequiredIf(
                "notifier.provider",
                equals=("google_chat", "slack", "generic_webhook"),
            ),
            default="",
            legacy_keys=("notify.webhook_ref",),
            description=(
                "incoming webhook URL 이 담긴 **파일의 secrets.base_dir 상대 참조**"
                "(URL 자체가 시크릿이므로 값이 아니라 참조만 둔다). URL 얻는 법 — "
                "Google Chat: 스페이스 메뉴 → 앱 및 통합 → Webhook 관리 → 웹훅 추가 / "
                "Slack: api.slack.com/apps → 앱 생성 → Incoming Webhooks 켜기 → "
                "Add New Webhook to Workspace / generic_webhook: 사내 수신 엔드포인트 URL."
            ),
            example="service/notifier-webhook",
        ),
        SchemaField(
            key="notifier.notify_interrupted",
            type=FieldType.BOOL,
            default=True,
            legacy_keys=("notify.notify_interrupted",),
            description="토큰/레이트 한도로 중단(interrupted)됐을 때도 짧게 알릴지.",
        ),
        SchemaField(
            key="notifier.notify_cancelled",
            type=FieldType.BOOL,
            default=True,
            legacy_keys=("notify.notify_cancelled",),
            description="티켓 취소(cancelled)로 잡이 끝났을 때도 짧게 알릴지.",
        ),
    ),
)

_JIRA = SchemaSection(
    name="jira",
    title="Jira 인스턴스",
    description=(
        "감시할 Jira 와 **그 인스턴스에서만 통하는 값들**. 커스텀필드 id·전이 id·상태 "
        "이름·라벨은 인스턴스마다 다르다 — 지금까지 일부가 코드 상수였고"
        "(app/jira_client.py) 그래서 다른 조직에서는 그대로 쓸 수 없었다."
    ),
    fields=(
        SchemaField(
            key="jira.base_url",
            type=FieldType.STRING,
            required=True,
            description="Jira Cloud 사이트 URL(끝 슬래시는 제거된다).",
            example="https://your-org.atlassian.net",
        ),
        SchemaField(
            key="jira.project",
            type=FieldType.STRING,
            required=True,
            description="감시할 프로젝트 키. 폴러 JQL 의 project 절이 된다.",
            example="PROJ",
        ),
        SchemaField(
            key="jira.poll_interval_sec",
            type=FieldType.INT,
            default=60,
            description="폴링 주기(초). 웹훅이 켜져 있으면 폴링은 백스톱이라 짧을 필요가 없다.",
        ),
        SchemaField(
            key="jira.watcher_token_file",
            type=FieldType.STRING,
            required=True,
            secret_ref=True,
            description="central 감시 계정 Jira API 토큰 **파일의 secrets.base_dir 상대 참조**.",
            example="service/jira-token",
        ),
        SchemaField(
            key="jira.watcher_email",
            type=FieldType.STRING,
            default="",
            description="감시 계정 이메일(Basic auth actor). 비우면 env ``JIRA_WATCHER_EMAIL`` 폴백.",
        ),
        SchemaField(
            key="jira.trigger_statuses",
            type=FieldType.STRING_LIST,
            required=True,
            default=[],
            legacy_keys=("match.statuses",),
            description=(
                "신규 착수(트리거) 상태 화이트리스트. **이 워크플로우의 상태 이름 그대로** "
                "적는다(언어·명명 규칙이 조직마다 다르다)."
            ),
            example=["해야 할 일"],
        ),
        SchemaField(
            key="jira.cancel_statuses",
            type=FieldType.STRING_LIST,
            default=["취소됨"],
            legacy_keys=("match.cancel_statuses",),
            description=(
                "'취소' 상태 이름들. 이 상태로 들어온 티켓은 추적 잡을 즉시 취소한다. "
                "명시적으로 빈 리스트를 주면 기능을 끈다(기본값은 하위호환 유지값)."
            ),
            example=["취소됨"],
        ),
        SchemaField(
            key="jira.optout_labels",
            type=FieldType.STRING_LIST,
            default=["자동화_추적_해제"],
            legacy_keys=("match.optout_labels",),
            description=(
                "자동화 추적 해제 라벨. 붙으면 신규 착수하지 않고, 추적 중이면 취소로 "
                "수렴한다. 빈 리스트면 기능을 끈다(기본값은 하위호환 유지값)."
            ),
            example=["자동화_추적_해제"],
        ),
        SchemaField(
            key="jira.custom_fields",
            type=FieldType.STRING_MAP,
            default={},
            description=(
                "논리 키 → 이 인스턴스의 커스텀필드 id 매핑. 논리 키 목록·설명·오늘의 "
                "기본값은 :data:`JIRA_CUSTOM_FIELD_KEYS` 참조. 비우거나 일부만 주면 "
                "나머지는 app/jira_client.py 모듈 상수를 그대로 쓴다(하위호환)."
            ),
            example={"start_date": "customfield_10015", "due_date": "duedate"},
        ),
        SchemaField(
            key="jira.done_transition_id",
            type=FieldType.STRING,
            default="",
            description=(
                "'완료' 전이의 숫자 id(문자열). 인스턴스마다 다르다 — "
                "``GET /rest/api/2/issue/{key}/transitions`` 로 실측해 채운다. 비우면 "
                "이름(``jira.done_transition_names``)으로 찾고, 이름도 안 맞으면 done "
                "카테고리 전이가 **정확히 하나**일 때 그것을 쓴다. 그래도 못 정하면 "
                "모듈 상수 기본값을 쓰는데 **조건이 있다** — 그 상수 id 가 이 이슈의 "
                "실제 done 전이 목록에 있을 때만 쓴다(남의 인스턴스에서 같은 id 가 "
                "엉뚱한 전이인 경우를 고르지 않으려고). 그 조건도 못 맞추면 폴백하지 "
                "않고 **가능한 전이 목록을 담은 에러**로 실패한다(조용한 오작동 금지). "
                "예외: jira 설정을 아예 주입하지 않은 레거시 클라이언트만 무조건 상수로 "
                "폴백한다(기존 배포 무변경 보장). 구현은 "
                ":meth:`app.jira_client.JiraClient.resolve_done_transition_id`."
            ),
            example="41",
        ),
        SchemaField(
            key="jira.done_transition_names",
            type=FieldType.STRING_LIST,
            default=["완료", "Done"],
            description=(
                "id 대신 **이름**으로 완료 전이를 식별할 때의 후보 목록. id 를 모르는 "
                "설치자가 그대로 쓸 수 있게 하는 폴백 경로."
            ),
        ),
    ),
)

_DOCS_REPO = SchemaSection(
    name="docs_repo",
    title="설계 문서 레포(선택)",
    description=(
        "에이전트가 **참고**하는 설계 문서 저장소. 완전히 선택이다 — URL 이 비면 "
        "프로비저닝을 조용히 건너뛰고 경고만 남긴다(잡은 정상 진행). 예전 이름 "
        "``dataspace_docs`` 는 특정 프로젝트의 레포 이름이었다."
    ),
    optional=True,
    fields=(
        SchemaField(
            key="run.docs_repo_url",
            type=FieldType.STRING,
            default="",
            legacy_keys=("run.dataspace_docs_repo_url",),
            description=(
                "설계 문서 레포의 clone URL(**토큰 없는 형태**). 비우면 이 레포를 쓰지 "
                "않는다(프로비저닝 skip + 경고)."
            ),
            example="https://gitlab.example.com/<your-group>/docs.git",
        ),
        SchemaField(
            key="run.docs_repo",
            type=FieldType.STRING,
            default="",
            legacy_keys=("run.dataspace_docs_repo",),
            description=(
                "공유 워크스페이스 안의 로컬 클론 경로. 비우면 "
                "``<run.workspace_dir>/docs`` 로 파생한다."
            ),
            example="/app/workspace/docs",
        ),
    ),
)

_DEPLOY = SchemaSection(
    name="deploy",
    title="배포 형태",
    description=(
        "어디에 세우는가. 프로파일 하나만 고르면 나머지 값이 파생된다"
        "(:data:`PROFILE_DEFAULTS`) — 개별 값을 명시하면 그게 우선한다."
    ),
    fields=(
        SchemaField(
            key="deploy.profile",
            type=FieldType.ENUM,
            choices=DEPLOY_PROFILES,
            required=True,
            default="local",
            description=(
                "배포 형태. ``local``=개발자 노트북(도커 소켓 직결), "
                "``cloud_vm``/``onprem_server``=compose + socket-proxy 경유."
            ),
            example="local",
        ),
        SchemaField(
            key="deploy.host_deploy_dir",
            type=FieldType.STRING,
            default="",
            required_if=RequiredIf("deploy.profile", equals=("cloud_vm", "onprem_server")),
            legacy_keys=("spawn.host_deploy_dir",),
            description=(
                "**호스트**의 배포 디렉토리 절대경로(central 컨테이너 내부 경로가 아니다). "
                "central 이 sibling 컨테이너로 워커를 띄울 때 바인드 source 를 호스트 "
                "docker 데몬이 해석하기 때문에 필요하다. env ``HOST_DEPLOY_DIR`` 이 우선."
            ),
            example="/home/<deploy-user>/deploy/jira-auto-dispatcher",
        ),
        SchemaField(
            key="deploy.docker_host",
            type=FieldType.STRING,
            default="unix:///var/run/docker.sock",
            legacy_keys=("spawn.docker_host",),
            description=(
                "워커를 띄울 docker 엔드포인트. 서버 프로파일은 socket-proxy 경유를 "
                "기본으로 한다(소켓 직결은 특권 확대)."
            ),
            example="tcp://socket-proxy:2375",
        ),
        SchemaField(
            key="deploy.secrets_base_dir",
            type=FieldType.STRING,
            required=True,
            default="",
            legacy_keys=("secrets.base_dir",),
            description=(
                "시크릿 **파일들의 루트 디렉토리**(값이 아니라 파일이 사는 곳). 모든 "
                "``*_ref``·``*_token_file`` 은 이 경로 상대다. env ``SECRETS_DIR`` 로도 준다."
            ),
            example="/run/secrets",
        ),
        SchemaField(
            key="deploy.workspace_volume",
            type=FieldType.STRING,
            default="jad-workspace",
            legacy_keys=("spawn.workspace_volume",),
            description=(
                "central·모든 워커가 공유하는 워크스페이스 **named 볼륨** 이름"
                "(레포 단일 클론·단일 pull 지점)."
            ),
        ),
    ),
)

_CONSENT = SchemaSection(
    name="consent",
    title="풀 퍼미션 동의",
    description=(
        "⚠️ 이 시스템은 **사람의 매 단계 승인 없이** 도구 권한(파일 쓰기·셸·git push)을 가진 "
        "코딩 에이전트를 헤드리스로 실행한다. 그 위험을 이해하고 감수한다는 설치자의 명시 "
        "동의를 남긴다 — 기본값은 '동의 안 함'이며, 동의는 사람이 직접 켜야 한다."
    ),
    fields=(
        SchemaField(
            key="consent.full_permissions",
            type=FieldType.BOOL,
            required=True,
            default=False,
            description=(
                "에이전트를 풀 퍼미션(사람 승인 없이 도구 사용)으로 실행하는 데 동의한다. "
                "이 값이 참이 아니면 설치는 완료된 것으로 보지 않는다."
            ),
        ),
        SchemaField(
            key="consent.accepted_at",
            type=FieldType.STRING,
            required_if=RequiredIf("consent.full_permissions", truthy=True),
            default="",
            description="동의 시각(ISO-8601, 예: 2026-08-25T09:00:00+09:00). 감사 흔적.",
            example="2026-08-25T09:00:00+09:00",
        ),
    ),
)

#: 온보딩이 물어야 하는 항목 **전체**(선언 순서 = 권장 온보딩 진행 순서).
SETUP_SCHEMA: Tuple = (_FORGE, _NOTIFIER, _JIRA, _DOCS_REPO, _DEPLOY, _CONSENT)


# ---------------------------------------------------------------------------
# 조회 헬퍼(순수 — 검증·렌더링은 후속 작업의 몫)
# ---------------------------------------------------------------------------


def iter_sections() -> Iterator[SchemaSection]:
    """선언 순서대로 섹션을 순회한다."""
    yield from SETUP_SCHEMA


def iter_fields() -> Iterator[SchemaField]:
    """모든 섹션의 모든 필드를 선언 순서대로 순회한다."""
    for section in SETUP_SCHEMA:
        yield from section.fields


def get_section(name: str) -> Optional[SchemaSection]:
    """섹션 이름으로 조회(없으면 None)."""
    for section in SETUP_SCHEMA:
        if section.name == name:
            return section
    return None


def get_field(key: str) -> Optional[SchemaField]:
    """점 표기 키 경로로 필드 조회(없으면 None)."""
    for f in iter_fields():
        if f.key == key:
            return f
    return None


def legacy_key_map() -> dict:
    """``{레거시 키 경로: 신규 키 경로}`` — 하위호환 매핑 한눈에 보기.

    후속 마이그레이션 도구가 옛 config.yaml 을 신규 키로 옮길 때 쓰라고 노출한다.
    """
    out: dict = {}
    for f in iter_fields():
        for old in f.legacy_keys:
            out[old] = f.key
    return out
