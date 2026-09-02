"""프로젝트 스킬 생성기(app/setup_skill.py) 단위테스트.

이 테스트가 고정하는 계약:
    - **목록은 코드가 아니라 템플릿 디렉토리가 정한다** — 템플릿 파일을 하나 더 넣으면
      코드 수정 없이 설치 대상이 하나 더 된다(이름은 frontmatter 에서 읽는다).
    - **멱등** — 같은 내용이면 아무것도 쓰지 않고, 사용자가 손댄 파일은 ``--force``
      없이는 **덮어쓰지 않는다**(조용히 날리지 않는다).
    - **생성 실패는 설치를 막지 않는다** — 권한 없음·읽기전용 FS 여도 예외가 아니라
      경고로 끝나고, 마법사는 그 실패와 무관하게 자기 종료코드를 낸다
      (짝 프레임워크의 EX-15 degraded 사상).
    - 산출물은 UTF-8(BOM 없음)·LF (POLICY-ENCODING).
    - 스킬 본문은 리포가 추적하지 않는 **개인 파일**이다 — `.claude/` 아래로만 간다.
"""

from __future__ import annotations

import json
import os

import pytest

from app import setup as CLI
from app import setup_skill as K

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# 픽스처 — 가짜 템플릿 루트(리포 실물에 의존하지 않는다)
# ---------------------------------------------------------------------------


def write_template(root, name, *, body="본문.\n", filename=None, description="설명"):
    """템플릿 하나를 만든다. ``filename`` 을 주면 **한 파일짜리** 배치로."""
    if filename:
        path = os.path.join(str(root), filename)
    else:
        os.makedirs(os.path.join(str(root), name), exist_ok=True)
        path = os.path.join(str(root), name, K.TEMPLATE_BASENAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path


@pytest.fixture
def root(tmp_path):
    """템플릿 두 개(디렉토리형 · 한 파일형)를 가진 루트."""
    base = tmp_path / "skill-templates"
    base.mkdir()
    write_template(base, "alpha-skill")
    write_template(base, "beta-skill", filename="beta.template.md")
    return str(base)


@pytest.fixture
def project(tmp_path):
    """생성 대상 배포 디렉토리."""
    path = tmp_path / "clone"
    path.mkdir()
    return str(path)


def read(path) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 목록은 코드가 아니라 디렉토리가 정한다
# ---------------------------------------------------------------------------


def test_templates_are_discovered_not_hardcoded(root):
    """스캔 결과가 곧 설치 대상 — 두 배치(디렉토리형·한 파일형)를 모두 줍는다."""
    names = [t.name for t in K.discover_templates(root=root)]
    assert names == ["alpha-skill", "beta-skill"]


def test_adding_a_template_adds_a_skill_without_touching_code(root, project):
    """⚠️ 이 테스트가 이 모듈의 존재 이유다 — 파일 하나 추가 = 설치 대상 하나 추가."""
    before = K.install_skills(project, root=root)
    assert [r.name for r in before.results] == ["alpha-skill", "beta-skill"]

    write_template(root, "gamma-skill")          # 코드는 한 줄도 고치지 않는다
    after = K.install_skills(project, root=root)

    assert [r.name for r in after.results] == ["alpha-skill", "beta-skill",
                                               "gamma-skill"]
    assert os.path.exists(os.path.join(project, ".claude", "skills",
                                       "gamma-skill", "SKILL.md"))


def test_skill_name_comes_from_frontmatter_not_from_the_filename(root, project):
    """이름의 원천은 frontmatter — 파일명과 달라도 frontmatter 가 이긴다."""
    write_template(root, "renamed-skill", filename="anything-else.template.md")
    report = K.install_skills(project, root=root, only=["renamed-skill"])

    assert report.ok
    assert report.results[0].path.endswith(
        os.path.join(".claude", "skills", "renamed-skill", "SKILL.md"))


def test_template_without_frontmatter_falls_back_to_its_path_name(tmp_path, project):
    """frontmatter 가 없어도 대상에서 빠지지 않는다(경로에서 이름을 유도)."""
    base = tmp_path / "skill-templates"
    (base / "no-meta").mkdir(parents=True)
    (base / "no-meta" / K.TEMPLATE_BASENAME).write_text("본문뿐\n", encoding="utf-8")

    found = K.discover_templates(root=str(base))
    assert [(t.name, t.name_source) for t in found] == [("no-meta", "path")]


def test_only_selects_a_subset_and_unknown_names_are_a_usage_error(root, project):
    report = K.install_skills(project, root=root, only=["beta-skill"])
    assert [r.name for r in report.results] == ["beta-skill"]
    assert not os.path.exists(os.path.join(project, ".claude", "skills",
                                           "alpha-skill", "SKILL.md"))

    with pytest.raises(ValueError) as exc:
        K.install_skills(project, root=root, only=["없는스킬"])
    assert "없는스킬" in str(exc.value)


# ---------------------------------------------------------------------------
# 생성 — 내용·인코딩·위치
# ---------------------------------------------------------------------------


def test_generated_file_is_a_verbatim_copy_in_the_personal_area(root, project):
    report = K.install_skills(project, root=root)
    assert report.ok
    target = os.path.join(project, ".claude", "skills", "alpha-skill", "SKILL.md")

    assert report.results[0].status == K.STATUS_CREATED
    assert read(target) == read(os.path.join(root, "alpha-skill",
                                             K.TEMPLATE_BASENAME))
    # 개인 영역 밖으로는 아무것도 쓰지 않는다.
    assert sorted(os.listdir(project)) == [".claude"]


def test_generated_file_is_utf8_lf_without_bom(root, project):
    """POLICY-ENCODING — CRLF 템플릿을 넣어도 산출물은 LF 다."""
    path = os.path.join(root, "alpha-skill", K.TEMPLATE_BASENAME)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write("---\r\nname: alpha-skill\r\n---\r\n\r\n한글 본문\r\n")

    K.install_skills(project, root=root, only=["alpha-skill"])
    raw = open(os.path.join(project, ".claude", "skills", "alpha-skill",
                            "SKILL.md"), "rb").read()

    assert not raw.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" not in raw
    assert "한글 본문" in raw.decode("utf-8")


def test_report_tells_how_to_invoke_what_it_installed(root, project):
    text = K.install_skills(project, root=root).format_text()
    assert "/alpha-skill" in text and "/beta-skill" in text
    assert "gitignore" in text                      # 개인 파일이라는 사실을 말한다
    assert "python -m app.setup wizard" in text     # 1차 진입점을 계속 가리킨다


# ---------------------------------------------------------------------------
# 멱등 — 두 번 돌려도 같고, 사용자가 손댄 파일은 지킨다
# ---------------------------------------------------------------------------


def test_running_twice_writes_nothing_the_second_time(root, project):
    first = K.install_skills(project, root=root)
    target = os.path.join(project, ".claude", "skills", "alpha-skill", "SKILL.md")
    stamp = os.stat(target).st_mtime_ns

    second = K.install_skills(project, root=root)

    assert first.ok and second.ok
    assert [r.status for r in second.results] == [K.STATUS_UNCHANGED] * 2
    assert os.stat(target).st_mtime_ns == stamp     # 파일을 건드리지도 않았다


def test_user_edited_skill_is_not_silently_overwritten(root, project):
    K.install_skills(project, root=root, only=["alpha-skill"])
    target = os.path.join(project, ".claude", "skills", "alpha-skill", "SKILL.md")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write("내가 직접 고친 내용\n")

    report = K.install_skills(project, root=root, only=["alpha-skill"])

    assert not report.ok
    assert report.results[0].status == K.STATUS_KEPT
    assert read(target) == "내가 직접 고친 내용\n"     # 그대로 살아 있다
    assert "--force" in report.results[0].detail


def test_force_overwrites_but_backs_up_first(root, project):
    K.install_skills(project, root=root, only=["alpha-skill"])
    target = os.path.join(project, ".claude", "skills", "alpha-skill", "SKILL.md")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write("내가 직접 고친 내용\n")

    report = K.install_skills(project, root=root, only=["alpha-skill"], force=True)

    assert report.ok
    assert report.results[0].status == K.STATUS_UPDATED
    assert read(target) != "내가 직접 고친 내용\n"
    assert read(report.results[0].backup) == "내가 직접 고친 내용\n"


# ---------------------------------------------------------------------------
# 실패는 예외가 아니라 경고다 — 설치를 막지 않는다
# ---------------------------------------------------------------------------


def test_write_failure_is_reported_as_a_value_not_raised(root, project, monkeypatch):
    """읽기전용 FS·권한 없음 대역 — 예외가 밖으로 나가면 설치가 죽는다."""
    def boom(*_args, **_kwargs):
        raise PermissionError(13, "권한이 없습니다")

    monkeypatch.setattr(K.os, "makedirs", boom)
    report = K.install_skills(project, root=root)

    assert not report.ok
    assert [r.status for r in report.results] == [K.STATUS_FAILED] * 2
    assert "설치는 그대로" in report.results[0].detail


def test_missing_template_root_is_not_an_error_path_that_raises(project, tmp_path):
    report = K.install_skills(project, root=str(tmp_path / "없는디렉토리"))
    assert report.templates == [] and report.results == []
    assert "템플릿" in report.format_text()


def test_one_broken_template_does_not_stop_the_others(root, project, monkeypatch):
    """하나가 깨져도 나머지는 설치된다(전부 아니면 전무가 아니다)."""
    os.remove(os.path.join(root, "beta.template.md"))
    found = K.discover_templates(root=root)
    found.append(K.SkillTemplate(name="ghost-skill",
                                 template_path=os.path.join(root, "ghost.template.md")))

    results = [K.install_template(t, project) for t in found]

    assert results[0].ok and results[0].status == K.STATUS_CREATED
    assert not results[1].ok and results[1].status == K.STATUS_FAILED


def test_best_effort_never_raises_even_if_the_scan_explodes(project, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("스캔이 터졌다")

    monkeypatch.setattr(K, "install_skills", boom)
    report = K.install_best_effort(project)

    assert not report.ok
    assert "설치는 계속합니다" in report.results[0].detail


def test_wizard_finishes_even_when_skill_generation_fails(tmp_path, monkeypatch):
    """⚠️ 핵심 계약 — 스킬 생성 실패가 **설치 종료코드에 영향을 주지 않는다.**"""
    from app import setup_wizard as W

    from tests.test_setup_wizard import run

    def boom(*_args, **_kwargs):
        raise OSError(30, "읽기 전용 파일 시스템")

    monkeypatch.setattr(W.setup_skill, "install_best_effort", boom)
    code, _responder, out = run(tmp_path)

    assert code == W.EXIT_OK, out.text           # 설치는 그대로 성공이다
    assert "스킬" in out.text                     # 무슨 일이 있었는지는 말한다
    assert os.path.exists(str(tmp_path / "config.yaml"))


def test_wizard_installs_the_skill_when_it_can(tmp_path):
    from app import setup_wizard as W

    from tests.test_setup_wizard import run

    code, _responder, out = run(tmp_path)

    assert code == W.EXIT_OK
    assert os.path.exists(str(tmp_path / ".claude" / "skills"
                              / "install-jira-auto-dispatcher" / "SKILL.md"))
    assert "/install-jira-auto-dispatcher" in out.text


# ---------------------------------------------------------------------------
# CLI 계약 — 종료코드
# ---------------------------------------------------------------------------


def _args(project, root, *rest):
    return ["skill", "--project-dir", project, "--templates", root, *rest]


def test_cli_installs_and_reports_json(root, project, capsys):
    assert CLI.main(_args(project, root, "--json")) == CLI.EXIT_OK
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    assert [s["name"] for s in payload["skills"]] == ["alpha-skill", "beta-skill"]


def test_cli_list_does_not_write_anything(root, project, capsys):
    assert CLI.main(_args(project, root, "--list")) == CLI.EXIT_OK
    assert "alpha-skill" in capsys.readouterr().out
    assert not os.path.exists(os.path.join(project, ".claude"))


def test_cli_returns_gate_failure_when_a_user_file_would_be_clobbered(root, project):
    CLI.main(_args(project, root))
    target = os.path.join(project, ".claude", "skills", "alpha-skill", "SKILL.md")
    with open(target, "w", encoding="utf-8", newline="") as fh:
        fh.write("손댄 내용\n")

    assert CLI.main(_args(project, root)) == CLI.EXIT_GATE_FAILED
    assert read(target) == "손댄 내용\n"
    assert CLI.main(_args(project, root, "--force")) == CLI.EXIT_OK


def test_cli_unknown_skill_name_is_a_usage_error(root, project):
    with pytest.raises(SystemExit) as exc:
        CLI.main(_args(project, root, "--only", "없는스킬"))
    assert exc.value.code == CLI.EXIT_USAGE


def test_repo_ships_a_template_for_the_install_skill():
    """리포 실물 계약 — 설치 스킬은 **추적되는 템플릿**으로만 존재한다."""
    names = [t.name for t in K.discover_templates(REPO_ROOT)]
    assert "install-jira-auto-dispatcher" in names
    # `.claude/` 아래 산출물은 리포가 추적하지 않는다(gitignore) — 여기서는 템플릿의
    # 존재만 계약으로 고정한다(생성물 유무는 머신마다 다르다).
    assert os.path.isfile(os.path.join(REPO_ROOT, K.TEMPLATE_DIRNAME,
                                       "install-jira-auto-dispatcher",
                                       K.TEMPLATE_BASENAME))
