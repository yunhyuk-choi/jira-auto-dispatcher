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
        CLAUDE_CODE_OAUTH_TOKEN=<주입>            # setup-token(값은 시크릿 참조에서)
        SECRETS_DIR=/run/secrets                  # 컨테이너 내부 시크릿 루트(tmpfs)
        JIRA_TOKEN_FILE / FORGE_TOKEN_FILE        # per-user 토큰 "파일경로"(값 아님)
                                                  # (GITLAB_TOKEN_FILE 도 같은 값으로 방출)
        JIRA_EMAIL                                # Jira actor 이메일
        JAD_INJECT_CONFIG / JAD_INJECT_SECRETS    # 스폰 시 주입 페이로드(app.inject)
    volumes (**named 볼륨 2개뿐 — bind 마운트 없음**):
        jad-<username>:/home/app/.claude (rw)          # 사용자 ~/.claude 영속(인증/세션)
        jad-workspace:<run.workspace_dir> (rw)         # 공유 워크스페이스(단일 클론)
    tmpfs:
        /run/secrets                                   # 주입 시크릿이 사는 곳(RAM 전용)
    restart_policy  unless-stopped (상시 폴링)
    mem_limit       spawn.mem_limit (예: 4g)

⚠️ **bind 마운트를 쓰지 않는 이유**(``spawn.host_deploy_dir`` 제거의 근거):
    central은 워커를 직접 만들지 않고 socket-proxy 경유로 **호스트 docker 데몬**에게
    요청한다(sibling container). 그래서 바인드 source는 호스트가 해석한다 — central
    안의 경로(/run/secrets · /app/config)를 그대로 넘기면 호스트의 다른 것(또는 없는
    것)이 마운트되고, 워커는 **에러 없이 뜬 다음** 잡 실행 시점에야 죽는다. 그래서
    예전엔 설치자가 호스트 배포 절대경로를 정확히 적어야 했고, 틀리면 조용히 깨졌다.
    지금은 config·시크릿·웹훅을 전부 **스폰 시 주입**(:mod:`app.inject`)으로 넘기고
    마운트는 named 볼륨만 남겼다 — 볼륨은 *이름* 으로 해석되므로 호스트 경로 개념이
    아예 없다. 설치자가 틀릴 값이 사라졌다.

컨테이너 사전 인가(중요):
    worker의 ``claude``는 ``--dangerously-skip-permissions`` 를 헤드리스로 쓰므로
    bypass 수락 다이얼로그가 **사람 입력 없이** 통과해야 한다. 이를 위해 spawner가
    그 사용자 사전 인가 ``settings.json`` 내용(:func:`render_settings`)을 주입
    페이로드에 ``<user>/claude-settings.json`` 으로 실어 보내고, worker가 부팅 시
    이를 ``~/.claude/settings.json``(CLAUDE_CONFIG_DIR)으로 복사한다
    (:func:`app.main.copy_worker_settings`). 이는 시스템 레벨 인가로 모든 컨테이너
    공통이며, 온보딩은 권한 단계 없이 자격증명만 받는다.
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
from typing import Any, Callable, Optional

from app import inject
from app.config import DEFAULT_CONFIG_PATH, read_secret

log = logging.getLogger("jad.spawner")

# 비-root 실행 사용자(컨테이너 내부) — 특권 축소. config.spawn.run_as로 오버라이드.
DEFAULT_RUN_AS = "1000:1000"

# 컨테이너 내부 경로 상수.
CLAUDE_CONFIG_DIR = "/home/app/.claude"
SETTINGS_PATH_IN_CONTAINER = CLAUDE_CONFIG_DIR + "/settings.json"
# ⚠️ 아래 두 상수는 :mod:`app.inject` 에서 **파생**한다 — 주입을 쓰는 쪽(spawner)과
# 되살리는 쪽(worker)이 같은 경로를 봐야 하므로 값을 두 곳에 적지 않는다.
SECRETS_MOUNT = inject.DEFAULT_SECRETS_DIR
# worker가 load_config로 읽는 config 디렉토리(컨테이너 내부). worker 워킹디렉토리는
# /app 이라 DEFAULT_CONFIG_PATH(config/config.yaml)가 /app/config/config.yaml 로 해석된다.
CONFIG_DIR_IN_CONTAINER = posixpath.dirname(inject.DEFAULT_CONFIG_DEST)

# 주입 시크릿이 사는 tmpfs 크기(토큰 몇 개 + settings.json — 넉넉).
SECRETS_TMPFS_SIZE = "8m"

# 사전 인가 settings.json 의 시크릿 참조 파일명(worker가 부팅 시 ~/.claude로 복사).
SETTINGS_SECRET_FILENAME = "claude-settings.json"

# 공유 워크스페이스 named 볼륨의 컨테이너 내부 bind 경로 폴백(run.workspace_dir 미설정 시).
# central compose(jad-workspace → /app/workspace)와 정합.
DEFAULT_WORKSPACE_DIR = "/app/workspace"
DEFAULT_WORKSPACE_VOLUME = "jad-workspace"

# 사전 인가 기본 레벨.
DEFAULT_PERMISSION_LEVEL = "bypass"


def _norm_ref(ref) -> str:
    """시크릿 참조를 비교 가능한 한 가지 표기로(슬래시·선행 / 제거)."""
    return str(ref or "").replace(chr(92), "/").lstrip("/")


def _parse_run_as(run_as: str) -> tuple:
    """``"1000:1000"`` → ``(1000, 1000)``. 숫자로 못 읽으면 ``(None, None)``.

    docker 의 ``user`` 는 이름도 허용하지만(``app``), tmpfs 마운트 옵션은 숫자 uid 만
    받는다. 이름/빈 값은 판정 불가로 보고 호출자가 폴백하게 한다.
    """
    text = str(run_as or "").strip()
    if not text:
        return (None, None)
    parts = text.split(":", 1)
    try:
        uid = int(parts[0])
    except ValueError:
        return (None, None)
    gid = None
    if len(parts) == 2:
        try:
            gid = int(parts[1])
        except ValueError:
            gid = None
    return (uid, gid)


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
            config: AppConfig(또는 유사 객체). spawn/secrets 참조.
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

    # -- 스폰 시 주입 페이로드 조립(호스트 경로 없음) --

    def _config_path(self) -> str:
        """central 자신이 로드한 config.yaml 경로(없으면 기본 경로)."""
        return getattr(self.config, "config_path", "") or DEFAULT_CONFIG_PATH

    def config_text(self) -> str:
        """worker에 넘길 config.yaml **원문**(없으면 빈 문자열).

        원문 그대로 넘긴다 — ``${SECRETS_DIR}`` 같은 토큰은 worker가 자기 env로
        치환하므로(bind 마운트 시절과 동일 의미) 해석된 값을 다시 직렬화하지 않는다.
        """
        path = self._config_path()
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            log.warning(
                "worker 주입용 config 원문을 읽지 못했습니다(%s) — worker가 config를 "
                "찾지 못해 부팅에 실패할 수 있습니다.", path,
            )
            return ""

    def collect_injected_secrets(self, user) -> dict:
        """이 사용자 worker에 넘길 ``{ref: 내용}`` (**격리 강제 지점**).

        담는 것:
            - ``<user>/…`` 로 시작하는 per-user 시크릿 참조(jira/forge/claude 토큰).
            - 사전 인가 ``<user>/claude-settings.json`` (파일이 아니라 렌더 결과).
            - (조건부) 알림 웹훅 **파일 하나** — ``notify.webhook_ref``.

        ⚠️ **격리 불변식**: per-user 참조는 반드시 ``<username>/`` 접두어여야 하고,
        그 외에 허용되는 유일한 ref 는 config 에 선언된 웹훅 ref 하나뿐이다. 조건에
        맞지 않는 ref 는 경고와 함께 **버린다** — 레지스트리가 오염돼 남의 경로를
        가리켜도 그 값이 다른 사용자의 워커로 새지 않는다.

        ⚠️ ``service/`` 디렉토리를 통째로 주지 않는다 — 거기엔 central watcher의 Jira
        토큰도 있어 워커에 노출하면 최소권한 위반이다(웹훅 파일 단 하나만).
        """
        cfg = self.config
        username = user.username
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        secrets_ref = getattr(user, "secrets_ref", None)

        notify_cfg = getattr(cfg, "notify", None)
        webhook_ref = _norm_ref(
            getattr(notify_cfg, "webhook_ref", "") if notify_cfg else "")
        webhook_allowed = bool(webhook_ref) and bool(getattr(notify_cfg, "enabled", False))

        prefix = f"{username}/"
        files: dict = {}

        candidates = []
        if secrets_ref is not None:
            for attr in ("jira_token", "forge_token", "gitlab_token", "claude_oauth_token"):
                ref = getattr(secrets_ref, attr, "") or ""
                if ref:
                    candidates.append(ref)
        if webhook_allowed:
            candidates.append(webhook_ref)

        seen: set = set()
        for ref in candidates:
            norm = _norm_ref(ref)
            if norm in seen:
                continue  # 같은 파일을 두 이름으로 참조(forge/gitlab 미러) — 한 번만.
            seen.add(norm)
            is_own = norm.startswith(prefix)
            is_webhook = webhook_allowed and norm == webhook_ref
            if not inject.is_safe_ref(norm) or not (is_own or is_webhook):
                log.warning(
                    "worker 주입에서 제외 — 이 사용자(%s)의 참조가 아닙니다: %s",
                    username, norm,
                )
                continue
            content = self._read_secret_raw(base_dir, norm)
            if content is None:
                log.warning("worker 주입 시크릿 파일 없음(건너뜀): %s", norm)
                continue
            files[norm] = content

        # 사전 인가 settings.json — 디스크에 없고 매 spawn 시 렌더한다(항상 최신).
        level = (getattr(user, "permission_level", DEFAULT_PERMISSION_LEVEL)
                 or DEFAULT_PERMISSION_LEVEL)
        files[prefix + SETTINGS_SECRET_FILENAME] = render_settings_json(level)
        return files

    @staticmethod
    def _read_secret_raw(base_dir: str, ref: str) -> Optional[str]:
        """시크릿 파일 **원문**(strip 없음 — 바이트 그대로 왕복). 없으면 None.

        :func:`app.config.read_secret` 은 값을 strip 하지만, 여기서는 파일을 그대로
        복제해 worker 쪽 ``read_secret`` 이 오늘과 똑같이 동작하게 한다.
        """
        path = os.path.join(base_dir, *ref.split("/")) if base_dir else ref
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None

    def build_injection(self, user) -> dict:
        """주입 env 조각 ``{JAD_INJECT_CONFIG, JAD_INJECT_SECRETS}``.

        ⚠️ 이 dict 는 시크릿 **값**을 담는다(base64) — 절대 로깅하지 않는다.
        """
        env: dict = {}
        text = self.config_text()
        if text:
            env[inject.ENV_CONFIG] = inject.encode_config(text)
        files = self.collect_injected_secrets(user)
        if files:
            env[inject.ENV_SECRETS] = inject.encode_secrets(files)
        return env

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

        시크릿 값(CLAUDE_CODE_OAUTH_TOKEN)은 read_secret/config로만 읽고, Jira/forge
        토큰은 값이 아니라 **참조**(상대 ref)와 **파일경로**(마운트 경로) 포인터로만 넘긴다. 이 dict는 절대 로깅하지 않는다(identity 이름·이메일은
        시크릿 값이 아니라 로깅해도 무해하나, dict 통째 로깅은 여전히 금지).
        """
        cfg = self.config
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        env: dict = {
            "ROLE": "worker",
            "DISPATCH_USER": user.username,
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

        # ⚠️ ``CENTRAL_URL`` · ``WORKER_SHARED_SECRET`` 은 **더 이상 주입하지 않는다.**
        # 워커가 중앙을 폴링하던 시절의 값이었고(폴링 대상 주소 · ``X-Worker-Secret``
        # 공유 시크릿), 그 서빙 표면과 폴링 소비자가 프랙탈 seam(중앙 → docker exec
        # 푸시)으로 대체되며 컨테이너 안에 읽는 곳이 하나도 남지 않았다. 쓰이지 않는
        # 시크릿을 컨테이너 스펙에 실어 두는 것은 노출 표면만 늘린다(docker inspect).

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

        # 스폰 시 주입(config·per-user 시크릿·웹훅) — bind 마운트 대체. 이 사용자
        # 것만 담긴다(collect_injected_secrets 가 접두어로 강제).
        env.update(self.build_injection(user))

        return env

    def build_volumes(self, user, settings_path: Optional[str] = None) -> dict:
        """컨테이너 volumes dict 조립 — **named 볼륨 2개뿐(bind 마운트 없음)**.

        - ``jad-<user>`` → ``/home/app/.claude`` (rw). 사용자 인증/세션 영속.
        - ``<spawn.workspace_volume>`` → ``run.workspace_dir`` (rw). central·모든 워커가
          공유하는 단일 클론 지점(설계 §4) — N중 클론·N번 pull 제거.

        둘 다 **볼륨 이름**으로 docker가 해석하므로 호스트 경로가 등장하지 않는다.
        예전엔 여기에 세 개의 bind(config·per-user 시크릿·알림 웹훅)가 더 있었고,
        그 source 를 호스트 데몬이 해석하는 탓에 설치자가 ``spawn.host_deploy_dir``
        을 정확히 적어야 했다(틀리면 빈 디렉토리가 조용히 마운트됨). 그 셋은 이제
        **스폰 시 주입**(:meth:`build_injection` → :func:`app.inject.materialize`)으로
        넘어가고, 워커는 자기 컨테이너 안(tmpfs·이미지 레이어)에 스스로 기록한다.

        ⚠️ 사전 인가 ``settings.json`` 은 여전히 **마운트하지 않는다**. 명명 볼륨
        (``jad-<user>``)이 ``/home/app/.claude`` 를 덮으므로 그 하위 파일 경로에 다시
        마운트하면 runc가 거부한다. 주입된
        ``$SECRETS_DIR/<user>/claude-settings.json`` 을 worker가 부팅 시 복사한다
        (:func:`app.main.copy_worker_settings`).

        Args:
            settings_path: 하위호환용(무시됨).
        """
        del settings_path  # 더 이상 쓰지 않음(하위호환 시그니처 유지).
        cfg = self.config
        username = user.username

        workspace_volume = getattr(getattr(cfg, "spawn", None), "workspace_volume", "") \
            or DEFAULT_WORKSPACE_VOLUME
        workspace_dir = getattr(getattr(cfg, "run", None), "workspace_dir", "") \
            or DEFAULT_WORKSPACE_DIR

        return {
            # 사용자 ~/.claude 영속(인증/세션).
            self.volume_name(username): {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"},
            # 공유 워크스페이스(rw) — 레포 단일 클론 공유. 볼륨명으로 마운트(named).
            workspace_volume: {"bind": workspace_dir, "mode": "rw"},
        }

    @staticmethod
    def secrets_tmpfs(run_as: str) -> dict:
        """주입 시크릿이 사는 ``/run/secrets`` tmpfs 스펙(``containers.run(tmpfs=...)``).

        왜 tmpfs인가:
            - **소유권**: 워커는 비-root(기본 uid 1000)로 돌고 ``/run`` 은 root 소유라
              그냥은 쓸 수 없다. tmpfs 를 ``uid=`` 로 걸면 그 uid 가 소유자가 되어
              **주입 파일을 쓴 주체와 읽는 주체가 동일**해진다(권한 드리프트 불가).
              uid 는 ``spawn.run_as`` 에서 파생한다 — 두 값이 갈라질 수 없다.
            - **잔류 축소**: 시크릿이 컨테이너 이미지 레이어(디스크)가 아니라 RAM 에만
              존재하고, 컨테이너가 죽으면 사라진다.
            - **재기동 안전**: 재시작으로 tmpfs 가 비어도 부팅 시 env 에서 다시
              materialize 되므로 자가 복구된다(파일을 미리 넣어두는 방식과 다른 점).

        ``run_as`` 가 숫자 uid 로 파싱되지 않으면(예: 이름) uid/gid 를 걸지 않고
        mode=0777 로 둔다 — 컨테이너 전용 tmpfs 라 무해하고, 파일 자체는 0600 이다.
        """
        opts = ["rw", "noexec", "nosuid", "nodev", f"size={SECRETS_TMPFS_SIZE}"]
        uid, gid = _parse_run_as(run_as)
        if uid is None:
            opts.append("mode=0777")
        else:
            opts.append("mode=0700")
            opts.append(f"uid={uid}")
            if gid is not None:
                opts.append(f"gid={gid}")
        return {SECRETS_MOUNT: ",".join(opts)}

    def build_spec(self, user, settings_path: Optional[str] = None) -> dict:
        """사용자 레코드 + config.spawn으로 ``containers.run`` kwargs 조립.

        Args:
            settings_path: 하위호환용(무시됨) — 사전 인가 settings.json 은 호스트에
                기록하지 않고 주입 페이로드로 넘어간다.
        """
        del settings_path
        cfg = self.config
        run_as = getattr(cfg.spawn, "run_as", "") or DEFAULT_RUN_AS
        return {
            "image": cfg.spawn.image,
            "name": self.container_name(user.username),
            "environment": self.build_env(user),
            "volumes": self.build_volumes(user),
            # 주입 시크릿이 사는 RAM 전용 디렉토리(비-root 소유 — run_as 파생).
            "tmpfs": self.secrets_tmpfs(run_as),
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

        existing = self._get_container(name)
        if existing is not None:
            if getattr(existing, "status", "") != "running":
                existing.start()
            self._safe_set_status(username, name, "running")
            log.info("worker 재사용: %s", name)  # 값은 로깅하지 않음
            return getattr(existing, "id", name)

        spec = self.build_spec(user)
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

    @staticmethod
    def _container_injection(container) -> Optional[dict]:
        """실행 중인 컨테이너에 **구워진** 주입 페이로드(판정 불가면 None).

        ``container.attrs["Config"]["Env"]`` 는 ``["K=V", ...]`` 형태다. 대역/구버전
        객체가 attrs 를 주지 않을 수도 있어 어떤 실패든 "판정 불가(None)"로 흡수한다
        (best-effort — 모르면 재생성하지 않는다).
        """
        try:
            env_list = container.attrs["Config"]["Env"]
        except Exception:  # noqa: BLE001 — attrs 부재/형태 불일치 = 판정 불가
            return None
        if not isinstance(env_list, list):
            return None  # 대역/구버전 객체 — 모르면 건드리지 않는다.
        pairs = dict(
            item.split("=", 1) for item in env_list if isinstance(item, str) and "=" in item
        )
        return {k: pairs.get(k, "") for k in (inject.ENV_CONFIG, inject.ENV_SECRETS)}

    def _injection_drifted(self, container, user) -> bool:
        """컨테이너에 구워진 주입 페이로드가 **지금의 config/시크릿과 다른가**.

        bind 마운트 시절엔 config·시크릿이 파일이라 워커가 재시작만 해도 최신 값을
        읽었다. 주입 방식에서는 페이로드가 컨테이너 생성 시점에 고정되므로, 값이
        바뀌었는데 컨테이너가 그대로면 워커가 **옛 자격증명으로 조용히** 돈다 —
        이번 작업이 없애려는 바로 그 실패 유형이다. 그래서 이미지 stale 과 **같은
        기준**으로 다루어, 유휴면 즉시 / 활성 잡이 있으면 드레인 후 재생성한다.

        판정 불가(attrs 없음 등)면 False — 모르면 건드리지 않는다(best-effort).
        """
        baked = self._container_injection(container)
        if baked is None:
            return False
        try:
            fresh = self.build_injection(user)
        except Exception:  # noqa: BLE001 — 조립 실패는 상위 per-user 격리로 넘긴다
            log.exception("주입 페이로드 조립 실패(드리프트 판정 생략): %s",
                          getattr(user, "username", ""))
            return False
        for key in (inject.ENV_CONFIG, inject.ENV_SECRETS):
            if baked.get(key, "") != fresh.get(key, ""):
                return True
        return False

    def _recreate_worker(self, user) -> str:
        """워커 컨테이너를 제거 후 현재 이미지·현재 주입 페이로드로 재spawn."""
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
                # 이미지가 같아도 **주입 페이로드**(config·시크릿)가 바뀌었으면 stale이다
                # — 안 그러면 워커가 옛 자격증명/설정으로 조용히 계속 돈다.
                if up_to_date and self._injection_drifted(container, user):
                    log.info("worker 주입 페이로드(config·시크릿) 변경 감지 → stale 판정: %s",
                             self.container_name(username))
                    up_to_date = False
                if up_to_date:
                    self._pending_recreate.discard(username)  # 최신 → 대기 해제
                    summary["skipped"].append(username)
                    continue
                # stale(다르거나 판정 불가/404) → 유휴면 즉시, 활성이면 드레인.
                if has_active_job(username):
                    self._pending_recreate.add(username)
                    summary["deferred"].append(username)
                    log.info("worker stale이나 활성 잡 있음 → 드레인 대기: %s",
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
