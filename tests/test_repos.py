"""app.repos.ensure_repos 단위테스트 — clone/pull 분기·토큰 규율·실패 격리.

라이브 git은 절대 호출하지 않는다(runner 주입으로 대체). 토큰이 반환/에러 문자열
어디에도 새지 않음을 검증한다.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

from app import repos as R


TOKEN = "GLTOKEN-abcdef-123456"


def _cfg(tmp_path, *, with_urls=True):
    run = SimpleNamespace(
        orchestrator_repo=str(tmp_path / "orch"),
        dlc_meta_repo=str(tmp_path / "meta"),
        docs_repo=str(tmp_path / "docs"),
        orchestrator_repo_url=(
            "http://gitlab.example.com/your-namespace/ai-dlc-orchestrator.git"
            if with_urls else ""
        ),
        dlc_meta_repo_url=(
            "http://gitlab.example.com/your-namespace/dlc-meta.git" if with_urls else ""
        ),
        docs_repo_url=(
            "http://gitlab.example.com/your-namespace/docs.git" if with_urls else ""
        ),
    )
    return SimpleNamespace(run=run)


class Runner:
    """subprocess.run 대역 — 호출 기록 + 경로별 실패 주입."""

    def __init__(self, *, fail_substr=(), branch="main"):
        self.calls = []
        self.fail_substr = tuple(fail_substr)
        self.branch = branch

    def __call__(self, cmd, capture_output=None, text=None, check=None):
        self.calls.append(list(cmd))
        if "rev-parse" in cmd:
            return SimpleNamespace(returncode=0, stdout=self.branch + "\n", stderr="")
        for sub in self.fail_substr:
            if any(sub in str(a) for a in cmd):
                # stderr에 토큰/자격 URL을 일부러 흘려 마스킹을 검증한다.
                return SimpleNamespace(
                    returncode=128,
                    stdout="",
                    stderr=f"fatal: could not read from http://oauth2:{TOKEN}@server",
                )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def cmds_str(self):
        return "\n".join(" ".join(c) for c in self.calls)


def _assert_no_token(value):
    assert TOKEN not in str(value), f"토큰 유출: {value!r}"


# --- clone-if-absent ---------------------------------------------------------


def test_clone_when_absent_uses_token_url_then_scrubs(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner()
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res == {"orchestrator": "cloned", "dlc_meta": "cloned", "docs": "cloned"}

    clones = [c for c in runner.calls if c[:2] == ["git", "clone"]]
    set_urls = [c for c in runner.calls if "set-url" in c]
    assert len(clones) == 3 and len(set_urls) == 3

    # clone 인자 URL엔 토큰이 담긴다(oauth2:<token>@).
    for c in clones:
        url = c[2]
        assert url.startswith("http://oauth2:") and TOKEN in url
    # 클론 직후 remote는 토큰 없는 원본 URL로 정리된다.
    for c in set_urls:
        clean = c[-1]
        assert clean.startswith("http://gitlab.example.com") and "oauth2" not in clean
        _assert_no_token(clean)

    # 결과 dict엔 토큰이 없다.
    for v in res.values():
        _assert_no_token(v)


# --- fresh(신규 잡): force reset-if-present ----------------------------------


def test_fresh_present_force_resets_to_remote_no_pull(tmp_path):
    # 신규 잡(fresh=True, 기본): .git 존재 → plain pull이 아니라 fetch→checkout→
    # reset --hard FETCH_HEAD→clean -fd 로 dirty 워크스페이스를 원격에 강제 정합.
    # ⚠️ Phase 3a: dlc-meta는 워커가 라이터가 아니다(read_only) — 있으면 손대지 않고
    # "present". orchestrator/docs만 reset된다.
    cfg = _cfg(tmp_path)
    for name in ("orch", "meta", "docs"):
        os.makedirs(tmp_path / name / ".git")
    runner = Runner(branch="main")
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res == {"orchestrator": "reset", "dlc_meta": "present", "docs": "reset"}

    # plain pull은 절대 일어나지 않는다(abort 원인 제거).
    assert not any("pull" in c for c in runner.calls)
    # ⚠️ dlc-meta 경로엔 어떤 git 명령도 실행되지 않는다(present = 무동작).
    meta = str(tmp_path / "meta")
    assert not any(meta in " ".join(c) for c in runner.calls)
    # orchestrator/docs만 fetch/checkout/reset --hard/clean -fd (레포당 1회 = 2개씩).
    fetches = [c for c in runner.calls if "fetch" in c]
    resets = [c for c in runner.calls if "reset" in c and "--hard" in c]
    cleans = [c for c in runner.calls if "clean" in c and "-fd" in c]
    checkouts = [c for c in runner.calls if "checkout" in c]
    assert len(fetches) == 2 and len(resets) == 2
    assert len(cleans) == 2 and len(checkouts) == 2
    for c in fetches:
        # 명시 토큰 URL로 fetch(원격 config엔 저장 안 됨) + 브랜치.
        assert any(str(a).startswith("http://oauth2:") and TOKEN in str(a) for a in c)
        assert c[-1] == "main"
    for c in resets:
        assert c[-1] == "FETCH_HEAD"  # origin/main이 아니라 방금 fetch한 tip으로 reset
    assert not any(c[:2] == ["git", "clone"] for c in runner.calls)
    for v in res.values():
        _assert_no_token(v)


def test_dlc_meta_read_only_present_is_untouched(tmp_path):
    # dlc-meta가 이미 있으면(공유 클론 존재) 워커는 절대 손대지 않는다(단일 라이터=central).
    cfg = _cfg(tmp_path)
    os.makedirs(tmp_path / "meta" / ".git")   # dlc-meta만 present, 나머진 부재
    runner = Runner(branch="main")
    res = R.ensure_repos(cfg, TOKEN, runner=runner)
    assert res["dlc_meta"] == "present"
    # dlc-meta 경로엔 clone/fetch/reset/pull/commit 어느 것도 없다.
    meta = str(tmp_path / "meta")
    assert not any(meta in " ".join(c) for c in runner.calls)


def test_dlc_meta_read_only_clones_when_absent(tmp_path):
    # dlc-meta가 없으면(콜드 스타트) 읽기 스냅샷만 clone-if-absent(커밋/리셋 없음).
    cfg = _cfg(tmp_path)
    runner = Runner(branch="main")
    res = R.ensure_repos(cfg, TOKEN, runner=runner)
    assert res["dlc_meta"] == "cloned"
    meta = str(tmp_path / "meta")
    meta_calls = [c for c in runner.calls if meta in " ".join(c)]
    # clone + remote set-url 정리뿐 — reset/pull/commit 없음.
    assert any(c[:2] == ["git", "clone"] for c in meta_calls)
    assert not any("reset" in c or "pull" in c or "commit" in c for c in meta_calls)


# --- resume(재개): 완만한 pull(파괴적 reset 금지) ---------------------------


def test_resume_present_uses_ff_pull_and_never_resets(tmp_path):
    # 재개(fresh=False): 진행 중 작업 보존 — reset/clean 없이 pull --ff-only만.
    cfg = _cfg(tmp_path)
    for name in ("orch", "meta", "docs"):
        os.makedirs(tmp_path / name / ".git")
    runner = Runner(branch="main")
    res = R.ensure_repos(cfg, TOKEN, runner=runner, fresh=False)

    # ⚠️ Phase 3a: dlc-meta는 재개에서도 read_only(present) — pull하지 않는다.
    assert res == {"orchestrator": "pulled", "dlc_meta": "present", "docs": "pulled"}

    pulls = [c for c in runner.calls if "pull" in c]
    assert len(pulls) == 2   # orchestrator/docs만
    meta = str(tmp_path / "meta")
    assert not any(meta in " ".join(c) for c in runner.calls)
    for c in pulls:
        assert "--ff-only" in c
        assert any(str(a).startswith("http://oauth2:") and TOKEN in str(a) for a in c)
        assert c[-1] == "main"
    # 재개에서는 파괴적 reset --hard/clean이 절대 일어나지 않는다(작업 보존).
    assert not any("reset" in c for c in runner.calls)
    assert not any("clean" in c for c in runner.calls)
    for v in res.values():
        _assert_no_token(v)


# --- 실패 격리 + 마스킹 ------------------------------------------------------


def test_per_repo_failure_is_isolated_and_masked(tmp_path):
    cfg = _cfg(tmp_path)
    # dlc-meta 경로가 걸린 명령만 실패시킨다(다른 레포는 성공).
    runner = Runner(fail_substr=(str(tmp_path / "meta"),))
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res["orchestrator"] == "cloned"
    assert res["docs"] == "cloned"
    assert res["dlc_meta"].startswith("err")
    # 실패 메시지에 토큰/자격 URL이 마스킹된다.
    _assert_no_token(res["dlc_meta"])
    assert "***" in res["dlc_meta"]
    assert "oauth2:***@" in res["dlc_meta"]
    # 로그로도 토큰이 새지 않도록(반환값 기준 검증).
    for v in res.values():
        _assert_no_token(v)


# --- 토큰 없음 → 전체 skip(런너 미호출) --------------------------------------


def test_no_token_skips_all_without_running(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner()
    res = R.ensure_repos(cfg, None, runner=runner)
    assert res == {
        "orchestrator": "skipped: no forge token",
        "dlc_meta": "skipped: no forge token",
        "docs": "skipped: no forge token",
    }
    assert runner.calls == []  # 라이브 git 절대 미호출


def test_empty_token_string_skips_all(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner()
    res = R.ensure_repos(cfg, "", runner=runner)
    assert all(v == "skipped: no forge token" for v in res.values())
    assert runner.calls == []


# --- URL 비면 그 레포만 skip -------------------------------------------------


def test_missing_url_skips_that_repo(tmp_path):
    cfg = _cfg(tmp_path, with_urls=False)
    runner = Runner()
    res = R.ensure_repos(cfg, TOKEN, runner=runner)
    assert all(v == "skipped: no url" for v in res.values())
    assert runner.calls == []


# --- provision_one(단일 레포 — repo_resolver가 dlc-meta 신선화에 재사용) ------


def test_provision_one_clone_and_pull(tmp_path):
    path = str(tmp_path / "dlc-meta")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner()
    # 없으면 clone.
    assert R.provision_one(path, url, TOKEN, runner=runner) == "cloned"
    assert any(c[:2] == ["git", "clone"] for c in runner.calls)
    # clone URL엔 토큰, set-url로 정리.
    for v in [R.provision_one(path, url, TOKEN, runner=runner)]:
        _assert_no_token(v)

    # 있으면 기본(force_clean=True) → reset로 강제 정합.
    os.makedirs(tmp_path / "dlc-meta" / ".git", exist_ok=True)
    runner2 = Runner(branch="main")
    assert R.provision_one(path, url, TOKEN, runner=runner2) == "reset"
    assert any("reset" in c and "--hard" in c for c in runner2.calls)

    # force_clean=False → 완만한 pull(재개 경로에서 재사용 가능).
    runner3 = Runner(branch="main")
    assert R.provision_one(path, url, TOKEN, runner=runner3, force_clean=False) == "pulled"
    assert any("pull" in c for c in runner3.calls)
    assert not any("reset" in c for c in runner3.calls)


def test_provision_one_no_token_or_url_skips(tmp_path):
    path = str(tmp_path / "dlc-meta")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner()
    assert R.provision_one(path, url, None, runner=runner) == "skipped: no forge token"
    assert R.provision_one("", url, TOKEN, runner=runner) == "skipped: no path"
    assert R.provision_one(path, "", TOKEN, runner=runner) == "skipped: no url"
    assert runner.calls == []   # 라이브 git 미호출


def test_provision_one_failure_is_masked_and_no_raise(tmp_path):
    path = str(tmp_path / "meta")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner(fail_substr=("clone",))
    res = R.provision_one(path, url, TOKEN, runner=runner)
    assert res.startswith("err")
    _assert_no_token(res)
    assert "oauth2:***@" in res


# --- safe_pull_rebase_autostash(dlc-meta 전용 비파괴 동기화) ------------------


def test_safe_pull_uses_rebase_autostash_with_identity_never_resets(tmp_path):
    # dlc-meta present → pull --rebase --autostash(정체성 주입) + reset/clean 절대 없음.
    path = str(tmp_path / "dlc-meta")
    os.makedirs(tmp_path / "dlc-meta" / ".git")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner(branch="master")   # dlc-meta 정본 브랜치 = master
    res = R.safe_pull_rebase_autostash(path, url, TOKEN, runner=runner)
    assert res == "pulled"

    pulls = [c for c in runner.calls if "pull" in c]
    assert len(pulls) == 1
    cmd = pulls[0]
    assert "--rebase" in cmd and "--autostash" in cmd
    # 현재 브랜치(master)를 pull(하드코딩 아님).
    assert cmd[-1] == "master"
    # 명시 토큰 URL로 pull(원격 config 미저장) — 토큰은 인자에만.
    assert any(str(a).startswith("http://oauth2:") and TOKEN in str(a) for a in cmd)
    # 정체성을 -c 로 주입(rebase/stash 가 커밋 생성·재작성 → identity 필요).
    joined = " ".join(cmd)
    assert "-c user.name=" in joined and "-c user.email=" in joined
    # ⚠️ dlc-meta엔 파괴적 reset --hard / clean 이 절대 실행되지 않는다.
    assert not any("reset" in c for c in runner.calls)
    assert not any("clean" in c for c in runner.calls)
    for v in [res]:
        _assert_no_token(v)


def test_safe_pull_absent_skips_without_pull(tmp_path):
    # 클론 부재면 pull하지 않고 skip(clone은 read_only 프로비저닝 몫).
    path = str(tmp_path / "dlc-meta")   # .git 없음
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner()
    assert R.safe_pull_rebase_autostash(path, url, TOKEN, runner=runner) == "skipped: absent"
    assert not any("pull" in c for c in runner.calls)


def test_safe_pull_failure_aborts_rebase_and_is_masked(tmp_path):
    # pull 실패(충돌 등) → rebase --abort로 비파괴 원상복구 + 마스킹된 err.
    path = str(tmp_path / "dlc-meta")
    os.makedirs(tmp_path / "dlc-meta" / ".git")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner(branch="master", fail_substr=("--autostash",))  # pull만 실패
    res = R.safe_pull_rebase_autostash(path, url, TOKEN, runner=runner)
    assert res.startswith("err")
    _assert_no_token(res)
    assert "oauth2:***@" in res
    # 비파괴 원상복구: rebase --abort가 호출된다(로컬 커밋·WIP 보존).
    assert any(c[-2:] == ["rebase", "--abort"] for c in runner.calls)
    # reset --hard / clean 은 절대 없다(파괴 금지).
    assert not any("reset" in c for c in runner.calls)
    assert not any("clean" in c for c in runner.calls)


def test_safe_pull_no_token_or_url_or_path_skips(tmp_path):
    path = str(tmp_path / "dlc-meta")
    url = "http://gitlab.example.com/g/dlc-meta.git"
    runner = Runner()
    assert R.safe_pull_rebase_autostash(path, url, None, runner=runner) == "skipped: no forge token"
    assert R.safe_pull_rebase_autostash("", url, TOKEN, runner=runner) == "skipped: no path"
    assert R.safe_pull_rebase_autostash(path, "", TOKEN, runner=runner) == "skipped: no url"
    assert runner.calls == []   # 라이브 git 미호출


# --- 헬퍼 순수함수 -----------------------------------------------------------


def test_with_token_preserves_scheme_and_strips_existing_creds():
    assert (
        R._with_token("http://host:30000/a/b.git", "T")
        == "http://oauth2:T@host:30000/a/b.git"
    )
    assert (
        R._with_token("https://host/a.git", "T") == "https://oauth2:T@host/a.git"
    )
    # 기존 자격은 제거 후 재주입.
    assert (
        R._with_token("http://old:pw@host/a.git", "T") == "http://oauth2:T@host/a.git"
    )
    # 스킴 없으면 원문 유지.
    assert R._with_token("host/a.git", "T") == "host/a.git"


def test_strip_token_removes_embedded_credentials():
    """#6: remote 저장용 clean URL — 임베디드 자격정보(oauth2/user:pass)를 제거한다."""
    assert (
        R._strip_token("http://oauth2:TKN@host:30000/a/b.git")
        == "http://host:30000/a/b.git"
    )
    assert R._strip_token("https://old:pw@host/a.git") == "https://host/a.git"
    # 이미 clean 이면 그대로.
    assert R._strip_token("http://host/a.git") == "http://host/a.git"
    # 스킴 없으면 원문 유지.
    assert R._strip_token("host/a.git") == "host/a.git"


def test_strip_then_with_token_roundtrip_uses_only_current_token():
    """clean(strip) 후 그 순간의 토큰만 주입 — 임베디드 타인 토큰이 새 URL 에 남지 않는다."""
    dirty = "http://oauth2:OTHER_USER_TOKEN@host/g/repo.git"
    assert R._with_token(R._strip_token(dirty), "MY") == "http://oauth2:MY@host/g/repo.git"
    assert "OTHER_USER_TOKEN" not in R._with_token(R._strip_token(dirty), "MY")


# --- 공유 워크스페이스 pull 락(직렬화) ------------------------------------


def test_provision_lock_creates_lockfile_and_yields(tmp_path):
    # 락 컨텍스트가 <path>.lock 을 만들고 정상 진입/이탈한다(무락 폴백 환경 포함).
    path = str(tmp_path / "repo")
    entered = False
    with R._provision_lock(path):
        entered = True
    assert entered
    assert os.path.exists(path + ".lock")


def test_provision_lock_best_effort_on_bad_path(tmp_path):
    # 락 파일을 만들 수 없는 경로(부모 미존재 불가 케이스)라도 예외 없이 진입한다.
    bad = str(tmp_path / "no" / "such" / "deep")  # makedirs로 부모 생성됨 → 정상
    with R._provision_lock(bad):
        pass  # 예외 없이 통과하면 성공
    assert True


def test_ensure_repos_still_works_with_lock(tmp_path):
    # 락이 clone/pull을 감싸도 결과는 동일(락은 runner 호출을 늘리지 않는다).
    cfg = _cfg(tmp_path)
    runner = Runner()
    res = R.ensure_repos(cfg, TOKEN, runner=runner)
    assert res == {"orchestrator": "cloned", "dlc_meta": "cloned", "docs": "cloned"}
    # git 명령 외 별도 호출은 없다(락은 파일시스템 os 호출이라 runner와 무관).
    assert all(c[0] == "git" for c in runner.calls)


def test_mask_hides_token_value_and_cred_url():
    assert R._mask(f"x http://oauth2:{TOKEN}@h y", TOKEN) == "x http://oauth2:***@h y"
    # 토큰 값이 없어도 oauth2 자격 패턴은 가린다.
    assert R._mask("http://oauth2:whatever@h", None) == "http://oauth2:***@h"
    assert R._mask("", TOKEN) == ""


# --- 실제 git 통합: dirty 워크스페이스가 프로비저닝을 막지 못한다 -------------
#
# 이 테스트가 이 변경의 핵심을 실증한다: plain pull이 abort하던
# "local changes/untracked files would be overwritten … Aborting" 상황
# (추적 파일 수정 + untracked 파일)에서도 프로비저닝이 성공하고, 트리가
# 원격 최신에 정확히 정합(clean)됨을 실제 git으로 검증한다. Windows/Linux 공통.

import shutil
import subprocess

import pytest


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "core.autocrlf=false", "-c", "user.email=t@t",
         "-c", "user.name=t", "-C", cwd, *args],
        check=True, capture_output=True, text=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_dirty_workspace_force_reset_matches_remote_real_git(tmp_path):
    src = str(tmp_path / "src")            # 원격 대역(로컬 경로 = 스킴 없음 → 토큰 무주입)
    ws = str(tmp_path / "ws")              # 공유 워커 워크스페이스 클론
    os.makedirs(src)
    _git(src, "init", "-q")
    (tmp_path / "src" / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c1")

    # 워크스페이스 클론(현재 브랜치가 _current_branch로 감지됨).
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", src, ws],
                   check=True, capture_output=True, text=True)

    # 원격을 전진시킨다(reset이 HEAD를 실제로 옮기는지 확인용).
    (tmp_path / "src" / "tracked.txt").write_text("v2\n", encoding="utf-8")
    _git(src, "commit", "-q", "-am", "c2")

    # 워크스페이스를 dirty하게 만든다: 추적 파일 수정 + untracked 파일(pull이 abort하던 조건).
    (tmp_path / "ws" / "tracked.txt").write_text("LOCAL EDIT\n", encoding="utf-8")
    (tmp_path / "ws" / "cycle-log.txt").write_text("uncommitted cycle log\n", encoding="utf-8")

    # 실제 subprocess.run으로 프로비저닝(fresh 기본) — abort 없이 성공해야 한다.
    res = R.provision_one(ws, src, "unused-token-no-scheme")
    assert res == "reset", res

    # 트리가 클린이고 원격 최신(v2)에 정합됐다.
    status = subprocess.run(["git", "-C", ws, "status", "--porcelain"],
                            check=True, capture_output=True, text=True).stdout
    assert status == "", f"트리가 클린이 아님: {status!r}"
    assert (tmp_path / "ws" / "tracked.txt").read_text(encoding="utf-8") == "v2\n"
    assert not (tmp_path / "ws" / "cycle-log.txt").exists()  # untracked 제거됨


@pytest.mark.skipif(shutil.which("git") is None, reason="git 필요(통합 테스트)")
def test_resume_preserves_dirty_worktree_real_git(tmp_path):
    # 재개(force_clean=False): dirty 워크트리(진행 중 작업)를 보존해야 한다.
    src = str(tmp_path / "src")
    ws = str(tmp_path / "ws")
    os.makedirs(src)
    _git(src, "init", "-q")
    (tmp_path / "src" / "tracked.txt").write_text("v1\n", encoding="utf-8")
    _git(src, "add", "-A")
    _git(src, "commit", "-q", "-m", "c1")
    subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "-q", src, ws],
                   check=True, capture_output=True, text=True)

    # 진행 중 미커밋 작업(untracked in-progress 산출물).
    (tmp_path / "ws" / "in-progress.txt").write_text("resuming work\n", encoding="utf-8")

    # 재개 프로비저닝은 파괴적 정리를 하지 않으므로 in-progress 파일이 살아남는다.
    res = R.provision_one(ws, src, "unused-token-no-scheme", force_clean=False)
    assert res == "pulled", res
    assert (tmp_path / "ws" / "in-progress.txt").exists()  # 진행 중 작업 보존됨


# --- 선택 레포(docs) + 레거시 속성 이름 하위호환 ------------------------------


def test_optional_docs_repo_without_url_is_skipped_not_fatal(tmp_path):
    """설계 문서 레포는 **선택** — URL이 비면 조용히 skip하고 나머지는 정상 프로비저닝."""
    cfg = _cfg(tmp_path)
    cfg.run.docs_repo_url = ""      # 설치자가 이 레포를 쓰지 않는 경우
    runner = Runner()
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res["docs"] == "skipped: no url"
    assert res["orchestrator"] == "cloned"
    assert res["dlc_meta"] == "cloned"
    # skip된 레포에는 어떤 git 명령도 실행되지 않는다(clone 2회 = orchestrator/dlc_meta).
    clones = [c for c in runner.calls if c[:2] == ["git", "clone"]]
    assert len(clones) == 2
    assert not any(str(tmp_path / "docs") in " ".join(c) for c in runner.calls)


def test_legacy_dataspace_attr_names_still_provision(tmp_path):
    """옛 이름(dataspace_docs_repo*)만 가진 config 객체도 그대로 프로비저닝된다."""
    run = SimpleNamespace(
        orchestrator_repo=str(tmp_path / "orch"),
        dlc_meta_repo=str(tmp_path / "meta"),
        dataspace_docs_repo=str(tmp_path / "legacy-docs"),
        orchestrator_repo_url="http://gitlab.example.com/your-namespace/ai-dlc-orchestrator.git",
        dlc_meta_repo_url="http://gitlab.example.com/your-namespace/dlc-meta.git",
        dataspace_docs_repo_url="http://gitlab.example.com/your-namespace/dataspace-docs.git",
    )
    runner = Runner()
    res = R.ensure_repos(SimpleNamespace(run=run), TOKEN, runner=runner)

    # 결과 키는 신규 이름(docs)이지만, 값은 레거시 속성에서 읽혔다.
    assert res["docs"] == "cloned"
    assert any(str(tmp_path / "legacy-docs") in " ".join(c) for c in runner.calls)


def test_new_docs_attr_wins_over_legacy_attr(tmp_path):
    """신규·레거시 속성이 둘 다 있으면 신규(docs_repo*)를 쓴다."""
    run = SimpleNamespace(
        orchestrator_repo=str(tmp_path / "orch"),
        dlc_meta_repo=str(tmp_path / "meta"),
        docs_repo=str(tmp_path / "new-docs"),
        dataspace_docs_repo=str(tmp_path / "legacy-docs"),
        orchestrator_repo_url="http://gitlab.example.com/your-namespace/ai-dlc-orchestrator.git",
        dlc_meta_repo_url="http://gitlab.example.com/your-namespace/dlc-meta.git",
        docs_repo_url="http://gitlab.example.com/your-namespace/docs.git",
        dataspace_docs_repo_url="http://gitlab.example.com/your-namespace/dataspace-docs.git",
    )
    runner = Runner()
    res = R.ensure_repos(SimpleNamespace(run=run), TOKEN, runner=runner)

    assert res["docs"] == "cloned"
    joined = [" ".join(c) for c in runner.calls]
    assert any(str(tmp_path / "new-docs") in j for j in joined)
    assert not any(str(tmp_path / "legacy-docs") in j for j in joined)
