"""스포너 — 사용자별 worker 컨테이너 동적 기동/중지/상태(중앙 전용).

역할:
    온보딩 시(또는 UI start 버튼) 사용자마다 worker 컨테이너를 Docker SDK로
    동적 생성/기동하고, stop/status로 수명을 관리한다. worker는 이 이미지와
    동일하되 ROLE=worker로 분기해 뜬다.

역할 소속: **central**.

구현 Phase: **Phase 6** (컨테이너/배포) — 골격 시그니처는 지금 확정.

컨테이너 스펙(config.spawn + 사용자 레코드에서 조립):
    image           spawn.image (예: jira-auto-dispatcher:latest, 단일 이미지)
    network         spawn.network (예: jad-net — central과 같은 사설망)
    name            jad-worker-<username> (레지스트리 container.name)
    env:
        ROLE=worker
        DISPATCH_USER=<username>
        CENTRAL_URL=<spawn.central_url>          # 예: http://central:8787
        CLAUDE_CODE_OAUTH_TOKEN=<주입>            # setup-token(값은 시크릿 참조에서)
        (+ JIRA/GITLAB 토큰은 worker가 잡 실행 직전 정체성 주입 — agent_runner)
    volumes:
        사용자 ~/.claude 영속(예: jad-claude-<username>:/home/app/.claude)
        + orchestrator/dlc-meta/dataspace_docs/workspace (run.* 경로)
    restart_policy  unless-stopped (상시 폴링)
    mem_limit       spawn.mem_limit (예: 4g)

⚠️ 보안(docker.sock 특권):
    central이 docker.sock에 접근해 컨테이너를 띄우는 것은 사실상 호스트 root
    권한과 동치다(특권 상승 표면). 완화책:
      - docker-socket-proxy(tecnativa 등)를 앞단에 두어 CONTAINERS/POST만 최소
        허용하고 central은 프록시 TCP 엔드포인트로만 접근(sock 직결 금지).
      - central 자체를 비-root 사용자로 실행, 사내망 한정.
    docker_host는 config.spawn.docker_host(로컬=unix:///var/run/docker.sock,
    프록시 사용 시 tcp://socket-proxy:2375)로 주입한다.
"""

from __future__ import annotations

from typing import Optional


class Spawner:
    """Docker SDK 기반 worker 컨테이너 라이프사이클(스텁)."""

    def __init__(self, config, registry) -> None:
        """의존성 주입(설정·레지스트리) + docker 클라이언트 준비.

        TODO(Phase 6): docker.DockerClient(base_url=config.spawn.docker_host).
        (프록시 사용 시 tcp:// 엔드포인트. sock 직결은 사내망/개발용 한정)
        """
        self.config = config
        self.registry = registry
        self._docker = None  # docker.DockerClient (Phase 6)

    def build_spec(self, user) -> dict:
        """사용자 레코드 + config.spawn으로 컨테이너 생성 스펙 조립.

        TODO(Phase 6): image/name/network/env/volumes/restart_policy/mem_limit
        구성. CLAUDE_CODE_OAUTH_TOKEN은 secrets_ref에서 값을 해석해 주입.
        """
        raise NotImplementedError("TODO(Phase 6): build_spec")

    def start(self, username: str) -> str:
        """사용자 worker 컨테이너 생성/기동 후 상태를 레지스트리에 반영.

        TODO(Phase 6): 기존 컨테이너 있으면 재사용/재기동, 없으면 run(detach).
        registry.set_container(username, name, "running"). 컨테이너ID 반환.
        """
        raise NotImplementedError("TODO(Phase 6): start")

    def stop(self, username: str) -> None:
        """사용자 worker 컨테이너 중지(레지스트리 상태 갱신).

        TODO(Phase 6): container.stop() → registry.set_container(.., "stopped").
        """
        raise NotImplementedError("TODO(Phase 6): stop")

    def status(self, username: str) -> Optional[str]:
        """사용자 worker 컨테이너 현재 상태 조회(running/stopped/absent 등).

        TODO(Phase 6): docker inspect → 상태 문자열. 없으면 "absent".
        """
        raise NotImplementedError("TODO(Phase 6): status")

    def remove(self, username: str) -> None:
        """사용자 worker 컨테이너 제거(사용자 삭제/재프로비저닝 시).

        TODO(Phase 6): container.remove(force=True) + 상태 갱신.
        """
        raise NotImplementedError("TODO(Phase 6): remove")
