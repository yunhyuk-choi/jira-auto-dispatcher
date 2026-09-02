"""사용자 레지스트리 — 등록 사용자 CRUD + 영속(중앙 전용).

역할:
    자동 트리거 대상 사용자를 등록/조회/수정/삭제하고 state/registry.json에
    영속한다. 폴러는 Jira 담당자(account_id)를 이 레지스트리로 사용자에 매핑하고,
    스포너는 여기 담긴 컨테이너 정보로 사용자 worker를 기동한다.

역할 소속: **central** (worker는 이 모듈을 쓰지 않는다).

구현 Phase: **Phase 3** (레지스트리 + 온보딩).

레코드 스키마(1 사용자):
    username            로그인/식별 키(고유). worker의 DISPATCH_USER.
                        ⚠️ 고유성은 **대소문자를 무시하고** 본다 — 시크릿 디렉토리
                        (``secrets/<user>/``)가 대소문자를 구별하지 않는 파일시스템에
                        놓이면 ``Alice``·``alice`` 가 서로의 토큰을 덮어쓴다
                        (:func:`username_key`).
    display_name        표시 이름
    jira_account_id     Jira 담당자 계정ID(폴러 매핑 키)
    jira_email          Jira 계정 이메일(Basic auth actor)
    enabled             자동 트리거 토글(false면 폴러가 skip)
    autonomy_mode       "A"(완전자율 MR초안) | "B"(경량1차+로컬완성)
    permission_level    worker 컨테이너 사전 인가 레벨(기본 "bypass").
                        스포너가 Claude settings.json으로 번역(SECURITY.md).
                        향후 "sandbox"·"allowlist"로 조일 수 있다(현재 TODO).
    agent               dlc-meta/agents/<user>/ 오버레이 키
    per_repo            {repo: "A"|"B"} 레포별 모드 오버라이드
    identity            {git_name, git_email} — worker git 커밋 author
    scope               {projects: [...]} — 이 사용자가 받을 Jira 프로젝트
    consent             {full_permissions, accepted_at} — **본인**의 풀 퍼미션 동의와
                        그 시각(ISO-8601, 서버 수신 시각). 이 사람의 자격증명으로 자율
                        에이전트가 도는 데 대한 동의라 설치자의 동의로 갈음하지 않는다
                        (app/onboarding.py 가 등록 시 강제한다).
    container           {name, status} — 스포너가 갱신하는 worker 컨테이너 상태
    secrets_ref         {jira_token, forge_token, claude_oauth_token}
                        (``forge_token`` 이 정본. 옛 이름 ``gitlab_token`` 은 같은 값으로
                        미러돼 기존 registry.json·옛 리더가 그대로 동작한다.)
                        ⚠️ 시크릿 "값"이 아니라 "참조"만 담는다(파일경로/시크릿키/
                        볼륨 경로 등). 실토큰은 레지스트리에 절대 넣지 않는다.

참고:
    - POLICY-ENCODING: JSON은 UTF-8(BOM 없음)·LF, ensure_ascii=False.
    - 영속은 state.py(atomic_write_json)로 위임(부분 기록 손상 방지).
    - state/registry.json은 gitignore(온보딩으로 채워지는 운영 데이터).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Union

from app import state

log = logging.getLogger("jad.registry")


# ---------------------------------------------------------------------------
# username 이름 규칙 — **이 시스템에서 username 은 식별자가 아니라 "이름"이다**
# ---------------------------------------------------------------------------

#: username 최대 길이. 사람이 읽는 짧은 식별자이지 자유 문자열이 아니다.
USERNAME_MAX_LEN = 32

#: Windows 예약 장치명(확장자를 붙여도 예약이다 — ``CON.txt`` 도 파일이 될 수 없다).
#: 시크릿 디렉토리는 배포에 따라 Windows 호스트에도 생긴다.
_RESERVED_STEMS = "con|prn|aux|nul|com[1-9]|lpt[1-9]"

#: 허용하는 username 모양 — **디렉토리명과 docker 이름 양쪽에서 안전한 교집합**.
#:
#: 왜 이 집합인가 (username 이 흘러가는 두 자리가 근거다):
#:   1. **시크릿 디렉토리** ``secrets/<username>/`` (:func:`app.onboarding._write_secret`).
#:      ``/``·``\``·``..`` 가 들어가면 그건 이름이 아니라 **경로**이고, 무인증 관리 UI 가
#:      임의 위치에 0600 파일을 쓰는 통로가 된다. 그래서 경로 문자·상위 표기·공백·제어
#:      문자·비ASCII 를 전부 뺀다(비ASCII 는 파일시스템 정규화 차이로 같은 이름이 두 개가
#:      되는 문제까지 따라온다).
#:   2. **docker 컨테이너·볼륨 이름** ``<instance>-worker-<username>``·
#:      ``<instance>-<username>`` (:mod:`app.spawner`·:mod:`app.naming`).
#:      docker 가 허용하는 이름은
#:      ``[a-zA-Z0-9][a-zA-Z0-9_.-]*`` 다 — 즉 영숫자·``_``·``.``·``-`` 뿐이고, 이 규칙은
#:      그 집합을 **넘지 않는다**(우리 쪽이 더 좁다).
#:
#: 추가 제약과 그 이유:
#:   - **첫 글자는 영숫자** — docker 의 첫 글자 규칙과 같고, 동시에 ``..``·``.hidden`` ·
#:     ``-flag``(CLI 옵션으로 오인)를 한 번에 막는다.
#:   - **끝 글자도 영숫자** — Windows 는 이름 끝의 ``.``·공백을 잘라 버려서 ``a.`` 와 ``a``
#:     가 같은 디렉토리가 된다(이름 충돌).
#:   - **Windows 예약 장치명 금지** — ``CON``·``NUL``·``COM1`` … 은 디렉토리로 만들 수 없다.
#:   - **길이 1~32** — 사람이 읽는 이름의 상한. 컨테이너 이름 접두어(``jad-worker-``)를
#:     붙여도 여유가 넉넉하다.
#:
#: ⚠️ 대소문자는 **허용한다**(기존 이름을 무효로 만들지 않기 위해). 그 대가인 대소문자
#: 무시 파일시스템에서의 이름 충돌은 모양이 아니라 **등록 시 중복 검사**로 막는다
#: (:func:`username_key` · :meth:`Registry.find_case_conflict`).
USERNAME_PATTERN = (
    rf"(?!(?i:{_RESERVED_STEMS})(?:\..*)?\Z)"
    rf"[A-Za-z0-9](?:[A-Za-z0-9._-]{{0,{USERNAME_MAX_LEN - 2}}}[A-Za-z0-9])?"
)

#: 위 규칙을 사람 말로(거부 메시지의 유일한 안내 — 거부는 값을 되비추지 않는다).
USERNAME_HINT = (
    "영문자·숫자로 시작하고 끝나며, 가운데에 ``.`` ``_`` ``-`` 만 쓸 수 있습니다"
    f"(1~{USERNAME_MAX_LEN}자). 경로 구분자(/ \\)·``..``·공백·한글은 쓸 수 없고, "
    "Windows 예약 이름(CON·NUL·COM1 …)도 쓸 수 없습니다. 예: ``yhchoi``, ``yh.choi``."
)

USERNAME_RE = re.compile(USERNAME_PATTERN)


def is_valid_username(value: Any) -> bool:
    """디렉토리명·docker 이름 양쪽에서 안전한 username 모양인가(**단일 원천**).

    온보딩 게이트(:data:`app.user_schema.USER_SCHEMA` 의 ``username`` 선언)와 소비 지점
    (:func:`app.onboarding._write_secret` · :meth:`app.spawner.Spawner.container_name`)이
    **같은 이 함수**를 본다 — 판정이 두 벌이 되면 한쪽이 반드시 낡는다.
    """
    return bool(isinstance(value, str) and USERNAME_RE.fullmatch(value))


def username_key(value: Any) -> str:
    """**대소문자 무시 파일시스템에서 같은 자리를 쓰게 되는** 이름들의 공통 키.

    왜 필요한가 (실제로 무엇이 겹치는가):
        ``Alice`` 와 ``alice`` 는 이 레지스트리에서 **서로 다른 두 등록**이다(dict 키가
        정확 일치이므로). 그런데 온보딩이 자격증명을 쓰는 자리
        (:func:`app.onboarding._write_secret` → ``secrets/<username>/``)는 **호스트
        파일시스템**이고, Windows(NTFS 기본)·macOS(APFS 기본)는 대소문자를 구별하지 않는다.
        그래서 나중에 등록한 쪽이 앞사람의 ``jira-token``·``forge-token``·
        ``claude-oauth-token`` 을 **조용히 덮어쓴다** — 그 뒤로 Alice 의 워커는 alice 의
        자격증명으로 남의 Jira·forge 에 글을 쓴다. 정확 일치 중복 검사는 이걸 통과시킨다.

    ⚠️ **docker 이름은 이 문제를 겪지 않는다.** 컨테이너(``<instance>-worker-<user>``)와
    볼륨(``<instance>-<user>``)의 이름은 항상 리눅스 docker 데몬이 바이트 단위로 비교한다
    (Docker Desktop 도 리눅스 VM 안에서 돈다) — ``Alice`` 와 ``alice`` 는 서로 다른
    컨테이너·서로 다른 볼륨이다. 겹치는 것은 **호스트에 바인드된 시크릿 디렉토리 하나**다.
    그래도 검사는 username 단위로 한 번만 한다 — 한 사람이 다른 사람의 토큰을 받는 순간
    컨테이너가 갈라져 있다는 사실은 위안이 되지 않는다.

    ``casefold()`` 를 쓴다 — 이름 규칙이 ASCII 만 허용하므로(:data:`USERNAME_PATTERN`)
    신규 이름에서는 ``lower()`` 와 결과가 같고, 규칙 이전에 등록된 비ASCII 이름에서는
    더 넓게(안전한 쪽으로) 접힌다.
    """
    return str(value or "").casefold()


# secrets_ref 안에 실토큰이 섞여 들어오는 것을 막기 위한 참조 키 화이트리스트.
# ``forge_token`` 이 정본이고 ``gitlab_token`` 은 레거시 미러 — 둘 다 허용해야 옛
# registry.json 과 새 온보딩이 함께 통과한다.
_SECRETS_REF_KEYS = ("jira_token", "forge_token", "gitlab_token", "claude_oauth_token")


@dataclass
class Identity:
    """git 커밋 author 정체성."""

    git_name: str = ""
    git_email: str = ""


@dataclass
class Scope:
    """이 사용자가 받을 작업 범위."""

    projects: list = field(default_factory=list)


@dataclass
class Consent:
    """이 사용자 **본인**의 풀 퍼미션 동의 기록(감사 흔적).

    이 시스템은 워커 안에서 ``--dangerously-skip-permissions`` 로 에이전트를 돌리고, 그
    에이전트는 **이 사람의** Jira·forge·Claude 자격증명으로 동작한다. 그러니 동의는 인스턴스
    단위가 아니라 사람 단위여야 한다 — 설치자가 켠 ``consent.full_permissions``(config.yaml)
    는 *설치자 자신*의 동의일 뿐이다.

    Attributes:
        full_permissions: 동의했는가. 기본 False(동의 없이는 등록이 통과하지 않는다).
        accepted_at: 동의 시각(ISO-8601). ⚠️ **서버 수신 시각**이다 — 클라이언트가 보낸
            시각은 쓰지 않는다(그러면 감사 흔적이 아니다).

    ⚠️ 이 필드가 없던 시절에 등록된 레코드는 ``full_permissions=False`` 로 읽힌다. 그것을
    소급 차단 근거로 쓰지 않는다(등록은 이미 끝났다) — 이 값은 *새 등록의 게이트*와 *누가
    언제 동의했는지의 기록*이다.
    """

    full_permissions: bool = False
    accepted_at: str = ""


@dataclass
class Container:
    """스포너가 갱신하는 worker 컨테이너 상태."""

    name: str = ""
    status: str = "absent"  # absent|created|running|stopped|error


@dataclass
class SecretsRef:
    """시크릿 참조(값이 아님) — 파일경로/시크릿키/볼륨 경로.

    ``forge_token`` 은 사용자가 브랜치를 push 하고 MR/PR 을 만들 때 쓰는 **개인 forge
    토큰**의 참조다(GitLab PAT / GitHub PAT — forge 는 ``config.forge.kind`` 가 정한다).

    ⚠️ ``gitlab_token`` 은 그 필드의 **레거시 이름**이며 정본(``forge_token``)과 항상
    같은 값으로 유지된다(:meth:`UserRecord.from_dict` 가 둘 중 채워진 쪽을 읽어 양쪽에
    싣는다). 덕분에 기존 ``state/registry.json`` 과 옛 이름을 읽는 코드가 그대로 동작한다.
    """

    forge_token: str = ""
    jira_token: str = ""
    gitlab_token: str = ""
    claude_oauth_token: str = ""

    def __post_init__(self) -> None:
        """forge ↔ gitlab 참조를 같은 값으로 수렴(직접 생성 경로 포함).

        :meth:`UserRecord.from_dict` 뿐 아니라 ``SecretsRef(gitlab_token=...)`` 처럼
        **직접 생성**하는 경로(옛 코드·테스트 대역)에서도 두 이름이 갈라지지 않게 한다.
        """
        ref = self.forge_token or self.gitlab_token
        self.forge_token = ref
        self.gitlab_token = ref


@dataclass
class UserRecord:
    """등록 사용자 1명."""

    username: str = ""
    display_name: str = ""
    jira_account_id: str = ""
    jira_email: str = ""
    # (선택) **알림 채널의 사용자 id** — 완료 알림 @멘션용. 없으면 display_name 폴백.
    # 값의 모양은 provider 마다 다르다(Google Chat=숫자 userId / Slack=U…). 시크릿 아님.
    notify_user_id: str = ""
    # ⚠️ 레거시 미러(Google Chat 전용 시절 이름). **정본은 위 notify_user_id** 이며 여기엔
    # 같은 값이 복사된다 — 옛 키를 읽는 코드·기존 registry.json 이 그대로 동작한다
    # (:meth:`from_dict` 가 둘 중 채워진 쪽을 읽어 양쪽에 싣는다).
    google_chat_user_id: str = ""
    enabled: bool = True
    autonomy_mode: str = "B"
    permission_level: str = "bypass"
    agent: str = "default"
    per_repo: dict = field(default_factory=dict)
    identity: Identity = field(default_factory=Identity)
    scope: Scope = field(default_factory=Scope)
    consent: Consent = field(default_factory=Consent)
    container: Container = field(default_factory=Container)
    secrets_ref: SecretsRef = field(default_factory=SecretsRef)

    def __post_init__(self) -> None:
        """알림 사용자 id 의 신규/레거시 이름을 같은 값으로 수렴(직접 생성 경로 포함)."""
        uid = self.notify_user_id or self.google_chat_user_id
        self.notify_user_id = uid
        self.google_chat_user_id = uid

    # -- 직렬화 --

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "UserRecord":
        d = dict(d or {})
        identity = d.get("identity") or {}
        scope = d.get("scope") or {}
        container = d.get("container") or {}
        secrets_ref = d.get("secrets_ref") or {}
        consent = d.get("consent") or {}
        # 알림 사용자 id: 신규 키 우선, 없으면 레거시 키(Google Chat 전용 시절)를 읽고
        # 양쪽 필드에 같은 값을 싣는다(옛 키를 읽는 코드도 그대로 동작 — 하위호환).
        notify_uid = str(d.get("notify_user_id", "") or d.get("google_chat_user_id", ""))
        # forge 토큰 참조도 같은 규율: 신규 forge_token 우선, 레거시 gitlab_token 폴백.
        forge_ref = str(secrets_ref.get("forge_token", "")
                        or secrets_ref.get("gitlab_token", ""))
        return UserRecord(
            username=str(d.get("username", "")),
            display_name=str(d.get("display_name", "")),
            jira_account_id=str(d.get("jira_account_id", "")),
            jira_email=str(d.get("jira_email", "")),
            notify_user_id=notify_uid,
            google_chat_user_id=notify_uid,
            enabled=bool(d.get("enabled", True)),
            autonomy_mode=str(d.get("autonomy_mode", "B")),
            permission_level=str(d.get("permission_level", "bypass") or "bypass"),
            agent=str(d.get("agent", "default")),
            per_repo=dict(d.get("per_repo", {}) or {}),
            identity=Identity(
                git_name=str(identity.get("git_name", "")),
                git_email=str(identity.get("git_email", "")),
            ),
            scope=Scope(projects=list(scope.get("projects", []) or [])),
            # 동의: 이 필드가 없던 시절의 registry.json 은 미동의로 읽힌다(위 Consent 주석).
            consent=Consent(
                full_permissions=bool(consent.get("full_permissions", False)),
                accepted_at=str(consent.get("accepted_at", "") or ""),
            ),
            container=Container(
                name=str(container.get("name", "")),
                status=str(container.get("status", "absent")),
            ),
            secrets_ref=SecretsRef(
                jira_token=str(secrets_ref.get("jira_token", "")),
                # forge 토큰 참조: 신규 키 우선, 없으면 레거시 키를 읽고 **양쪽에** 같은
                # 값을 싣는다(옛 이름을 읽는 코드·기존 registry.json 하위호환).
                forge_token=forge_ref,
                gitlab_token=forge_ref,
                claude_oauth_token=str(secrets_ref.get("claude_oauth_token", "")),
            ),
        )


class Registry:
    """사용자 레지스트리 CRUD + 영속."""

    def __init__(self) -> None:
        """락 + state/registry.json 로드로 초기화.

        ⚠️ **이름 규칙(:func:`is_valid_username`)으로 기존 레코드를 걸러내지 않는다.**
        규칙은 나중에 생겼고, 이 파일은 이미 운영 중인 등록의 정본이다 — 로드 단계에서
        떨어뜨리면 그 사람은 예고 없이 사라지고(폴러가 티켓을 매핑하지 못한다), 예외로
        올리면 그 한 줄이 **central 부팅 전체를 막는다.** 둘 다 이 시스템에서 가장 비싼
        실패 모드다. 그래서 기존 이름은 **그대로 싣되 경고로 드러낸다**(조용한 관용은
        관용이 아니라 은폐다). 규칙은 *새 등록의 게이트*로만 강제되고
        (:mod:`app.onboarding`), 위험한 이름이 실제로 경로·컨테이너 이름을 조립하려 들면
        그때 소비 지점이 막는다(:func:`app.onboarding._write_secret` ·
        :meth:`app.spawner.Spawner.container_name`) — 부팅이 아니라 그 사용자만 실패한다.
        같은 규율이 ``consent`` 필드에도 적용된다(위 :class:`Consent` 주석).
        """
        self._lock = threading.Lock()
        raw = state.load_registry()
        users = raw.get("users", []) if isinstance(raw, dict) else (raw or [])
        self._users: dict[str, UserRecord] = {}
        legacy_names: list = []
        for u in users:
            rec = UserRecord.from_dict(u)
            if rec.username:
                if not is_valid_username(rec.username):
                    # %r 로 싣는다 — repr 이 개행·제어문자를 이스케이프해 로그 라인이
                    # 조작되지 않는다(이름은 시크릿이 아니므로 가려서 얻을 게 없다).
                    legacy_names.append(rec.username)
                self._users[rec.username] = rec
        if legacy_names:
            log.warning(
                "현재 이름 규칙에 맞지 않는 username 이 레지스트리에 있습니다"
                "(그대로 사용합니다 — 부팅은 막지 않습니다). %d건: %s. 규칙: %s",
                len(legacy_names), ", ".join(repr(n) for n in legacy_names),
                USERNAME_HINT,
            )
        self._warn_case_conflicts()

    def _warn_case_conflicts(self) -> None:
        """이미 등록돼 있는 **대소문자만 다른 이름 짝**을 경고로 드러낸다.

        ⚠️ 부팅을 막지 않는다 — 이유는 :meth:`__init__` 의 이름 규칙 처리와 같다. 이
        파일은 운영 중인 등록의 정본이고, 한 줄 때문에 central 이 뜨지 않으면 설정을 고칠
        관리 UI 도 함께 사라진다. 그리고 여기서 **어느 쪽을 떨어뜨릴지 고를 근거가 없다**
        (둘 다 실재하는 사람이다). 그래서 새 등록은 :meth:`find_case_conflict` 로 막고,
        이미 들어와 있는 짝은 사람이 보고 손으로 정리하도록 이름을 찍어 준다.

        경고가 뜨는 배포에서 실제로 일어나는 일: 두 사람의 시크릿이 대소문자 무시
        파일시스템에서 같은 ``secrets/<user>/`` 를 쓰므로 **나중에 등록한 쪽의 토큰만
        남는다**(:func:`username_key`). 한쪽을 지우고 다른 이름으로 다시 등록해야 한다.
        """
        groups: dict = {}
        for name in self._users:
            groups.setdefault(username_key(name), []).append(name)
        clashes = [names for names in groups.values() if len(names) > 1]
        if not clashes:
            return
        log.warning(
            "⚠️ 대소문자만 다른 username 이 함께 등록돼 있습니다(부팅은 막지 않습니다). "
            "%d짝: %s. 대소문자를 구별하지 않는 파일시스템(Windows·macOS 기본)에서는 이들이 "
            "**같은 secrets/<user>/ 디렉토리**를 쓰므로 나중에 등록한 쪽이 앞사람의 Jira·"
            "forge·Claude 토큰을 덮어씁니다 — 한쪽을 삭제하고 겹치지 않는 이름으로 다시 "
            "등록한 뒤 그 사람의 토큰을 재발급하세요.",
            len(clashes),
            " / ".join(", ".join(repr(n) for n in sorted(names)) for names in clashes),
        )

    def find_case_conflict(self, username: str) -> Optional[str]:
        """``username`` 과 **대소문자만 다른** 기존 등록 이름(없으면 None).

        정확 일치는 여기서 걸리지 않는다 — 그건 "덮어쓰기(갱신)"이지 충돌이 아니고,
        호출부가 먼저 판정한다(:mod:`app.onboarding`).
        """
        key = username_key(username)
        with self._lock:
            for existing in self._users:
                if existing != username and username_key(existing) == key:
                    return existing
        return None

    def _persist(self) -> None:
        state.save_registry({"users": [u.to_dict() for u in self._users.values()]})

    def list_users(self) -> list:
        """전체 사용자 레코드 스냅샷(관리 UI 목록용)."""
        with self._lock:
            return list(self._users.values())

    def get(self, username: str) -> Optional[UserRecord]:
        """username으로 사용자 조회(없으면 None)."""
        with self._lock:
            return self._users.get(username)

    def find_by_account_id(self, account_id: str) -> Optional[UserRecord]:
        """Jira account_id로 사용자 조회(enabled 무관, 없으면 None)."""
        if not account_id:
            return None
        with self._lock:
            for u in self._users.values():
                if u.jira_account_id == account_id:
                    return u
        return None

    def get_by_account_id(self, account_id: str) -> Optional[UserRecord]:
        """Jira account_id로 **enabled 사용자만** 조회(폴러 매핑용).

        미등록 또는 비활성이면 None.
        """
        rec = self.find_by_account_id(account_id)
        return rec if (rec and rec.enabled) else None

    def upsert(self, record: Union[UserRecord, dict]) -> None:
        """사용자 레코드 등록/갱신 후 영속(온보딩 폼 → 이 경로).

        dict/UserRecord 모두 허용. 시크릿 "값"이 secrets_ref로 섞여 들어와도
        참조 키만 통과시킨다(방어). username 필수.

        **심층 방어** — *신규* 이름이 기존 이름과 대소문자만 다르면 거부한다
        (:func:`username_key`). 온보딩이 이미 같은 판정으로 409 를 주지만
        (:mod:`app.onboarding`), 레지스트리는 시크릿이 **이미 파일로 쓰인 뒤**에 불리는
        마지막 관문이라 여기서도 본다 — 상류 게이트가 빠지거나 다른 호출부가 생겼을 때
        조용히 남의 토큰을 덮어쓰는 등록이 성립하면 안 된다. 기존 이름을 **그대로** 다시
        upsert 하는 것(갱신·컨테이너 상태 반영)은 충돌이 아니다.
        """
        rec = record if isinstance(record, UserRecord) else UserRecord.from_dict(record)
        if not rec.username:
            raise ValueError("upsert: username은 필수입니다")
        clash = self.find_case_conflict(rec.username)
        if clash is not None:
            raise ValueError(
                f"이미 등록된 사용자 {clash!r} 와 대소문자만 다른 username 입니다 — "
                "대소문자를 구별하지 않는 파일시스템에서는 두 사람이 같은 "
                "secrets/<user>/ 디렉토리를 쓰게 되어 토큰이 서로 덮어써집니다. "
                "구별되는 이름을 쓰세요.")
        # secrets_ref는 참조 키만 유지(값이 실수로 섞이는 것 방지는 from_dict가 이미
        # 화이트리스트 필드만 취함 — 여기서는 존재 필드 재확인).
        _ = _SECRETS_REF_KEYS  # 문서화용 상수 참조
        with self._lock:
            self._users[rec.username] = rec
            self._persist()

    def set_enabled(self, username: str, enabled: bool) -> None:
        """자동 트리거 토글(UI enabled 스위치)."""
        with self._lock:
            u = self._users.get(username)
            if u is None:
                raise KeyError(f"등록되지 않은 사용자: {username}")
            u.enabled = bool(enabled)
            self._persist()

    def set_container_status(self, username: str, name: str, status: str) -> None:
        """worker 컨테이너 상태 갱신(스포너가 호출)."""
        with self._lock:
            u = self._users.get(username)
            if u is None:
                raise KeyError(f"등록되지 않은 사용자: {username}")
            u.container = Container(name=name, status=status)
            self._persist()

    # 하위호환 별칭(기존 스텁 시그니처).
    def set_container(self, username: str, name: str, status: str) -> None:
        """set_container_status 별칭(하위호환)."""
        self.set_container_status(username, name, status)

    def remove(self, username: str) -> None:
        """사용자 제거 후 영속(컨테이너 정리는 스포너 담당)."""
        with self._lock:
            if username in self._users:
                del self._users[username]
                self._persist()
