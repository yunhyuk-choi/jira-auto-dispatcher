"""설정 로드/검증 계층.

역할:
    config/config.yaml(gitignore됨)을 읽어 검증하고, 타입드 설정 객체로
    노출한다. token_file 등 파일 참조는 여기서 읽어 메모리에만 보관한다.

구현 Phase: **Phase 1** (config + state 영속).

참고:
    - 스키마 정본은 config/config.example.yaml.
    - 누락/오타 키는 부팅 시점에 명확한 에러로 실패시킨다(fail-fast).
    - POLICY-ENCODING: 파일은 UTF-8(BOM 없음)로 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

DEFAULT_CONFIG_PATH = "config/config.yaml"


@dataclass
class JiraConfig:
    """[jira] 섹션."""

    base_url: str = ""
    project: str = ""
    email: str = ""
    token_file: str = ""
    poll_interval_sec: int = 60


@dataclass
class TriggerConfig:
    """[triggers[]] 항목 — 자동 실행 대상 사용자 1명."""

    account_id: str = ""
    display_name: str = ""
    enabled: bool = True
    autonomy_mode: str = "B"  # A=완전자율 MR초안 / B=경량1차+로컬완성
    agent: str = "default"
    per_repo: dict = field(default_factory=dict)


@dataclass
class MatchConfig:
    """[match] 섹션 — 트리거 조건."""

    statuses: list = field(default_factory=list)


@dataclass
class RunConfig:
    """[run] 섹션 — 오케스트레이터 실행 파라미터."""

    orchestrator_repo: str = ""
    dlc_meta_repo: str = ""
    dataspace_docs_repo: str = ""
    workspace_dir: str = ""
    claude_bin: str = "claude"
    permission_mode: str = "skip"
    output_format: str = "stream-json"
    concurrency: int = 1


@dataclass
class WebhookConfig:
    """[webhook] 섹션."""

    enabled: bool = False
    path: str = "/jira-webhook"
    shared_secret_file: str = ""


@dataclass
class ResumeConfig:
    """[resume] 섹션 — 재개/드레인 스케줄."""

    work_hours: str = ""
    timezone: str = "Asia/Seoul"
    nightly_drain: str = ""
    reset_buffer_sec: int = 120


@dataclass
class GitConfig:
    """[git] 섹션."""

    branch_prefix: str = "auto/"


@dataclass
class AppConfig:
    """전체 설정의 루트 객체."""

    jira: JiraConfig = field(default_factory=JiraConfig)
    triggers: list = field(default_factory=list)
    match: MatchConfig = field(default_factory=MatchConfig)
    run: RunConfig = field(default_factory=RunConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    git: GitConfig = field(default_factory=GitConfig)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """config.yaml을 읽어 검증된 AppConfig로 반환한다.

    TODO(Phase 1): yaml.safe_load → 스키마 검증 → 타입드 객체 매핑.
    누락 필수키·타입 불일치는 명확한 예외로 실패시킨다.
    """
    raise NotImplementedError("TODO(Phase 1): config 로드/검증 구현")


def read_secret_file(path: str) -> Optional[str]:
    """토큰/시크릿 파일을 읽어 문자열로 반환(없으면 None).

    TODO(Phase 1): UTF-8 읽기 + strip. 파일 부재 시 명확한 처리.
    """
    raise NotImplementedError("TODO(Phase 1): 시크릿 파일 읽기 구현")
