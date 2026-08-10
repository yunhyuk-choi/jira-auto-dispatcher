"""설정 로드/검증 계층(중앙 전용).

역할:
    config/config.yaml(gitignore됨)을 읽어 검증하고, 타입드 설정 객체로
    노출한다. 시크릿 "값"은 담지 않는다 — watcher_token_file 등 파일 참조만
    두고, 값은 secrets.base_dir 기준으로 런타임에 읽는다.

역할 소속: **central** (worker는 대부분 env로 구성되며 run.* 만 공유).

구현 Phase: **Phase 1** (config + state 영속).

참고:
    - 스키마 정본은 config/config.example.yaml.
    - 사용자별 트리거 목록은 더 이상 config가 아니라 registry(state/registry.json)
      가 소유한다(온보딩으로 채워짐). config는 시스템 수준 설정만 담는다.
    - 누락/오타 키는 부팅 시점에 명확한 에러로 실패시킨다(fail-fast).
    - POLICY-ENCODING: 파일은 UTF-8(BOM 없음)로 읽는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field


DEFAULT_CONFIG_PATH = "config/config.yaml"


@dataclass
class ServerConfig:
    """[server] 섹션 — central Flask 바인딩."""

    host: str = "0.0.0.0"
    port: int = 8787


@dataclass
class JiraConfig:
    """[jira] 섹션 — 중앙이 Jira를 감시하는 설정."""

    base_url: str = ""
    project: str = ""
    poll_interval_sec: int = 60
    watcher_token_file: str = ""  # 중앙 감시 토큰(내 것/봇). secrets.base_dir 상대


@dataclass
class MatchConfig:
    """[match] 섹션 — 트리거 조건."""

    statuses: list = field(default_factory=list)


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
class SpawnConfig:
    """[spawn] 섹션 — 사용자 worker 컨테이너 동적 기동 파라미터."""

    image: str = "jira-auto-dispatcher:latest"
    network: str = "jad-net"
    central_url: str = "http://central:8787"
    mem_limit: str = "4g"
    docker_host: str = "unix:///var/run/docker.sock"


@dataclass
class GitConfig:
    """[git] 섹션."""

    branch_prefix: str = "auto/"
    github_owner: str = ""  # github은 central만


@dataclass
class SecretsConfig:
    """[secrets] 섹션 — 시크릿 파일 기준 경로(값 아님)."""

    base_dir: str = ""  # 로컬=C:/temp, 컨테이너=/run/secrets 또는 볼륨


@dataclass
class RunConfig:
    """[run] 섹션 — 오케스트레이터 실행 파라미터(central↔worker 공유)."""

    orchestrator_repo: str = ""
    dlc_meta_repo: str = ""
    dataspace_docs_repo: str = ""
    workspace_dir: str = ""
    claude_bin: str = "claude"
    permission_mode: str = "skip"
    output_format: str = "stream-json"
    concurrency_per_worker: int = 1


@dataclass
class AppConfig:
    """전체 설정의 루트 객체."""

    role: str = "central"  # central | worker
    server: ServerConfig = field(default_factory=ServerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    match: MatchConfig = field(default_factory=MatchConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    spawn: SpawnConfig = field(default_factory=SpawnConfig)
    git: GitConfig = field(default_factory=GitConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    run: RunConfig = field(default_factory=RunConfig)


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """config.yaml을 읽어 검증된 AppConfig로 반환한다.

    TODO(Phase 1): yaml.safe_load → ${ENV} 치환(secrets.base_dir 등) →
    스키마 검증 → 타입드 객체 매핑. 누락 필수키·타입 불일치는 명확한 예외로.
    """
    raise NotImplementedError("TODO(Phase 1): config 로드/검증 구현")


def read_secret(base_dir: str, ref: str) -> "str | None":
    """secrets.base_dir 기준으로 시크릿 참조(ref=상대경로)를 읽어 반환.

    TODO(Phase 1): os.path.join(base_dir, ref) UTF-8 읽기 + strip.
    파일 부재 시 명확한 처리(None 또는 예외).
    """
    raise NotImplementedError("TODO(Phase 1): 시크릿 파일 읽기 구현")
