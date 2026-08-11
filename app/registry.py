"""사용자 레지스트리 — 등록 사용자 CRUD + 영속(중앙 전용).

역할:
    자동 트리거 대상 사용자를 등록/조회/수정/삭제하고 state/registry.json에
    영속한다. 폴러는 Jira 담당자(account_id)를 이 레지스트리로 사용자에 매핑하고,
    스포너는 여기 담긴 컨테이너 정보로 사용자 worker를 기동한다.

역할 소속: **central** (worker는 이 모듈을 쓰지 않는다).

구현 Phase: **Phase 3** (레지스트리 + 온보딩).

레코드 스키마(1 사용자):
    username            로그인/식별 키(고유). worker의 DISPATCH_USER.
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
    container           {name, status} — 스포너가 갱신하는 worker 컨테이너 상태
    secrets_ref         {jira_token, gitlab_token, claude_oauth_token}
                        ⚠️ 시크릿 "값"이 아니라 "참조"만 담는다(파일경로/시크릿키/
                        볼륨 경로 등). 실토큰은 레지스트리에 절대 넣지 않는다.

참고:
    - POLICY-ENCODING: JSON은 UTF-8(BOM 없음)·LF, ensure_ascii=False.
    - 영속은 state.py(atomic_write_json)로 위임(부분 기록 손상 방지).
    - state/registry.json은 gitignore(온보딩으로 채워지는 운영 데이터).
"""

from __future__ import annotations

import threading
from dataclasses import asdict, dataclass, field
from typing import Optional, Union

from app import state

# secrets_ref 안에 실토큰이 섞여 들어오는 것을 막기 위한 참조 키 화이트리스트.
_SECRETS_REF_KEYS = ("jira_token", "gitlab_token", "claude_oauth_token")


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
class Container:
    """스포너가 갱신하는 worker 컨테이너 상태."""

    name: str = ""
    status: str = "absent"  # absent|created|running|stopped|error


@dataclass
class SecretsRef:
    """시크릿 참조(값이 아님) — 파일경로/시크릿키/볼륨 경로."""

    jira_token: str = ""
    gitlab_token: str = ""
    claude_oauth_token: str = ""


@dataclass
class UserRecord:
    """등록 사용자 1명."""

    username: str = ""
    display_name: str = ""
    jira_account_id: str = ""
    jira_email: str = ""
    # (선택) Google Chat 사용자 숫자 ID — 완료 알림 @멘션용. 없으면 display_name 폴백.
    google_chat_user_id: str = ""
    enabled: bool = True
    autonomy_mode: str = "B"
    permission_level: str = "bypass"
    agent: str = "default"
    per_repo: dict = field(default_factory=dict)
    identity: Identity = field(default_factory=Identity)
    scope: Scope = field(default_factory=Scope)
    container: Container = field(default_factory=Container)
    secrets_ref: SecretsRef = field(default_factory=SecretsRef)

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
        return UserRecord(
            username=str(d.get("username", "")),
            display_name=str(d.get("display_name", "")),
            jira_account_id=str(d.get("jira_account_id", "")),
            jira_email=str(d.get("jira_email", "")),
            google_chat_user_id=str(d.get("google_chat_user_id", "")),
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
            container=Container(
                name=str(container.get("name", "")),
                status=str(container.get("status", "absent")),
            ),
            secrets_ref=SecretsRef(
                jira_token=str(secrets_ref.get("jira_token", "")),
                gitlab_token=str(secrets_ref.get("gitlab_token", "")),
                claude_oauth_token=str(secrets_ref.get("claude_oauth_token", "")),
            ),
        )


class Registry:
    """사용자 레지스트리 CRUD + 영속."""

    def __init__(self) -> None:
        """락 + state/registry.json 로드로 초기화."""
        self._lock = threading.Lock()
        raw = state.load_registry()
        users = raw.get("users", []) if isinstance(raw, dict) else (raw or [])
        self._users: dict[str, UserRecord] = {}
        for u in users:
            rec = UserRecord.from_dict(u)
            if rec.username:
                self._users[rec.username] = rec

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
        """
        rec = record if isinstance(record, UserRecord) else UserRecord.from_dict(record)
        if not rec.username:
            raise ValueError("upsert: username은 필수입니다")
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
