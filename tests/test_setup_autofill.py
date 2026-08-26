"""설치 자동 채움(app/setup_autofill.py) 단위테스트.

이 모듈이 지켜야 하는 약속:
    - dlc-meta 원격 URL 은 **클론에서 읽어** 답변에 들어간다(사람이 옮겨 적지 않는다).
    - 그 URL 은 **설정에 적어도 되는 형태**여야 한다 — 토큰이 박힌 URL 이 config.yaml 로
      새면 이 리포의 제1 규율("값이 아니라 참조")이 깨진다.

⚠️ git 은 실제로 부르지 않는다 — ``runner`` 대역만 쓴다(CI: ubuntu, 네트워크 없음).
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from app import setup_autofill as A


def _runner(url, *, rc=0):
    """``git remote get-url`` 대역 — 항상 같은 URL 을 돌려준다."""
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=rc, stdout=url + "\n", stderr="")

    run.calls = calls
    return run


def _clone(tmp_path, name="dlc-meta"):
    """git 작업 트리처럼 보이는 디렉토리를 만든다(.git 존재만 본다)."""
    path = tmp_path / name
    (path / ".git").mkdir(parents=True)
    return str(path)


# ---------------------------------------------------------------------------
# URL 정규화 — 설정에 적어도 되는 형태인가
# ---------------------------------------------------------------------------


def test_plain_https_url_passes_through_untouched():
    url, notes = A.sanitize_repo_url("https://gitlab.example.com/g/dlc-meta.git")
    assert url == "https://gitlab.example.com/g/dlc-meta.git"
    assert notes == []


def test_token_in_remote_url_is_stripped():
    """⚠️ 가장 중요한 케이스 — 로컬 클론의 origin 에 토큰이 박혀 있을 수 있다."""
    url, notes = A.sanitize_repo_url(
        "https://oauth2:glpat-SECRETVALUE@gitlab.example.com/g/dlc-meta.git")
    assert url == "https://gitlab.example.com/g/dlc-meta.git"
    assert "glpat-SECRETVALUE" not in url
    assert notes and all("glpat-SECRETVALUE" not in n for n in notes)


def test_scp_style_ssh_remote_becomes_https_with_a_note():
    """central 은 SSH 키가 아니라 PAT 를 http(s) URL 에 실어 인증한다 — 조용히 바꾸지 않는다."""
    url, notes = A.sanitize_repo_url("git@gitlab.example.com:g/dlc-meta.git")
    assert url == "https://gitlab.example.com/g/dlc-meta.git"
    assert len(notes) == 1 and "https" in notes[0]


def test_ssh_scheme_drops_user_and_port():
    url, notes = A.sanitize_repo_url("ssh://git@gitlab.example.com:2222/g/dlc-meta.git")
    assert url == "https://gitlab.example.com/g/dlc-meta.git"
    assert len(notes) == 1          # 자격정보 경고를 겹쳐 내지 않는다(ssh user 는 자격이 아니다)


def test_windows_path_is_not_mistaken_for_scp():
    """``C:\\work\\dlc-meta`` 를 scp 형식으로 오인하면 엉뚱한 URL 이 만들어진다."""
    url, notes = A.sanitize_repo_url(r"C:\work\dlc-meta")
    assert url == r"C:\work\dlc-meta" and notes == []


# ---------------------------------------------------------------------------
# 클론 탐색 · origin 조회
# ---------------------------------------------------------------------------


def test_read_origin_url_returns_empty_when_git_fails():
    assert A.read_origin_url("/nope", runner=_runner("x", rc=128)) == ""


def test_read_origin_url_survives_missing_git_binary():
    def boom(*_a, **_k):
        raise FileNotFoundError("git")

    assert A.read_origin_url("/nope", runner=boom) == ""


def test_explicit_path_wins_and_is_probed_first(tmp_path):
    path = _clone(tmp_path, "somewhere-else")
    run = _runner("https://git.example.com/acme/dlc-meta.git")
    url, found, notes = A.discover_dlc_meta_url(path=path, project_dir=str(tmp_path),
                                                env={}, runner=run)
    assert url == "https://git.example.com/acme/dlc-meta.git"
    assert found == os.path.abspath(path)
    assert notes == []


def test_sibling_directory_is_found_without_an_explicit_path(tmp_path):
    """SETTER 산출물의 관례 이름(dlc-meta)을 배포 디렉토리 이웃에서 찾는다."""
    deploy = tmp_path / "jira-auto-dispatcher"
    deploy.mkdir()
    _clone(tmp_path)                       # <deploy>/../dlc-meta
    run = _runner("https://git.example.com/acme/dlc-meta.git")
    url, found, _notes = A.discover_dlc_meta_url(project_dir=str(deploy), env={},
                                                 runner=run)
    assert url == "https://git.example.com/acme/dlc-meta.git"
    assert found == os.path.abspath(str(tmp_path / "dlc-meta"))


def test_env_path_is_honoured(tmp_path):
    path = _clone(tmp_path)
    run = _runner("https://git.example.com/acme/dlc-meta.git")
    url, found, _notes = A.discover_dlc_meta_url(
        project_dir=str(tmp_path / "nowhere"), env={A.DLC_META_ENV: path}, runner=run)
    assert url and found == os.path.abspath(path)


def test_bad_explicit_path_says_so_instead_of_silently_picking_another(tmp_path):
    """사람이 준 경로가 틀렸다는 것이 정보다 — 조용히 다른 레포를 집지 않는다."""
    _clone(tmp_path)                       # 자동 탐색이 집을 수 있는 진짜 클론
    run = _runner("https://git.example.com/acme/dlc-meta.git")
    url, _found, notes = A.discover_dlc_meta_url(
        path=str(tmp_path / "not-a-clone"), project_dir=str(tmp_path), env={}, runner=run)
    assert url                              # 폴백은 하되
    assert any("git 클론이 아닙니다" in n for n in notes)   # 말은 한다


# ---------------------------------------------------------------------------
# 답변 채움
# ---------------------------------------------------------------------------


def test_autofill_fills_url_and_infers_forge_kind(tmp_path):
    path = _clone(tmp_path)
    run = _runner("https://github.example.com/acme/dlc-meta.git")
    report = A.autofill_answers({}, dlc_meta_path=path, project_dir=str(tmp_path),
                                env={}, runner=run)
    assert report.answers["run.dlc_meta_repo_url"].startswith("https://github.example.com")
    # 호스트가 스스로 밝히면 forge 종류까지 채운다(토큰 헤더·MR/PR 용어가 여기서 갈린다).
    assert report.answers["forge.kind"] == "github"
    assert {f.key for f in report.filled} == {"run.dlc_meta_repo_url", "forge.kind"}


def test_autofill_does_not_override_an_explicit_answer(tmp_path):
    path = _clone(tmp_path)
    run = _runner("https://github.example.com/acme/dlc-meta.git")
    given = {"run.dlc_meta_repo_url": "https://git.corp.example/x/dlc-meta.git",
             "forge.kind": "gitlab"}
    report = A.autofill_answers(given, dlc_meta_path=path, project_dir=str(tmp_path),
                                env={}, runner=run)
    assert report.answers == given and report.filled == []
    assert run.calls == []                  # git 을 부르지도 않는다


def test_autofill_replaces_the_example_placeholder(tmp_path):
    """``<your-group>`` 이 남은 값은 "답한 것"이 아니다 — 채워야 한다."""
    path = _clone(tmp_path)
    run = _runner("https://git.example.com/acme/dlc-meta.git")
    report = A.autofill_answers(
        {"run.dlc_meta_repo_url": "https://gitlab.example.com/<your-group>/dlc-meta.git"},
        dlc_meta_path=path, project_dir=str(tmp_path), env={}, runner=run)
    assert report.answers["run.dlc_meta_repo_url"] == \
        "https://git.example.com/acme/dlc-meta.git"


def test_autofill_says_how_to_fix_it_when_nothing_is_found(tmp_path):
    report = A.autofill_answers({}, project_dir=str(tmp_path / "empty"), env={},
                                runner=_runner("", rc=1))
    assert report.filled == []
    assert any("--dlc-meta" in n for n in report.notes)


def test_autofill_never_mutates_the_input(tmp_path):
    path = _clone(tmp_path)
    given = {}
    A.autofill_answers(given, dlc_meta_path=path, project_dir=str(tmp_path), env={},
                       runner=_runner("https://git.example.com/acme/dlc-meta.git"))
    assert given == {}


def test_ambiguous_host_does_not_guess_forge_kind(tmp_path):
    """``git.corp.example`` 은 어느 forge 인지 밝히지 않는다 — 추측으로 인증을 깨지 않는다."""
    path = _clone(tmp_path)
    report = A.autofill_answers({}, dlc_meta_path=path, project_dir=str(tmp_path),
                                env={},
                                runner=_runner("https://git.corp.example/x/dlc-meta.git"))
    assert "forge.kind" not in report.answers
