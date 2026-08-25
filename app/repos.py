"""worker 오케스트레이터 레포 프로비저닝 — clone(없으면)/pull(있으면).

역할:
    worker가 잡을 실행할 때 ``claude -p`` 의 cwd=``run.orchestrator_repo`` 에서
    오케스트레이터로 동작하려면, 아래 레포가 worker 파일시스템에 있어야 한다.
        - orchestrator_repo  프레임워크(pristine, read-only) — fresh reset/pull
        - dlc_meta_repo      인스턴스(**다중 리더·단일 라이터**) — 워커는 **읽기 전용**
        - docs_repo          설계 문서(**선택**) — fresh reset/pull
    이 모듈이 사용자 forge 토큰으로 각 레포를 clone(없으면)/reset·pull(있으면) 한다.

    ``docs_repo`` 는 **선택**이다 — URL이 비어 있으면 조용히 건너뛰고(``"skipped: no url"``)
    경고만 남긴다. 잡은 그대로 진행한다. (예전 이름 ``dataspace_docs`` 는 특정 프로젝트의
    레포 이름이었다. 옛 config 속성명도 계속 읽는다 — :data:`_REPOS` 의 속성 후보 목록.)

    ⚠️ **dlc-meta 단일 라이터(Phase 3a)**: dlc-meta의 **유일한 git 라이터는 central**
    이다(commit/push/pull은 central만). 워커는 그 공유 클론에서 룰/사용자정보를 **읽기만**
    하고, 사이클로그 파일을 작업트리에 쓰되(파일 write) **커밋/리셋/pull 하지 않는다**.
    따라서 워커 프로비저닝은 dlc-meta를 read_only로 다룬다 — 있으면 손대지 않고(central이
    신선화 담당), 없을 때만 최초 읽기 스냅샷만 clone-if-absent. (과거 MR !18의 워커측
    ``reset --hard`` 가 아직 커밋 전 사이클로그를 clobber 하던 위험을 central 단일
    라이터로 이관해 제거했다.)

역할 소속: **worker**.

forge 중립(GitLab/GitHub):
    토큰 URL 의 자격 사용자명은 forge 마다 다르다(GitLab=``oauth2`` / GitHub=
    ``x-access-token``) — GitLab 형식을 GitHub 에 쓰면 **인증이 실패한다**. 그 분기는
    :mod:`app.forge` 가 단독으로 안다. 종류는 그 URL 의 호스트가 우선 결정하고
    (한 배포가 여러 forge 를 섞어 쓴다 — 공개 프레임워크는 github.com, 사내 레포는
    사내 GitLab), 중립 호스트면 ``config.forge.kind`` → 기본값(gitlab)으로 내려간다
    (:func:`app.forge.kind_for`).

⚠️ 시크릿 규율(토큰 유출 방어):
    토큰은 clone/pull "그 순간의 인자 URL"에만 담고, 영속 config(git remote)·로그·
    예외·반환값 어디에도 남기지 않는다.
        - clone: ``https?://<자격사용자명>:<token>@host/...`` 로 클론 → 즉시
          ``git -C <path> remote set-url origin <토큰 없는 URL>`` 로 정리.
        - pull : origin(토큰 없음)이 아니라 **명시 URL**로
          ``git -C <path> pull --ff-only <토큰 URL> <branch>`` (토큰 config 미저장).
    모든 반환/로그 문자열은 :func:`_mask` 로 토큰을 마스킹한다(값 + ``<자격사용자명>:...@``
    + 임의 URL userinfo). 마스킹 대상 자격 사용자명은 :data:`app.forge.CRED_USERNAMES`
    에서 파생하므로 forge 를 추가해도 마스킹이 자동으로 함께 넓어진다.

격리:
    레포별로 독립 시도한다 — 하나가 실패해도 나머지는 시도하고, 결과는
    ``{repo_key: "cloned"|"reset"|"pulled"|"skipped: ..."|"err: ..."}`` dict로 반환한다.

테스트:
    ``runner`` 를 주입해(기본 ``subprocess.run``) 라이브 git 없이 검증한다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
from typing import Any, Callable, Optional

from app import forge

log = logging.getLogger("jad.repos")

# (결과 키, config.run 경로 속성 후보, config.run URL 속성 후보, 선택 여부)
# 속성은 **후보 튜플**이다 — 앞에서부터 값이 있는 첫 속성을 쓴다. 신규 이름(docs_repo)이
# 먼저이고 레거시 이름(dataspace_docs_repo)이 폴백이라, 옛 config 객체(구버전 AppConfig·
# 테스트 더블)가 넘어와도 그대로 동작한다(하위호환).
_REPOS = (
    ("orchestrator", ("orchestrator_repo",), ("orchestrator_repo_url",), False),
    ("dlc_meta", ("dlc_meta_repo",), ("dlc_meta_repo_url",), False),
    ("docs", ("docs_repo", "dataspace_docs_repo"),
     ("docs_repo_url", "dataspace_docs_repo_url"), True),
)

# 스킴(scheme://) 분리용.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)(.*)$")

# 출력에 섞인 자격정보 방어 마스킹.
#   1) <자격사용자명>:<token>@ → <자격사용자명>:***@ (라벨 보존, 토큰만 가림)
#      자격 사용자명 목록은 app.forge 에서 파생한다(oauth2=GitLab / x-access-token=GitHub).
#      forge 가 늘면 이 목록도 자동으로 늘어난다 → 마스킹 누락 회귀를 원천 차단.
#   2) 그 외 URL userinfo(user:pass@) → ***@ (이미 *** 포함 시 건너뜀)
_FORGE_CRED = re.compile(
    r"(" + "|".join(re.escape(u) for u in forge.CRED_USERNAMES) + r"):[^@\s/]+@",
    re.IGNORECASE,
)
_URL_CRED = re.compile(r"(https?://)(?![^@\s/]*\*\*\*)[^/@\s]+@", re.IGNORECASE)


class RepoError(Exception):
    """레포 프로비저닝 실패(메시지는 이미 마스킹된 상태로 던진다)."""


def _first_attr(obj: Any, names: tuple) -> str:
    """후보 속성 이름들 중 **값이 있는 첫 번째**를 반환(없으면 빈 문자열).

    신규 이름 → 레거시 이름 순으로 조회해 옛 config 객체와도 호환한다.
    """
    for name in names:
        val = (getattr(obj, name, "") or "") if obj is not None else ""
        if val:
            return str(val)
    return ""


def _with_token(url: str, token: str, *, forge_kind: Any = None,
                config: Any = None) -> str:
    """URL 권한부(authority) 앞에 forge 자격(``<사용자명>:<token>@``)을 주입(스킴 보존).

    자격 사용자명은 forge 마다 다르다 — GitLab ``oauth2`` / GitHub ``x-access-token``.
    **GitLab 형식을 GitHub 에 쓰면 인증이 실패**하므로 반드시 분기해야 한다. 분기 판단은
    :func:`app.forge.kind_for` 에 위임한다: URL 호스트가 스스로 밝히면 그게 이기고
    (한 배포가 여러 forge 를 섞어 쓴다 — 공개 프레임워크는 github.com, 사내 레포는 사내
    GitLab), 중립 호스트면 ``forge_kind`` → ``config.forge.kind`` → 기본 gitlab 순.

    기존 자격정보가 있으면 제거하고 재주입한다. 스킴이 없으면 원문 반환.
    """
    return forge.with_token(url, token, kind=forge_kind, config=config)


def _strip_token(url: str) -> str:
    """URL 권한부(authority)에서 임베디드 자격정보(``user[:pass]@``·``oauth2:<token>@``)를 제거.

    자격증명 위생(#6): remote ``origin`` 에 토큰이 박히면 이후 ``git push origin`` 이
    **그 임베디드 토큰**(타인 것일 수 있음)으로 나가 MR/PR 작성자 귀속이 오염된다. clone/
    set-url 로 remote 를 저장할 때는 항상 이 함수로 **토큰 없는 clean URL** 만 남긴다
    (토큰은 그 순간의 인자 URL :func:`_with_token` 에만 실어 push/fetch 한다). forge 무관
    — ``oauth2:``·``x-access-token:``·임의 ``user:pass@`` 를 모두 걷어낸다. 스킴이
    없으면 원문 반환.
    """
    m = _SCHEME.match(url or "")
    if not m:
        return url
    scheme, rest = m.group(1), m.group(2)
    if "@" in rest:  # user[:pass]@ / oauth2:<token>@ 제거
        rest = rest.split("@", 1)[1]
    return f"{scheme}{rest}"


def _mask(text: Any, token: Optional[str]) -> str:
    """텍스트에서 토큰 값과 forge 자격 패턴(``<사용자명>:...@``)을 마스킹.

    토큰 값 자체는 물론, (값이 살짝 달라진 경우에도) URL 자격 패턴을 통째로
    가려 유출을 이중 방어한다. 자격 사용자명은 forge 마다 다르므로
    (:data:`app.forge.CRED_USERNAMES`) 그 목록 전체를 가린다 — GitHub 의
    ``x-access-token:<token>@`` 도 GitLab 의 ``oauth2:<token>@`` 과 똑같이 가려진다.
    """
    out = "" if text is None else str(text)
    if not out:
        return out
    if token and len(token) >= 3:
        out = out.replace(token, "***")
    out = _FORGE_CRED.sub(r"\1:***@", out)
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


def _abort_rebase(path: str, runner: Callable) -> None:
    """진행 중 rebase를 **비파괴 중단**한다(원상복구) — best-effort(예외/실패 무시).

    ``pull --rebase --autostash`` 가 충돌 등으로 실패하면 워크트리는 rebase 진행
    상태로 남는다. ``rebase --abort`` 는 HEAD를 rebase 시작 전으로 되돌리고(=아직
    push 안 된 로컬 커밋 보존) autostash 를 복원한다(=WIP 보존). rebase가 진행 중이
    아니면(fetch 실패 등) 이 명령은 무해하게 실패하므로 반환값을 무시한다.
    """
    try:
        runner(["git", "-C", path, "rebase", "--abort"],
               capture_output=True, text=True, check=False)
    except Exception:  # noqa: BLE001 — 원상복구는 best-effort(실패해도 상위 흐름 유지)
        pass


def _flock_fd(fd: int) -> bool:
    """fd에 배타적 파일락(blocking). 성공 True. flock 미지원(Windows 등)이면 False.

    프로덕션은 Linux 컨테이너라 fcntl.flock으로 컨테이너 간(공유 볼륨 위 락파일)
    상호배제된다. fcntl이 없는 환경(예: 테스트 Windows)에서는 무락으로 진행한다
    (best-effort — 그 환경엔 동시 워커가 없으니 무해).
    """
    try:
        import fcntl  # noqa: PLC0415 — POSIX 전용, 지연 import
    except ImportError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return True
    except OSError:
        return False


@contextlib.contextmanager
def _provision_lock(path: str):
    """공유 워크스페이스에서 clone/pull을 **직렬화**하는 레포별 파일락(best-effort).

    설계 §4: 여러 워커가 같은 공유 클론(``<workspace>/<repo>``)을 per-job pull하므로,
    clone-if-absent+pull-if-present 가 **락 안에서 원자적**이어야 레이스를 막는다.
    락은 레포 경로별(``<path>.lock``)이라 서로 다른 레포는 병렬로 진행하고(읽기·서로
    다른 레포는 안전), 같은 레포만 직렬화된다. 락파일을 레포 경로 옆에 두어 공유
    볼륨 위에 놓이므로 fcntl.flock이 컨테이너 간에도 상호배제한다.

    ⚠️ 동일 타겟레포 쓰기 안전: central 스케줄러의 레포락이 같은 타겟레포 동시 잡을
    이미 직렬화하므로, 공유 타겟레포 클론이라도 한 번에 한 job만 만진다(브랜치는
    ``auto/<ticket>`` 로 per-job 격리). 이 provision 락은 그 위에서 clone/pull의
    파일시스템 레이스만 추가로 막는다.

    락을 걸 수 없는 환경(fcntl 부재·경로 생성 불가)에서는 조용히 무락으로 진행한다
    (락 실패로 프로비저닝을 막지 않는다).
    """
    lock_path = (path or "").rstrip("/\\") + ".lock"
    fd: Optional[int] = None
    try:
        parent = os.path.dirname(lock_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError:
        fd = None
    if fd is not None:
        _flock_fd(fd)
    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)  # close가 flock도 해제(프로세스 종료·close 시 자동 해제).
            except OSError:
                pass


def _ensure_one(
    path: str,
    url: str,
    token: str,
    runner: Callable,
    *,
    force_clean: bool = True,
    read_only: bool = False,
    forge_kind: Any = None,
) -> str:
    """단일 레포를 최신화. ``.git`` 존재 → (force_clean에 따라) reset/pull, 없음 → clone.

    공유 워크스페이스 레이스 방지를 위해 레포별 파일락 안에서 원자적으로 수행한다
    (:func:`_provision_lock`).

    ``read_only``(**dlc-meta 단일 라이터, Phase 3a**): 워커는 dlc-meta의 **유일한 git
    라이터가 아니다** — central만 commit/push/pull 한다. 따라서 워커 프로비저닝은
    dlc-meta를 **절대 reset/pull/commit 하지 않는다**. 이미 있으면(공유 클론이 이미
    존재) 손대지 않고 ``"present"`` 로 반환하고(central이 신선화 담당·오케스트레이터는
    읽기만), 없을 때만 최초 **읽기 스냅샷**을 clone-if-absent 로 bootstrap 한다. 이로써
    과거 MR !18의 워커측 ``reset --hard`` 가 아직 커밋 전인 사이클로그를 clobber 하던
    위험을 제거한다(그 리셋을 워커에서 걷어내 central 단일 라이터로 이관).

    ``force_clean``(**신규 잡의 기본**): 워크트리를 원격 브랜치에 **강제 정합**한다
    (``fetch`` → ``checkout`` → ``reset --hard FETCH_HEAD`` → ``clean -fd``). 공유
    워커 워크스페이스가 이전 실행의 dirty 상태(추적 변경·쌓인 untracked 사이클 로그)를
    누적해도 plain pull이 *"local changes/untracked files would be overwritten …
    Aborting"* 로 프로비저닝을 막던 문제를 제거한다. 각 잡은 클린·최신 트리에서 시작.
    (read_only 레포에는 적용하지 않는다.)

    ``force_clean=False``(**재개(resume) 경로**): 중단된 잡을 이어받을 때는 워크트리에
    진행 중 작업(미커밋 산출물)이 남아 있으므로 **파괴적 reset/clean을 하지 않는다** —
    완만한 ``pull --ff-only`` 만 시도한다(dirty면 abort하지만 상위에서 비오케스트레이터
    레포는 비치명 격리되고, 오케스트레이터 레포는 pristine이라 정상적으로 당겨진다).
    """
    with _provision_lock(path):
        # forge 자격 사용자명 분기(GitLab oauth2 / GitHub x-access-token). URL 호스트가
        # 결정적이면 그게 이기고, 중립 호스트면 forge_kind(=config.forge.kind)로 내려간다.
        token_url = _with_token(url, token, forge_kind=forge_kind)
        has_git = os.path.isdir(os.path.join(path, ".git"))
        if read_only:
            # dlc-meta: 워커는 라이터가 아니다 — 있으면 손대지 않고, 없으면 읽기 스냅샷만.
            if has_git:
                return "present"
            _run(["git", "clone", token_url, path], token, runner)
            _run(["git", "-C", path, "remote", "set-url", "origin", _strip_token(url)], token, runner)
            return "cloned"
        if has_git:
            branch = _current_branch(path, token, runner)
            if force_clean:
                # dirty 워크스페이스에서도 abort 없이 원격 최신으로 강제 정합.
                # 토큰 규율: origin(토큰 없음)이 아니라 **명시 토큰 URL**로 fetch하므로
                # origin/<branch> 원격추적 ref는 갱신되지 않는다 → FETCH_HEAD로 reset한다.
                fetch = ["git", "-C", path, "fetch", token_url]
                if branch:
                    fetch.append(branch)
                _run(fetch, token, runner)
                if branch:
                    _run(["git", "-C", path, "checkout", branch], token, runner)
                _run(["git", "-C", path, "reset", "--hard", "FETCH_HEAD"], token, runner)
                # untracked(쌓인 사이클 로그 등)도 제거해 클린 트리 보장. -fd(디렉토리 포함),
                # 무시된 빌드 캐시는 남긴다(-x 미사용 — 불필요한 재빌드 방지).
                _run(["git", "-C", path, "clean", "-fd"], token, runner)
                return "reset"
            # 재개(resume): in-progress 워크트리 보존 — 파괴적 reset/clean 금지.
            cmd = ["git", "-C", path, "pull", "--ff-only", token_url]
            if branch:
                cmd.append(branch)
            _run(cmd, token, runner)
            return "pulled"
        # clone-if-absent: 토큰 URL로 클론 → 즉시 remote를 토큰 없는 URL로 정리.
        _run(["git", "clone", token_url, path], token, runner)
        _run(["git", "-C", path, "remote", "set-url", "origin", _strip_token(url)], token, runner)
        return "cloned"


def provision_one(
    path: str,
    url: str,
    forge_token: Optional[str],
    *,
    runner: Callable = subprocess.run,
    force_clean: bool = True,
    forge_kind: Any = None,
) -> str:
    """단일 레포를 clone(없으면)/reset·pull(있으면) — 격리·마스킹(예외 안 던짐).

    :func:`ensure_repos` 와 동일한 토큰 규율·마스킹을 쓰되 **레포 하나**만 다룬다
    (central의 repo_resolver가 dlc-meta를 신선화할 때 재사용). 실패해도 예외를
    올리지 않고 ``"err: <masked>"`` 문자열로 환원한다 — best-effort 신선화용.

    ``force_clean``(기본 True): dirty 워크스페이스에서도 abort 없이 원격 최신으로 강제
    정합(:func:`_ensure_one` 참고). central의 REPO-MAP 신선화 클론은 read-only 소비라
    강제 정합이 안전하고 바람직하다.

    ``forge_kind``: 이 레포의 forge 종류 힌트(보통 ``config.forge.kind``). URL 호스트가
    스스로 밝히면 그게 우선하므로 대개 생략해도 되지만, GitHub Enterprise 처럼 호스트명이
    중립적인 self-hosted 배포에서는 이 힌트가 있어야 자격 사용자명이 맞는다.

    Returns:
        ``"cloned"|"reset"|"pulled"|"skipped: ..."|"err: <masked>"``
        (토큰·자격정보는 어떤 값에도 담기지 않는다.)
    """
    if not forge_token:
        return "skipped: no forge token"
    if not path or not url:
        return "skipped: no path" if not path else "skipped: no url"
    try:
        return _ensure_one(path, url, forge_token, runner,
                           force_clean=force_clean, forge_kind=forge_kind)
    except RepoError as exc:  # 이미 마스킹됨
        return f"err: {exc}"
    except Exception as exc:  # noqa: BLE001 — 격리(마스킹 후 문자열 환원)
        return f"err: {_mask(str(exc), forge_token)}"


# dlc-meta 안전 pull 시 rebase/stash 가 커밋을 생성·재작성하므로 필요한 커미터 정체성
# (전역 git config 부재 환경에서도 실패하지 않도록 -c 로 명시 주입 — dlc_meta_writer와 동일 사유).
DLC_META_COMMITTER_NAME = "jad-central"
DLC_META_COMMITTER_EMAIL = "jad-central@localhost"


def safe_pull_rebase_autostash(
    path: str,
    url: str,
    forge_token: Optional[str],
    *,
    branch: Optional[str] = None,
    runner: Callable = subprocess.run,
    committer_name: str = DLC_META_COMMITTER_NAME,
    committer_email: str = DLC_META_COMMITTER_EMAIL,
    forge_kind: Any = None,
) -> str:
    """dlc-meta 전용 **비파괴** 동기화 — ``pull --rebase --autostash`` (reset/clean 금지).

    dlc-meta는 다른 참고 레포와 달리 **central 단일 라이터의 작업 클론**이다: 아직 push
    되지 않은 사이클로그 **로컬 커밋**과 진행 중 **WIP**(tracked/untracked dirty)를 가질
    수 있다. 따라서 :func:`_freshen_one` 의 ``reset --hard FETCH_HEAD`` + ``clean -fd`` 로
    원격에 강제 정합하면 그 로컬 커밋·WIP를 clobber 한다(과거 read_only 가드가 막던 바로
    그 위험). 그렇다고 clone 후 아무도 pull하지 않으면 사용자가 자기 오케스트레이터에서
    dlc-meta로 push한 **학습/진화 데이터가 서버측 central·워커 에이전트에 영영 닿지 않는다**
    (config: "per-user 학습 — 매 잡 pull"). 이 함수가 그 갭을 **비파괴**로 메운다:

        - ``git -c user.*  -C <path> pull --rebase --autostash <token-url> <branch>``:
          로컬 커밋을 원격 위로 **rebase**(사용자 학습 데이터 흡수 + central 커밋 보존)하고,
          dirty WIP는 **autostash**로 stash→rebase→복원한다(진행 중 작업 보존). 정체성을
          ``-c`` 로 주입해 전역 git config 부재(CI 컨테이너)에서도 rebase/stash가
          *"Committer identity unknown"* 로 실패하지 않게 한다.
        - **reset --hard / clean -fd 를 절대 하지 않는다** — central의 미푸시 커밋을
          보존한다.
        - 실패(충돌·경합 등)면 :func:`_abort_rebase` 로 **원상복구**(로컬 커밋·WIP 보존)한
          뒤 ``"err: <masked>"`` 를 반환한다 — central의 로컬 커밋을 절대 잃지 않는다.

    브랜치는 인자로 받거나(없으면) **현재 체크아웃 브랜치**(:func:`_current_branch`)를
    쓴다 — dlc-meta는 central이 자기 작업 브랜치(``master``)에 체크아웃해 두므로 그 브랜치를
    그대로 pull 한다(강제 checkout 금지 — WIP 보존). 토큰 규율·마스킹·레포락은
    :func:`_ensure_one` 과 동일하다. 예외를 올리지 않고 상태 문자열을 반환한다(best-effort).

    Returns:
        ``"pulled"|"skipped: ..."|"err: <masked>"`` (토큰·자격정보는 담기지 않는다).
    """
    if not forge_token:
        return "skipped: no forge token"
    if not path or not url:
        return "skipped: no path" if not path else "skipped: no url"
    try:
        with _provision_lock(path):
            if not os.path.isdir(os.path.join(path, ".git")):
                # 클론 부재 = read_only 프로비저닝(_ensure_one)의 몫. 여기선 pull만 한다.
                return "skipped: absent"
            token_url = _with_token(url, forge_token, forge_kind=forge_kind)
            br = branch or _current_branch(path, forge_token, runner)
            # 정체성을 -c 로 주입(rebase/stash 가 커밋을 생성·재작성 → 정체성 필요).
            ident = ["-c", f"user.name={committer_name}",
                     "-c", f"user.email={committer_email}"]
            cmd = ["git", *ident, "-C", path,
                   "pull", "--rebase", "--autostash", token_url]
            if br:
                cmd.append(br)
            try:
                _run(cmd, forge_token, runner)
                return "pulled"
            except RepoError as exc:  # 이미 마스킹됨
                # 충돌 등 → 비파괴 원상복구(로컬 커밋·WIP 보존) 후 격리 반환.
                _abort_rebase(path, runner)
                log.warning(
                    "dlc-meta 안전 pull 실패 — rebase 중단(원상복구), 로컬 커밋 보존: %s", exc
                )
                return f"err: {exc}"
    except Exception as exc:  # noqa: BLE001 — 격리(마스킹 후 문자열 환원)
        return f"err: {_mask(str(exc), forge_token)}"


def ensure_repos(
    config: Any,
    forge_token: Optional[str],
    *,
    runner: Callable = subprocess.run,
    fresh: bool = True,
) -> dict:
    """오케스트레이터 참고 레포들을 사용자 forge 토큰으로 프로비저닝.

    각 (경로, URL) 쌍에 대해 clone(없으면)/reset·pull(있으면). 레포별 실패는 격리하고
    (하나 실패해도 나머지 시도) 결과 dict를 반환한다. 토큰이 없으면 전체 skip.

    **선택 레포**(:data:`_REPOS` 의 optional=True — 현재 ``docs``)는 URL이 비면 조용히
    건너뛴다: ``"skipped: no url"`` 을 남기고 경고 한 줄만 찍으며, 다른 레포와 잡 실행은
    그대로 진행한다. 필수 레포가 비는 것과 구분해 로그 문구도 다르게 남긴다.

    Args:
        config: ``config.run.*`` 를 갖는 AppConfig(또는 유사 객체). ``config.forge.kind``
            가 있으면 중립 호스트 레포의 자격 사용자명 판정에 쓰인다(없으면 gitlab).
        forge_token: 사용자 forge 토큰 값(없으면 전체 skip).
        runner: 명령 실행자(테스트 주입용, 기본 ``subprocess.run``).
        fresh: **신규 잡**이면 True(기본) — 워크트리를 원격에 강제 정합(reset --hard +
            clean)해 dirty 워크스페이스 abort를 제거한다. **재개(resume)**면 False —
            진행 중 미커밋 작업을 보존하려 파괴적 reset을 하지 않고 완만한 pull만 한다.

    Returns:
        ``{repo_key: "cloned"|"reset"|"pulled"|"skipped: ..."|"err: <masked>"}``.
        (토큰·자격정보는 어떤 값에도 담기지 않는다.)
    """
    results: dict = {}
    run = getattr(config, "run", None)
    # 설정상의 forge 종류(중립 호스트일 때만 쓰인다 — URL 호스트가 밝히면 그게 우선).
    forge_kind = forge.resolve_kind(config)

    if not forge_token:
        log.warning("forge 토큰 없음 — 오케스트레이터 레포 프로비저닝 전체 skip")
        for key, _path_attrs, _url_attrs, _optional in _REPOS:
            results[key] = "skipped: no forge token"
        return results

    for key, path_attrs, url_attrs, optional in _REPOS:
        path = _first_attr(run, path_attrs)
        url = _first_attr(run, url_attrs)
        if not path or not url:
            reason = "no path" if not path else "no url"
            if optional:
                log.warning("선택 레포 %s 미설정 — 프로비저닝 skip(%s). 잡은 계속 진행한다.",
                            key, reason)
            else:
                log.warning("레포 %s 프로비저닝 skip: %s", key, reason)
            results[key] = f"skipped: {reason}"
            continue
        try:
            # dlc-meta는 워커가 git 라이터가 아니다(단일 라이터 = central). 워커
            # 프로비저닝은 read_only — 있으면 손대지 않고, 없으면 읽기 스냅샷만 clone.
            results[key] = _ensure_one(
                path, url, forge_token, runner,
                force_clean=fresh, read_only=(key == "dlc_meta"),
                forge_kind=forge_kind,
            )
            log.info("레포 %s: %s", key, results[key])
        except RepoError as exc:  # 이미 마스킹됨
            results[key] = f"err: {exc}"
            log.warning("레포 %s 프로비저닝 실패: %s", key, exc)
        except Exception as exc:  # noqa: BLE001 — 격리(마스킹 후 계속)
            masked = _mask(str(exc), forge_token)
            results[key] = f"err: {masked}"
            log.warning("레포 %s 프로비저닝 실패: %s", key, masked)

    return results
