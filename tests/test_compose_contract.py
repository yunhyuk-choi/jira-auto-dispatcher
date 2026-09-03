"""계약 정합 테스트 — ``docker-compose.yml`` ↔ :mod:`app.naming` 드리프트 방지.

무엇을 지키나:
    compose 가 **실제로 만드는 이름**(컨테이너·네트워크·볼륨·**이미지 태그**)과, central 이
    설정에서 **파생해 부르는 이름**이 같아야 한다(app/naming.py 「계약 A」). 한쪽만 고치면
    조용히 어긋나고, 그 증상은 한참 뒤에 엉뚱한 자리에서 나타난다:

    - 이미지가 어긋나면 spawner 가 **없는 태그**로 워커를 띄우거나(즉시 실패),
      더 나쁘게는 **남의 인스턴스 이미지**로 띄운다(조용히 다른 코드가 돈다).
    - 네트워크·워크스페이스 볼륨이 어긋나면 워커가 남의 인스턴스 사설망·레포 클론에 붙는다.

    실측 결함(리허설): compose 가 ``image: jira-auto-dispatcher:latest`` 를 변수 없이
    하드코딩해, 두 번째 인스턴스를 빌드하는 순간 **돌고 있는 첫 인스턴스의 워커 이미지가
    갈아치워졌다.** 그래서 이미지도 인스턴스 축에 묶고, 그 정합을 여기서 잠근다.

무엇을 안 하나:
    docker 를 부르지 않는다(라이브 스택 무관 — 파일만 읽는다). compose 의 보간 규칙
    ``${VAR:-default}`` 은 아래 :func:`interpolate` 가 그대로 흉내 낸다(중첩 포함 —
    ``docker compose config`` 로 동작을 실측해 맞췄다).

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import os
import re

import pytest
import yaml

from app import naming

COMPOSE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "docker-compose.yml")

#: 가장 안쪽 ``${NAME:-default}`` (default 안에 ``{``/``}`` 가 없는 것) — 안쪽부터 접는다.
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^{}]*))?\}")


def interpolate(text: str, env: dict) -> str:
    """compose 의 ``${VAR}``·``${VAR:-default}`` 보간을 흉내 낸다(중첩 지원).

    안쪽 표현식부터 치환하므로 ``${A:-x:${B:-y}}`` 같은 중첩도 compose 와 같은 결과가
    된다. 미정의 변수는 기본값(없으면 빈 문자열)으로 접힌다 — compose 와 같다.
    """
    prev = None
    out = str(text)
    while prev != out:
        prev = out
        out = _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), out)
    return out


@pytest.fixture(scope="module")
def compose() -> dict:
    """compose 파일을 YAML 로 읽는다(앵커는 파싱 시점에 펼쳐진다 — 그래서 같은 문자열)."""
    with open(COMPOSE_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _central(compose: dict) -> dict:
    return compose["services"]["central"]


# --- 이미지: 두 자리가 같은 앵커여야 하고, 인스턴스에서 파생돼야 한다 ----------------


def test_image_and_env_come_from_the_same_expression(compose):
    """``image:`` 와 central 의 env ``JAD_IMAGE`` 는 **같은 문자열**이어야 한다.

    central 은 env 쪽을 정본으로 삼아 spawner 에 넘긴다
    (:func:`app.config._apply_image_override`). 두 자리가 갈리면 "compose 가 빌드한 태그"와
    "워커를 띄우는 태그"가 달라진다 — 그래서 파일 안에서 앵커 하나로 묶는다.
    """
    central = _central(compose)
    assert central["image"] == central["environment"]["JAD_IMAGE"]


@pytest.mark.parametrize("env,instance", [
    ({}, naming.DEFAULT_INSTANCE),                       # .env 없음 = 기존 배포
    ({"JAD_INSTANCE": "jad-stg"}, "jad-stg"),
    ({"JAD_INSTANCE": "jad-eval2"}, "jad-eval2"),
])
def test_compose_image_matches_naming_default_image(compose, env, instance):
    """compose 가 쓰는 이미지 == :func:`app.naming.default_image` (계약 A)."""
    rendered = interpolate(_central(compose)["image"], env)
    assert rendered == naming.default_image(instance)


def test_default_instance_image_is_unchanged_from_before(compose):
    """기본 인스턴스의 이미지 이름은 **예전과 한 글자도 다르지 않다**(기존 배포 무변경)."""
    assert interpolate(_central(compose)["image"], {}) == "jira-auto-dispatcher:latest"


def test_two_instances_never_share_an_image_tag(compose):
    """다중 인스턴스의 핵심 성질 — 인스턴스가 다르면 **이미지 태그도 다르다**.

    이게 깨지면 한쪽에서 ``docker compose build`` 하는 순간 다른 쪽 워커의 실행 코드가
    바뀐다(리허설에서 실제로 밟은 결함).
    """
    expr = _central(compose)["image"]
    first = interpolate(expr, {})                                  # 기본 인스턴스
    second = interpolate(expr, {"JAD_INSTANCE": "jad-stg"})
    third = interpolate(expr, {"JAD_INSTANCE": "jad-eval2"})
    assert len({first, second, third}) == 3


def test_explicit_jad_image_wins(compose):
    """``JAD_IMAGE`` 를 직접 주면 그게 이긴다(사내 레지스트리 이미지 등)."""
    rendered = interpolate(_central(compose)["image"],
                           {"JAD_IMAGE": "registry.example.com/jad:1.2.3",
                            "JAD_INSTANCE": "jad-stg"})
    assert rendered == "registry.example.com/jad:1.2.3"


# --- 나머지 이름들도 같은 축에서 파생되는지 ------------------------------------------


@pytest.mark.parametrize("env,instance", [
    ({}, naming.DEFAULT_INSTANCE),
    ({"JAD_INSTANCE": "jad-stg"}, "jad-stg"),
])
def test_network_and_workspace_volume_match_naming(compose, env, instance):
    """네트워크·공유 워크스페이스 볼륨 이름 == naming 의 파생값(계약 A)."""
    assert interpolate(compose["networks"]["jad-net"]["name"], env) == \
        naming.default_network_name(instance)
    assert interpolate(compose["volumes"]["jad-workspace"]["name"], env) == \
        naming.default_workspace_volume(instance)


@pytest.mark.parametrize("env,instance", [
    ({}, naming.DEFAULT_INSTANCE),
    ({"JAD_INSTANCE": "jad-stg"}, "jad-stg"),
])
def test_container_names_carry_the_instance_prefix(compose, env, instance):
    """컨테이너 이름 접두어(관례 — 사람이 ``docker logs`` 로 부르는 이름)."""
    services = compose["services"]
    assert interpolate(services["central"]["container_name"], env) == f"{instance}-central"
    assert interpolate(services["socket-proxy"]["container_name"], env) == \
        f"{instance}-socket-proxy"


# --- 스키마·설정 로더와의 정합 --------------------------------------------------------


@pytest.mark.parametrize("instance", ["jad", "jad-stg", "jad-eval2"])
def test_schema_instance_derivation_matches_compose(compose, instance):
    """설치 스키마의 인스턴스 파생값 == compose 가 실제로 만드는 이름.

    렌더된 ``config.yaml`` 이 이 값을 갖고, central 이 그걸로 워커를 띄운다 — 즉 이
    단언이 "문서만 따라 두 번째 인스턴스를 세워도 첫 인스턴스를 밟지 않는다"의 근거다.
    """
    from app import setup_schema as S

    derived = S.instance_derived_values(instance)
    env = {} if instance == naming.DEFAULT_INSTANCE else {"JAD_INSTANCE": instance}
    assert derived["spawn.image"] == interpolate(_central(compose)["image"], env)
    assert derived["spawn.network"] == interpolate(
        compose["networks"]["jad-net"]["name"], env)
    assert derived["deploy.workspace_volume"] == interpolate(
        compose["volumes"]["jad-workspace"]["name"], env)
