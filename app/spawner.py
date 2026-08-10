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
        JIRA_TOKEN_FILE / GITLAB_TOKEN_FILE       # per-user 토큰 "파일경로"(값 아님)
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
      - central 자체를 비-root 사용자로 실행, 사내망 한정.
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

# 사전 인가 기본 레벨.
DEFAULT_PERMISSION_LEVEL = "bypass"


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

        시크릿 값(CLAUDE_CODE_OAUTH_TOKEN·WORKER_SHARED_SECRET)은 read_secret/config로만
        읽고, Jira/GitLab 토큰은 값이 아니라 **파일경로**(마운트 경로) 포인터로 넘긴다.
        이 dict는 절대 로깅하지 않는다.
        """
        cfg = self.config
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        env: dict = {
            "ROLE": "worker",
            "DISPATCH_USER": user.username,
            "CENTRAL_URL": cfg.spawn.central_url,
            "SECRETS_DIR": SECRETS_MOUNT,
        }

        # dispatch HTTP 인증 공유 시크릿(값). 미설정이면 생략(사내망 신뢰).
        if getattr(cfg, "worker_shared_secret", ""):
            env["WORKER_SHARED_SECRET"] = cfg.worker_shared_secret

        secrets_ref = getattr(user, "secrets_ref", None)

        # 사용자 Claude setup-token(값). read_secret로만.
        claude_ref = getattr(secrets_ref, "claude_oauth_token", "") if secrets_ref else ""
        if claude_ref:
            val = read_secret(base_dir, claude_ref)
            if val:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = val

        # Jira/GitLab 토큰: 값이 아니라 파일경로 포인터(컨테이너 내부 마운트 경로).
        jira_ref = getattr(secrets_ref, "jira_token", "") if secrets_ref else ""
        if jira_ref:
            env["JIRA_TOKEN_FILE"] = self._in_container_secret(jira_ref)
        gitlab_ref = getattr(secrets_ref, "gitlab_token", "") if secrets_ref else ""
        if gitlab_ref:
            env["GITLAB_TOKEN_FILE"] = self._in_container_secret(gitlab_ref)

        if getattr(user, "jira_email", ""):
            env["JIRA_EMAIL"] = user.jira_email

        return env

    def build_volumes(self, user, settings_path: str) -> dict:
        """컨테이너 volumes dict 조립(영속 .claude + 사전 인가 settings + per-user 시크릿 ro)."""
        cfg = self.config
        username = user.username
        base_dir = getattr(getattr(cfg, "secrets", None), "base_dir", "") or ""
        volumes: dict = {
            # 사용자 ~/.claude 영속(인증/세션).
            self.volume_name(username): {"bind": CLAUDE_CONFIG_DIR, "mode": "rw"},
            # 사전 인가 settings.json (read-only — 컨테이너가 못 바꾼다).
            settings_path: {"bind": SETTINGS_PATH_IN_CONTAINER, "mode": "ro"},
        }
        # per-user 시크릿 디렉토리(값) read-only 마운트. 다른 사용자 시크릿은 안 보인다.
        user_secret_dir = os.path.join(base_dir, username)
        volumes[user_secret_dir] = {
            "bind": posixpath.join(SECRETS_MOUNT, username),
            "mode": "ro",
        }
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
