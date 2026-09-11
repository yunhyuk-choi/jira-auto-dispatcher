"""``.gitignore`` 계약 — **패턴 뒤 인라인 주석 금지** + 개인 산출물이 실제로 무시되는가.

왜 테스트가 필요한가(이 리포에서 실제로 난 사고):
    ``.gitignore`` 에서 ``#`` 는 **줄 첫 칸에서만** 주석이다. 패턴 뒤에 붙이면
    ``secrets/  # 시크릿`` 전체가 하나의 패턴이 되어 **아무것도 무시하지 않는다** —
    시크릿 **값** 디렉토리가 추적 가능한 상태로 남아 있었다. 사람이 눈으로 읽으면
    "주석이 달린 패턴"으로 보이기 때문에 리뷰로 잡히지 않는다. 그래서 기계가 본다.

여기서 강제하는 것:
    1. 패턴 줄에 인라인 ``#`` 이 없다(설명은 패턴 **위 줄**로 올린다).
    2. 조직 메타데이터·개인 동의 기록이 무시 대상에 실제로 들어 있다.

⚠️ 2번은 문자열 매칭이 아니라 **파일이 무시되는지**로 본다 — 패턴이 있어도 앵커·순서
때문에 안 먹을 수 있고, 이 리포에서 실제로 그런 함정이 있었다(``docs/`` 앵커 주석 참조).
"""

from __future__ import annotations

import os
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GITIGNORE = os.path.join(REPO_ROOT, ".gitignore")

#: 무시돼야 하는 개인·조직 산출물. "왜"를 함께 적는다 — 지우려는 사람이 근거를 보게.
MUST_BE_IGNORED = (
    ("config/config.yaml", "실토큰 참조·계정 id 가 든 로컬 설정"),
    ("setup-answers.json", "설치 마법사가 모은 답(조직 URL·프로젝트 키·계정)"),
    ("setup-consent.json", "동의 증서 — 사람 이름과 그 사람의 원문이 들어간다"),
    ("jira-discover.json", "조직 내부 메타데이터(accountId·커스텀필드·라벨 전량)"),
    ("jira-fields.json", "위와 같은 조회 결과의 다른 이름(CLI 예시가 쓴다)"),
    ("secrets/service/jira-token", "시크릿 **값** 파일"),
    ("state/registry.json", "등록 사용자(운영 데이터)"),
)


def _lines() -> list:
    with open(GITIGNORE, "r", encoding="utf-8") as fh:
        return fh.read().splitlines()


def test_no_pattern_carries_an_inline_comment():
    """패턴 뒤 ``#`` 은 주석이 아니라 패턴의 일부다 — 그 순간 그 줄은 무력해진다."""
    offenders = [
        line for line in _lines()
        if line.strip() and not line.lstrip().startswith("#") and "#" in line
    ]
    assert not offenders, (
        "패턴 뒤에 인라인 주석이 붙었습니다 — gitignore 에서 `#` 는 줄 첫 칸에서만 "
        "주석이라 이 줄들은 아무것도 무시하지 않습니다. 설명은 패턴 위 줄로 올리세요: "
        + repr(offenders)
    )


@pytest.mark.parametrize("path,why", MUST_BE_IGNORED, ids=[p for p, _ in MUST_BE_IGNORED])
def test_personal_artifacts_are_actually_ignored(path, why):
    """선언이 아니라 **git 의 판정**으로 확인한다(`git check-ignore`)."""
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "--no-index", path],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    if proc.returncode == 128:                      # git 이 없거나 레포가 아니다
        pytest.skip(f"git check-ignore 를 쓸 수 없습니다: {proc.stderr.strip()}")
    assert proc.returncode == 0, f"{path} 가 무시되지 않습니다 — {why}"
