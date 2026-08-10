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

env 오버라이드(런타임 우선):
    - ``ROLE``                → role
    - ``SECRETS_DIR``         → secrets.base_dir 의 ``${SECRETS_DIR}`` 치환값
    - ``CENTRAL_URL``         → spawn.central_url (worker→central 폴링 대상)
    - ``WORKER_SHARED_SECRET``→ worker_shared_secret (dispatch HTTP 인증)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any


DEFAULT_CONFIG_PATH = "config/config.yaml"

# ${VAR} 형태의 env 치환 토큰
_ENV_TOKEN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ConfigError(Exception):
    """설정 로드/검증 실패(fail-fast)."""


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
    watcher_email: str = ""       # Basic auth actor(감시 계정 이메일). env JIRA_WATCHER_EMAIL 폴백


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
    run_as: str = "1000:1000"  # worker 컨테이너 비-root 실행 사용자(특권 축소)
    # ⚠️ 호스트 배포 디렉토리의 **절대경로**(central 컨테이너 내부 경로가 아님).
    # central이 Docker SDK(socket-proxy 경유)로 worker를 띄울 때 바인드 마운트의
    # source 경로는 **호스트 docker 데몬**이 해석한다(sibling container). 따라서
    # worker 바인드 source를 호스트 경로로 주려면 central이 자신의 호스트 배포
    # 경로를 알아야 한다. 예: /home/<deploy-user>/deploy/jira-auto-dispatcher.
    # env HOST_DEPLOY_DIR 폴백 우선. 비어 있으면(로컬 개발 등) 직접 경로 폴백.
    host_deploy_dir: str = ""


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
    # 위 3개 레포의 clone 원본 URL(토큰 없는 형태). 비면 그 레포 프로비저닝 skip.
    # worker가 사용자 GitLab 토큰으로 clone(없으면)/pull(있으면)한다 — app/repos.py.
    orchestrator_repo_url: str = ""
    dlc_meta_repo_url: str = ""
    dataspace_docs_repo_url: str = ""
    workspace_dir: str = ""
    claude_bin: str = "claude"
    permission_mode: str = "skip"
    output_format: str = "stream-json"
    concurrency_per_worker: int = 1
    # 전역 동시성 cap(worker당이 아니라 central 전체). config에 없으면 기본 3.
    global_concurrency: int = 3


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
    # dispatch HTTP(worker→central) 공유 시크릿. env WORKER_SHARED_SECRET 우선.
    worker_shared_secret: str = ""
    # 티켓 components/labels → 레포 매핑(REPO-MAP). {키: [repo,...]} 또는 {키: repo}.
    repo_map: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 내부 헬퍼
# ---------------------------------------------------------------------------


def _substitute_env(value: Any) -> Any:
    """문자열 내 ``${VAR}`` 토큰을 os.environ 값으로 치환(재귀).

    미정의 env는 원문 그대로 남긴다(부재를 조용히 빈 문자열로 만들지 않음 →
    검증 단계에서 드러나도록).
    """
    if isinstance(value, str):
        def repl(m: "re.Match[str]") -> str:
            name = m.group(1)
            return os.environ.get(name, m.group(0))

        return _ENV_TOKEN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


def _section(raw: dict, key: str) -> dict:
    """raw[key]가 dict가 아니면 빈 dict로(관대한 섹션 접근)."""
    v = raw.get(key)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------


def load_config(path: str = DEFAULT_CONFIG_PATH) -> AppConfig:
    """config.yaml을 읽어 검증된 AppConfig로 반환한다.

    순서: yaml.safe_load → env 치환(${VAR}) → env 오버라이드 →
    타입드 매핑 → 필수키 검증(fail-fast).

    Raises:
        ConfigError: 파일 부재, 파싱 실패, 필수키 누락 시.
    """
    import yaml  # 지연 import(테스트가 config 없이도 dataclass만 쓸 수 있게)

    if not os.path.exists(path):
        raise ConfigError(
            f"설정 파일이 없습니다: {path} "
            f"(config/config.example.yaml을 복사해 채우세요)"
        )

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigError(f"설정 파싱 실패({path}): {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(f"설정 최상위는 매핑이어야 합니다: {path}")

    raw = _substitute_env(raw)
    return _build_config(raw)


def load_config_from_dict(raw: dict) -> AppConfig:
    """이미 로드된 dict에서 AppConfig를 구성(테스트/임베드용).

    env 치환·오버라이드·검증은 동일하게 적용한다.
    """
    raw = _substitute_env(dict(raw))
    return _build_config(raw)


def _build_config(raw: dict) -> AppConfig:
    """치환 완료된 raw dict → 검증된 AppConfig."""
    server = _section(raw, "server")
    jira = _section(raw, "jira")
    match = _section(raw, "match")
    webhook = _section(raw, "webhook")
    resume = _section(raw, "resume")
    spawn = _section(raw, "spawn")
    git = _section(raw, "git")
    secrets = _section(raw, "secrets")
    run = _section(raw, "run")

    cfg = AppConfig(
        role=str(raw.get("role", "central")).strip().lower(),
        server=ServerConfig(
            host=str(server.get("host", "0.0.0.0")),
            port=int(server.get("port", 8787)),
        ),
        jira=JiraConfig(
            base_url=str(jira.get("base_url", "")).rstrip("/"),
            project=str(jira.get("project", "")),
            poll_interval_sec=int(jira.get("poll_interval_sec", 60)),
            watcher_token_file=str(jira.get("watcher_token_file", "")),
            watcher_email=str(jira.get("watcher_email", "")),
        ),
        match=MatchConfig(statuses=list(match.get("statuses", []) or [])),
        webhook=WebhookConfig(
            enabled=bool(webhook.get("enabled", False)),
            path=str(webhook.get("path", "/jira-webhook")),
            shared_secret_file=str(webhook.get("shared_secret_file", "")),
        ),
        resume=ResumeConfig(
            work_hours=str(resume.get("work_hours", "")),
            timezone=str(resume.get("timezone", "Asia/Seoul")),
            nightly_drain=str(resume.get("nightly_drain", "")),
            reset_buffer_sec=int(resume.get("reset_buffer_sec", 120)),
        ),
        spawn=SpawnConfig(
            image=str(spawn.get("image", "jira-auto-dispatcher:latest")),
            network=str(spawn.get("network", "jad-net")),
            central_url=str(spawn.get("central_url", "http://central:8787")),
            mem_limit=str(spawn.get("mem_limit", "4g")),
            docker_host=str(spawn.get("docker_host", "unix:///var/run/docker.sock")),
            run_as=str(spawn.get("run_as", "1000:1000")),
            host_deploy_dir=str(spawn.get("host_deploy_dir", "")),
        ),
        git=GitConfig(
            branch_prefix=str(git.get("branch_prefix", "auto/")),
            github_owner=str(git.get("github_owner", "")),
        ),
        secrets=SecretsConfig(base_dir=str(secrets.get("base_dir", ""))),
        run=RunConfig(
            orchestrator_repo=str(run.get("orchestrator_repo", "")),
            dlc_meta_repo=str(run.get("dlc_meta_repo", "")),
            dataspace_docs_repo=str(run.get("dataspace_docs_repo", "")),
            orchestrator_repo_url=str(run.get("orchestrator_repo_url", "")),
            dlc_meta_repo_url=str(run.get("dlc_meta_repo_url", "")),
            dataspace_docs_repo_url=str(run.get("dataspace_docs_repo_url", "")),
            workspace_dir=str(run.get("workspace_dir", "")),
            claude_bin=str(run.get("claude_bin", "claude")),
            permission_mode=str(run.get("permission_mode", "skip")),
            output_format=str(run.get("output_format", "stream-json")),
            concurrency_per_worker=int(run.get("concurrency_per_worker", 1)),
            global_concurrency=int(run.get("global_concurrency", 3)),
        ),
        worker_shared_secret=str(raw.get("worker_shared_secret", "")),
        repo_map=dict(raw.get("repo_map", {}) or {}),
    )

    _apply_env_overrides(cfg)
    _validate(cfg)
    return cfg


def _apply_env_overrides(cfg: AppConfig) -> None:
    """env 오버라이드 적용(YAML보다 우선).

    ``SECRETS_DIR``은 secrets.base_dir 안의 ``${SECRETS_DIR}`` 치환에서 이미
    반영되지만, base_dir이 비어 있으면 SECRETS_DIR을 직접 채워 넣는다.
    """
    role = os.environ.get("ROLE")
    if role:
        cfg.role = role.strip().lower()

    secrets_dir = os.environ.get("SECRETS_DIR")
    if secrets_dir and (not cfg.secrets.base_dir or "${SECRETS_DIR}" in cfg.secrets.base_dir):
        cfg.secrets.base_dir = cfg.secrets.base_dir.replace("${SECRETS_DIR}", secrets_dir) or secrets_dir

    central_url = os.environ.get("CENTRAL_URL")
    if central_url:
        cfg.spawn.central_url = central_url

    worker_secret = os.environ.get("WORKER_SHARED_SECRET")
    if worker_secret:
        cfg.worker_shared_secret = worker_secret

    # 호스트 배포 디렉토리(worker 바인드 source용). env HOST_DEPLOY_DIR 폴백 우선.
    host_deploy_dir = os.environ.get("HOST_DEPLOY_DIR")
    if host_deploy_dir:
        cfg.spawn.host_deploy_dir = host_deploy_dir
    # env 미설정으로 ${HOST_DEPLOY_DIR} 토큰이 미치환으로 남았으면 빈 값으로
    # 취급한다(→ spawner가 직접 경로 폴백 + 경고). 조용한 broken bind 방지.
    if "${" in cfg.spawn.host_deploy_dir:
        cfg.spawn.host_deploy_dir = ""


def _validate(cfg: AppConfig) -> None:
    """필수키 검증(fail-fast). central 역할에 필요한 최소 집합만 강제."""
    missing: list[str] = []

    if cfg.role not in ("central", "worker"):
        raise ConfigError(f"role은 central|worker 여야 합니다: {cfg.role!r}")

    if cfg.role == "central":
        if not cfg.jira.base_url:
            missing.append("jira.base_url")
        if not cfg.jira.project:
            missing.append("jira.project")
        if not cfg.secrets.base_dir:
            missing.append("secrets.base_dir (또는 env SECRETS_DIR)")
        if not cfg.jira.watcher_token_file:
            missing.append("jira.watcher_token_file")

    # 미치환 ${VAR} 토큰이 남아 있으면 실패(조용한 오설정 방지).
    if "${" in cfg.secrets.base_dir:
        raise ConfigError(
            f"secrets.base_dir 에 미치환 토큰이 남아 있습니다: {cfg.secrets.base_dir!r} "
            f"(env SECRETS_DIR을 설정하세요)"
        )

    if missing:
        raise ConfigError("필수 설정 누락: " + ", ".join(missing))


def read_secret(base_dir: str, ref: str) -> "str | None":
    """secrets.base_dir 기준으로 시크릿 참조(ref=상대경로)를 읽어 반환.

    Args:
        base_dir: 시크릿 루트(로컬=C:/temp, 컨테이너=/run/secrets 등).
        ref: base_dir 상대 경로(예: ``service/jira-token``).

    Returns:
        파일 내용(양끝 공백/개행 strip). 파일이 없으면 None.
    """
    if not ref:
        return None
    path = os.path.join(base_dir, ref) if base_dir else ref
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read().strip()
