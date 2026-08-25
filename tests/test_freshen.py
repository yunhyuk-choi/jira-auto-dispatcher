"""app.freshen.freshen_unlocked_repos 단위 + 실제 git 통합 테스트.

검증 핵심:
    - 미락(un-locked) 레포는 원격 기본 브랜치로 강제 정합(stale→fresh).
    - 레포락이 걸린 레포는 **건드리지 않는다**(skip — 진행 중 WIP 보호).
    - 제외 레포(orchestrator·dlc-meta)는 미락이어도 skip.
    - dirty 미락 레포는 force-clean(추적 변경·untracked 제거)돼 원격에 정합.
    - 이전 잡이 남긴 non-default 브랜치가 체크아웃돼 있어도 **기본 브랜치**를 최신화.
    - 레포별 실패는 격리(한 레포 실패가 다른 레포·반환을 막지 않음).

단위 테스트는 runner/provision을 주입해 오케스트레이션(어느 레포가 최신화되는지)만
결정적으로 본다. 실제 git 통합 테스트가 정합 결과(트리 상태)를 실증한다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import freshen as F


# ===========================================================================
# 단위: 주입 runner/provision으로 skip·선택 로직 검증(라이브 git 미호출)
# ===========================================================================


def _cfg(ws, *, orchestrator="orchestrator", dlc_meta="dlc-meta"):
    run = SimpleNamespace(
        workspace_dir=str(ws),
        orchestrator_repo=f"/app/{orchestrator}",
        dlc_meta_repo=f"/app/{dlc_meta}",
    )
    return SimpleNamespace(run=run)


def _mk_repo(ws, slug, *, git=True):
    d = ws / slug
    d.mkdir(parents=True, exist_ok=True)
    if git:
        (d / ".git").mkdir()
    return d


class FakeRunner:
    """git 대역 — get-url/symbolic-ref만 응답, 나머지는 성공(returncode 0)."""

    def __init__(self, *, origin="http://gitlab.example.com/g/repo.git",
                 head="origin/main", no_origin_for=()):
        self.calls = []
        self.origin = origin
        self.head = head
        self.no_origin_for = tuple(no_origin_for)

    def __call__(self, cmd, capture_output=None, text=None, check=None):
        self.calls.append(list(cmd))
        # cmd = ["git", "-C", <path>, <sub...>]
        path = cmd[2] if len(cmd) > 2 else ""
        sub = cmd[3:]
        if sub[:2] == ["remote", "get-url"]:
            if any(s in path for s in self.no_origin_for):
                return SimpleNamespace(returncode=1, stdout="", stderr="no origin")
            return SimpleNamespace(returncode=0, stdout=self.origin + "\n", stderr="")
        if sub[:1] == ["symbolic-ref"]:
            return SimpleNamespace(returncode=0, stdout=self.head + "\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")


class FakeProvision:
    """provision_one 대역 — 호출 기록 + 고정 상태 반환(또는 예외 주입)."""

    def __init__(self, *, ret="reset", raise_for=()):
        self.calls = []
        self.ret = ret
        self.raise_for = tuple(raise_for)

    def __call__(self, path, url, token, *, runner=None, force_clean=True,
                 forge_kind=None):
        self.calls.append({"path": path, "url": url, "token": token,
                           "force_clean": force_clean, "forge_kind": forge_kind})
        if any(s in path for s in self.raise_for):
            raise RuntimeError("boom")
        return self.ret

    def paths(self):
        return [os.path.basename(c["path"]) for c in self.calls]


class FakeSafePull:
    """safe_pull_rebase_autostash 대역 — 호출 기록 + 고정 상태 반환(비파괴 pull 경로용)."""

    def __init__(self, *, ret="pulled", raise_for=()):
        self.calls = []
        self.ret = ret
        self.raise_for = tuple(raise_for)

    def __call__(self, path, url, token, *, runner=None, forge_kind=None):
        self.calls.append({"path": path, "url": url, "token": token,
                           "forge_kind": forge_kind})
        if any(s in path for s in self.raise_for):
            raise RuntimeError("boom")
        return self.ret

    def paths(self):
        return [os.path.basename(c["path"]) for c in self.calls]


def test_locked_and_excluded_are_skipped_only_unlocked_freshened(tmp_path):
    ws = tmp_path / "ws"
    _mk_repo(ws, "metapage-backend")   # 미락 참고 레포 → reset 최신화 대상
    _mk_repo(ws, "portal-frontend")    # 레포락 걸림 → skip
    _mk_repo(ws, "orchestrator")       # 제외(모든 claude cwd)
    _mk_repo(ws, "dlc-meta")           # 단일 라이터 클론 → 비파괴 안전 pull(제외 아님)
    _mk_repo(ws, "not-a-repo", git=False)  # .git 없음 → 무시

    runner = FakeRunner()
    prov = FakeProvision()
    safe = FakeSafePull()
    res = F.freshen_unlocked_repos(
        _cfg(ws), locked_repos={"portal-frontend"}, forge_token="T",
        runner=runner, provision=prov, safe_pull=safe,
    )

    assert res["metapage-backend"] == "reset"
    assert res["portal-frontend"] == "skipped: locked"
    assert res["orchestrator"] == "skipped: excluded"
    assert res["dlc-meta"] == "pulled"        # 비파괴 안전 pull로 최신화(더 이상 제외 아님)
    assert "not-a-repo" not in res            # git 레포가 아니면 결과에 없음

    # provision(reset 경로)은 오직 미락·비제외·비-dlc-meta 레포에만 걸린다.
    assert prov.paths() == ["metapage-backend"]
    assert prov.calls[0]["force_clean"] is True
    assert prov.calls[0]["token"] == "T"
    # dlc-meta는 오직 safe_pull(비파괴)로만 라우팅된다 — provision(reset)엔 절대 안 걸린다.
    assert safe.paths() == ["dlc-meta"]
    assert safe.calls[0]["token"] == "T"
    assert "dlc-meta" not in prov.paths()


def test_default_branch_checked_out_before_reset(tmp_path):
    # 이전 잡이 auto/HAN-1 브랜치를 남겨도, 최신화 전에 기본 브랜치를 강제 체크아웃한다.
    ws = tmp_path / "ws"
    _mk_repo(ws, "metapage-backend")
    runner = FakeRunner(head="origin/main")
    prov = FakeProvision()
    F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                             runner=runner, provision=prov)
    checkouts = [c for c in runner.calls if c[3:5] == ["checkout", "-f"]]
    assert checkouts and checkouts[0][-1] == "main"


def test_freshen_sanitizes_embedded_token_in_origin(tmp_path):
    """#6 자격증명 위생: origin 에 임베디드 토큰이 박혀 있으면 freshen 이 clean URL 로
    재설정(set-url)하고, provision 에도 토큰 없는 URL 을 넘긴다(잔재 토큰 자동 정리)."""
    ws = tmp_path / "ws"
    _mk_repo(ws, "metapage-frontend")
    dirty = "http://oauth2:LEAKED_TOKEN@server.example/hansa/connector/metapage-frontend.git"
    clean = "http://server.example/hansa/connector/metapage-frontend.git"
    runner = FakeRunner(origin=dirty)
    prov = FakeProvision()
    F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                             runner=runner, provision=prov)
    # origin 을 clean URL 로 재저장했다.
    set_urls = [c for c in runner.calls if c[3:6] == ["remote", "set-url", "origin"]]
    assert set_urls, "임베디드 토큰 origin 은 set-url 로 정리돼야 한다"
    assert set_urls[0][-1] == clean
    assert "LEAKED_TOKEN" not in set_urls[0][-1]
    # provision 은 토큰 없는 clean URL 로 호출된다.
    assert prov.calls and prov.calls[0]["url"] == clean
    assert "LEAKED_TOKEN" not in prov.calls[0]["url"]


def test_freshen_leaves_clean_origin_untouched(tmp_path):
    """origin 이 이미 clean 이면 set-url 재설정을 하지 않는다(불필요한 쓰기 없음)."""
    ws = tmp_path / "ws"
    _mk_repo(ws, "metapage-frontend")
    runner = FakeRunner(origin="http://server.example/g/repo.git")
    prov = FakeProvision()
    F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                             runner=runner, provision=prov)
    set_urls = [c for c in runner.calls if c[3:6] == ["remote", "set-url", "origin"]]
    assert not set_urls


def test_no_origin_repo_skipped_without_provision(tmp_path):
    ws = tmp_path / "ws"
    _mk_repo(ws, "no-origin")
    runner = FakeRunner(no_origin_for=("no-origin",))
    prov = FakeProvision()
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=runner, provision=prov)
    assert res["no-origin"] == "skipped: no origin"
    assert prov.calls == []            # origin 없으면 provision 미호출


def test_per_repo_failure_is_isolated(tmp_path):
    ws = tmp_path / "ws"
    _mk_repo(ws, "repo-ok-1")
    _mk_repo(ws, "repo-bad")
    _mk_repo(ws, "repo-ok-2")
    runner = FakeRunner()
    prov = FakeProvision(raise_for=("repo-bad",))
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=runner, provision=prov)
    # 한 레포가 예외를 던져도 나머지는 최신화되고 함수는 예외 없이 반환한다.
    assert res["repo-ok-1"] == "reset"
    assert res["repo-ok-2"] == "reset"
    assert res["repo-bad"].startswith("err")


def test_dlc_meta_safe_pull_failure_is_isolated(tmp_path):
    # dlc-meta 안전 pull이 예외를 던져도 나머지 레포는 최신화되고 함수는 예외 없이 반환한다.
    ws = tmp_path / "ws"
    _mk_repo(ws, "metapage-backend")
    _mk_repo(ws, "dlc-meta")
    runner = FakeRunner()
    prov = FakeProvision()
    safe = FakeSafePull(raise_for=("dlc-meta",))
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=runner, provision=prov, safe_pull=safe)
    assert res["metapage-backend"] == "reset"      # 다른 레포는 정상 최신화
    assert res["dlc-meta"].startswith("err")        # dlc-meta 실패는 격리·기록


def test_dlc_meta_returns_safe_pull_err_string_verbatim(tmp_path):
    # safe_pull이 비파괴 원상복구 후 "err: ..." 문자열을 돌려주면 그대로 관측에 실린다.
    ws = tmp_path / "ws"
    _mk_repo(ws, "dlc-meta")
    safe = FakeSafePull(ret="err: git rc=1: conflict")
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=FakeRunner(), provision=FakeProvision(),
                                   safe_pull=safe)
    assert res["dlc-meta"] == "err: git rc=1: conflict"


def test_dlc_meta_no_origin_skipped_without_safe_pull(tmp_path):
    ws = tmp_path / "ws"
    _mk_repo(ws, "dlc-meta")
    runner = FakeRunner(no_origin_for=("dlc-meta",))
    safe = FakeSafePull()
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=runner, provision=FakeProvision(), safe_pull=safe)
    assert res["dlc-meta"] == "skipped: no origin"
    assert safe.calls == []            # origin 없으면 safe_pull 미호출


def test_orchestrator_still_excluded_dlc_meta_not(tmp_path):
    # 회귀 가드: orchestrator는 여전히 제외, dlc-meta는 제외에서 빠진다.
    ws = tmp_path / "ws"
    _mk_repo(ws, "orchestrator")
    _mk_repo(ws, "dlc-meta")
    safe = FakeSafePull()
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(), forge_token="T",
                                   runner=FakeRunner(), provision=FakeProvision(),
                                   safe_pull=safe)
    assert res["orchestrator"] == "skipped: excluded"
    assert res["dlc-meta"] == "pulled"
    assert safe.paths() == ["dlc-meta"]


def test_missing_workspace_returns_empty(tmp_path):
    cfg = _cfg(tmp_path / "does-not-exist")
    res = F.freshen_unlocked_repos(cfg, locked_repos=set(), forge_token="T",
                                   runner=FakeRunner(), provision=FakeProvision())
    assert res == {}


def test_empty_workspace_dir_config_returns_empty():
    cfg = SimpleNamespace(run=SimpleNamespace(workspace_dir="",
                          orchestrator_repo="", dlc_meta_repo=""))
    res = F.freshen_unlocked_repos(cfg, locked_repos=set(), forge_token="T",
                                   runner=FakeRunner(), provision=FakeProvision())
    assert res == {}


# ===========================================================================
# 실제 git 통합 — 정합 결과(트리 상태)를 실증. Windows/Linux 공통.
# ===========================================================================


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.email=t@t",
         "-c", "user.name=t", "-C", cwd, *args],
        check=True, capture_output=True, text=True,
    )


def _make_remote_and_clone(tmp_path, slug):
    """기본 브랜치 main인 원격(src) 생성 + 워크스페이스 클론(ws/<slug>). (src, ws) 반환."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    src = str(tmp_path / f"src-{slug}")
    os.makedirs(src)
    subprocess.run(["git", "-c", "init.defaultBranch=main", "init", "-q", src],
                   check=True, capture_output=True, text=True)
    (tmp_path / f"src-{slug}" / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c1")
    dest = str(ws / slug)
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", src, dest],
                   check=True, capture_output=True, text=True)
    return src, ws


def _advance_remote(src):
    """원격 main을 v2로 전진(reset이 HEAD를 실제로 옮기는지 확인용)."""
    with open(os.path.join(src, "tracked.txt"), "w", encoding="utf-8") as fh:
        fh.write("v2\n")
    _git(src, "commit", "-q", "-am", "c2")


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_unlocked_stale_repo_is_reset_to_remote_real_git(tmp_path):
    src, ws = _make_remote_and_clone(tmp_path, "metapage-backend")
    _advance_remote(src)  # 원격이 v2로 전진 — ws 클론은 아직 v1(stale)

    cfg = _cfg(ws)
    res = F.freshen_unlocked_repos(cfg, locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["metapage-backend"] == "reset", res

    # 미락 stale 레포가 원격 최신(v2)에 정합됐다.
    assert (ws / "metapage-backend" / "tracked.txt").read_text(encoding="utf-8") == "v2\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_dirty_unlocked_repo_is_force_cleaned_real_git(tmp_path):
    src, ws = _make_remote_and_clone(tmp_path, "metapage-frontend")
    _advance_remote(src)

    # 미락 레포를 dirty하게: 추적 파일 수정 + untracked 파일(plain pull이라면 abort).
    (ws / "metapage-frontend" / "tracked.txt").write_text("LOCAL EDIT\n", encoding="utf-8")
    (ws / "metapage-frontend" / "cycle-log.txt").write_text("junk\n", encoding="utf-8")

    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["metapage-frontend"] == "reset", res

    status = subprocess.run(["git", "-C", str(ws / "metapage-frontend"),
                             "status", "--porcelain"],
                            check=True, capture_output=True, text=True).stdout
    assert status == "", f"트리가 클린이 아님: {status!r}"
    assert (ws / "metapage-frontend" / "tracked.txt").read_text(encoding="utf-8") == "v2\n"
    assert not (ws / "metapage-frontend" / "cycle-log.txt").exists()  # untracked 제거


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_locked_repo_is_not_touched_real_git(tmp_path):
    src, ws = _make_remote_and_clone(tmp_path, "portal-frontend")
    _advance_remote(src)

    # 레포락이 걸린(활성 잡 소유) 레포는 진행 중 WIP가 있을 수 있으므로 손대면 안 된다.
    (ws / "portal-frontend" / "tracked.txt").write_text("WIP\n", encoding="utf-8")
    (ws / "portal-frontend" / "wip-artifact.txt").write_text("in progress\n", encoding="utf-8")

    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos={"portal-frontend"},
                                   forge_token="unused-token-no-scheme")
    assert res["portal-frontend"] == "skipped: locked"

    # WIP·dirty 상태가 그대로 보존된다(정합·clean 안 함).
    assert (ws / "portal-frontend" / "tracked.txt").read_text(encoding="utf-8") == "WIP\n"
    assert (ws / "portal-frontend" / "wip-artifact.txt").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_stale_non_default_branch_repo_freshens_default_branch_real_git(tmp_path):
    # 이전 잡이 남긴 auto/<ticket> 브랜치가 체크아웃돼 있어도 기본 브랜치(main)를 최신화한다.
    src, ws = _make_remote_and_clone(tmp_path, "metapage-backend")
    repo = str(ws / "metapage-backend")
    _git(repo, "checkout", "-q", "-b", "auto/HAN-999")  # non-default 브랜치로 이탈
    _advance_remote(src)

    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["metapage-backend"] == "reset", res

    # 최신화 후 기본 브랜치(main)로 돌아와 원격 최신(v2)에 정합됐다.
    branch = subprocess.run(["git", "-C", repo, "rev-parse", "--abbrev-ref", "HEAD"],
                            check=True, capture_output=True, text=True).stdout.strip()
    assert branch == "main"
    assert (ws / "metapage-backend" / "tracked.txt").read_text(encoding="utf-8") == "v2\n"


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_orchestrator_excluded_real_git(tmp_path):
    # orchestrator는 stale·미락이어도 최신화하지 않는다(모든 claude cwd — 라이브 프로세스 보호).
    src, ws = _make_remote_and_clone(tmp_path, "orchestrator")
    _advance_remote(src)
    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["orchestrator"] == "skipped: excluded"
    # stale 상태 그대로(정합 안 함).
    assert (ws / "orchestrator" / "tracked.txt").read_text(encoding="utf-8") == "v1\n"


def _log_files(repo):
    """레포 HEAD 히스토리의 모든 커밋에 등장한 파일 집합(로컬 커밋 보존 검증용)."""
    out = subprocess.run(["git", "-C", repo, "log", "--name-only", "--pretty=format:"],
                         check=True, capture_output=True, text=True).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_dlc_meta_safe_pull_absorbs_remote_and_preserves_local_commit_and_wip_real_git(tmp_path):
    # dlc-meta는 비파괴 안전 pull(rebase+autostash) — 사용자 학습 데이터(원격 선행)를
    # 흡수하되 central 단일 라이터의 **미푸시 로컬 커밋**과 진행 중 **WIP**를 모두 보존한다.
    src, ws = _make_remote_and_clone(tmp_path, "dlc-meta")
    repo = str(ws / "dlc-meta")

    # 원격에 '사용자 학습 데이터'가 push됨(다른 파일 → 무충돌 rebase).
    with open(os.path.join(src, "learning.txt"), "w", encoding="utf-8") as fh:
        fh.write("user learning\n")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "user pushes learning data")

    # central의 미푸시 로컬 커밋(사이클로그) — 아직 push 안 됨.
    (ws / "dlc-meta" / "cycle-log.txt").write_text("central cycle log\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "central cycle log (unpushed)")
    head_before = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                 check=True, capture_output=True, text=True).stdout.strip()

    # 진행 중 WIP: tracked 수정 + untracked 파일(autostash가 보존해야 함).
    (ws / "dlc-meta" / "tracked.txt").write_text("WIP edit\n", encoding="utf-8")
    (ws / "dlc-meta" / "wip-untracked.txt").write_text("in progress\n", encoding="utf-8")

    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["dlc-meta"] == "pulled", res

    # 1) 사용자 학습 데이터(원격) 흡수됨.
    assert (ws / "dlc-meta" / "learning.txt").read_text(encoding="utf-8") == "user learning\n"
    # 2) central의 미푸시 로컬 커밋이 원격 위로 rebase되어 보존됨(히스토리에 둘 다 존재).
    files = _log_files(repo)
    assert "cycle-log.txt" in files and "learning.txt" in files
    head_after = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                check=True, capture_output=True, text=True).stdout.strip()
    assert head_after != head_before  # rebase로 SHA는 바뀌되(재작성) 커밋은 유실 안 됨
    # 3) 진행 중 WIP가 autostash로 보존됨(reset --hard/clean이었다면 사라졌을 것).
    assert (ws / "dlc-meta" / "tracked.txt").read_text(encoding="utf-8") == "WIP edit\n"
    assert (ws / "dlc-meta" / "wip-untracked.txt").exists()


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_dlc_meta_safe_pull_conflict_is_non_destructive_real_git(tmp_path):
    # 충돌 시 비파괴: rebase --abort로 원상복구 → central의 로컬 커밋을 절대 잃지 않는다.
    src, ws = _make_remote_and_clone(tmp_path, "dlc-meta")
    repo = str(ws / "dlc-meta")

    # 원격이 tracked.txt를 REMOTE로 전진(사용자 push).
    with open(os.path.join(src, "tracked.txt"), "w", encoding="utf-8") as fh:
        fh.write("REMOTE\n")
    _git(src, "commit", "-q", "-am", "remote edits tracked")

    # central이 같은 파일을 CENTRAL로 로컬 커밋(=rebase 시 충돌).
    (ws / "dlc-meta" / "tracked.txt").write_text("CENTRAL\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "central edits tracked (unpushed)")
    # 진행 중 untracked WIP.
    (ws / "dlc-meta" / "wip.txt").write_text("in progress\n", encoding="utf-8")

    res = F.freshen_unlocked_repos(_cfg(ws), locked_repos=set(),
                                   forge_token="unused-token-no-scheme")
    assert res["dlc-meta"].startswith("err"), res       # 충돌 → 격리 err(비파괴 원상복구)

    # central의 로컬 커밋이 그대로 살아 있다(원격 값으로 clobber되지 않음).
    assert (ws / "dlc-meta" / "tracked.txt").read_text(encoding="utf-8") == "CENTRAL\n"
    subj = subprocess.run(["git", "-C", repo, "log", "-1", "--pretty=format:%s"],
                          check=True, capture_output=True, text=True).stdout.strip()
    assert subj == "central edits tracked (unpushed)"
    # rebase가 중간에 멈춰 있지 않다(원상복구 완료).
    assert not os.path.isdir(os.path.join(repo, ".git", "rebase-merge"))
    assert not os.path.isdir(os.path.join(repo, ".git", "rebase-apply"))
    # 진행 중 WIP(untracked)도 보존됨(autostash 복원).
    assert (ws / "dlc-meta" / "wip.txt").exists()
