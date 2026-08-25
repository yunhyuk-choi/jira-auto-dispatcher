"""설정 로드/검증 계층(중앙 전용).

역할:
    config/config.yaml(gitignore됨)을 읽어 검증하고, 타입드 설정 객체로
    노출한다. 시크릿 "값"은 담지 않는다 — watcher_token_file 등 파일 참조만
    두고, 값은 secrets.base_dir 기준으로 런타임에 읽는다.

역할 소속: **central** (worker는 대부분 env로 구성되며 run.* 만 공유).

구현 Phase: **Phase 1** (config + state 영속).

참고:
    - "무엇을 물어야 하는가"의 스키마 정본은 :mod:`app.setup_schema`(기계가 읽는 선언),
      사람이 읽는 예시는 config/config.example.yaml. 이 모듈은 "그 답을 런타임에 어떻게
      읽는가"의 정본이다 — 세 곳이 **같은 키 경로**를 공유한다.
    - 사용자별 트리거 목록은 더 이상 config가 아니라 registry(state/registry.json)
      가 소유한다(온보딩으로 채워짐). config는 시스템 수준 설정만 담는다.
    - 누락/오타 키는 부팅 시점에 명확한 에러로 실패시킨다(fail-fast).
    - POLICY-ENCODING: 파일은 UTF-8(BOM 없음)로 읽는다.

프레임워크화 신규 섹션(``forge``·``notifier``·``deploy``·``consent``)과 하위호환:
    특정 조직에 묶여 있던 값(GitLab 전제·Google Chat 전제·특정 배포 토폴로지·특정
    프로젝트의 설계문서 레포)을 설정으로 뺐다. **기존 배포는 아무것도 바꾸지 않아도
    오늘과 동일하게 동작한다** — 신규 키가 있으면 신규가 이기고, 없으면 레거시 키를
    그대로 읽으며, 계산된 값은 레거시 필드에도 **미러**되어 기존 리더(spawner·notify·
    poller 등)를 손대지 않아도 된다. 우선순위는 일관되게:

        명시 신규 키 > 명시 레거시 키 > (deploy 는) 프로파일 파생 > 코드 기본값

    그 위에 env 오버라이드가 최종 우선한다(:func:`_apply_env_overrides`).

env 오버라이드(런타임 우선):
    - ``ROLE``                → role
    - ``SECRETS_DIR``         → secrets.base_dir 의 ``${SECRETS_DIR}`` 치환값
    - ``CENTRAL_URL``         → spawn.central_url (worker→central 폴링 대상)
    - ``WORKER_SHARED_SECRET``→ worker_shared_secret (dispatch HTTP 인증)
    - ``NOTIFIER_PROVIDER``   → notifier.provider (신규·중립)
    - ``NOTIFIER_WEBHOOK_REF``→ notifier.webhook_ref (신규·중립. 레거시
      ``GOOGLE_CHAT_WEBHOOK_REF``·``NOTIFY_ENABLED`` 도 계속 받는다)
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
from dataclasses import dataclass, field
from typing import Any

from app.setup_schema import (
    DEPLOY_PROFILES,
    FORGE_KINDS,
    NOTIFIER_PROVIDERS,
    PROFILE_DEFAULTS,
)

log = logging.getLogger("jad.config")

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
class ForgeConfig:
    """[forge] 섹션 — 코드 호스팅(GitLab/GitHub) 어댑터 선택.

    지금까지 GitLab 이 코드에 전제돼 있었다(변수명·URL 조립). 이 섹션이 그 전제를
    설정으로 끌어낸다. ⚠️ 사용자 **개인** forge 토큰은 여기가 아니라 관리 UI 온보딩이
    받아 레지스트리에 참조로 둔다 — ``token_ref`` 는 central **서비스 자신**의 토큰이다.

    하위호환: ``token_ref`` 가 비면 레거시 ``run.repo_resolver_gitlab_token_ref`` 를
    쓰고, 반대로 신규만 주면 레거시 필드에도 미러한다(기존 리더가 그대로 동작).
    """

    kind: str = "gitlab"     # gitlab | github
    base_url: str = ""       # self-hosted forge base URL(비우면 SaaS 기본)
    token_ref: str = ""      # secrets.base_dir 상대 참조(값 아님)


@dataclass
class NotifierConfig:
    """[notifier] 섹션 — 완료 알림 채널(기본 none = 알림 없음).

    레거시 ``notify.enabled``/``webhook_ref`` 를 일반화한다. ``provider`` 가 ``none``
    이면 알림을 보내지 않으며, 알림 없이도 시스템은 완전히 동작한다(best-effort).
    """

    provider: str = "none"           # none | google_chat | slack | generic_webhook
    webhook_ref: str = ""            # 웹훅 URL이 담긴 파일의 secrets.base_dir 상대 참조
    notify_interrupted: bool = True
    notify_cancelled: bool = True

    @property
    def enabled(self) -> bool:
        """provider 가 none 이 아니면 알림 활성."""
        return self.provider != "none"


@dataclass
class DeployConfig:
    """[deploy] 섹션 — 배포 형태 하나로 나머지 배포 값 파생.

    프로파일(local | cloud_vm | onprem_server) 하나를 고르면
    ``docker_host``·``secrets_base_dir``·``workspace_volume``·``host_deploy_dir`` 이
    :data:`app.setup_schema.PROFILE_DEFAULTS` 에서 파생된다. 개별 값을 명시하면 그게
    우선하고, 레거시 ``spawn.*``/``secrets.base_dir`` 도 계속 존중한다(그리고 계산 결과는
    그 레거시 필드에 **미러**되어 spawner 등 기존 리더가 손대지 않아도 된다).
    """

    profile: str = "local"
    host_deploy_dir: str = ""
    docker_host: str = "unix:///var/run/docker.sock"
    secrets_base_dir: str = ""
    workspace_volume: str = "jad-workspace"


@dataclass
class ConsentConfig:
    """[consent] 섹션 — 풀 퍼미션 실행에 대한 설치자 명시 동의.

    이 시스템은 사람의 매 단계 승인 없이 도구 권한(파일 쓰기·셸·git push)을 가진 코딩
    에이전트를 헤드리스로 돌린다. 설치자가 그 위험을 이해하고 감수했다는 흔적을 남긴다.

    ⚠️ 로드 단계에서 **강제(fail-fast)하지 않는다** — 동의 키가 없는 기존 배포를 깨지
    않기 위해서다. 미동의면 경고만 남기고(:func:`_validate`), 강제는 **설치 관문**의
    몫이다: :mod:`app.setup_validate` 가 통과시키지 않고 ``python -m app.setup validate``
    가 non-zero 로 끝난다(``doctor`` 의 ``config`` 검사도 같은 것을 실패로 본다).
    """

    full_permissions: bool = False
    accepted_at: str = ""   # ISO-8601 동의 시각(감사 흔적)


@dataclass
class JiraConfig:
    """[jira] 섹션 — 중앙이 Jira를 감시하는 설정 + **인스턴스마다 다른 값들**.

    커스텀필드 id·완료 전이 id·상태 이름·라벨은 Jira 인스턴스마다 다르다. 지금까지 일부가
    코드 상수(app/jira_client.py)여서 다른 조직에서는 그대로 쓸 수 없었다 — 그 자리를
    여기에 만든다. 비어 있으면 기존 모듈 상수/기본값을 그대로 쓴다(하위호환).

    ``trigger_statuses``/``cancel_statuses``/``optout_labels`` 는 레거시 ``match.*`` 의
    신규 이름이며, 양쪽은 항상 같은 값으로 유지된다(:class:`MatchConfig` 미러).
    """

    base_url: str = ""
    project: str = ""
    poll_interval_sec: int = 60
    watcher_token_file: str = ""  # 중앙 감시 토큰(내 것/봇). secrets.base_dir 상대
    watcher_email: str = ""       # Basic auth actor(감시 계정 이메일). env JIRA_WATCHER_EMAIL 폴백
    # --- 인스턴스별 트리거/역-트리거(레거시 match.* 의 신규 이름 — 항상 미러) ---
    trigger_statuses: list = field(default_factory=list)
    cancel_statuses: list = field(default_factory=list)
    optout_labels: list = field(default_factory=list)
    # --- 인스턴스별 필드/전이 식별 ---
    # 논리 키 → 커스텀필드 id. 논리 키 목록은 setup_schema.JIRA_CUSTOM_FIELD_KEYS.
    # 비거나 일부만 주면 나머지는 app/jira_client.py 모듈 상수 폴백(하위호환).
    custom_fields: dict = field(default_factory=dict)
    done_transition_id: str = ""   # 비우면 이름으로 식별 → 그래도 없으면 모듈 상수
    done_transition_names: list = field(default_factory=lambda: ["완료", "Done"])


@dataclass
class MatchConfig:
    """[match] 섹션 — 트리거 조건 + 역-트리거(취소/추적제외).

    ⚠️ **레거시 이름·현역 미러**: 신규 키 경로는 ``jira.trigger_statuses`` /
    ``jira.cancel_statuses`` / ``jira.optout_labels`` 다(:class:`JiraConfig`). 두 곳은
    로드 시 항상 같은 값으로 유지되므로, 기존 리더(폴러·워처·웹훅)는 계속 ``cfg.match``
    를 읽어도 되고 신규 코드는 ``cfg.jira`` 를 읽어도 된다. 설정 파일에 둘 다 있으면
    **신규(jira.*)가 이긴다**.

    - ``statuses``: 신규 착수(트리거) 상태 화이트리스트.
    - ``cancel_statuses``: "취소" 상태 이름 목록. 이 상태로 들어온 티켓은 추적 잡을
      즉시 취소한다(폴러·워처·웹훅 공유). 기존 하드코딩 ``"취소됨"`` 을 대체.
    - ``optout_labels``: "자동화 추적 해제" 라벨 목록. 이 라벨이 붙은 티켓은 신규
      착수하지 않고(폴러 JQL 제외), 이미 추적 중이면 취소로 수렴한다(cancel_job).
    """

    statuses: list = field(default_factory=list)
    cancel_statuses: list = field(default_factory=lambda: ["취소됨"])
    optout_labels: list = field(default_factory=lambda: ["자동화_추적_해제"])


@dataclass
class WebhookConfig:
    """[webhook] 섹션 — Jira 웹훅 수신(이벤트 구동 트리거).

    이벤트 구동 경로(POST /webhook/jira)는 poller.trigger_ticket을 재사용해
    폴링과 동일한 dedup 게이트/매핑 수렴점을 탄다(폴링은 백스톱 유지). 토큰은
    "값"이 아니라 secrets.base_dir 상대 참조(secret_ref)로만 둔다 — 미설정/조회불가
    면 엔드포인트가 503으로 거부한다(무인증 실행 금지). path/shared_secret_file은
    레거시 동기 경로(app/webhook.py) 전용 필드로 남겨둔다(하위호환).
    """

    enabled: bool = True
    secret_ref: str = "service/jira-webhook"
    path: str = "/jira-webhook"           # 레거시 동기 경로 전용(하위호환)
    shared_secret_file: str = ""          # 레거시 동기 경로 시크릿 참조(하위호환)


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
    # 공유 워크스페이스 **named 볼륨** 이름(central·모든 워커가 공유하는 단일 클론
    # 지점, 설계 §4). central compose가 이 이름으로 선언(`jad-workspace`)하고 worker는
    # spawner가 같은 이름으로 마운트한다 → 레포 한 벌 공유. named 볼륨이라 host_deploy_dir
    # 무관(볼륨명으로 docker가 해석). 볼륨 bind 경로는 run.workspace_dir.
    workspace_volume: str = "jad-workspace"


@dataclass
class GitConfig:
    """[git] 섹션 — 브랜치 명명 규칙.

    ``branch_prefix`` 만 현역이다(``auto/<ticket>``).

    ⚠️ ``github_owner`` 는 **사용되지 않는 유물**이다(deprecated). 이름이 GitHub 에
    묶여 있지만 forge 중립 이름으로 바꾸지도, 지우지도 않는다 — 코드베이스 전체에서
    이 값을 **읽는 곳이 하나도 없어서**(리네임은 순수 churn) 그리고 기존 config.yaml·
    기존 리더(``cfg.git.github_owner``)를 깨지 않기 위해서다. forge 를 가리키는 현역
    설정은 ``forge.kind``/``forge.base_url`` 이며, 새 설정 예시에서는 이 필드를 뺐다.
    후속에서 실제 소유자/네임스페이스가 필요해지면 ``forge.*`` 아래에 새로 만든다.
    """

    branch_prefix: str = "auto/"
    github_owner: str = ""  # deprecated — 읽는 곳 없음(하위호환 잔존 필드)


@dataclass
class SecretsConfig:
    """[secrets] 섹션 — 시크릿 파일 기준 경로(값 아님)."""

    # 시크릿 파일들의 루트. 값이 아니라 **경로**다. 호스트 OS 무관 — 어디에 두든 된다:
    #   로컬 개발  : 임의의 로컬 디렉토리(예: `~/.jad/secrets`,
    #                윈도우는 `%LOCALAPPDATA%\jad\secrets`)
    #   컨테이너   : `/run/secrets` 또는 마운트한 볼륨 경로
    # 보통 config 에 직접 쓰지 않고 env `SECRETS_DIR` 로 주입한다.
    base_dir: str = ""


@dataclass
class NotifyConfig:
    """[notify] 섹션 — 잡 종료 알림의 **레거시 미러**(정본은 :class:`NotifierConfig`).

    Google Chat 전용이던 시절의 이름이다. 지금은 provider 어댑터(none|google_chat|
    slack|generic_webhook)가 정본이고, 이 필드들은 항상 그 값으로 동기화된다
    (:func:`_sync_notify_alias`) — 옛 이름을 읽는 리더가 그대로 동작하게 한다.

    웹훅 URL은 **시크릿**이므로 값이 아니라 참조(secrets.base_dir 상대 파일 경로)만
    담는다(webhook_ref). worker가 잡 종료(터미널 상태)에 팀 채널 웹훅으로 POST한다.
    """

    enabled: bool = False
    # incoming webhook URL이 담긴 파일의 secrets.base_dir 상대 참조(시크릿).
    # env NOTIFIER_WEBHOOK_REF(신규)·GOOGLE_CHAT_WEBHOOK_REF(레거시) 폴백.
    # 값 자체는 절대 여기/추적 파일에 넣지 않는다.
    webhook_ref: str = ""
    notify_interrupted: bool = True  # 토큰 한도(interrupted) 시에도 짧게 알림
    notify_cancelled: bool = True    # 취소(cancelled) 시 짧게 알림


@dataclass
class AdmissionConfig:
    """[admission] 섹션 — central의 **서버 자원 기반** dispatch 어드미션(고정 잡 수 cap 대체).

    central은 "잡 수"가 아니라 **호스트 서버의 실제 자원 상태**(가용 메모리 + CPU 부하)로
    dispatch 여부를 판단한다: 여유가 있으면 준비된 잡을 dispatch, 압박이면 큐에 대기.
    자원이 넉넉하면 한 사용자가 (서로 다른 레포에서) 여러 잡을 동시에 굴릴 수도 있다 —
    per-user 잡 수 cap도, 전역 잡 수 cap도 없다. 스로틀은 오직 **자원**이다.

    ⚠️ 토큰/레이트 한도는 central의 관심사가 **아니다** — 각 per-user worker 컨테이너의
    오케스트레이터가 자기 계정의 토큰/레이트를 스스로 관리한다. central은 이를 모델링하지 않는다.

    어드미션 규칙(메트릭 지연 완화 포함):
        - 메모리(즉각 신호, PRIMARY): ``MemAvailable``(호스트 값, /proc/meminfo)에서
          in-flight 잡의 예약분을 뺀 유효 가용치가 하한 이상이어야 한다.
            effective_available_mb = MemAvailable_mb - active_job_count * per_job_mem_reserve_mb
          → loadavg가 오르기 전에 버스트를 과다 admit하는 것을 예약으로 선제 차단.
        - 부하(느린 1/5/15분 평균, SECONDARY 거친 상한): loadavg_1min / max(1, ncpu) < 상한.
    """

    # 유효 가용 메모리(MB)가 이 하한 미만이면 dispatch를 멈추고 큐잉한다(메모리 압박).
    min_free_mem_mb: int = 1536
    # in-flight 잡 1개당 예약(선점)하는 메모리(MB). loadavg 지연을 예약으로 선제 보정한다.
    per_job_mem_reserve_mb: int = 1024
    # CPU 코어당 1분 loadavg 상한(거친 2차 천장). 이상이면 큐잉한다(부하 압박).
    max_load_per_core: float = 0.9


@dataclass
class RunConfig:
    """[run] 섹션 — 오케스트레이터 실행 파라미터(central↔worker 공유)."""

    orchestrator_repo: str = ""
    dlc_meta_repo: str = ""
    # 설계 문서 레포(**선택**). 예전 이름 dataspace_docs 는 특정 프로젝트의 레포
    # 이름이었다 — 범용 docs_repo 로 일반화했다. URL이 비면 프로비저닝을 조용히
    # 건너뛰고 경고만 남긴다(잡은 정상 진행 — app/repos.py "skipped: no url").
    docs_repo: str = ""
    # 위 레포들의 clone 원본 URL(토큰 없는 형태). 비면 그 레포 프로비저닝 skip.
    # worker가 사용자 forge 토큰으로 clone(없으면)/pull(있으면)한다 — app/repos.py.
    orchestrator_repo_url: str = ""
    dlc_meta_repo_url: str = ""
    docs_repo_url: str = ""
    # --- 레거시 미러(하위호환·읽기 전용 취급) ---
    # 옛 키/속성 이름을 쓰는 리더가 남아 있어도 깨지지 않도록 docs_repo* 와 항상 같은
    # 값으로 동기화한다(:func:`_sync_docs_repo_aliases`). 새 코드는 docs_repo* 를 쓴다.
    dataspace_docs_repo: str = ""
    dataspace_docs_repo_url: str = ""
    workspace_dir: str = ""
    claude_bin: str = "claude"
    permission_mode: str = "skip"
    output_format: str = "stream-json"
    # --- 지속 세션(Phase 1, HAN-537 지혈) ---
    # 워커 오케스트레이터를 one-shot `claude -p "<prompt>"`가 아니라 **지속 세션**
    # (`claude -p --input-format stream-json --output-format stream-json`)으로 띄운다.
    # 티켓 프롬프트는 stdin에 stream-json user 메시지로 주입하고, 세션을 살려 백그라운드
    # 위임 → 다음 턴 완료 알림 → 커밋까지 한 세션에서 완결한다(app/agent_runner.py).
    # False면 레거시 one-shot으로 폴백(안전 롤백). input_format이 stream-json이 아니어도
    # 폴백. 프레임워크(ai-dlc-orchestrator) 룰북은 불변.
    persistent_session: bool = True
    input_format: str = "stream-json"
    # 지속 세션 백스톱(초). 정상 종료는 완료 판정(pending bg=0의 result)으로 이뤄지고,
    # 아래는 result를 영영 못 내는 이상 상황(행/누수)만 막는 안전망. 0 이하면 비활성.
    session_max_sec: int = 7200     # 세션 최대 수명(벽시계).
    session_idle_sec: int = 1800    # 이벤트 무발생 유휴 상한.
    # worker 동시 실행 **안전 상한**(runaway 방지 백스톱 — 정책 cap이 아님). worker는
    # central이 dispatch한 잡을 **모두** 동시에 굴린다(진짜 스로틀은 central의 자원 어드미션).
    # 이 값은 버그로 인한 무한 스레드 폭주만 막는 상한이다. env WORKER_CONCURRENCY가 있으면
    # 그게 최우선(스포너가 주입). 기본 64.
    worker_max_concurrency: int = 64
    # --- central LLM 레포 리졸버(신규 티켓 → target_repos 판단) ---
    # "llm": central이 claude로 dlc-meta REPO-MAP+티켓을 읽어 target_repos 판단.
    # "static": 기존 정적 config.repo_map 룩업(폴백/오버라이드 전용 경로).
    repo_resolution: str = "llm"
    # central이 REPO-MAP을 읽을 **공유 워크스페이스** dlc-meta 클론 경로.
    # 비면 <workspace_dir>/dlc-meta 로 파생(설계 §4 단일 공유 클론). 리모트는
    # dlc_meta_repo_url 재사용. central GitLab 토큰으로 clone/pull(app/repos.py).
    repo_map_path: str = ""
    # --- dlc-meta 단일 라이터(Phase 3a) — central이 **유일한** git 라이터 ---
    # 공유 워크스페이스 dlc-meta 클론은 다중 리더(워커 오케스트레이터가 룰/사용자정보
    # 를 읽음)·단일 라이터(central만 commit/push/pull)다. 잡 완료 시 central이 그 잡의
    # 사이클로그 경로만(git add <경로> — git add . 아님) 아래 브랜치에 커밋·push한다.
    # dlc-meta 기본 브랜치명(master). 워커는 dlc-meta를 절대 commit/reset/pull하지 않는다.
    dlc_meta_branch: str = "master"
    # central forge 토큰의 secrets.base_dir 상대 참조(dlc-meta pull/push·참고 레포
    # fetch용, 값 아님).
    #
    # ⚠️ 이름 정리(프레임워크화): 설정 파일에서의 **중립 정본 키는 ``forge.token_ref``**
    # 다(:class:`ForgeConfig`). 아래 두 속성은 항상 같은 값으로 동기화된다:
    #   - ``forge_token_ref``                 forge 중립 **별칭**(새 코드는 이걸 읽는다)
    #   - ``repo_resolver_gitlab_token_ref``  레거시 이름(기존 리더·기존 config.yaml 키)
    # 읽을 때는 :func:`central_forge_token_ref` 를 쓰면 세 표현 중 채워진 것을 고른다.
    forge_token_ref: str = ""
    repo_resolver_gitlab_token_ref: str = ""
    # claude 판단 호출 상한(초). 작은 호출이라 짧게.
    repo_resolver_timeout_sec: int = 60
    # --- central 자기 토큰 레이트/사용량 한도 쿨다운(축1) ---
    # central은 *자기* Claude 토큰(CLAUDE_CODE_OAUTH_TOKEN)으로 레포 해석을 한다.
    # 그 토큰이 한도에 걸리면 claude 호출을 잠시 멈추고(쿨다운) 그 사이 도착한
    # 티켓은 pending으로 보관했다가(claim 유지) 회복 후 드레인한다. ⚠️ 워커 토큰/
    # 레이트는 각 worker 컨테이너 오케스트레이터의 몫 — central은 자기 것만 관리한다.
    ai_cooldown_default_sec: int = 120   # 한도 감지 시 기본 쿨다운(초). reset_at 있으면 그걸 우선.
    ai_cooldown_max_sec: int = 900       # 연속 한도 시 백오프 상한(초).
    # --- Phase 3b-2 파일럿 Tier-2 에이전트 피처 플래그(기본 OFF) ---
    # 값이 있으면 그 **단일 사용자**만 Tier-2 경로(에이전트 제안 / 파이썬 강제, D1)로
    # 태운다. 비면(기본) 전체 Tier-2 경로가 **완전히 비활성** — 디스패치 동작은 오늘과
    # byte-for-byte 동일하다(무동작변경). env TIER2_PILOT_USER가 있으면 그게 우선.
    # ⚠️ 이 플래그가 켜져도 이 MR(스캐폴드) 단계에서는 러너가 **자동 실행되지 않는다** —
    # 자원툴+러너 인터페이스만 배선하고, 실제 SDK 백엔드 러너 기동은 오너의 의존성
    # 확정 후 후속(§5 D3, requirements/이미지 결정)에서 얹는다.
    tier2_pilot_user: str = ""
    # --- 프랙탈 P1(컨테이너/워커 계층) 신경로 피처 플래그(기본 OFF) ---
    # True 면 워커가 **티켓당 프로세스 스폰**(agent_runner.run_job/_consume) 대신
    # **사용자당 지속 세션 하나**(app/session_manager.UserSession)를 유지하며 티켓을
    # 이어 주입(재사용-if-up)하고 drain 때 종료한다(설계 §3.3·§4·§9 P1). 기본 OFF:
    # 비면 워커 경로는 오늘과 **byte-for-byte 동일**하다(무동작변경). env
    # FRACTAL_WORKER 가 있으면 그게 우선(스포너/운영 토글). 지속 세션이 꺼져 있으면
    # (persistent_session False) 이 플래그가 켜져도 신경로는 성립하지 않아 무시된다.
    fractal_worker: bool = False
    # --- 프랙탈 P2(센트럴 계층) 신경로 피처 플래그(기본 ON — 승격됨) ---
    # True 면 central 이 신규/갱신 티켓을 **스케줄러 큐(dispatcher.enqueue)** 로 넣는
    # 대신 **상주 센트럴 라이브 세션**(app/central_session.CentralSession)에 이벤트로
    # 주입한다(설계 §3.1·§9 P2). 센트럴 세션(=ai-dlc-orchestrator 에이전트)이 사용자별
    # 서브에이전트를 네이티브로 스폰해 각 워커 컨테이너로 위임하고, 리치 완료-리포트를
    # 관찰해 설정된 알림 채널(notifier.provider)로 상신한다.
    # **기본 ON 으로 승격**(라이브 검증 완료 — 티켓 552 클린 완주): 예전엔 비영속 env
    # FRACTAL_CENTRAL=1 override 로만 켜져 배포마다 꺼졌으나, 이제 config/코드 기본이
    # True 라 **override 없이 배포만으로 fractal ON** 이 유지된다. env FRACTAL_CENTRAL
    # 이 명시되면 여전히 그게 우선(운영 토글 — 명시 falsy 로 끌 수도 있음). 지속 세션이
    # 꺼져 있으면(persistent_session False / 스트림 포맷 불일치) 이 플래그가 켜져도 신경로는
    # 성립하지 않아 무시된다(central_fractal_enabled 게이트가 함께 요구).
    fractal_central: bool = True


@dataclass
class AppConfig:
    """전체 설정의 루트 객체."""

    role: str = "central"  # central | worker
    server: ServerConfig = field(default_factory=ServerConfig)
    jira: JiraConfig = field(default_factory=JiraConfig)
    match: MatchConfig = field(default_factory=MatchConfig)  # jira.* 의 레거시 미러
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    resume: ResumeConfig = field(default_factory=ResumeConfig)
    spawn: SpawnConfig = field(default_factory=SpawnConfig)
    git: GitConfig = field(default_factory=GitConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)  # notifier 의 레거시 미러
    admission: AdmissionConfig = field(default_factory=AdmissionConfig)
    run: RunConfig = field(default_factory=RunConfig)
    # --- 프레임워크화 신규 섹션 ---
    forge: ForgeConfig = field(default_factory=ForgeConfig)
    notifier: NotifierConfig = field(default_factory=NotifierConfig)
    deploy: DeployConfig = field(default_factory=DeployConfig)
    consent: ConsentConfig = field(default_factory=ConsentConfig)
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


def _pick(new_sect: dict, new_key: str, old_sect: dict, old_key: str, default: Any) -> Any:
    """신규 키 우선 → 레거시 키 → 기본값.

    ``get(key, default)`` 가 아니라 **키 존재 여부**로 판정한다 — 명시적으로 준 빈 값
    (빈 문자열·빈 리스트·false)을 "미설정"으로 오해해 기본값으로 덮어쓰지 않기 위해서다
    (예: ``cancel_statuses: []`` 는 "기능 끔"이라는 명시 의사표시다).
    """
    if new_key in new_sect:
        return new_sect[new_key]
    if old_key in old_sect:
        return old_sect[old_key]
    return default


def _pick_list(new_sect: dict, new_key: str, old_sect: dict, old_key: str,
               default: list) -> list:
    """:func:`_pick` 의 리스트 판(None → 빈 리스트로 환원)."""
    return list(_pick(new_sect, new_key, old_sect, old_key, default) or [])


def _build_deploy(deploy: dict, spawn: dict, secrets: dict) -> DeployConfig:
    """[deploy] 섹션 구성 — 프로파일 파생 + 레거시(spawn/secrets) 하위호환.

    우선순위(항목별 독립):
        1. 명시 ``deploy.<key>``
        2. 명시 레거시 키(``spawn.host_deploy_dir``·``spawn.docker_host``·
           ``spawn.workspace_volume``·``secrets.base_dir``)
        3. ``deploy.profile`` 파생 기본(:data:`app.setup_schema.PROFILE_DEFAULTS`)
        4. 코드 기본값(dataclass)

    2를 3보다 앞에 두는 것이 핵심 하위호환이다 — 기존 배포는 ``deploy`` 섹션 없이 레거시
    키만 갖고 있으므로 프로파일 기본이 그 값을 덮으면 안 된다.
    """
    profile = str(deploy.get("profile", "local")).strip().lower() or "local"
    if profile not in DEPLOY_PROFILES:
        raise ConfigError(
            f"deploy.profile 은 {'|'.join(DEPLOY_PROFILES)} 여야 합니다: {profile!r}"
        )
    derived = PROFILE_DEFAULTS[profile]
    return DeployConfig(
        profile=profile,
        host_deploy_dir=str(_pick(deploy, "host_deploy_dir", spawn, "host_deploy_dir",
                                  derived["host_deploy_dir"])),
        docker_host=str(_pick(deploy, "docker_host", spawn, "docker_host",
                              derived["docker_host"])),
        secrets_base_dir=str(_pick(deploy, "secrets_base_dir", secrets, "base_dir",
                                   derived["secrets_base_dir"])),
        workspace_volume=str(_pick(deploy, "workspace_volume", spawn, "workspace_volume",
                                   derived["workspace_volume"])),
    )


def _build_notifier(notifier: dict, notify: dict) -> NotifierConfig:
    """[notifier] 섹션 구성 — 레거시 [notify] 하위호환.

    신규 ``notifier`` 섹션이 있으면 그게 이긴다. 없으면 레거시 ``notify`` 에서 파생하되,
    ``notify.enabled: true`` 만 있는 옛 설정은 **google_chat** 으로 해석한다(그게 그 시절
    유일한 구현이었다). 계산 결과는 :class:`NotifyConfig` 에 미러되어 기존 리더
    (app/notify.py 등)가 그대로 동작한다.
    """
    if "provider" in notifier:
        provider = str(notifier.get("provider") or "none").strip().lower()
    else:
        provider = "google_chat" if bool(notify.get("enabled", False)) else "none"
    if provider not in NOTIFIER_PROVIDERS:
        raise ConfigError(
            f"notifier.provider 는 {'|'.join(NOTIFIER_PROVIDERS)} 여야 합니다: {provider!r}"
        )
    return NotifierConfig(
        provider=provider,
        webhook_ref=str(_pick(notifier, "webhook_ref", notify, "webhook_ref", "")),
        notify_interrupted=bool(_pick(notifier, "notify_interrupted",
                                      notify, "notify_interrupted", True)),
        notify_cancelled=bool(_pick(notifier, "notify_cancelled",
                                    notify, "notify_cancelled", True)),
    )


def _build_forge(forge: dict, run: dict) -> ForgeConfig:
    """[forge] 섹션 구성 — 레거시 ``run.repo_resolver_gitlab_token_ref`` 하위호환."""
    kind = str(forge.get("kind", "gitlab")).strip().lower() or "gitlab"
    if kind not in FORGE_KINDS:
        raise ConfigError(f"forge.kind 는 {'|'.join(FORGE_KINDS)} 여야 합니다: {kind!r}")
    return ForgeConfig(
        kind=kind,
        base_url=str(forge.get("base_url", "")).rstrip("/"),
        token_ref=str(_pick(forge, "token_ref", run, "repo_resolver_gitlab_token_ref", "")),
    )


def _sync_docs_repo_aliases(run: RunConfig) -> None:
    """``docs_repo*`` → 레거시 ``dataspace_docs_repo*`` 미러(하위호환).

    옛 속성 이름을 읽는 코드가 남아 있어도 값이 갈라지지 않게 한 방향으로만 복사한다
    (정본은 ``docs_repo*``). 경로 파생 후·env 오버라이드 후 모두 호출한다.
    """
    run.dataspace_docs_repo = run.docs_repo
    run.dataspace_docs_repo_url = run.docs_repo_url


def _sync_notify_alias(cfg: "AppConfig") -> None:
    """``notifier`` → 레거시 ``notify`` 미러(하위호환). 정본은 notifier."""
    cfg.notify.enabled = cfg.notifier.enabled
    cfg.notify.webhook_ref = cfg.notifier.webhook_ref
    cfg.notify.notify_interrupted = cfg.notifier.notify_interrupted
    cfg.notify.notify_cancelled = cfg.notifier.notify_cancelled


def _sync_deploy_aliases(cfg: "AppConfig") -> None:
    """``deploy`` ↔ 레거시 ``spawn``/``secrets`` 미러.

    ``deploy`` 가 정본이지만, env 오버라이드(HOST_DEPLOY_DIR·SECRETS_DIR)는 레거시 필드에
    적용되므로 **양방향 수렴**이 필요하다: 먼저 deploy → 레거시로 밀고, env 단계 이후에는
    레거시 → deploy 로 되당겨(:func:`_apply_env_overrides` 끝) 두 값이 항상 일치하게 한다.
    """
    cfg.spawn.host_deploy_dir = cfg.deploy.host_deploy_dir
    cfg.spawn.docker_host = cfg.deploy.docker_host
    cfg.spawn.workspace_volume = cfg.deploy.workspace_volume
    cfg.secrets.base_dir = cfg.deploy.secrets_base_dir


def _sync_forge_token_ref_aliases(cfg: "AppConfig") -> None:
    """central forge 토큰 참조의 세 표현을 **같은 값**으로 수렴시킨다(하위호환).

    표현 셋:
        - ``cfg.forge.token_ref``                     설정 파일의 **중립 정본 키**
        - ``cfg.run.forge_token_ref``                 forge 중립 속성 별칭(신규 리더)
        - ``cfg.run.repo_resolver_gitlab_token_ref``  레거시 속성(기존 리더·기존 키)

    수렴 규칙(둘 다 명시되면 각 자리의 값을 존중하되 빈 쪽만 채운다):
        신규 키만 준 설정 → 레거시 속성이 채워져 main·poller·dlc_meta_writer 가 그대로 동작.
        레거시 키만 준 설정 → ``forge.token_ref`` 는 :func:`_build_forge` 가 이미 흡수했고,
        중립 별칭도 여기서 채워진다.
    """
    legacy = cfg.run.repo_resolver_gitlab_token_ref
    if cfg.forge.token_ref and not legacy:
        cfg.run.repo_resolver_gitlab_token_ref = cfg.forge.token_ref
    elif legacy and not cfg.forge.token_ref:
        cfg.forge.token_ref = legacy
    cfg.run.forge_token_ref = (
        cfg.run.repo_resolver_gitlab_token_ref or cfg.forge.token_ref
    )


def _sync_jira_match_aliases(cfg: "AppConfig") -> None:
    """``jira.*_statuses``/``optout_labels`` ↔ 레거시 ``match.*`` 미러(같은 값 유지)."""
    cfg.match.statuses = list(cfg.jira.trigger_statuses)
    cfg.match.cancel_statuses = list(cfg.jira.cancel_statuses)
    cfg.match.optout_labels = list(cfg.jira.optout_labels)


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
    """치환 완료된 raw dict → 검증된 AppConfig.

    신규 섹션(forge·notifier·deploy·consent)과 레거시 섹션(notify·match·spawn·secrets·
    run.dataspace_docs_*)은 **키 존재 여부**로 우선순위를 정하고(:func:`_pick`), 계산 결과를
    레거시 필드에 미러해 기존 리더를 깨지 않는다(모듈 docstring 참조).
    """
    server = _section(raw, "server")
    jira = _section(raw, "jira")
    match = _section(raw, "match")
    webhook = _section(raw, "webhook")
    resume = _section(raw, "resume")
    spawn = _section(raw, "spawn")
    git = _section(raw, "git")
    secrets = _section(raw, "secrets")
    notify = _section(raw, "notify")
    admission = _section(raw, "admission")
    run = _section(raw, "run")
    # --- 프레임워크화 신규 섹션 ---
    forge = _section(raw, "forge")
    notifier = _section(raw, "notifier")
    deploy_sect = _section(raw, "deploy")
    consent = _section(raw, "consent")

    # 배포 값은 spawn/secrets 보다 먼저 확정한다(그 섹션들의 값을 이게 결정하므로).
    deploy = _build_deploy(deploy_sect, spawn, secrets)

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
            # 신규 jira.* 우선, 없으면 레거시 match.*.
            # 키 부재 → 문서화된 기본. 명시 빈 리스트([]) → 기능 비활성(존중).
            trigger_statuses=_pick_list(jira, "trigger_statuses", match, "statuses", []),
            cancel_statuses=_pick_list(jira, "cancel_statuses", match, "cancel_statuses",
                                       ["취소됨"]),
            optout_labels=_pick_list(jira, "optout_labels", match, "optout_labels",
                                     ["자동화_추적_해제"]),
            # 인스턴스별 필드/전이 식별 — 비면 jira_client 모듈 상수 폴백(하위호환).
            custom_fields=dict(jira.get("custom_fields", {}) or {}),
            done_transition_id=str(jira.get("done_transition_id", "") or ""),
            done_transition_names=list(
                jira.get("done_transition_names", ["완료", "Done"]) or []
            ),
        ),
        # match 는 jira.* 의 레거시 미러 — 아래 _sync_jira_match_aliases 가 채운다.
        match=MatchConfig(),
        webhook=WebhookConfig(
            enabled=bool(webhook.get("enabled", True)),
            secret_ref=str(webhook.get("secret_ref", "service/jira-webhook")),
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
            run_as=str(spawn.get("run_as", "1000:1000")),
            # docker_host·host_deploy_dir·workspace_volume 은 deploy 가 정본이다
            # (레거시 spawn.* 값도 _build_deploy 가 이미 흡수했다).
            docker_host=deploy.docker_host,
            host_deploy_dir=deploy.host_deploy_dir,
            workspace_volume=deploy.workspace_volume,
        ),
        git=GitConfig(
            branch_prefix=str(git.get("branch_prefix", "auto/")),
            github_owner=str(git.get("github_owner", "")),
        ),
        # secrets.base_dir 도 deploy 가 정본(레거시 secrets.base_dir 흡수 완료).
        secrets=SecretsConfig(base_dir=deploy.secrets_base_dir),
        # notify 는 notifier 의 레거시 미러 — 아래 _sync_notify_alias 가 채운다.
        notify=NotifyConfig(),
        admission=AdmissionConfig(
            min_free_mem_mb=int(admission.get("min_free_mem_mb", 1536)),
            per_job_mem_reserve_mb=int(admission.get("per_job_mem_reserve_mb", 1024)),
            max_load_per_core=float(admission.get("max_load_per_core", 0.9)),
        ),
        run=RunConfig(
            orchestrator_repo=str(run.get("orchestrator_repo", "")),
            dlc_meta_repo=str(run.get("dlc_meta_repo", "")),
            # 신규 docs_repo* 우선, 없으면 레거시 dataspace_docs_repo*(하위호환).
            docs_repo=str(_pick(run, "docs_repo", run, "dataspace_docs_repo", "")),
            orchestrator_repo_url=str(run.get("orchestrator_repo_url", "")),
            dlc_meta_repo_url=str(run.get("dlc_meta_repo_url", "")),
            docs_repo_url=str(_pick(run, "docs_repo_url", run,
                                    "dataspace_docs_repo_url", "")),
            workspace_dir=str(run.get("workspace_dir", "")),
            claude_bin=str(run.get("claude_bin", "claude")),
            permission_mode=str(run.get("permission_mode", "skip")),
            output_format=str(run.get("output_format", "stream-json")),
            persistent_session=bool(run.get("persistent_session", True)),
            input_format=str(run.get("input_format", "stream-json")),
            session_max_sec=int(run.get("session_max_sec", 7200)),
            session_idle_sec=int(run.get("session_idle_sec", 1800)),
            worker_max_concurrency=int(run.get("worker_max_concurrency", 64)),
            repo_resolution=str(run.get("repo_resolution", "llm")).strip().lower(),
            repo_map_path=str(run.get("repo_map_path", "")),
            dlc_meta_branch=str(run.get("dlc_meta_branch", "master")) or "master",
            # 설정 키의 중립 정본은 forge.token_ref 다 — 여기서는 레거시 키만 읽고,
            # 두 표현의 수렴은 아래 _sync_forge_token_ref_aliases 가 담당한다.
            repo_resolver_gitlab_token_ref=str(run.get("repo_resolver_gitlab_token_ref", "")),
            repo_resolver_timeout_sec=int(run.get("repo_resolver_timeout_sec", 60)),
            ai_cooldown_default_sec=int(run.get("ai_cooldown_default_sec", 120)),
            ai_cooldown_max_sec=int(run.get("ai_cooldown_max_sec", 900)),
            tier2_pilot_user=str(run.get("tier2_pilot_user", "")).strip(),
            fractal_worker=bool(run.get("fractal_worker", False)),
            # 승격됨: 키 부재 → 기본 True(override 없이 배포만으로 fractal ON). env
            # FRACTAL_CENTRAL 명시 시 _apply_env_overrides 가 여전히 우선(끌 수도 있음).
            fractal_central=bool(run.get("fractal_central", True)),
        ),
        worker_shared_secret=str(raw.get("worker_shared_secret", "")),
        repo_map=dict(raw.get("repo_map", {}) or {}),
        forge=_build_forge(forge, run),
        notifier=_build_notifier(notifier, notify),
        deploy=deploy,
        consent=ConsentConfig(
            full_permissions=bool(consent.get("full_permissions", False)),
            accepted_at=str(consent.get("accepted_at", "") or ""),
        ),
    )

    # 신규→레거시 미러(기존 리더 무변경 보장). deploy 미러는 spawn/secrets 구성 시
    # 이미 반영됐지만, env 오버라이드 이후 재수렴을 위해 한 번 더 맞춰 둔다.
    _sync_jira_match_aliases(cfg)
    _sync_notify_alias(cfg)
    _sync_deploy_aliases(cfg)
    # central forge 토큰 참조: 중립 정본(forge.token_ref) ↔ 중립 별칭(run.forge_token_ref)
    # ↔ 레거시(run.repo_resolver_gitlab_token_ref) 를 같은 값으로 수렴시킨다.
    _sync_forge_token_ref_aliases(cfg)

    _derive_workspace_paths(cfg.run)
    _apply_env_overrides(cfg)
    _validate(cfg)
    return cfg


def _derive_workspace_paths(run: RunConfig) -> None:
    """3개 오케스트레이터 레포 경로를 **공유 워크스페이스** 하위로 파생(미지정 시).

    설계 §4 "공유 워크스페이스 하나 = 단일 클론·단일 pull 지점" — central·모든
    워커가 같은 named 볼륨(``jad-workspace`` → ``run.workspace_dir``)을 공유하고,
    레포를 그 하위 한 벌만 두어 N중 클론/pull을 없앤다. 명시값이 있으면 존중한다.

        orchestrator_repo ← <workspace_dir>/orchestrator
        dlc_meta_repo     ← <workspace_dir>/dlc-meta
        docs_repo         ← <workspace_dir>/docs

    ⚠️ 설계 문서 레포의 파생 디렉토리명이 예전엔 특정 프로젝트 이름(``dataspace_docs``)
    이었다 — 범용 ``docs`` 로 바꿨다. 경로를 **명시한** 기존 배포는 그 값을 그대로 존중하므로
    영향이 없고(레거시 키도 계속 읽는다), 비워 둔 배포만 새 디렉토리에 다시 clone 한다
    (읽기 전용 참고 레포라 안전하다).

    (컨테이너 경로는 posix이므로 posixpath로 합쳐 슬래시를 유지한다. repo_map_path는
    repo_resolver가 동일 규칙으로 파생하므로 여기서 건드리지 않는다.)
    """
    ws = (run.workspace_dir or "").strip()
    if not ws:
        _sync_docs_repo_aliases(run)
        return
    if not run.orchestrator_repo:
        run.orchestrator_repo = posixpath.join(ws, "orchestrator")
    if not run.dlc_meta_repo:
        run.dlc_meta_repo = posixpath.join(ws, "dlc-meta")
    if not run.docs_repo:
        run.docs_repo = posixpath.join(ws, "docs")
    _sync_docs_repo_aliases(run)


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

    # 완료 알림 웹훅 참조(시크릿 파일 참조) env 폴백 — YAML보다 우선. 값이 아니라 참조.
    # ⚠️ 정본은 cfg.notifier 이므로 거기에 적용하고 레거시 cfg.notify 로 미러한다.
    # 이름도 provider 중립으로 일반화했다 — 신규 NOTIFIER_WEBHOOK_REF 가 우선하고,
    # 옛 GOOGLE_CHAT_WEBHOOK_REF 도 계속 받는다(기존 배포의 env 를 손대지 않아도 된다).
    webhook_ref = (os.environ.get("NOTIFIER_WEBHOOK_REF")
                   or os.environ.get("GOOGLE_CHAT_WEBHOOK_REF"))
    if webhook_ref:
        cfg.notifier.webhook_ref = webhook_ref
    # provider 를 직접 지정하는 중립 env(신규). 목록 밖 값은 무시하고 경고만 남긴다 —
    # 오설정으로 알림 채널이 조용히 바뀌는 것보다 낫다.
    provider_env = (os.environ.get("NOTIFIER_PROVIDER") or "").strip().lower()
    if provider_env:
        if provider_env in NOTIFIER_PROVIDERS:
            cfg.notifier.provider = provider_env
        else:
            log.warning("env NOTIFIER_PROVIDER(%r)가 허용값이 아닙니다 — 무시합니다. 허용: %s",
                        provider_env, "|".join(NOTIFIER_PROVIDERS))
    # 레거시 on/off 토글(그 시절 유일 구현이 google_chat 이었다). NOTIFIER_PROVIDER 가
    # 명시됐으면 그게 이긴다 — 여기서는 "끄라"는 지시만 여전히 존중한다.
    if os.environ.get("NOTIFY_ENABLED"):
        on = os.environ["NOTIFY_ENABLED"].strip().lower() in ("1", "true", "yes", "on")
        if on:
            # 켜라는 지시인데 provider 가 none 이면 레거시 의미(google_chat)로 해석한다.
            if cfg.notifier.provider == "none":
                cfg.notifier.provider = "google_chat"
        elif not provider_env:
            cfg.notifier.provider = "none"
    _sync_notify_alias(cfg)

    # Phase 3b-2 파일럿 Tier-2 사용자 env 폴백(YAML보다 우선). 빈 문자열이면 무시(OFF 유지).
    tier2_pilot = os.environ.get("TIER2_PILOT_USER")
    if tier2_pilot is not None and tier2_pilot.strip():
        cfg.run.tier2_pilot_user = tier2_pilot.strip()

    # 프랙탈 P1 신경로 토글 env 폴백(YAML보다 우선). 명시된 truthy/falsy 만 반영(미설정 무시).
    fractal = os.environ.get("FRACTAL_WORKER")
    if fractal is not None and fractal.strip():
        cfg.run.fractal_worker = fractal.strip().lower() in ("1", "true", "yes", "on")

    # 프랙탈 P2 센트럴 신경로 토글 env 폴백(YAML보다 우선). 명시된 truthy/falsy 만 반영.
    fractal_central = os.environ.get("FRACTAL_CENTRAL")
    if fractal_central is not None and fractal_central.strip():
        cfg.run.fractal_central = fractal_central.strip().lower() in ("1", "true", "yes", "on")

    # 호스트 배포 디렉토리(worker 바인드 source용). env HOST_DEPLOY_DIR 폴백 우선.
    host_deploy_dir = os.environ.get("HOST_DEPLOY_DIR")
    if host_deploy_dir:
        cfg.spawn.host_deploy_dir = host_deploy_dir
    # env 미설정으로 ${HOST_DEPLOY_DIR} 토큰이 미치환으로 남았으면 빈 값으로
    # 취급한다(→ spawner가 직접 경로 폴백 + 경고). 조용한 broken bind 방지.
    if "${" in cfg.spawn.host_deploy_dir:
        cfg.spawn.host_deploy_dir = ""

    # env 는 레거시 필드(spawn.*/secrets.base_dir)에 적용됐다 — deploy 정본으로 되당겨
    # 두 표현이 항상 같은 값을 갖게 한다(역방향 수렴).
    cfg.deploy.host_deploy_dir = cfg.spawn.host_deploy_dir
    cfg.deploy.docker_host = cfg.spawn.docker_host
    cfg.deploy.workspace_volume = cfg.spawn.workspace_volume
    cfg.deploy.secrets_base_dir = cfg.secrets.base_dir


def _validate(cfg: AppConfig) -> None:
    """필수키 검증(fail-fast). central 역할에 필요한 최소 집합만 강제.

    ⚠️ ``consent.full_permissions`` 는 **여기서 강제하지 않는다** — 동의 키가 없는 기존
    배포를 깨지 않기 위해 경고만 남긴다. 설치 단계의 강제는 :mod:`app.setup_validate`
    (``python -m app.setup validate``)가 한다 — :mod:`app.setup_schema` 의 required
    선언이 그 근거다.
    """
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

    if cfg.role == "central" and not cfg.consent.full_permissions:
        log.warning(
            "consent.full_permissions 가 설정되지 않았습니다 — 이 시스템은 사람 승인 없이 "
            "도구 권한을 가진 에이전트를 헤드리스로 실행합니다. config.yaml 의 consent 섹션에 "
            "명시 동의를 남기세요(기존 배포 호환을 위해 지금은 경고만 합니다)."
        )


def central_forge_token_ref(config: Any) -> str:
    """central 서비스 forge 토큰의 **secrets.base_dir 상대 참조**(없으면 "").

    이름이 GitLab 에 묶여 있던 ``run.repo_resolver_gitlab_token_ref`` 를 읽던 자리
    (main·poller·dlc_meta_writer)의 forge 중립 단일 진입점이다. 채워진 첫 표현을 고른다:

        1. ``run.forge_token_ref``                 중립 별칭(로더가 채운다)
        2. ``run.repo_resolver_gitlab_token_ref``  레거시 속성(옛 config 객체·테스트 대역)
        3. ``forge.token_ref``                     중립 정본 키

    ⚠️ ``AppConfig`` 가 아닌 **유사 객체**(테스트 대역·옛 설정)도 그대로 받는다 —
    getattr 폴백이라 어떤 섹션이 없어도 예외를 내지 않는다. 값이 아니라 **참조**만
    돌려준다(토큰 값은 :func:`read_secret` 이 그 순간에만 읽는다).
    """
    run = getattr(config, "run", None)
    forge_sect = getattr(config, "forge", None)
    for holder, name in ((run, "forge_token_ref"),
                         (run, "repo_resolver_gitlab_token_ref"),
                         (forge_sect, "token_ref")):
        if holder is None:
            continue
        ref = str(getattr(holder, name, "") or "").strip()
        if ref:
            return ref
    return ""


def read_secret(base_dir: str, ref: str) -> "str | None":
    """secrets.base_dir 기준으로 시크릿 참조(ref=상대경로)를 읽어 반환.

    Args:
        base_dir: 시크릿 루트. 로컬 개발이면 임의의 로컬 디렉토리(예
            ``~/.jad/secrets``), 컨테이너면 ``/run/secrets`` 나 마운트 볼륨.
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
