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
        dataspace_docs_repo=str(tmp_path / "docs"),
        orchestrator_repo_url=(
            "http://server.interxlab.io:30000/yhchoi/ai-dlc-orchestrator.git"
            if with_urls else ""
        ),
        dlc_meta_repo_url=(
            "http://server.interxlab.io:30000/hansa/docs/dlc-meta.git" if with_urls else ""
        ),
        dataspace_docs_repo_url=(
            "http://server.interxlab.io:30000/hansa/dataspace_docs.git" if with_urls else ""
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

    assert res == {"orchestrator": "cloned", "dlc_meta": "cloned", "dataspace_docs": "cloned"}

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
        assert clean.startswith("http://server.interxlab.io") and "oauth2" not in clean
        _assert_no_token(clean)

    # 결과 dict엔 토큰이 없다.
    for v in res.values():
        _assert_no_token(v)


# --- pull-if-present ---------------------------------------------------------


def test_pull_when_present_uses_explicit_token_url(tmp_path):
    cfg = _cfg(tmp_path)
    for name in ("orch", "meta", "docs"):
        os.makedirs(tmp_path / name / ".git")
    runner = Runner(branch="main")
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res == {"orchestrator": "pulled", "dlc_meta": "pulled", "dataspace_docs": "pulled"}

    pulls = [c for c in runner.calls if "pull" in c]
    assert len(pulls) == 3
    for c in pulls:
        assert "--ff-only" in c
        # 명시 토큰 URL로 pull(원격 config엔 저장 안 됨) + 브랜치.
        assert any(str(a).startswith("http://oauth2:") and TOKEN in str(a) for a in c)
        assert c[-1] == "main"
    # clone/set-url은 일어나지 않는다.
    assert not any(c[:2] == ["git", "clone"] for c in runner.calls)
    for v in res.values():
        _assert_no_token(v)


# --- 실패 격리 + 마스킹 ------------------------------------------------------


def test_per_repo_failure_is_isolated_and_masked(tmp_path):
    cfg = _cfg(tmp_path)
    # dlc-meta 경로가 걸린 명령만 실패시킨다(다른 레포는 성공).
    runner = Runner(fail_substr=(str(tmp_path / "meta"),))
    res = R.ensure_repos(cfg, TOKEN, runner=runner)

    assert res["orchestrator"] == "cloned"
    assert res["dataspace_docs"] == "cloned"
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
        "orchestrator": "skipped: no gitlab token",
        "dlc_meta": "skipped: no gitlab token",
        "dataspace_docs": "skipped: no gitlab token",
    }
    assert runner.calls == []  # 라이브 git 절대 미호출


def test_empty_token_string_skips_all(tmp_path):
    cfg = _cfg(tmp_path)
    runner = Runner()
    res = R.ensure_repos(cfg, "", runner=runner)
    assert all(v == "skipped: no gitlab token" for v in res.values())
    assert runner.calls == []


# --- URL 비면 그 레포만 skip -------------------------------------------------


def test_missing_url_skips_that_repo(tmp_path):
    cfg = _cfg(tmp_path, with_urls=False)
    runner = Runner()
    res = R.ensure_repos(cfg, TOKEN, runner=runner)
    assert all(v == "skipped: no url" for v in res.values())
    assert runner.calls == []


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


def test_mask_hides_token_value_and_cred_url():
    assert R._mask(f"x http://oauth2:{TOKEN}@h y", TOKEN) == "x http://oauth2:***@h y"
    # 토큰 값이 없어도 oauth2 자격 패턴은 가린다.
    assert R._mask("http://oauth2:whatever@h", None) == "http://oauth2:***@h"
    assert R._mask("", TOKEN) == ""
