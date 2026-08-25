"""디스패치 시 미락(un-locked) 참고 레포 기계적 최신화 — stale 참고 레포 방지.

역할:
    central이 새 잡을 dispatch(running 표시)할 때, 공유 워크스페이스(jad-workspace)
    안의 **레포락이 걸리지 않은** 모든 git 레포를 원격 기본 브랜치로 강제 최신화한다.
    잡의 **작업 대상(target)** 레포는 프로비저닝(app.repos.ensure_repos·오케스트레이터)이
    최신화하지만, 잡이 단지 **참고(reference)** 하는 레포는 아무도 pull하지 않아
    stale인 채로 읽혔다 — 그래서 이미 머지된 티켓을 '없음'으로 오판하는 오설계가 났다.
    이 모듈이 그 갭을 메운다: dispatch 순간, 활성 잡이 없는(=미락) 레포를 원격에 강제
    정합해 두어, 뒤이어 잡을 집는 워커의 오케스트레이터가 **최신 참고 레포**를 본다.

왜 안전한가(hard-reset):
    레포락이 걸린 레포는 활성 잡이 **소유**하며 그 잡이 시작 시 이미 최신화했으므로
    건드리지 않는다(skip — 진행 중 WIP clobber 방지). 미락 레포는 활성 잡이 없으므로
    되돌릴 형제 WIP가 없다 → reset --hard + clean -fd 가 안전하다. 공유 볼륨을
    central·워커가 함께 mount하므로 central이 한 번 최신화하면 워커에도 최신이 된다.

제외(미락이어도 **reset --hard 최신화**하지 않는 레포):
    - **orchestrator 레포**: 실행 중 **모든** claude 프로세스의 cwd(agent_runner).
      실행 중 reset은 라이브 프로세스를 교란할 수 있고, 프레임워크 룰북은 거의 안
      바뀐다(가치 낮음) → 제외. cwd라 애초에 target_repos에 안 들어가 항상 '미락'이라
      명시 제외가 없으면 매번 리셋될 위험이 있어 반드시 배제한다.

dlc-meta 레포 — **비파괴 안전 pull**(reset 경로 아님):
    dlc-meta는 예전엔 "central 단일 라이터가 별도 관리"라며 여기서 **제외**했으나, 실제로는
    아무도 pull하지 않아(clone 이후 영구 stale) 사용자가 자기 오케스트레이터에서 dlc-meta로
    push한 **학습/진화 데이터가 서버측 central·워커에 영영 닿지 않는** 버그가 있었다(config:
    "per-user 학습 — 매 잡 pull"). 이제 dlc-meta는 :func:`_dlc_meta_slug` 로 식별해 매
    dispatch마다 최신화하되, **reset --hard/clean 대신** :func:`_safe_pull_one` 의
    ``pull --rebase --autostash`` (:func:`app.repos.safe_pull_rebase_autostash`)로 당긴다.
    dlc-meta는 central 단일 라이터의 작업 클론이라 미푸시 사이클로그 **로컬 커밋**과 진행 중
    **WIP**가 있을 수 있으므로 파괴적 정합이 그것을 clobber하기 때문이다(그 보호는 그대로
    유지하되 '건드리지 않음'을 '안전 rebase-pull'로 대체). 충돌/실패 시 비파괴로 원상복구된다.
    (orchestrator·dlc-meta 모두 ``config.run`` 경로의 basename으로 식별.)

기계적·순수 Python(에이전트 판단 없음). 실제 dispatch 시에만 돈다(유휴=0 — 빈 tick엔
전혀 호출되지 않음, 호출부 스케줄러가 게이트). best-effort: 레포별 실패는 격리·로깅하고
다른 레포·dispatch를 막지 않는다.

역할 소속: **central**.

최신화 메커니즘은 app.repos의 하드닝(MR !18)을 **재사용**한다 — 기본 브랜치를 강제
체크아웃한 뒤 :func:`app.repos.provision_one` (force_clean=True: fetch→checkout→
reset --hard FETCH_HEAD→clean -fd)를 호출한다. 새로 발명하지 않는다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable, Iterable, Optional

from app import forge
from app.repos import _strip_token, provision_one, safe_pull_rebase_autostash

log = logging.getLogger("jad.freshen")

# 기본 브랜치 이름 폴백 후보(origin/HEAD 심볼릭 ref가 없을 때).
_DEFAULT_BRANCH_FALLBACKS = ("main", "master")


def _basename(path: str) -> str:
    """워크스페이스 경로의 마지막 세그먼트(slug). posix/win 구분자 모두 관대.

    config.run 경로는 posix(``<ws>/orchestrator``)이고 실제 디렉토리는 OS별이라,
    양쪽 구분자로 잘라 마지막 비어있지 않은 세그먼트를 slug로 본다.
    """
    p = (path or "").replace("\\", "/").rstrip("/")
    return p.rsplit("/", 1)[-1] if p else ""


def _excluded_slugs(config: Any) -> set:
    """reset --hard 최신화에서 항상 제외할 slug 집합.

    ⚠️ **orchestrator만** 제외한다. dlc-meta는 더 이상 제외하지 않는다 — 예전엔 제외했으나
    아무도 pull하지 않아 사용자 학습 데이터가 서버에 닿지 않는 버그가 있었다. 이제 dlc-meta는
    :func:`_dlc_meta_slug` 로 식별해 **비파괴 안전 pull**(rebase+autostash)로 최신화한다.
    """
    run = getattr(config, "run", None)
    base = _basename(getattr(run, "orchestrator_repo", "") if run else "")
    return {base or "orchestrator"}


def _dlc_meta_slug(config: Any) -> str:
    """dlc-meta 공유 클론의 slug(``config.run.dlc_meta_repo`` basename). 폴백 ``dlc-meta``."""
    run = getattr(config, "run", None)
    base = _basename(getattr(run, "dlc_meta_repo", "") if run else "")
    return base or "dlc-meta"


def _git_out(runner: Callable, path: str, *args: str) -> Optional[str]:
    """``git -C <path> <args...>`` 실행 후 stdout(strip)을 반환. 실패 시 None(best-effort)."""
    try:
        cp = runner(["git", "-C", path, *args],
                    capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 — git 미가용/예외는 신호 없음으로 보수 처리
        return None
    if (getattr(cp, "returncode", 1) or 0) != 0:
        return None
    return (getattr(cp, "stdout", "") or "").strip()


def _origin_url(runner: Callable, path: str) -> Optional[str]:
    """레포의 origin 원격 URL(토큰 없는 형태 — clone 직후 정리됨). 없으면 None."""
    url = _git_out(runner, path, "remote", "get-url", "origin")
    return url or None


def _sanitize_origin(runner: Callable, path: str, url: str) -> str:
    """origin remote 에 임베디드 토큰이 박혀 있으면 제거해 **clean URL 로 재저장**한다(#6).

    자격증명 위생: 에이전트가 ``git clone http://oauth2:<token>@host/...`` 로 target
    레포를 공유 워크스페이스에 클론하면 그 토큰이 ``.git/config`` origin 에 영속돼, 이후
    **다른 사용자/티켓**의 워커가 같은 공유 클론에서 ``git push origin`` 하면 그 임베디드
    토큰으로 나가 MR 작성자 귀속이 오염된다(지상검증). 매 dispatch 의 freshen 이 미락
    레포의 origin 을 이렇게 clean 하게 재설정하므로, 잔재 임베디드 토큰이 **다음
    dispatch 에 자동 정리**된다(런타임 수동 정리 불필요). clean URL(또는 변경 불요 시
    원본)을 반환한다.
    """
    clean = _strip_token(url)
    if clean != url:
        _git_out(runner, path, "remote", "set-url", "origin", clean)
    return clean


def _default_branch(runner: Callable, path: str) -> Optional[str]:
    """레포의 기본 브랜치명 판정.

    우선순위: ``origin/HEAD`` 심볼릭 ref(clone 시 설정) → 로컬/원격에 존재하는
    main/master 폴백. 어느 것도 못 찾으면 None(호출부가 그 레포 skip).
    """
    ref = _git_out(runner, path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if ref:
        # 예: "origin/main" → "main". "/"가 없으면 그대로 브랜치명으로 본다.
        return ref.split("/", 1)[1] if "/" in ref else ref
    for cand in _DEFAULT_BRANCH_FALLBACKS:
        # 로컬 head 또는 원격추적 ref 어느 쪽이든 있으면 그 이름을 기본으로 채택.
        if _git_out(runner, path, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{cand}") is not None:
            return cand
        if _git_out(runner, path, "rev-parse", "--verify", "--quiet",
                    f"refs/remotes/origin/{cand}") is not None:
            return cand
    return None


def _freshen_one(
    path: str,
    forge_token: str,
    runner: Callable,
    provision: Callable,
    forge_kind: Any = None,
) -> str:
    """단일 레포를 원격 **기본 브랜치**로 강제 정합(app.repos 하드닝 재사용).

    origin URL·기본 브랜치를 판정 → 기본 브랜치를 강제 체크아웃(``checkout -f`` —
    미락 레포라 되돌릴 WIP 없음) → :func:`app.repos.provision_one` (force_clean=True)
    로 fetch→reset --hard FETCH_HEAD→clean -fd. 예외를 올리지 않고 상태 문자열 반환.
    """
    url = _origin_url(runner, path)
    if not url:
        return "skipped: no origin"
    # #6 자격증명 위생: origin 에 박힌 임베디드 토큰(타인 것일 수 있음)을 제거해 재저장한다
    # — 이후 어떤 `git push origin` 도 clean URL 을 쓰게 하고, 잔재 토큰을 자동 정리한다.
    url = _sanitize_origin(runner, path, url)
    branch = _default_branch(runner, path)
    if not branch:
        return "skipped: no default branch"
    # 기본 브랜치를 강제 체크아웃해, 이전 잡이 남긴 auto/<ticket> 등 다른 브랜치가
    # 체크아웃돼 있어도 **기본 브랜치**를 최신화하게 만든다(참고는 기본 브랜치 기준).
    # -f: 추적 변경을 버린다(미락 레포라 보존할 진행 중 작업이 없다). untracked는
    # 이어지는 provision_one의 clean -fd가 제거한다.
    _git_out(runner, path, "checkout", "-f", branch)
    # 최신화 메커니즘 재사용(force_clean=True): fetch→checkout→reset --hard→clean -fd.
    # forge_kind: 중립 호스트(GHE 등)에서 토큰 URL 자격 사용자명을 맞추기 위한 힌트.
    return provision(path, url, forge_token, runner=runner, force_clean=True,
                     forge_kind=forge_kind)


def _safe_pull_one(
    path: str,
    forge_token: str,
    runner: Callable,
    safe_pull: Callable,
    forge_kind: Any = None,
) -> str:
    """dlc-meta 전용 **비파괴** 최신화 — ``pull --rebase --autostash`` (reset/clean 금지).

    origin URL을 판정한 뒤 :func:`app.repos.safe_pull_rebase_autostash` 로 당긴다.
    ⚠️ 다른 참고 레포와 달리 **checkout -f / reset --hard / clean -fd 를 하지 않는다** —
    central 단일 라이터의 미푸시 사이클로그 커밋·진행 중 WIP를 보존해야 하기 때문이다.
    브랜치는 강제하지 않고 **현재 체크아웃 브랜치**(central 작업 브랜치=master) 그대로
    pull 한다(safe_pull이 :func:`app.repos._current_branch` 로 판정). 예외를 올리지 않고
    상태 문자열을 반환한다.
    """
    url = _origin_url(runner, path)
    if not url:
        return "skipped: no origin"
    # #6 자격증명 위생: dlc-meta origin 도 clean URL 로 유지(임베디드 토큰 제거·재저장).
    url = _sanitize_origin(runner, path, url)
    return safe_pull(path, url, forge_token, runner=runner, forge_kind=forge_kind)


def freshen_unlocked_repos(
    config: Any,
    locked_repos: Iterable[str],
    forge_token: Optional[str],
    *,
    runner: Callable = subprocess.run,
    provision: Callable = provision_one,
    safe_pull: Callable = safe_pull_rebase_autostash,
) -> dict:
    """공유 워크스페이스의 **미락** 레포를 매 dispatch마다 원격 최신으로 정합.

    ``config.run.workspace_dir`` 하위의 각 git 레포(``<ws>/<slug>/.git``)를 순회하며:
        - slug이 ``locked_repos`` 에 있으면 skip(활성 잡 소유 = 이미 최신·WIP 보호).
        - slug이 제외 집합(orchestrator)이면 skip.
        - slug이 **dlc-meta**면 :func:`_safe_pull_one` 로 **비파괴** 최신화
          (``pull --rebase --autostash`` — reset/clean 금지, 미푸시 커밋·WIP 보존).
        - 그 외(일반 참고 레포) → :func:`_freshen_one` 로 강제 정합(reset --hard+clean).
    (모두 best-effort, 레포별 실패 격리.)

    forge_token이 없으면 provision_one·safe_pull이 전체 skip한다(private fetch 불가 — 로그만).
    실제 dispatch 시에만 호출되므로(호출부 게이트) 유휴=0을 유지한다.

    Args:
        config: ``config.run.workspace_dir`` 등을 갖는 AppConfig(또는 유사 객체).
        locked_repos: 지금 레포락이 걸린 slug들(활성 잡 target_repos의 합집합).
        forge_token: central forge 토큰(private 레포 fetch용). 없으면 전체 skip.
            토큰 URL 의 자격 사용자명은 ``config.forge.kind``·URL 호스트로 분기한다
            (:mod:`app.forge`) — GitLab ``oauth2`` / GitHub ``x-access-token``.
        runner: git 실행자(테스트 주입, 기본 subprocess.run).
        provision: 일반 레포 강제 최신화자(테스트 주입, 기본 app.repos.provision_one).
        safe_pull: dlc-meta 비파괴 최신화자(테스트 주입, 기본
            app.repos.safe_pull_rebase_autostash).

    Returns:
        ``{slug: "reset"|"pulled"|"cloned"|"skipped: ..."|"err: <masked>"}`` (관측용).
        토큰·자격정보는 어떤 값에도 담기지 않는다(provision_one/safe_pull 마스킹 계승).
    """
    results: dict = {}
    run = getattr(config, "run", None)
    ws = (getattr(run, "workspace_dir", "") if run else "") or ""
    if not ws or not os.path.isdir(ws):
        return results

    locked = set(locked_repos or ())
    excluded = _excluded_slugs(config)
    dlc_meta_slug = _dlc_meta_slug(config)
    # 설정상의 forge 종류(중립 호스트일 때만 쓰인다 — URL 호스트가 밝히면 그게 우선).
    forge_kind = forge.resolve_kind(config)

    try:
        entries = sorted(os.listdir(ws))
    except OSError:
        log.warning("freshen: 워크스페이스 나열 실패(%s)", ws)
        return results

    for slug in entries:
        path = os.path.join(ws, slug)
        if not os.path.isdir(os.path.join(path, ".git")):
            continue  # git 레포가 아니면 대상 아님
        if slug in locked:
            results[slug] = "skipped: locked"
            continue
        if slug in excluded:
            results[slug] = "skipped: excluded"
            continue
        try:
            if slug == dlc_meta_slug:
                # dlc-meta는 단일 라이터의 작업 클론 — 비파괴 안전 pull(reset/clean 금지).
                status = _safe_pull_one(path, forge_token, runner, safe_pull, forge_kind)
            else:
                status = _freshen_one(path, forge_token, runner, provision, forge_kind)
        except Exception as exc:  # noqa: BLE001 — 레포별 실패 격리(다른 레포·dispatch 불방해)
            status = f"err: {type(exc).__name__}"
            log.warning("freshen: 레포 %s 최신화 실패(격리): %s", slug, type(exc).__name__)
        results[slug] = status
        log.info("freshen: 레포 %s → %s", slug, status)

    return results
