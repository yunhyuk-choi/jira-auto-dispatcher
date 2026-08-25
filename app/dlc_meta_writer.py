"""dlc-meta 단일 라이터(central 전용) — 사이클로그 커밋·push의 **유일한** git 라이터.

역할(Phase 3a):
    공유 워크스페이스 dlc-meta 클론은 **다중 리더·단일 라이터**다. 워커
    오케스트레이터들은 그 클론에서 룰/사용자정보를 **읽기만** 하고, 사이클로그
    파일을 작업트리에 **쓰되(파일 write) 커밋하지 않는다**(leave-local). central은
    잡 완료(채널 F terminal)를 받으면, 그 공유 클론에서 아직 커밋되지 않은
    사이클로그 경로만 골라 **git add <경로>(git add . 아님)** → commit → push 한다.

    central은 단일 프로세스이므로 여러 워커의 동시 완료 회신(Flask 스레드)이 겹칠 수
    있다 → **인프로세스 락으로 커밋/push를 직렬화**하고, non-fast-forward(원격 선행)면
    ``pull --rebase`` 후 재시도(pull-rebase-retry)한다.

역할 소속: **central**.

⚠️ 시크릿 규율(토큰 유출 방어):
    push/pull은 :func:`app.repos._with_token` 로 **그 순간의 인자 URL**에만 토큰을
    담고, 영속 config(remote)·로그·예외·반환값 어디에도 남기지 않는다. 모든 반환/
    로그 문자열은 :func:`app.repos._mask` 로 토큰을 마스킹한다.

⚠️ 공유 클론 정합:
    커밋은 오케스트레이터가 dlc-meta에서 **다른 브랜치를 체크아웃하지 않는다**는
    전제(워커는 target 레포에서만 브랜치를 판다) 위에서 현재 HEAD(=기본 브랜치)에
    커밋하고 ``HEAD:<branch>`` 로 push한다. add 대상은 ``git status --porcelain`` 이
    보고하는 **실제 변경 경로만**(gitignore 무시분은 애초에 안 보임) 명시 pathspec으로
    스테이징한다 → 이후 central 리프레시의 ``reset --hard``/``clean`` 이 남은 throwaway
    잔재를 쓸어도 **진짜 사이클로그는 이미 커밋**돼 안전하다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any, Callable, List, Optional

from app.config import central_forge_token_ref, read_secret
from app.repos import _mask, _with_token

log = logging.getLogger("jad.dlc_meta_writer")

# 커밋 저자/커미터 정체성(central = 단일 라이터). git 전역 config에 의존하지 않도록
# 커밋 시 -c 로 명시 주입한다(컨테이너에 전역 user.* 가 없을 수 있음).
DEFAULT_COMMITTER_NAME = "jad-central"
DEFAULT_COMMITTER_EMAIL = "jad-central@localhost"

# push non-fast-forward(원격 선행) 시 pull --rebase 후 재시도 횟수.
PUSH_RETRY_ATTEMPTS = 3


def _default_git_run(cmd, **kwargs):
    """기본 git 실행기 — **UTF-8 강제**(POLICY-ENCODING: 로케일 의존 셸 출력 금지).

    ⚠️ Windows에서 subprocess가 기본 로케일 코덱(예: cp949)으로 git 출력을 디코드하면
    커밋 subject/경로의 한글(UTF-8)에서 UnicodeDecodeError가 난다. encoding='utf-8',
    errors='replace'를 명시해 OS 로케일과 무관하게 안전히 디코드한다.
    """
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return subprocess.run(cmd, **kwargs)  # noqa: S603


def _porcelain_paths(text: str) -> List[str]:
    """``git status --porcelain`` 출력 → 변경 경로 목록(순서 보존·중복 제거).

    각 줄은 ``XY <path>`` (상태 2자 + 공백 + 경로). ``??`` 는 untracked, ``R`` 은
    rename(``old -> new``)이다. rename은 새 경로를 취한다. gitignore 무시분은 애초에
    porcelain에 나오지 않으므로(=진짜 산출물만) 별도 필터가 불필요하다.
    """
    paths: List[str] = []
    seen: set = set()
    for raw in (text or "").splitlines():
        if len(raw) < 4:
            continue
        # 상태코드 2자 + 공백 1자 이후가 경로.
        payload = raw[3:].strip()
        if not payload:
            continue
        if " -> " in payload:  # rename: old -> new → new만.
            payload = payload.split(" -> ", 1)[1].strip()
        # core.quotepath=false 로 실행하므로 대개 따옴표가 없지만, 있으면 벗긴다.
        if len(payload) >= 2 and payload[0] == '"' and payload[-1] == '"':
            payload = payload[1:-1]
        if payload and payload not in seen:
            seen.add(payload)
            paths.append(payload)
    return paths


def _primary_relpath(paths: List[str]) -> Optional[str]:
    """커밋한 경로들에서 알림에 실을 **대표 사이클로그 경로**(dlc-meta 루트 상대)를 고른다.

    프레임워크 관례상 사이클로그는 ``<audit>/cycles/<cycle-id>/...`` 에 놓인다.
    ``/cycles/`` 세그먼트가 있으면 그 **사이클 디렉토리**(``.../cycles/<cycle-id>``)를
    돌려주고(리뷰어가 폴더를 바로 열도록), 없으면 결정적으로 첫(정렬) 경로를 돌려준다.
    """
    if not paths:
        return None
    for p in paths:
        norm = p.replace("\\", "/")
        marker = "/cycles/"
        idx = norm.find(marker)
        if idx == -1 and norm.startswith("cycles/"):
            idx = -len(marker) + len("/")  # 루트 바로 아래 cycles/
        if idx != -1:
            after = norm[idx + len(marker):] if idx >= 0 else norm[len("cycles/"):]
            cycle_id = after.split("/", 1)[0]
            base = norm[:idx] if idx >= 0 else ""
            prefix = (base + marker).lstrip("/") if idx >= 0 else "cycles/"
            return (prefix + cycle_id).rstrip("/")
    return sorted(paths)[0]


class DlcMetaWriter:
    """공유 dlc-meta 클론의 **단일 git 라이터**(central). 사이클로그 커밋·push 직렬화."""

    def __init__(
        self,
        config: Any,
        *,
        git_run: Callable = _default_git_run,
        committer_name: str = DEFAULT_COMMITTER_NAME,
        committer_email: str = DEFAULT_COMMITTER_EMAIL,
    ) -> None:
        self.config = config
        self._git_run = git_run
        self._committer_name = committer_name or DEFAULT_COMMITTER_NAME
        self._committer_email = committer_email or DEFAULT_COMMITTER_EMAIL
        # 인프로세스 직렬화: 여러 워커의 동시 완료 회신이 겹쳐도 커밋/push는 한 번에 하나.
        self._lock = threading.Lock()

    # -- config 파생 --

    def _run_cfg(self) -> Any:
        return getattr(self.config, "run", None)

    def _repo_path(self) -> str:
        """공유 dlc-meta 클론 경로(repo_map_path → dlc_meta_repo → <workspace>/dlc-meta)."""
        run = self._run_cfg()
        if run is None:
            return ""
        for attr in ("repo_map_path", "dlc_meta_repo"):
            val = getattr(run, attr, "") or ""
            if val:
                return val
        ws = getattr(run, "workspace_dir", "") or ""
        return os.path.join(ws, "dlc-meta") if ws else ""

    def _repo_url(self) -> str:
        run = self._run_cfg()
        return (getattr(run, "dlc_meta_repo_url", "") if run else "") or ""

    def _branch(self) -> str:
        run = self._run_cfg()
        return (getattr(run, "dlc_meta_branch", "") if run else "") or "master"

    def _token(self) -> Optional[str]:
        """central forge 토큰(중립 접근자 — 신규 forge.token_ref / 레거시 run.* 모두 수용)."""
        ref = central_forge_token_ref(self.config)
        if not ref:
            return None
        base_dir = getattr(getattr(self.config, "secrets", None), "base_dir", "") or ""
        return read_secret(base_dir, ref)

    # -- git 실행 --

    def _git(self, path: str, args: List[str]):
        """``git -C <path> <args>`` 실행(check=False). 예외를 올리지 않는다(호출부 격리).

        ⚠️ **커미터 정체성을 env로 주입(모든 명령)**: commit뿐 아니라 ``pull --rebase
        --autostash`` 도 커밋을 **재작성**(rebase가 committer 갱신)하고 **stash 커밋을
        생성**(autostash)하므로 정체성이 필요하다. 전역 git 정체성이 없는 환경(예: CI
        컨테이너)에서 rebase가 *"Committer identity unknown"* 로 실패하면 재push가 막혀
        사이클로그가 유실될 수 있다. GIT_AUTHOR_*/GIT_COMMITTER_* 를 env로 실어 **모든**
        writer git 명령이 정체성을 갖게 한다(전역 config 부재와 무관).
        """
        cmd = ["git", "-C", path, *args]
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = self._committer_name
        env["GIT_AUTHOR_EMAIL"] = self._committer_email
        env["GIT_COMMITTER_NAME"] = self._committer_name
        env["GIT_COMMITTER_EMAIL"] = self._committer_email
        return self._git_run(cmd, capture_output=True, text=True, check=False, env=env)

    @staticmethod
    def _rc(cp) -> int:
        return getattr(cp, "returncode", 0) or 0

    # -- 공개 API --

    def commit_cycle_log(self, job: Any) -> Optional[str]:
        """잡 완료 시 공유 dlc-meta 클론의 미커밋 사이클로그를 커밋·push한다(best-effort).

        절차(락 안에서 직렬화):
            1. ``git status --porcelain`` 으로 **실제 변경 경로만** 열거(없으면 no-op).
            2. 그 경로들만 명시 pathspec으로 ``git add -- <경로>``(``git add .`` 금지).
            3. central 정체성으로 commit(비어 있으면 스킵).
            4. ``HEAD:<branch>`` push. non-fast-forward면 ``pull --rebase`` 후 재시도.

        Returns:
            커밋한 대표 사이클로그 경로(dlc-meta 루트 상대) 또는 None(커밋할 것 없음/실패).
            토큰·자격정보는 어떤 반환/로그에도 담기지 않는다.
        """
        path = self._repo_path()
        if not path or not os.path.isdir(os.path.join(path, ".git")):
            return None
        token = self._token()
        with self._lock:
            try:
                return self._commit_locked(path, token, job)
            except Exception as exc:  # noqa: BLE001 — 커밋 실패가 완료 회신을 막지 않는다
                log.warning("dlc-meta 사이클로그 커밋 실패(격리): %s",
                            _mask(str(exc), token))
                return None

    def _commit_locked(self, path: str, token: Optional[str], job: Any) -> Optional[str]:
        # -uall: untracked 디렉토리를 **개별 파일**로 펼친다(기본은 ``n/`` 처럼 디렉토리로
        # 접혀 사이클로그 파일 경로를 못 얻는다). core.quotepath=false: 한글 경로 그대로.
        status = self._git(
            path, ["-c", "core.quotepath=false", "status", "--porcelain", "-uall"]
        )
        if self._rc(status) != 0:
            log.warning("dlc-meta status 실패 — 커밋 생략")
            return None
        changed = _porcelain_paths(getattr(status, "stdout", "") or "")
        if not changed:
            return None  # 커밋할 사이클로그 없음(no-op).

        # ⚠️ git add . 가 아니라 **열거한 경로만** 명시 스테이징(throwaway 잔재 배제).
        add = self._git(path, ["add", "--", *changed])
        if self._rc(add) != 0:
            log.warning("dlc-meta add 실패 — 커밋 생략")
            return None

        ticket = str(_job_get(job, "ticket", "") or "")
        msg = f"chore(dlc-meta): 사이클로그 {ticket}".strip()
        commit = self._git(
            path,
            ["-c", f"user.name={self._committer_name}",
             "-c", f"user.email={self._committer_email}",
             "commit", "-m", msg],
        )
        if self._rc(commit) != 0:
            # 스테이지가 비었거나(경합으로 이미 커밋됨) 기타 → 커밋 없음. push는 시도하지 않는다.
            log.info("dlc-meta commit no-op(스테이지 비었을 수 있음): %s", ticket)
            return None

        pushed = self._push_with_retry(path, token)
        if not pushed:
            log.warning("dlc-meta push 실패(커밋은 로컬에 남음) — 다음 완료 시 재시도됨")
        return _primary_relpath(changed)

    def _push_with_retry(self, path: str, token: Optional[str]) -> bool:
        """``HEAD:<branch>`` push. non-fast-forward면 ``pull --rebase --autostash`` 후 재시도.

        원격 URL은 그 순간의 토큰 URL로만 만들고(:func:`_with_token`) config에 저장하지
        않는다. 실패해도 예외를 올리지 않고 bool을 돌려준다(best-effort).

        ⚠️ **--autostash 필수(동시성 정확성)**: 공유 dlc-meta 클론은 여러 오케스트레이터가
        동시에 쓴다 → central이 자기 사이클로그만 커밋한 뒤에도 워크트리에 **다른 잡의
        미커밋 변경**(예: 진행 중인 ``ORCHESTRATOR.md`` 수정 = tracked 변경)이 남아 있을 수
        있다. 이때 plain ``pull --rebase`` 는 *"cannot pull with rebase: You have unstaged
        changes"* 로 **하드 실패** → 재push가 막혀 사이클로그가 원격에 안 올라간다(유실 위험).
        ``--autostash`` 는 그 dirty 변경을 rebase 전 stash → rebase → 복원해, 남의 진행 중
        작업을 **보존한 채** 안전하게 rebase·push 한다(파괴적 reset/clean로 clobber하지 않음).
        """
        url = self._repo_url()
        branch = self._branch()
        # forge 자격 사용자명 분기(GitLab oauth2 / GitHub x-access-token) — URL 호스트가
        # 결정적이면 그게 이기고, 중립 호스트면 config.forge.kind 로 내려간다.
        token_url = (_with_token(url, token, config=self.config)
                     if (url and token) else (url or "origin"))
        refspec = f"HEAD:{branch}"
        for attempt in range(max(1, PUSH_RETRY_ATTEMPTS)):
            push = self._git(path, ["push", token_url, refspec])
            if self._rc(push) == 0:
                return True
            detail = _mask(
                (getattr(push, "stderr", "") or getattr(push, "stdout", "") or ""), token
            )
            log.info("dlc-meta push 재시도 %d/%d (원격 선행 추정) — pull --rebase --autostash 선행",
                     attempt + 1, PUSH_RETRY_ATTEMPTS)
            log.debug("dlc-meta push 실패 detail(마스킹): %s", detail[:200])
            # non-fast-forward 등 → 원격 최신을 rebase로 흡수 후 재시도.
            # --autostash: 공유 워크트리의 dirty(다른 잡 진행분)를 stash→rebase→복원(하드 실패 방지).
            self._git(path, ["pull", "--rebase", "--autostash", token_url, branch])
        # 마지막 재시도.
        final = self._git(path, ["push", token_url, refspec])
        return self._rc(final) == 0


def _job_get(job: Any, name: str, default: Any = None) -> Any:
    """job(dict 또는 객체)에서 필드 접근(dispatch는 Job 객체, 테스트는 dict/객체)."""
    if isinstance(job, dict):
        return job.get(name, default)
    return getattr(job, name, default)
