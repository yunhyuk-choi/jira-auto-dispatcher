"""app.dlc_meta_writer.DlcMetaWriter 단위/통합 테스트 — central 단일 라이터.

검증 핵심(Phase 3a):
    - 지정 사이클로그 경로만 스테이징(``git add -- <경로>``, ``git add .`` 아님).
    - master 직커밋 + push. non-fast-forward면 ``pull --rebase`` 후 재시도.
    - 커밋할 것 없으면(클린) no-op(add/commit/push 미발생, None 반환).
    - (실git) 두 잡 완료 → master에 **분리된 두 커밋**, 유실 없음, 트리 클린.

단위테스트는 라이브 git을 부르지 않는다(runner 주입). 통합테스트만 실제 git 사용.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app import dlc_meta_writer as W
from app.dlc_meta_writer import DlcMetaWriter, _porcelain_paths, _primary_relpath


# --- 순수 함수 --------------------------------------------------------------


def test_porcelain_paths_parses_untracked_modified_and_rename():
    text = (
        "?? n/cycles/C1/audit.md\n"
        " M n/ORCHESTRATOR.md\n"
        "A  n/cycles/C1/handoff-v1.md\n"
        "R  old/x.md -> n/cycles/C1/moved.md\n"
    )
    paths = _porcelain_paths(text)
    assert paths == [
        "n/cycles/C1/audit.md",
        "n/ORCHESTRATOR.md",
        "n/cycles/C1/handoff-v1.md",
        "n/cycles/C1/moved.md",   # rename → 새 경로만
    ]


def test_porcelain_paths_empty_is_empty():
    assert _porcelain_paths("") == []
    assert _porcelain_paths("\n\n") == []


def test_primary_relpath_prefers_cycle_dir():
    paths = ["n/ORCHESTRATOR.md", "n/cycles/2026-08-19-abc/audit.md"]
    assert _primary_relpath(paths) == "n/cycles/2026-08-19-abc"


def test_primary_relpath_falls_back_to_first_sorted():
    assert _primary_relpath(["b.md", "a.md"]) == "a.md"
    assert _primary_relpath([]) is None


# --- 단위: 주입 runner --------------------------------------------------------


def _cfg(path, *, url="http://gitlab.example.com/g/dlc-meta.git", branch="master",
         token_ref="", base_dir=""):
    run = SimpleNamespace(
        repo_map_path=path,
        dlc_meta_repo=path,
        workspace_dir="",
        dlc_meta_repo_url=url,
        dlc_meta_branch=branch,
        repo_resolver_gitlab_token_ref=token_ref,
    )
    return SimpleNamespace(run=run, secrets=SimpleNamespace(base_dir=base_dir))


class FakeGit:
    """subprocess.run 대역 — 서브커맨드별 결과 주입 + 호출 기록.

    push_fail_first: 처음 N번의 push를 non-fast-forward로 실패시킨 뒤 성공(재시도 검증).
    """

    def __init__(self, *, porcelain="", commit_rc=0, push_fail_first=0):
        self.calls = []
        self.porcelain = porcelain
        self.commit_rc = commit_rc
        self.push_fail_first = push_fail_first
        self._push_seen = 0

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        # cmd = ["git","-C",path, ...]
        rest = cmd[3:]
        if "status" in rest:
            return SimpleNamespace(returncode=0, stdout=self.porcelain, stderr="")
        if rest and rest[0] == "add" or "add" in rest[:1]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "commit" in rest:
            return SimpleNamespace(returncode=self.commit_rc, stdout="", stderr="")
        if "push" in rest:
            self._push_seen += 1
            if self._push_seen <= self.push_fail_first:
                return SimpleNamespace(
                    returncode=1, stdout="",
                    stderr="! [rejected] master -> master (non-fast-forward)",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "pull" in rest:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def cmds(self):
        return [" ".join(c) for c in self.calls]


def _mk_git_dir(tmp_path):
    path = str(tmp_path / "dlc-meta")
    os.makedirs(os.path.join(path, ".git"))
    return path


def test_commit_stages_only_given_paths_not_dot(tmp_path):
    path = _mk_git_dir(tmp_path)
    git = FakeGit(porcelain="?? n/cycles/C1/audit.md\n M n/ORCHESTRATOR.md\n")
    w = DlcMetaWriter(_cfg(path), git_run=git)
    rel = w.commit_cycle_log({"ticket": "T-1"})

    assert rel == "n/cycles/C1"
    add_calls = [c for c in git.calls if "add" in c[3:4]]
    assert len(add_calls) == 1
    add = add_calls[0]
    # ⚠️ git add . / -A 가 아니라 명시 경로 pathspec.
    assert "." not in add and "-A" not in add
    assert add[3:] == ["add", "--", "n/cycles/C1/audit.md", "n/ORCHESTRATOR.md"]
    # master로 커밋·push(HEAD:master).
    assert any(c[3:5] == ["push", "http://gitlab.example.com/g/dlc-meta.git"]
               or ("push" in c[3:4] and "HEAD:master" in c) for c in git.calls)
    assert any("HEAD:master" in c for c in git.calls if "push" in c[3:4])


def test_commit_noop_when_clean(tmp_path):
    path = _mk_git_dir(tmp_path)
    git = FakeGit(porcelain="")   # 변경 없음
    w = DlcMetaWriter(_cfg(path), git_run=git)
    assert w.commit_cycle_log({"ticket": "T-1"}) is None
    # add/commit/push 어느 것도 발생하지 않는다.
    assert not any("add" in c[3:4] for c in git.calls)
    assert not any("commit" in c[3:] for c in git.calls)
    assert not any("push" in c[3:4] for c in git.calls)


def test_commit_noop_when_no_git_dir(tmp_path):
    path = str(tmp_path / "dlc-meta")   # .git 없음
    git = FakeGit(porcelain="?? x")
    w = DlcMetaWriter(_cfg(path), git_run=git)
    assert w.commit_cycle_log({"ticket": "T"}) is None
    assert git.calls == []   # git 미호출


def test_push_pull_rebase_retry_on_non_fast_forward(tmp_path):
    path = _mk_git_dir(tmp_path)
    # 첫 push는 non-fast-forward로 실패 → pull --rebase → 재시도 성공.
    git = FakeGit(porcelain="?? n/cycles/C2/audit.md\n", push_fail_first=1)
    w = DlcMetaWriter(_cfg(path), git_run=git)
    rel = w.commit_cycle_log({"ticket": "T-2"})
    assert rel == "n/cycles/C2"
    pushes = [c for c in git.calls if "push" in c[3:4]]
    pulls = [c for c in git.calls if "pull" in c[3:4]]
    assert len(pushes) >= 2      # 실패 후 재시도
    assert len(pulls) >= 1       # 재시도 전 pull --rebase
    assert any("--rebase" in c for c in pulls)
    # ⚠️ --autostash: 공유 워크트리의 dirty(다른 잡 진행분)를 보존한 채 rebase(하드 실패 방지).
    assert all("--autostash" in c for c in pulls)


def test_commit_skipped_when_commit_rc_nonzero(tmp_path):
    # 스테이지가 비어 commit이 nonzero(경합으로 이미 커밋됨 등) → push 안 하고 None.
    path = _mk_git_dir(tmp_path)
    git = FakeGit(porcelain="?? n/cycles/C3/audit.md\n", commit_rc=1)
    w = DlcMetaWriter(_cfg(path), git_run=git)
    assert w.commit_cycle_log({"ticket": "T-3"}) is None
    assert not any("push" in c[3:4] for c in git.calls)


def test_commit_uses_central_committer_identity(tmp_path):
    path = _mk_git_dir(tmp_path)
    git = FakeGit(porcelain="?? n/cycles/C1/audit.md\n")
    w = DlcMetaWriter(_cfg(path), git_run=git)
    w.commit_cycle_log({"ticket": "T-1"})
    commit = [c for c in git.calls if "commit" in c[3:]][0]
    assert "-c" in commit
    joined = " ".join(commit)
    assert "user.name=jad-central" in joined
    assert "사이클로그 T-1" in joined


def test_writer_never_raises_on_git_failure(tmp_path):
    path = _mk_git_dir(tmp_path)

    def boom(cmd, **kw):
        raise RuntimeError("git exploded with token GLTOKEN-secret")

    w = DlcMetaWriter(_cfg(path, token_ref=""), git_run=boom)
    # 예외를 전파하지 않고 None(격리). 반환값에 토큰/시크릿이 새지 않는다.
    assert w.commit_cycle_log({"ticket": "T"}) is None


# --- 통합: 실제 git ----------------------------------------------------------


def _git(cwd, *args, check=True):
    # encoding='utf-8': 커밋 subject의 한글(사이클로그)을 OS 로케일(cp949)로 디코드하다
    # UnicodeDecodeError 나지 않게 강제(POLICY-ENCODING).
    return subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.email=t@t",
         "-c", "user.name=t", "-C", cwd, *args],
        check=check, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


@pytest.fixture()
def git_env(monkeypatch, tmp_path):
    """실git 통합의 **OS-무관 결정성 + CI 조건 재현** env.

    - ``core.autocrlf=false``: Windows global autocrlf=true가 LF↔CRLF로 워크트리를
      더럽혀(phantom modified) rebase/status 단언을 흔드는 트랩 제거.
    - ``safe.bareRepository=all``: 이 환경 sandbox가 GIT_CONFIG_PARAMETERS로
      ``safe.bareRepository=explicit`` 를 주입해 **bare 레포 직접 조회**(단언용
      ``git -C origin ...``)를 막는다 → all로 허용(프로덕션 Linux엔 이 제약 없음).
    - **빈 GIT_CONFIG_GLOBAL/SYSTEM**: 전역 git 정체성(user.*)을 **제거**해 Linux CI
      조건(정체성 없음)을 Windows에서도 재현한다 → writer가 rebase/autostash에 자기
      정체성을 env로 self-주입하지 않으면 *"Committer identity unknown"* 로 실패해
      이 회귀를 잡는다(정체성 self-주입 픽스의 회귀 가드).

    GIT_CONFIG_PARAMETERS로 주입하면 writer의 subprocess(os.environ 상속)와 테스트
    헬퍼 git 모두에 일괄 적용된다. Linux CI에선 무해.
    """
    empty = tmp_path / "empty_gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv(
        "GIT_CONFIG_PARAMETERS", "'core.autocrlf=false' 'safe.bareRepository=all'"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))


def _clone(url, dst):
    subprocess.run(["git", "clone", "-q", url, dst],
                   check=True, capture_output=True, text=True)


def _seed_remote(tmp_path):
    """**bare** origin(master 시드) + non-bare shared 클론 반환.

    표준 bare 원격 = push/non-fast-forward/rebase 시맨틱이 OS 무관하게 깨끗하다
    (워크트리·denyCurrentBranch 꼼수 불요). 프로덕션 GitLab 원격과 동일한 push 대상.
    """
    origin = str(tmp_path / "origin.git")
    seed = str(tmp_path / "seed")
    shared = str(tmp_path / "shared")
    subprocess.run(["git", "init", "--bare", "-b", "master", origin],
                   check=True, capture_output=True, text=True)
    _clone(origin, seed)
    (tmp_path / "seed" / "README.md").write_text("seed\n", encoding="utf-8")
    (tmp_path / "seed" / "SHARED.md").write_text("tracked\n", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "seed")
    _git(seed, "push", "-q", "origin", "master")
    _clone(origin, shared)
    return origin, shared


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_real_git_two_completions_are_separate_commits_no_loss(tmp_path, git_env):
    origin, shared = _seed_remote(tmp_path)
    cfg = _cfg(shared, url=origin, branch="master")   # 로컬 경로 remote(토큰 무주입)
    w = DlcMetaWriter(cfg)   # 실제 subprocess.run

    # 잡1 완료: 사이클로그 C1 작성(오케스트레이터의 leave-local 흉내) → central 커밋.
    os.makedirs(os.path.join(shared, "n", "cycles", "C1"))
    with open(os.path.join(shared, "n", "cycles", "C1", "audit.md"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("cycle 1\n")
    rel1 = w.commit_cycle_log({"ticket": "T-1"})
    assert rel1 == "n/cycles/C1"

    # 잡2 완료: 사이클로그 C2 → 별도 커밋.
    os.makedirs(os.path.join(shared, "n", "cycles", "C2"))
    with open(os.path.join(shared, "n", "cycles", "C2", "audit.md"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("cycle 2\n")
    rel2 = w.commit_cycle_log({"ticket": "T-2"})
    assert rel2 == "n/cycles/C2"

    # 트리 클린.
    status = _git(shared, "status", "--porcelain").stdout
    assert status == "", f"트리 클린 아님: {status!r}"

    # origin master에 두 사이클로그가 유실 없이, **분리된 두 커밋**으로 도달.
    log = _git(origin, "log", "--oneline", "master").stdout.strip().splitlines()
    subjects = _git(origin, "log", "--format=%s", "master").stdout
    assert subjects.count("사이클로그 T-1") == 1
    assert subjects.count("사이클로그 T-2") == 1
    # seed + 2 사이클로그 커밋 = 3.
    assert len(log) == 3, log
    # 두 파일 모두 원격 트리에 존재.
    tree = _git(origin, "ls-tree", "-r", "--name-only", "master").stdout
    assert "n/cycles/C1/audit.md" in tree
    assert "n/cycles/C2/audit.md" in tree


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_real_git_push_retries_on_non_fast_forward(tmp_path, git_env):
    origin, shared = _seed_remote(tmp_path)

    # 다른 클론이 origin master를 전진시켜 shared를 뒤처지게 만든다(non-fast-forward 유발).
    other = str(tmp_path / "other")
    _clone(origin, other)
    (tmp_path / "other" / "remote-change.md").write_text("remote\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-q", "-m", "remote advance")
    _git(other, "push", "-q", "origin", "master")

    cfg = _cfg(shared, url=origin, branch="master")
    w = DlcMetaWriter(cfg)

    # shared에 사이클로그 작성 후 커밋 → 첫 push는 non-fast-forward → pull --rebase → 성공.
    os.makedirs(os.path.join(shared, "n", "cycles", "CX"))
    with open(os.path.join(shared, "n", "cycles", "CX", "audit.md"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write("cycle x\n")
    # ⚠️ 동시성 재현: 다른 잡이 tracked 파일을 수정 중(미커밋 dirty)인 상태로 push 재시도가
    # 일어난다 → plain pull --rebase면 "unstaged changes"로 하드 실패. --autostash가 보존.
    with open(os.path.join(shared, "SHARED.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("concurrent job WIP\n")
    rel = w.commit_cycle_log({"ticket": "T-X"})
    assert rel == "n/cycles/CX"

    # 원격에 우리 사이클로그 + 남의 변경 둘 다 존재(유실 없음).
    tree = _git(origin, "ls-tree", "-r", "--name-only", "master").stdout
    assert "n/cycles/CX/audit.md" in tree
    assert "remote-change.md" in tree
    # 다른 잡의 dirty(SHARED.md WIP)는 autostash로 보존됨(clobber 안 됨).
    assert "concurrent job WIP" in (tmp_path / "shared" / "SHARED.md").read_text(encoding="utf-8")
