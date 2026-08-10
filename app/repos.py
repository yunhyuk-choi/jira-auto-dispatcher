"""worker 오케스트레이터 레포 프로비저닝 — clone(없으면)/pull(있으면).

역할:
    worker가 잡을 실행할 때 ``claude -p`` 의 cwd=``run.orchestrator_repo`` 에서
    오케스트레이터로 동작하려면, 아래 3개 레포가 worker 파일시스템에 있어야 한다.
        - orchestrator_repo   프레임워크(pristine, read-only)
        - dlc_meta_repo       인스턴스(per-user 학습 — 매 잡 pull로 최신화 필수)
        - dataspace_docs_repo 설계 문서
    이 모듈이 사용자 GitLab 토큰으로 각 레포를 clone(없으면)/pull(있으면) 한다.
    "매 실행 pull" 설계 — dlc-meta는 per-user 학습이 갱신되므로 매 잡마다 최신화.

역할 소속: **worker**.

⚠️ 시크릿 규율(토큰 유출 방어):
    토큰은 clone/pull "그 순간의 인자 URL"에만 담고, 영속 config(git remote)·로그·
    예외·반환값 어디에도 남기지 않는다.
        - clone: ``https?://oauth2:<token>@host/...`` 로 클론 → 즉시
          ``git -C <path> remote set-url origin <토큰 없는 URL>`` 로 정리.
        - pull : origin(토큰 없음)이 아니라 **명시 URL**로
          ``git -C <path> pull --ff-only <토큰 URL> <branch>`` (토큰 config 미저장).
    모든 반환/로그 문자열은 :func:`_mask` 로 토큰을 마스킹한다(값 + ``oauth2:...@``).

격리:
    레포별로 독립 시도한다 — 하나가 실패해도 나머지는 시도하고, 결과는
    ``{repo_key: "cloned"|"pulled"|"skipped: ..."|"err: ..."}`` dict로 반환한다.

테스트:
    ``runner`` 를 주입해(기본 ``subprocess.run``) 라이브 git 없이 검증한다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from typing import Any, Callable, Optional

log = logging.getLogger("jad.repos")

# (결과 키, config.run 경로 속성, config.run URL 속성)
_REPOS = (
    ("orchestrator", "orchestrator_repo", "orchestrator_repo_url"),
    ("dlc_meta", "dlc_meta_repo", "dlc_meta_repo_url"),
    ("dataspace_docs", "dataspace_docs_repo", "dataspace_docs_repo_url"),
)

# 스킴(scheme://) 분리용.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)(.*)$")

# 출력에 섞인 자격정보 방어 마스킹.
#   1) oauth2:<token>@  → oauth2:***@ (라벨 보존, 토큰만 가림)
#   2) 그 외 URL userinfo(user:pass@) → ***@ (이미 *** 포함 시 건너뜀)
_OAUTH_CRED = re.compile(r"(oauth2:)[^@\s/]+@", re.IGNORECASE)
_URL_CRED = re.compile(r"(https?://)(?![^@\s/]*\*\*\*)[^/@\s]+@", re.IGNORECASE)


class RepoError(Exception):
    """레포 프로비저닝 실패(메시지는 이미 마스킹된 상태로 던진다)."""


def _with_token(url: str, token: str) -> str:
    """URL 권한부(authority) 앞에 ``oauth2:<token>@`` 를 주입(스킴 보존).

    기존 자격정보가 있으면 제거하고 재주입한다. 스킴이 없으면 원문 반환.
    """
    m = _SCHEME.match(url or "")
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    if "@" in rest:  # 기존 user[:pass]@ 제거
        rest = rest.split("@", 1)[1]
    return f"{scheme}oauth2:{token}@{rest}"


def _mask(text: Any, token: Optional[str]) -> str:
    """텍스트에서 토큰 값과 ``oauth2:...@`` 자격 패턴을 마스킹.

    토큰 값 자체는 물론, (값이 살짝 달라진 경우에도) URL 자격 패턴을 통째로
    가려 유출을 이중 방어한다.
    """
    out = "" if text is None else str(text)
    if not out:
        return out
    if token and len(token) >= 3:
        out = out.replace(token, "***")
    out = _OAUTH_CRED.sub(r"\1***@", out)
    out = _URL_CRED.sub(r"\1***@", out)
    return out


def _run(cmd: list, token: Optional[str], runner: Callable) -> Any:
    """git 명령 실행(check=False). 실패 시 마스킹된 :class:`RepoError`.

    ``cmd`` 에는 토큰 URL이 담길 수 있으므로 **절대 로그/예외에 원문을 싣지
    않는다** — 예외 메시지는 stderr/stdout을 마스킹해 구성한다.
    """
    cp = runner(cmd, capture_output=True, text=True, check=False)
    rc = getattr(cp, "returncode", 0) or 0
    if rc != 0:
        stderr = getattr(cp, "stderr", "") or ""
        stdout = getattr(cp, "stdout", "") or ""
        detail = (stderr.strip() or stdout.strip() or "").strip()
        raise RepoError(_mask(f"git rc={rc}: {detail}", token))
    return cp


def _current_branch(path: str, token: Optional[str], runner: Callable) -> Optional[str]:
    """현재 체크아웃 브랜치(실패/detached면 None) — best-effort."""
    try:
        cp = runner(
            ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except Exception:  # noqa: BLE001 — 브랜치 미확정은 치명적 아님(refspec 생략)
        return None
    if (getattr(cp, "returncode", 0) or 0) != 0:
        return None
    out = (getattr(cp, "stdout", "") or "").strip()
    return out if out and out != "HEAD" else None


def _ensure_one(path: str, url: str, token: str, runner: Callable) -> str:
    """단일 레포를 최신화. ``.git`` 존재 → pull, 없음 → clone."""
    token_url = _with_token(url, token)
    if os.path.isdir(os.path.join(path, ".git")):
        # pull-if-present: 명시 URL(토큰)로 당기고 config엔 토큰을 남기지 않는다.
        cmd = ["git", "-C", path, "pull", "--ff-only", token_url]
        branch = _current_branch(path, token, runner)
        if branch:
            cmd.append(branch)
        _run(cmd, token, runner)
        return "pulled"
    # clone-if-absent: 토큰 URL로 클론 → 즉시 remote를 토큰 없는 URL로 정리.
    _run(["git", "clone", token_url, path], token, runner)
    _run(["git", "-C", path, "remote", "set-url", "origin", url], token, runner)
    return "cloned"


def ensure_repos(
    config: Any,
    gitlab_token: Optional[str],
    *,
    runner: Callable = subprocess.run,
) -> dict:
    """3개 오케스트레이터 레포를 사용자 GitLab 토큰으로 프로비저닝.

    각 (경로, URL) 쌍에 대해 clone(없으면)/pull(있으면). 레포별 실패는 격리하고
    (하나 실패해도 나머지 시도) 결과 dict를 반환한다. 토큰이 없으면 전체 skip.

    Args:
        config: ``config.run.*`` 를 갖는 AppConfig(또는 유사 객체).
        gitlab_token: 사용자 GitLab 토큰 값(없으면 전체 skip).
        runner: 명령 실행자(테스트 주입용, 기본 ``subprocess.run``).

    Returns:
        ``{repo_key: "cloned"|"pulled"|"skipped: ..."|"err: <masked>"}``.
        (토큰·자격정보는 어떤 값에도 담기지 않는다.)
    """
    results: dict = {}
    run = getattr(config, "run", None)

    if not gitlab_token:
        log.warning("GitLab 토큰 없음 — 오케스트레이터 레포 프로비저닝 전체 skip")
        for key, _path_attr, _url_attr in _REPOS:
            results[key] = "skipped: no gitlab token"
        return results

    for key, path_attr, url_attr in _REPOS:
        path = (getattr(run, path_attr, "") if run else "") or ""
        url = (getattr(run, url_attr, "") if run else "") or ""
        if not path or not url:
            reason = "no path" if not path else "no url"
            log.warning("레포 %s 프로비저닝 skip: %s", key, reason)
            results[key] = f"skipped: {reason}"
            continue
        try:
            results[key] = _ensure_one(path, url, gitlab_token, runner)
            log.info("레포 %s: %s", key, results[key])
        except RepoError as exc:  # 이미 마스킹됨
            results[key] = f"err: {exc}"
            log.warning("레포 %s 프로비저닝 실패: %s", key, exc)
        except Exception as exc:  # noqa: BLE001 — 격리(마스킹 후 계속)
            masked = _mask(str(exc), gitlab_token)
            results[key] = f"err: {masked}"
            log.warning("레포 %s 프로비저닝 실패: %s", key, masked)

    return results
