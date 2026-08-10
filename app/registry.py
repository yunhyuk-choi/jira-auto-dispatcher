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

from dataclasses import dataclass, field
from typing import Optional


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
    enabled: bool = True
    autonomy_mode: str = "B"
    agent: str = "default"
    per_repo: dict = field(default_factory=dict)
    identity: Identity = field(default_factory=Identity)
    scope: Scope = field(default_factory=Scope)
    container: Container = field(default_factory=Container)
    secrets_ref: SecretsRef = field(default_factory=SecretsRef)


class Registry:
    """사용자 레지스트리 CRUD + 영속(스텁)."""

    def __init__(self) -> None:
        """락 + state/registry.json 로드로 초기화.

        TODO(Phase 3): threading.Lock + state.load(registry) 복원.
        """
        pass

    def list_users(self) -> list:
        """전체 사용자 레코드 스냅샷(관리 UI 목록용).

        TODO(Phase 3): 레코드 목록 반환.
        """
        raise NotImplementedError("TODO(Phase 3): list_users")

    def get(self, username: str) -> Optional[UserRecord]:
        """username으로 사용자 조회(없으면 None).

        TODO(Phase 3): 락 안에서 조회.
        """
        raise NotImplementedError("TODO(Phase 3): get")

    def find_by_account_id(self, account_id: str) -> Optional[UserRecord]:
        """Jira account_id로 사용자 조회(폴러 매핑용, 없으면 None).

        TODO(Phase 3): jira_account_id 일치 검색.
        """
        raise NotImplementedError("TODO(Phase 3): find_by_account_id")

    def upsert(self, record: UserRecord) -> None:
        """사용자 레코드 등록/갱신 후 영속(온보딩 폼 → 이 경로).

        TODO(Phase 3): 검증 → 락 안에서 추가/교체 → 영속.
        시크릿 "값"이 record에 섞여 들어오지 않도록 방어(참조만 저장).
        """
        raise NotImplementedError("TODO(Phase 3): upsert")

    def set_enabled(self, username: str, enabled: bool) -> None:
        """자동 트리거 토글(UI enabled 스위치).

        TODO(Phase 3): 필드 갱신 + 영속.
        """
        raise NotImplementedError("TODO(Phase 3): set_enabled")

    def set_container(self, username: str, name: str, status: str) -> None:
        """worker 컨테이너 상태 갱신(스포너가 호출).

        TODO(Phase 3): container 필드 갱신 + 영속.
        """
        raise NotImplementedError("TODO(Phase 3): set_container")

    def remove(self, username: str) -> None:
        """사용자 제거 후 영속.

        TODO(Phase 3): 락 안에서 삭제 → 영속. (컨테이너 정리는 스포너 담당)
        """
        raise NotImplementedError("TODO(Phase 3): remove")
