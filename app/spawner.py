"""스포너 — 사용자별 worker 컨테이너 동적 기동/중지/상태(중앙 전용).

역할:
    온보딩 시(또는 UI start 버튼) 사용자마다 worker 컨테이너를 Docker SDK로
    동적 생성/기동하고, stop/status로 수명을 관리한다. worker는 이 이미지와
    동일하되 ROLE=worker로 분기해 뜬다.

역할 소속: **central**.

구현 Phase: **Phase 6** (온보딩 + 관리 UI + spawner).

컨테이너 스펙(config.spawn + 사용자 레코드에서 조립):
    image           spawn.image (예: jira-auto-dispatcher:latest, 단일 이미지)
    network         spawn.network (예: jad-net — central과 같은 사설망)
    name            jad-worker-<username> (레지스트리 container.name)
    user            비-root (기본 1000:1000 — 특권 축소)
    env:
        ROLE=worker
        DISPATCH_USER=<username>
        CENTRAL_URL=<spawn.central_url>          # 예: http://central:8787
        WORKER_SHARED_SECRET=<주입>              # dispatch HTTP 인증(X-Worker-Secret)
        CLAUDE_CODE_OAUTH_TOKEN=<주입>            # setup-token(값은 시크릿 참조에서)
        SECRETS_DIR=/run/secrets                  # 컨테이너 내부 시크릿 마운트 루트
        JIRA_TOKEN_FILE / FORGE_TOKEN_FILE        # per-user 토큰 "파일경로"(값 아님)
                                                  # (GITLAB_TOKEN_FILE 도 같은 값으로 방출)
        JIRA_EMAIL                                # Jira actor 이메일
    volumes:
        jad-<username>:/home/app/.claude (rw)          # 사용자 ~/.claude 영속(인증/세션)
        <settings.json>:/home/app/.claude/settings.json (ro)  # 사전 인가(아래)
        <secrets>/<user>:/run/secrets/<user> (ro)      # per-user 시크릿 파일(값)
    restart_policy  unless-stopped (상시 폴링)
    mem_limit       spawn.mem_limit (예: 4g)

컨테이너 사전 인가(중요):
    worker의 ``claude``는 ``--dangerously-skip-permissions`` 를 헤드리스로 쓰므로
    bypass 수락 다이얼로그가 **사람 입력 없이** 통과해야 한다. 이를 위해 spawner가
    컨테이너 생성 시 그 사용자 ``~/.claude/settings.json``(CLAUDE_CONFIG_DIR)에
    **사전 인가 설정**을 써 넣는다(:func:`render_settings`). 이는 시스템 레벨 인가로
    모든 컨테이너 공통이며, 온보딩은 권한 단계 없이 자격증명만 받는다.
    사용자별 ``permission_level``(기본 ``bypass``)로 조일 수 있다(SECURITY.md 참조).

⚠️ 보안(docker.sock 특권):
    central이 docker.sock에 접근해 컨테이너를 띄우는 것은 사실상 호스트 root
    권한과 동치다(특권 상승 표면). 완화책:
      - docker-socket-proxy(tecnativa 등)를 앞단에 두어 CONTAINERS/POST만 최소
        허용하고 central은 프록시 TCP 엔드포인트로만 접근(sock 직결 금지).
      - central 자체를 비-root 사용자로 실행, 신뢰 네트워크 한정.
    docker_host는 config.spawn.docker_host(로컬=unix:///var/run/docker.sock,
    프록시 사용 시 tcp://socket-proxy:2375)로 주입한다.

    ⚠️ 시크릿 값은 :func:`app.config.read_secret` 로만 읽고, 로그·예외 메시지에
    절대 노출하지 않는다(env dict 자체를 로깅하지 않는다).
"""

from __future__ import annotations

import json
import logging
import os
import posixpath
from typing import Any, Optional

from app.config import read_secret

log = logging.getLogger("jad.spawner")

# 비-root 실행 사용자(컨테이너 내부) — 특권 축소. config.spawn.run_as로 오버라이드.
DEFAULT_RUN_AS = "1000:1000"

# 컨테이너 내부 경로 상수.
CLAUDE_CONFIG_DIR = "/home/app/.claude"
SETTINGS_PATH_IN_CONTAINER = CLAUDE_CONFIG_DIR + "/settings.json"
SECRETS_MOUNT = "/run/secrets"
# worker가 load_config로 읽는 config 디렉토리(컨테이너 내부). worker 워킹디렉토리는
# /app 이라 DEFAULT_CONFIG_PATH(config/config.yaml)가 /app/config/config.yaml 로 해석된다.
CONFIG_DIR_IN_CONTAINER = "/app/config"

# 공유 워크스페이스 named 볼륨의 컨테이너 내부 bind 경로 폴백(run.workspace_dir 미설정 시).
# central compose(jad-workspace → /app/workspace)와 정합.
DEFAULT_WORKSPACE_DIR = "/app/workspace"
DEFAULT_WORKSPACE_VOLUME = "jad-workspace"

# 사전 인가 기본 레벨.
DEFAULT_PERMISSION_LEVEL = "bypass"


def _is_image_not_found(exc: Exception) -> bool:
    """예외가 docker ImageNotFound(404)인지 판정(수정 3).

    rebuild로 워커 컨테이너의 옛 이미지 ID가 사라지면 docker SDK가 컨테이너 이미지를
    inspect할 때 ``docker.errors.ImageNotFound``(HTTP 404)를 던진다. docker 미설치·
    테스트 대역 예외까지 포용하도록 **클래스 이름**(ImageNotFound/NotFound) 또는
    **status_code==404**(예외 자체 또는 .response)로 느슨하게 판정한다.
    """
    if type(exc).__name__ in ("ImageNotFound", "NotFound"):
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status == 404


# ---------------------------------------------------------------------------
# 사전 인가 settings.json 번역
# ---------------------------------------------------------------------------


def _bypass_settings() -> dict:
    """헤드리스 자율 실행용 bypass 사전 인가 settings.json(시스템 레벨).

    ``--dangerously-skip-permissions`` 의 수락/경고 다이얼로그를 사람 입력 없이
    통과시킨다. 모든 컨테이너 공통(사용자 입력 불필요).
    """
    return {
        "permissions": {"defaultMode": "bypassPermissions"},
        "skipDangerousModePermissionPrompt": True,
        "skipAutoPermissionPrompt": True,
        "skipWorkflowUsageWarning": True,
    }


def render_settings(permission_level: str = DEFAULT_PERMISSION_LEVEL) -> dict:
    """``permission_level`` 을 Claude ``settings.json`` dict로 번역.

    - ``bypass`` : 위 :func:`_bypass_settings` (헤드리스 자율 전제).
    - ``sandbox`` / ``allowlist`` : **TODO 분기 자리**(향후 더 조인 권한 세트).

    Raises:
        NotImplementedError: 아직 미구현 레벨(현재는 bypass만).
    """
    level = (permission_level or DEFAULT_PERMISSION_LEVEL).strip().lower()
    if level == "bypass":
        return _bypass_settings()
    # TODO(향후): sandbox(격리 FS/네트워크) · allowlist(도구/명령 화이트리스트)
    # 분기 자리. 지금은 명시적으로 실패시켜 조용한 오설정을 막는다.
    raise NotImplementedError(
        f"permission_level={level!r}는 아직 미구현입니다(현재 bypass만 지원). "
        "sandbox/allowlist는 TODO 분기."
    )


def render_settings_json(permission_level: str = DEFAULT_PERMISSION_LEVEL) -> str:
    """:func:`render_settings` 결과를 UTF-8·LF·2-space JSON 문자열로."""
    return json.dumps(render_settings(permission_level), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Spawner
# ---------------------------------------------------------------------------


class Spawner:
    """Docker SDK 기반 worker 컨테이너 라이프사이클(central).

    docker 클라이언트는 주입 가능하다(테스트는 mock 주입 → 실제 docker 불필요).
    """

    def __init__(self, config, registry=None, client: Any = None) -> None:
        """의존성 주입(설정·레지스트리) + (선택) docker 클라이언트.

        Args:
            config: AppConfig(또는 유사 객체). spawn/secrets/worker_shared_secret 참조.
            registry: 컨테이너 상태 갱신용(없으면 상태 갱신 no-op).
            client: docker.DockerClient(테스트 mock). None이면 최초 사용 시 지연 생성.
        """
        self.config = config
        self.registry = registry
        self._client = client
        # 이미지 stale이지만 활성 잡이 있어 **드레인 대기** 중인 사용자(즉시 죽이지
        # 않고 그 잡이 terminal로 끝난 뒤 재생성한다 — reconcile_pending). 배포가
        # in-flight 잡을 죽이지 않게 한다(작업 C).
        self._pending_recreate: set = set()

    # -- docker 클라이언트(지연 생성) --

    def client(self):
        """docker 클라이언트 반환(없으면 config.spawn.docker_host로 생성)."""
        if self._client is None:
            import docker  # 지연 import — 테스트는 client 주입, docker 미설치 허용

            self._client = docker.DockerClient(base_url=self.config.spawn.docker_host)
        return self._client

    # -- 이름 규칙 --

    @staticmethod
    def container_name(username: str) -> str:
        return f"jad-worker-{username}"

    @staticmethod
    def volume_name(username: str) -> str:
        return f"jad-{username}"

    # -- 사전 인가 settings.json 스테이징(호스트 파일 → ro 바인드) --

    def _settings_host_path(self, username: str) -> str:
        """사용자 사전 인가 settings.json 호스트 경로(secrets.base_dir/<user> 하위)."""
        base = getattr(getattr(self.config, "secrets", None), "base_dir", "") or ""
        return os.path.join(base, username, "claude-settings.json")

    def write_settings(self, username: str, permission_level: str) -> str:
        """사전 인가 settings.json을 호스트에 기록(0600)하고 경로 반환.

        컨테이너의 ``/home/app/.claude/settings.json`` 에 read-only로 바인드된다.
        UTF-8(BOM 없음)·LF.
        """
        path = self._settings_host_path(username)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = render_settings_json(permission_level)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        try:
            os.chmod(path, 0o600)
        except OSError:  # Windows 등 chmod 미지원 — 무해
            pass
        return path

    # -- env / spec 조립 --

    @staticmethod
    def _in_container_secret(ref: str) -> str:
        """secrets 참조(base_dir 상대)를 컨테이너 내부 마운트 경로로 변환."""
        return posixpath.join(SECRETS_MOUNT, ref.replace("\\", "/").lstrip("/"))

    def build_env(self, user) -> dict:
        """컨테이너 environment dict 조립.

        ⚠️ **계약 링치핀**: 여기서 emit하는 키는 worker의
        :meth:`app.agent_runner.UserCreds.from_env` 가 읽는 키와 반드시 일치해야
        한다(per-user attribution의 실제 배선). from_env 계약:
            DISPATCH_GIT_NAME / DISPATCH_GIT_EMAIL   git author 정체성
            DISPATCH_JIRA_EMAIL                       Jira Basic actor 이메일
            JIRA_TOKEN_REF / FORGE_TOKEN_REF          secrets.base_dir 상대 참조
            CLAUDE_OAUTH_TOKEN_REF                     (선택) 참조
            CLAUDE_CODE_OAUTH_TOKEN                    (폴백) 값 직접
            DISPATCH_NOTIFY_USER_ID                    (선택) 완료 알림 @멘션 id
        이 ``*_REF``(상대 참조)가 빠지면 worker의 ``ensure_repos`` 가 토큰을 못
        찾아 레포 프로비저닝을 skip → orchestrator_repo 부재로 잡이 실패한다.

        ⚠️ **forge/알림 중립 이름 + 레거시 동시 방출**: 이 시스템은 GitLab 전용이 아니다
        (``config.forge.kind`` = gitlab|github). 그래서 정본 이름은 ``FORGE_TOKEN_*``·
        ``DISPATCH_NOTIFY_USER_ID`` 이고, 옛 이름(``GITLAB_TOKEN_*``·
        ``DISPATCH_GOOGLE_CHAT_USER_ID``)도 **같은 값으로 함께 방출**한다. 컨테이너 안에서
        옛 이름을 읽는 것들(기존 에이전트 지시문·사용자 스크립트·이전 이미지)이 그대로
        동작해야 하기 때문이다 — 옛 이름 제거는 별도 사이클의 몫이다.

        시크릿 값(CLAUDE_CODE_OAUTH_TOKEN·WORKER_SHARED_SECRET)은 read_secret/config로만
        읽고, Jira/forge 토큰은 값이 아니라 **참조**(상대 ref)와 **파일경로**(마운트
        경로) 포인터로만 넘긴다. 이 dict는 절대 로깅하지 않는다(identity 이름·이메일은
        시크릿 값이 아니라 로깅해도 무해하나, dict 통째 로깅은 여전히 금지).
        """
        cfg = self.config
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        env: dict = {
            "ROLE": "worker",
            "DISPATCH_USER": user.username,
            "CENTRAL_URL": cfg.spawn.central_url,
            "SECRETS_DIR": SECRETS_MOUNT,
        }

        # worker 동시 실행 **안전 상한**(runaway 방지 백스톱 — 정책 cap 아님). worker는
        # central이 dispatch한 잡을 모두 동시에 굴린다(진짜 스로틀은 central의 서버 자원
        # 어드미션). 여기서는 그 안전 상한만 env WORKER_CONCURRENCY로 주입한다.
        # 값: run.worker_max_concurrency(>0). 미설정(구 config)이면 생략 → worker가 기본 64로 파생.
        run_cfg = getattr(cfg, "run", None)
        wc = 0
        try:
            wc = int(getattr(run_cfg, "worker_max_concurrency", 0) or 0)
        except (TypeError, ValueError):
            wc = 0
        if wc > 0:
            env["WORKER_CONCURRENCY"] = str(wc)

        # dispatch HTTP 인증 공유 시크릿(값). 미설정이면 생략(신뢰 네트워크 전제).
        if getattr(cfg, "worker_shared_secret", ""):
            env["WORKER_SHARED_SECRET"] = cfg.worker_shared_secret

        # git author 정체성(값이지만 시크릿 아님 — from_env DISPATCH_GIT_*).
        identity = getattr(user, "identity", None)
        git_name = getattr(identity, "git_name", "") if identity else ""
        git_email = getattr(identity, "git_email", "") if identity else ""
        if git_name:
            env["DISPATCH_GIT_NAME"] = git_name
        if git_email:
            env["DISPATCH_GIT_EMAIL"] = git_email

        secrets_ref = getattr(user, "secrets_ref", None)

        # 사용자 Claude setup-token(값). read_secret로만.
        claude_ref = getattr(secrets_ref, "claude_oauth_token", "") if secrets_ref else ""
        if claude_ref:
            # from_env 계약: 참조(ref)를 넘겨 worker가 base_dir 기준으로 읽게 한다.
            env["CLAUDE_OAUTH_TOKEN_REF"] = claude_ref
            val = read_secret(base_dir, claude_ref)
            if val:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = val

        # Jira/forge 토큰: 값이 아니라 **상대 참조(*_REF)** + 파일경로(*_FILE) 포인터.
        # ⚠️ from_env가 읽는 것은 *_REF 다(*_FILE은 하위호환/보조). *_REF 누락 = 프로비저닝 skip.
        jira_ref = getattr(secrets_ref, "jira_token", "") if secrets_ref else ""
        if jira_ref:
            env["JIRA_TOKEN_REF"] = jira_ref
            env["JIRA_TOKEN_FILE"] = self._in_container_secret(jira_ref)
        # forge 토큰 참조: 정본 forge_token, 없으면 레거시 gitlab_token(옛 registry.json).
        forge_ref = ""
        if secrets_ref:
            forge_ref = (getattr(secrets_ref, "forge_token", "")
                         or getattr(secrets_ref, "gitlab_token", ""))
        if forge_ref:
            forge_path = self._in_container_secret(forge_ref)
            env["FORGE_TOKEN_REF"] = forge_ref
            env["FORGE_TOKEN_FILE"] = forge_path
            # 레거시 미러(같은 값) — 옛 이름을 읽는 컨테이너 내부 소비자 하위호환.
            env["GITLAB_TOKEN_REF"] = forge_ref
            env["GITLAB_TOKEN_FILE"] = forge_path

        # Jira actor 이메일: from_env는 DISPATCH_JIRA_EMAIL 우선, JIRA_EMAIL 폴백.
        if getattr(user, "jira_email", ""):
            env["DISPATCH_JIRA_EMAIL"] = user.jira_email
            env["JIRA_EMAIL"] = user.jira_email

        # (선택) 완료 알림 @멘션용 **알림 채널 사용자 id**(값이지만 시크릿 아님).
        # 정본은 provider 중립 이름이고 옛 이름도 같은 값으로 함께 방출한다.
        # 없으면 둘 다 방출 생략(알림은 display_name 폴백으로 degrade).
        notify_uid = (getattr(user, "notify_user_id", "")
                      or getattr(user, "google_chat_user_id", "") or "")
        if notify_uid:
            env["DISPATCH_NOTIFY_USER_ID"] = notify_uid
            env["DISPATCH_GOOGLE_CHAT_USER_ID"] = notify_uid

        return env

    def build_volumes(self, user, settings_path: Optional[str] = None) -> dict:
        """컨테이너 volumes dict 조립(config + 영속 .claude + per-user 시크릿 ro).

        ⚠️ sibling container 문제: central이 Docker SDK(socket-proxy 경유)로 worker를
        띄울 때 바인드 마운트의 **source 경로는 호스트 docker 데몬이 해석**한다(central
        컨테이너 내부 경로가 아님). 따라서 worker 바인드 source는 반드시 **호스트 경로**
        여야 한다. central은 시크릿을 base_dir(컨테이너 내부, 예: /run/secrets)에 **기록**
        하지만, worker 바인드용 source는 그 같은 호스트 디렉토리의 다른 관점인
        ``spawn.host_deploy_dir`` 기준 경로로 매핑한다:

            base_dir(central 기록용)  == <host_deploy_dir>/secrets  (같은 호스트 dir, 두 관점)

        - ``<host_deploy_dir>/config`` → ``/app/config`` (ro)  ← 크래시 픽스. worker가
          config/config.yaml 을 읽어 ConfigError 크래시 루프를 벗어난다.
        - ``<host_deploy_dir>/secrets/<user>`` → ``/run/secrets/<user>`` (ro).
          이 디렉토리에 ``claude-settings.json`` 이 이미 포함돼 컨테이너 내부에서는
          ``/run/secrets/<user>/claude-settings.json`` 로 접근 가능하다.
        - ``jad-<user>`` 명명 볼륨 → ``/home/app/.claude`` (rw). 볼륨명은 호스트경로 무관.
        - (선택) ``<host_deploy_dir>/secrets/<webhook_ref>`` → ``/run/secrets/<webhook_ref>``
          (ro). notify가 설정된 경우에만, worker의 :mod:`app.notify` 가 팀 Google Chat
          웹훅을 읽도록 그 **파일 하나만** 바인드한다(service 디렉토리 전체 금지 —
          최소권한). host_deploy_dir 미설정(로컬 폴백) 시엔 생략.

        ⚠️ 사전 인가 ``settings.json`` 은 **파일 바인드하지 않는다**(두 번째 spawn 버그
        픽스). 명명 볼륨(``jad-<user>``)이 이미 ``/home/app/.claude`` 를 덮어쓰므로 그
        볼륨 마운트 *하위 파일 경로*(``/home/app/.claude/settings.json``)에 파일을 다시
        바인드하려 하면 runc가 거부한다("not a directory: Are you trying to mount a
        directory onto a file"). 대신 worker 부팅 시
        ``/run/secrets/<user>/claude-settings.json`` → ``$CLAUDE_CONFIG_DIR/settings.json``
        으로 **복사**한다(:func:`app.main.copy_worker_settings`).

        ``host_deploy_dir`` 가 비어 있으면(로컬 개발 등 — 호스트==central 파일시스템)
        직접 경로로 폴백하고 경고를 남긴다.

        Args:
            settings_path: 하위호환용(무시됨) — settings.json은 더 이상 바인드되지 않고
                per-user 시크릿 디렉토리 안에서 worker가 부팅 시 복사한다.
        """
        del settings_path  # 더 이상 바인드 source로 쓰지 않음(하위호환 시그니처 유지).
        cfg = self.config
        username = user.username
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        host_deploy_dir = getattr(getattr(cfg, "spawn", None), "host_deploy_dir", "") or ""

        if host_deploy_dir:
            # 호스트 docker 데몬이 해석하는 호스트 경로(sibling container).
            host_secrets = posixpath.join(host_deploy_dir, "secrets")
            config_src = posixpath.join(host_deploy_dir, "config")
            user_secret_src = posixpath.join(host_secrets, username)
        else:
            # 폴백(로컬 개발): 호스트==central 파일시스템 전제. base_dir·
            # 로컬 config 디렉토리를 직접 source로 쓴다.
            log.warning(
                "spawn.host_deploy_dir 미설정 — worker 바인드에 직접 경로 폴백. "
                "socket-proxy 경유 실배포에서는 HOST_DEPLOY_DIR(호스트 배포 절대경로)를 "
                "설정해야 worker 바인드 source가 호스트 데몬 기준으로 올바르게 해석된다."
            )
            config_src = os.path.abspath("config")
            user_secret_src = os.path.join(base_dir, username)

        # 공유 워크스페이스 named 볼륨(central·모든 워커가 공유하는 단일 클론 지점,
        # 설계 §4). named 볼륨이라 **볼륨명으로** docker가 해석 → host_deploy_dir 무관
        # (호스트 경로 매핑 불필요). central compose가 같은 이름(jad-workspace)으로
        # 선언하므로 central·워커가 레포 한 벌을 공유한다(N중 클론·N번 pull 제거).
        # bind 경로는 run.workspace_dir(=config 파생 orchestrator/dlc-meta/… 상위).
        workspace_volume = getattr(getattr(cfg, "spawn", None), "workspace_volume", "") \
            or DEFAULT_WORKSPACE_VOLUME
        workspace_dir = getattr(getattr(cfg, "run", None), "workspace_dir", "") \
            or DEFAULT_WORKSPACE_DIR

        volumes: dict = {
            # config (ro) — 항상 포함(크래시 픽스: worker가 /app/config/config.yaml 을 읽음).
            config_src: {"bind": CONFIG_DIR_IN_CONTAINER, "mode": "ro"},
            # 사용자 ~/.claude 영속(인증/세션).
            self.volume_name(username): {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"},
            # per-user 시크릿 디렉토리(값 + claude-settings.json) read-only.
            # 다른 사용자 시크릿은 안 보인다.
            user_secret_src: {"bind": posixpath.join(SECRETS_MOUNT, username), "mode": "ro"},
            # 공유 워크스페이스(rw) — 레포 단일 클론 공유. 볼륨명으로 마운트(named).
            workspace_volume: {"bind": workspace_dir, "mode": "rw"},
        }

        # 완료 알림(notify)이 설정돼 있으면 팀 Google Chat 웹훅 시크릿 파일 "하나만"
        # worker에 read-only로 바인드한다. worker의 :mod:`app.notify` 가 이 웹훅을
        # base_dir(=/run/secrets) 기준 ``webhook_ref`` 경로로 읽어야 하는데(예:
        # /run/secrets/service/google-chat-webhook), per-user 시크릿만 마운트하면
        # 못 읽어 알림이 조용히 스킵된다.
        # ⚠️ service/ 디렉토리 **전체**를 바인드하지 않는다 — 거기엔 central watcher의
        #    jira-token 도 있어 worker에 노출하면 최소권한 위반. 웹훅 파일 단 하나만.
        # 가드: host_deploy_dir(호스트 경로 해석 필수)·notify.enabled·webhook_ref가
        #    모두 있을 때만 추가(로컬 폴백/미설정 시 생략).
        notify_cfg = getattr(cfg, "notify", None)
        webhook_ref = getattr(notify_cfg, "webhook_ref", "") if notify_cfg else ""
        if host_deploy_dir and getattr(notify_cfg, "enabled", False) and webhook_ref:
            rel = webhook_ref.replace("\\", "/").lstrip("/")
            webhook_src = posixpath.join(host_deploy_dir, "secrets", rel)
            volumes[webhook_src] = {"bind": self._in_container_secret(webhook_ref), "mode": "ro"}

        return volumes

    def build_spec(self, user, settings_path: Optional[str] = None) -> dict:
        """사용자 레코드 + config.spawn으로 ``containers.run`` kwargs 조립.

        ``settings_path`` 미지정 시 사전 인가 settings.json을 기록해 사용한다.
        """
        cfg = self.config
        level = getattr(user, "permission_level", DEFAULT_PERMISSION_LEVEL) or DEFAULT_PERMISSION_LEVEL
        if settings_path is None:
            settings_path = self.write_settings(user.username, level)
        run_as = getattr(cfg.spawn, "run_as", "") or DEFAULT_RUN_AS
        return {
            "image": cfg.spawn.image,
            "name": self.container_name(user.username),
            "environment": self.build_env(user),
            "volumes": self.build_volumes(user, settings_path),
            "network": cfg.spawn.network,
            "restart_policy": {"Name": "unless-stopped"},
            "mem_limit": cfg.spawn.mem_limit,
            "user": run_as,
            "detach": True,
            # ⚠️ 좀비 프로세스(zombie/defunct) 방지 — **결정적 픽스**.
            # 워커 컨테이너의 PID 1 은 `python -m app.main`(ENTRYPOINT)이다. 파이썬은
            # 임의로 reparent된 손자 프로세스를 reap하지 않으므로, worker가 spawn한
            # `claude` 가 git/esbuild/gradle 손자를 남긴 채 죽으면 그 손자들이 PID 1
            # (python)로 reparent → **영구 좀비**로 쌓인다(컨테이너/데몬 재시작 전엔 안
            # 없어짐, 실측 136개). Docker의 init(tini)을 PID 1 로 주입하면 tini가
            # 고아를 reap한다. docker-py ``containers.run(init=True)`` →
            # HostConfig.Init=true → dockerd가 tini를 PID 1 로 실행(우리 ENTRYPOINT는
            # 그 자식). docker-py는 3.1.0+ 에서 이 kwarg를 지원한다(리포 requirements의
            # unpinned ``docker`` = 7.x → 지원).
            "init": True,
        }

    # -- 컨테이너 조회/상태 헬퍼 --

    def _get_container(self, name: str):
        """이름으로 컨테이너 조회(없거나 조회 실패 시 None).

        docker.errors.NotFound 등 어떤 예외든 "부재"로 취급한다(테스트는
        side_effect=Exception로 부재를 흉내낼 수 있다).
        """
        try:
            return self.client().containers.get(name)
        except Exception:  # noqa: BLE001 — NotFound 포함 모든 조회 실패 = 부재
            return None

    def _safe_set_status(self, username: str, name: str, status: str) -> None:
        """레지스트리 컨테이너 상태 갱신(등록 안 됐거나 registry 없으면 no-op)."""
        if self.registry is None:
            return
        try:
            self.registry.set_container_status(username, name, status)
        except KeyError:
            pass  # 미등록 사용자 — 상태 갱신 생략

    # -- 라이프사이클 --

    def ensure_worker(self, user) -> str:
        """사용자 worker 컨테이너 보장(없으면 run, 멈춰 있으면 start) 후 상태 갱신.

        Returns:
            컨테이너 id(또는 이름).
        """
        username = user.username
        name = self.container_name(username)
        c = self.client()

        # 볼륨 보장(없으면 생성).
        self._ensure_volume(self.volume_name(username))

        # 사전 인가 settings.json 기록.
        level = getattr(user, "permission_level", DEFAULT_PERMISSION_LEVEL) or DEFAULT_PERMISSION_LEVEL
        settings_path = self.write_settings(username, level)

        existing = self._get_container(name)
        if existing is not None:
            if getattr(existing, "status", "") != "running":
                existing.start()
            self._safe_set_status(username, name, "running")
            log.info("worker 재사용: %s", name)  # 값은 로깅하지 않음
            return getattr(existing, "id", name)

        spec = self.build_spec(user, settings_path)
        container = c.containers.run(**spec)
        self._safe_set_status(username, name, "running")
        log.info("worker 기동: %s (image=%s)", name, spec["image"])  # env 로깅 금지
        return getattr(container, "id", name)

    def _ensure_volume(self, name: str):
        """사용자 영속 볼륨 보장(없으면 생성)."""
        c = self.client()
        try:
            return c.volumes.get(name)
        except Exception:  # noqa: BLE001 — 부재/조회 실패 → 생성 시도
            return c.volumes.create(name)

    def stop_worker(self, username: str) -> None:
        """사용자 worker 컨테이너 중지(레지스트리 상태 갱신)."""
        container = self._get_container(self.container_name(username))
        if container is not None:
            container.stop()
        self._safe_set_status(username, self.container_name(username), "stopped")

    def remove_worker(self, username: str) -> None:
        """사용자 worker 컨테이너 제거(사용자 삭제/재프로비저닝 시)."""
        container = self._get_container(self.container_name(username))
        if container is not None:
            container.remove(force=True)
        self._safe_set_status(username, self.container_name(username), "absent")

    def worker_status(self, username: str) -> str:
        """사용자 worker 상태(running | stopped | absent). 레지스트리도 갱신."""
        container = self._get_container(self.container_name(username))
        if container is None:
            self._safe_set_status(username, self.container_name(username), "absent")
            return "absent"
        raw = getattr(container, "status", "") or ""
        if raw == "running":
            norm = "running"
        elif raw in ("exited", "paused", "dead", "created", "restarting"):
            norm = "stopped"
        else:
            norm = raw or "stopped"
        self._safe_set_status(username, self.container_name(username), norm)
        return norm

    # -- 배포 시 워커 이미지 reconcile(작업 C) --

    def worker_image_id(self, image: Optional[str] = None) -> Optional[str]:
        """현재 ``config.spawn.image`` 태그가 가리키는 **이미지 ID**(조회 실패 시 None).

        배포가 이미지(jira-auto-dispatcher:latest)를 재빌드하면 같은 태그가 새 ID를
        가리킨다. 실행 중인 워커 컨테이너의 이미지 ID와 이 값을 비교해 stale을 판정한다.
        """
        img = image or getattr(getattr(self.config, "spawn", None), "image", "")
        if not img:
            return None
        try:
            obj = self.client().images.get(img)
        except Exception:  # noqa: BLE001 — 미존재/조회 실패 = 판정 불가(None)
            return None
        return getattr(obj, "id", None)

    @staticmethod
    def _container_image_id(container) -> Optional[str]:
        """컨테이너가 실제로 실행 중인 이미지의 ID(조회 실패 시 None)."""
        return getattr(getattr(container, "image", None), "id", None)

    def _recreate_worker(self, user) -> str:
        """워커 컨테이너를 제거 후 현재 이미지로 재spawn(이미지 갱신 반영)."""
        username = getattr(user, "username", "") or str(user)
        self.remove_worker(username)
        cid = self.ensure_worker(user)
        log.info("worker 재생성(이미지 갱신): %s", self.container_name(username))
        return cid

    def reconcile_workers(self, users, has_active_job: Callable[[str], bool],
                          *, image_id: Optional[str] = None) -> dict:
        """등록 enabled 사용자들의 워커 이미지를 현재 이미지 ID와 대조 → stale 조정.

        배포가 central만 recreate하면 기존 워커는 **stale 이미지**로 남는다(워커측
        변경 미반영). 이 메서드가 부팅 시 각 워커의 실행 이미지 ID를 현재
        ``config.spawn.image`` ID와 비교해:
            - **동일** → no-op(최신). 혹시 대기 중이던 pending에서 제거.
            - **stale + 유휴**(활성 잡 없음) → 즉시 remove+재spawn.
            - **stale + 활성 잡**(running/cancelling) → **드레인**: 지금 죽이지 않고
              pending에 표시만 해뒀다가, 그 잡이 terminal로 끝난 뒤 재생성한다
              (:meth:`reconcile_pending`, tick에서 호출). in-flight 잡 보호.

        모두 **best-effort·예외격리**(한 사용자 실패가 다른 사용자/부팅을 막지 않음).
        시크릿은 로깅하지 않는다(username은 시크릿 아님).

        Args:
            users: 재생성 스펙 조립에 필요한 **user 레코드** 이터러블(enabled만 넘길 것).
            has_active_job: ``username -> bool`` — 그 사용자에게 활성 잡이 있는지.
            image_id: 현재 이미지 ID 주입(없으면 :meth:`worker_image_id` 조회).

        Returns:
            ``{"recreated": [...], "deferred": [...], "skipped": [...], "errors": [...]}``.
        """
        summary: dict = {"recreated": [], "deferred": [], "skipped": [], "errors": []}
        current = image_id if image_id is not None else self.worker_image_id()
        if not current:
            log.warning("현재 워커 이미지 ID 확인 불가 — reconcile 생략(best-effort)")
            return summary

        for user in users:
            username = getattr(user, "username", "") or str(user)
            try:
                container = self._get_container(self.container_name(username))
                if container is None:
                    # enabled인데 컨테이너 부재 = 이미지 stale 조정 대상 아님(별도
                    # 라이프사이클: ensure_worker가 최초 기동). 여기선 건너뛴다.
                    summary["skipped"].append(username)
                    continue
                # ⚠️ 이미지 ID inspect는 rebuild로 옛 ID가 사라지면 ImageNotFound(404)를
                # 던진다(실측). 예외로 죽어 재생성이 스킵되면 stale 워커가 방치되므로,
                # inspect 실패(ImageNotFound/404)를 **확실한 stale**로 취급한다(수정 3).
                try:
                    cur = self._container_image_id(container)
                    up_to_date = cur is not None and cur == current
                except Exception as exc:  # noqa: BLE001 — 아래에서 404만 stale로 흡수
                    if not _is_image_not_found(exc):
                        raise  # 비-404 예외는 상위 per-user 격리(errors)로.
                    log.info(
                        "worker 이미지 inspect ImageNotFound(404) → stale 판정: %s",
                        self.container_name(username),
                    )
                    up_to_date = False
                if up_to_date:
                    self._pending_recreate.discard(username)  # 최신 → 대기 해제
                    summary["skipped"].append(username)
                    continue
                # stale(다르거나 판정 불가/404) → 유휴면 즉시, 활성이면 드레인.
                if has_active_job(username):
                    self._pending_recreate.add(username)
                    summary["deferred"].append(username)
                    log.info("worker 이미지 stale이나 활성 잡 있음 → 드레인 대기: %s",
                             self.container_name(username))
                else:
                    self._recreate_worker(user)
                    self._pending_recreate.discard(username)
                    summary["recreated"].append(username)
            except Exception:  # noqa: BLE001 — 사용자별 격리(부팅/타 사용자 보호)
                log.exception("worker reconcile 실패(격리): %s", username)
                summary["errors"].append(username)
        return summary

    def reconcile_pending(self, has_active_job: Callable[[str], bool]) -> dict:
        """드레인 대기 중이던 워커를, 활성 잡이 끝났으면 재생성한다(tick에서 주기 호출).

        :meth:`reconcile_workers` 가 stale+활성으로 미뤄둔 사용자들을 재검사해, 이제
        유휴면(활성 잡 없음) remove+재spawn한다. 아직 활성이면 다음 tick으로 미룬다.
        best-effort·예외격리. user 레코드는 registry에서 조회한다.

        Returns:
            ``{"recreated": [...], "still_active": [...], "errors": [...]}``.
        """
        summary: dict = {"recreated": [], "still_active": [], "errors": []}
        for username in list(self._pending_recreate):
            try:
                if has_active_job(username):
                    summary["still_active"].append(username)
                    continue
                rec = self.registry.get(username) if self.registry is not None else None
                if rec is None:
                    self._pending_recreate.discard(username)  # 사라진 사용자 → 정리
                    continue
                self._recreate_worker(rec)
                self._pending_recreate.discard(username)
                summary["recreated"].append(username)
            except Exception:  # noqa: BLE001 — 사용자별 격리
                log.exception("pending worker 재생성 실패(격리): %s", username)
                summary["errors"].append(username)
        return summary

    # -- 하위호환 별칭(기존 스텁 시그니처) --

    def start(self, username: str) -> str:
        """username으로 레지스트리 조회 후 ensure_worker(하위호환)."""
        rec = self.registry.get(username) if self.registry is not None else None
        if rec is None:
            raise KeyError(f"등록되지 않은 사용자: {username}")
        return self.ensure_worker(rec)

    def stop(self, username: str) -> None:
        """stop_worker 별칭(하위호환)."""
        self.stop_worker(username)

    def status(self, username: str) -> str:
        """worker_status 별칭(하위호환)."""
        return self.worker_status(username)

    def remove(self, username: str) -> None:
        """remove_worker 별칭(하위호환)."""
        self.remove_worker(username)


# ---------------------------------------------------------------------------
# 모듈 레벨 편의 함수(계약 시그니처)
# ---------------------------------------------------------------------------


def ensure_worker(user_record, config, registry=None, client: Any = None) -> str:
    """:meth:`Spawner.ensure_worker` 편의 래퍼."""
    return Spawner(config, registry, client=client).ensure_worker(user_record)


def stop_worker(username: str, config, registry=None, client: Any = None) -> None:
    """:meth:`Spawner.stop_worker` 편의 래퍼."""
    Spawner(config, registry, client=client).stop_worker(username)


def remove_worker(username: str, config, registry=None, client: Any = None) -> None:
    """:meth:`Spawner.remove_worker` 편의 래퍼."""
    Spawner(config, registry, client=client).remove_worker(username)


def worker_status(username: str, config, registry=None, client: Any = None) -> str:
    """:meth:`Spawner.worker_status` 편의 래퍼."""
    return Spawner(config, registry, client=client).worker_status(username)
