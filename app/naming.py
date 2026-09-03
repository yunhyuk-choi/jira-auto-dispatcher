"""docker 리소스 이름 조립 — **인스턴스 접두어의 단일 원천**.

왜 이 모듈이 있는가:
    한 호스트에 이 시스템을 **두 벌 이상** 띄우려는 팀이 있다(평가·스테이징·프로덕션).
    그런데 compose 가 ``container_name: jad-central``·``jad-socket-proxy`` 로 이름을
    고정하고 네트워크·공유 볼륨도 ``name:`` 으로 고정해서, 두 번째 스택은
    ``Conflict. The container name "/jad-socket-proxy" is already in use`` 로 아예 뜨지
    못한다(실측). 워커 컨테이너(``jad-worker-<user>``)와 per-user 볼륨(``jad-<user>``)도
    같은 문제를 겪는다 — 두 인스턴스에 같은 사람이 등록되면 이름이 겹친다.

    이름을 고정한 데는 이유가 있었다(아래 "계약" 참조). 그래서 고정을 푸는 대신 **인스턴스
    이름 하나**(``deploy.instance``, 기본 ``jad``)를 접두어로 뽑아, 한 값만 바꾸면 스택
    전체가 통째로 다른 이름 공간으로 옮겨 가게 한다. 기본값에서는 조립 결과가 예전 문자열과
    **한 글자도 다르지 않다**(기존 배포 무변경).

무엇이 **계약**이고 무엇이 **관례**였나 (코드를 읽어 가른 결과):

    계약 A — *두 프로세스가 같은 이름을 말해야 성립하는 것*. 이름 자체는 자유지만 양쪽이
    **같아야** 한다. 그래서 접두어를 붙일 때 반드시 함께 움직여야 한다.
        - 네트워크(``spawn.network`` ↔ compose ``networks.*.name``): spawner 가 워커를
          이 **이름**으로 호스트 데몬에 붙인다. 어긋나면 워커 스폰이 실패한다.
        - 공유 워크스페이스 볼륨(``deploy.workspace_volume`` ↔ compose ``volumes.*.name``):
          central 과 모든 워커가 **같은 named 볼륨**을 마운트해 레포 한 벌을 공유한다
          (설계 §4). 어긋나면 조용히 서로 다른 클론을 보게 된다.
        - 이미지 태그(``spawn.image`` ↔ compose ``image:``): spawner 가 이 태그로 워커를
          띄운다.
        - 워커 컨테이너 이름: **spawner 가 만들고**(:meth:`app.spawner.Spawner.container_name`)
          **레지스트리에 적히고**(:mod:`app.onboarding`) **프랙탈 PUSH 가 docker exec 로
          때린다**(:func:`app.central_session.build_worker_exec_command`). 세 곳이 같은
          문자열을 조립해야 한다 — 그래서 여기가 그 단일 원천이다.

    계약 B — *경로·호스트명*. 이름 공간과 무관하며 **건드리지 않는다**.
        - ``tcp://socket-proxy:2375``: 이건 컨테이너 이름이 아니라 **compose 서비스 키**로
          도는 네트워크 DNS 다(compose 는 서비스 이름을 별칭으로 등록한다). 그래서
          ``container_name`` 에 접두어를 붙여도 이 주소는 그대로 유효하다 — 서비스 키
          (``central``·``socket-proxy``)는 바꾸지 않는다.
        - ``/home/app/.claude``·``/app``·``/run/secrets``: Dockerfile 과 짝인 컨테이너
          **내부** 경로. 인스턴스와 무관.

    관례 — *아무도 이 이름으로 무엇을 찾지 않는다*. 자유롭게 접두어를 붙일 수 있다.
        - ``container_name: jad-central``·``jad-socket-proxy``: 사람이 ``docker logs`` 로
          부를 때 쓰는 이름일 뿐이다. 워커는 central 에 HTTP 로 말하지 않고(프랙탈 PUSH 는
          central → 워커 방향), central 에 붙는 유일한 통로는 발행 포트(8787)다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

from typing import Any

#: 인스턴스 이름 기본값. **이 값을 바꾸면 기존 배포가 깨진다** — 기본 스택의 네트워크·
#: 볼륨·컨테이너 이름이 전부 여기서 파생되기 때문이다(``jad-net``·``jad-workspace``·
#: ``jad-worker-<user>``·``jad-<user>``).
DEFAULT_INSTANCE = "jad"

#: 이미지 리포지토리 이름(태그 앞부분). 태그는 인스턴스에서 파생한다
#: (:func:`default_image`) — 리포 이름 자체는 인스턴스와 무관해 고정한다.
DEFAULT_IMAGE_REPO = "jira-auto-dispatcher"


def instance_name(config: Any) -> str:
    """이 배포의 인스턴스 이름(``deploy.instance``, 기본 :data:`DEFAULT_INSTANCE`).

    ``config`` 가 ``deploy`` 를 갖지 않아도 된다(테스트 대역·워커 역할의 축소 config).
    그 경우 기본값을 쓴다 — 기본값에서는 조립 결과가 예전 하드코딩 문자열과 같다.
    """
    value = getattr(getattr(config, "deploy", None), "instance", "") or ""
    return str(value).strip() or DEFAULT_INSTANCE


def worker_container_prefix(config: Any) -> str:
    """워커 컨테이너 이름 접두어 — ``<instance>-worker-`` (기본 ``jad-worker-``)."""
    return f"{instance_name(config)}-worker-"


def worker_container_name(config: Any, username: str) -> str:
    """워커 컨테이너 이름 — ``<instance>-worker-<username>``.

    ⚠️ ``username`` 을 여기서 검증하지 않는다. 검증은 이름을 **소비하는** 자리의 몫이다
    (:meth:`app.spawner.Spawner._checked_username`) — 이 모듈은 순수 문자열 조립이라
    레지스트리에 의존하지 않는다(순환 임포트 회피).
    """
    return f"{worker_container_prefix(config)}{username}"


def user_volume_prefix(config: Any) -> str:
    """per-user 볼륨 이름 접두어 — ``<instance>-`` (기본 ``jad-``)."""
    return f"{instance_name(config)}-"


def user_volume_name(config: Any, username: str) -> str:
    """per-user ``~/.claude`` 영속 볼륨 이름 — ``<instance>-<username>``."""
    return f"{user_volume_prefix(config)}{username}"


def default_network_name(instance: str = DEFAULT_INSTANCE) -> str:
    """인스턴스 이름에서 파생한 네트워크 기본 이름 — ``<instance>-net``.

    ``spawn.network`` 를 **명시하지 않았을 때의 기본값**을 만드는 데 쓴다(설정 로더).
    명시한 값은 언제나 이긴다 — 이름을 직접 적어 두고 쓰던 배포를 흔들지 않는다.
    """
    return f"{(instance or DEFAULT_INSTANCE).strip() or DEFAULT_INSTANCE}-net"


def default_workspace_volume(instance: str = DEFAULT_INSTANCE) -> str:
    """인스턴스 이름에서 파생한 공유 워크스페이스 볼륨 기본 이름 — ``<instance>-workspace``."""
    return f"{(instance or DEFAULT_INSTANCE).strip() or DEFAULT_INSTANCE}-workspace"


def default_image(instance: str = DEFAULT_INSTANCE) -> str:
    """인스턴스 이름에서 파생한 워커/central 이미지 이름 — ``jira-auto-dispatcher:<tag>``.

    **왜 이미지도 인스턴스 축에 묶는가**: compose 가 이미지 이름을 고정하고 있으면
    (``image: jira-auto-dispatcher:latest``) 두 번째 인스턴스를 빌드하는 순간 **돌고 있는
    첫 인스턴스의 워커 이미지가 통째로 바뀐다.** 태그 하나가 두 스택의 코드를 공유해
    버리는 것이라, 다중 인스턴스의 격리가 이름 공간에서만 성립하고 **실행 코드에서는
    성립하지 않는다**(실측 결함 — 먼저 뜬 인스턴스가 override 파일로 우회하고 있었다).

    태그 규칙(:data:`DEFAULT_INSTANCE` 만 특례):
        - ``jad``(기본 인스턴스) → ``latest`` — **기존 배포 무변경**. 지금 돌고 있는
          모든 단일 인스턴스 배포의 이미지 이름이 한 글자도 달라지지 않는다.
        - 그 외 → 인스턴스 이름 그대로(``jad-stg`` → ``jira-auto-dispatcher:jad-stg``).

    compose 쪽 짝은 ``${JAD_IMAGE:-jira-auto-dispatcher:${JAD_INSTANCE:-latest}}`` 이며
    (``docker-compose.yml`` 상단 앵커), 계약 A 대로 **두 값이 같아야** 한다 —
    ``tests/test_compose_contract.py`` 가 그 드리프트를 잡는다. 그리고 compose 는 그
    결과 문자열을 central 에 env ``JAD_IMAGE`` 로도 넣어, config 에 옛 값이 남아 있어도
    **실재하는 이름이 이긴다**(:func:`app.config._apply_image_override`).
    """
    tag = (instance or DEFAULT_INSTANCE).strip() or DEFAULT_INSTANCE
    if tag == DEFAULT_INSTANCE:
        tag = "latest"
    return f"{DEFAULT_IMAGE_REPO}:{tag}"
