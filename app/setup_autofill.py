"""설치 **자동 채움** — 사람에게 물을 이유가 없는 값을 기계가 채운다.

역할:
    :mod:`app.setup_schema` 가 "무엇을 묻는가"를 선언하고 :mod:`app.setup_validate` 가
    "그 답이 맞는가"를 강제한다면, 이 모듈은 그 앞자리에서 **애초에 묻지 않아도 되는
    항목을 답으로 만들어** 준다. 설치자가 손으로 옮겨 적는 값은 그 자체가 오설정의
    원천이고(이 리포에서 가장 자주 재발한 실패 모드), 아래 값은 사람이 정할 근거가
    전혀 없다:

    - ``run.dlc_meta_repo_url`` — 이 시스템의 설치는 ``ai-dlc-orchestrator`` 프레임워크의
      SETTER 가 dlc-meta 레포를 만들어 원격에 push 한 **직후**에 이어진다. 즉 설치 시점에
      그 클론이 이미 로컬에 있고, 원격 URL 은 ``git -C <클론> remote get-url origin``
      한 줄이면 알 수 있다. 예전에는 이 값을 채우지 않으면 예시 파일의
      ``https://gitlab.example.com/<your-group>/dlc-meta.git`` 이 **그대로 남았다** —
      비어 있는 것도 아니라 형식상 유효해 보여 눈으로 넘어간다.

    이 값은 시크릿이 아니라 설정이다 → 답변에 채워 ``config.yaml`` 로 간다.

⚠️ 예전에 여기 있던 ``WORKER_SHARED_SECRET`` 생성(``ensure_worker_shared_secret``)은
    **없어졌다.** 그 값은 워커가 중앙의 dispatch HTTP 를 부르던 시절의
    ``X-Worker-Secret`` 인증용이었는데, 그 서빙 표면과 폴링 소비자가 프랙탈 seam
    (중앙 → ``docker exec`` 푸시)으로 대체되며 읽는 곳이 하나도 남지 않았다. 설치자가
    **왜 만드는지 모르는 시크릿을 만들게 하지 않는다** — 그것이 이 모듈의 목적과 정반대다.

의존성 주입:
    git 호출(``runner``)·환경(``env``)은 전부 주입 가능하다 — CI(ubuntu, 네트워크 없음)
    에서 대역만으로 전 경로를 테스트한다.

POLICY-ENCODING: 이 파일과 이 모듈이 만드는 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

from app import forge as forge_mod

#: ``git remote get-url`` 타임아웃(초). 로컬 조회라 길 이유가 없다.
GIT_TIMEOUT_SEC = 10

#: dlc-meta 클론을 찾을 때 쓰는 디렉토리 이름(SETTER 산출물의 관례 이름).
DLC_META_DIRNAME = "dlc-meta"

#: 명시 경로 대신 볼 수 있는 env(컨테이너/CI 배선용).
DLC_META_ENV = "DLC_META_DIR"

#: ``<your-group>`` 같은 예시 자리표시자(:mod:`app.setup_validate` 와 같은 정의).
_PLACEHOLDER = re.compile(r"<[^<>\s][^<>]*>")

#: ``scheme://나머지``.
_SCHEME = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)(.*)$")

#: scp 형식(``git@host:group/repo.git``). ⚠️ ``@`` 를 **필수**로 본다 — 그러지 않으면
#: 윈도우 경로(``C:\\work``)가 scp 로 오인된다.
_SCP = re.compile(r"^(?P<user>[^@/\\]+)@(?P<host>[^:/\\]+):(?P<path>[^\\].*)$")

#: SSH → https 변환 안내(조용한 변환 금지 — 사람이 알고 넘어가야 한다).
_SSH_NOTE = (
    "SSH 원격을 https 로 바꿔 적었습니다 — central 은 SSH 키가 아니라 forge 토큰을 "
    "http(s) URL 에 실어 인증합니다(app/forge.with_token 은 스킴이 없으면 아무것도 하지 "
    "않습니다). 이 호스트가 https 를 서빙하지 않으면 직접 고치세요."
)


# ---------------------------------------------------------------------------
# 결과 자료구조
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Filled:
    """자동으로 채운 답변 항목 하나.

    Attributes:
        key: 채운 **점 표기 키 경로**(``run.dlc_meta_repo_url`` 등).
        value: 채운 값. ⚠️ 시크릿이 아닌 값만 여기 담는다(이 모듈은 시크릿을 답변에
            채우지 않는다 — 시크릿은 ``.env`` 로 간다).
        source: 근거(``git-remote`` · ``inferred``).
        detail: 사람이 읽는 근거 한 줄.
    """

    key: str
    value: Any
    source: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"key": self.key, "value": self.value,
                "source": self.source, "detail": self.detail}


@dataclass
class AutofillReport:
    """자동 채움 결과.

    Attributes:
        answers: 채움이 반영된 **평탄화된** 답변(원본은 건드리지 않는다).
        filled: 채운 항목들.
        notes: 사람이 읽어야 할 안내·경고(못 찾음·자격정보 제거·URL 변환 등).
    """

    answers: dict = field(default_factory=dict)
    filled: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """기계가 읽는 요약."""
        return {"filled": [f.to_dict() for f in self.filled], "notes": list(self.notes)}

    def format_text(self) -> str:
        """사람이 읽는 요약(빈 결과면 빈 문자열 — 출력에 소음을 더하지 않는다)."""
        lines: list = []
        for f in self.filled:
            lines.append(f"자동 채움: {f.key} = {f.value} ({f.detail or f.source})")
        for note in self.notes:
            lines.append(f"  ⚠️ {note}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# dlc-meta 원격 URL
# ---------------------------------------------------------------------------


def sanitize_repo_url(url: str) -> tuple:
    """clone URL 을 **설정에 적어도 되는 형태**로 정규화 → ``(URL, 안내 목록)``.

    로컬 클론의 origin 은 그대로 쓰기 곤란한 모양일 수 있다:

        - ``https://oauth2:glpat-xxx@gitlab.example.com/g/dlc-meta.git``
          — :func:`app.forge.with_token` 이 주입한 **토큰이 박힌 URL**. 이걸 그대로
          config.yaml 에 쓰면 이 리포의 제1 규율(값이 아니라 참조)을 정면으로 깬다.
          → 자격정보를 제거한다.
        - ``git@gitlab.example.com:g/dlc-meta.git`` / ``ssh://git@host:22/g/dlc-meta.git``
          — SSH 경로. central 은 SSH 키가 아니라 **PAT 를 http(s) URL 에 실어** 인증하므로
          (:func:`app.forge.with_token` 은 스킴이 없으면 아무것도 하지 않는다) 그대로
          두면 dlc-meta pull/push 가 조용히 인증 없이 시도된다. 게다가 forge base_url
          유도도 실패한다(:data:`app.forge.SOURCE_UNRESOLVED`).
          → https 로 바꾸고 **바꿨다는 사실을 안내한다**(조용한 변환 금지).

    Returns:
        ``(정규화된 URL, [안내 문자열])``. 빈 입력이면 ``("", [])``.
    """
    raw = str(url or "").strip()
    if not raw:
        return "", []
    notes: list = []

    m = _SCHEME.match(raw)
    if m:
        scheme, rest = m.group(1), m.group(2)
        if scheme.lower().startswith("ssh"):
            # ssh 의 ``git@`` 은 자격정보가 아니라 원격 계정이고, 포트도 https 에선
            # 의미가 없다 — 변환하면서 함께 떨군다(자격정보 경고를 내지 않는다).
            authority, _, path = rest.partition("/")
            host = authority.split("@")[-1].split(":", 1)[0]
            rest = f"{host}/{path}" if path else host
            return "https://" + rest, notes + [_SSH_NOTE]
        if "@" in rest.split("/", 1)[0]:
            rest = rest.split("@", 1)[1]
            notes.append("원격 URL 에 자격정보가 박혀 있어 제거했습니다"
                         "(설정에는 토큰 없는 URL 만 둡니다).")
        return scheme + rest, notes

    scp = _SCP.match(raw)
    if scp:
        return f"https://{scp.group('host')}/{scp.group('path')}", notes + [_SSH_NOTE]

    return raw, notes


def read_origin_url(path: str, *, remote: str = "origin",
                    runner: Optional[Callable] = None) -> str:
    """``git -C <path> remote get-url <remote>`` → URL(못 읽으면 "").

    실패는 전부 ""로 수렴한다 — 이건 **편의 기능**이라, 못 읽었다고 설치를 세우지 않는다
    (못 채우면 스키마의 ``required`` 가 대신 막는다).
    """
    run = runner if runner is not None else subprocess.run
    env = dict(os.environ)
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "echo",
                "GCM_INTERACTIVE": "never"})
    try:
        cp = run(["git", "-C", path, "remote", "get-url", remote],
                 capture_output=True, text=True, timeout=GIT_TIMEOUT_SEC, env=env)
    except (OSError, subprocess.SubprocessError):
        return ""
    if int(getattr(cp, "returncode", 1) or 0) != 0:
        return ""
    out = (getattr(cp, "stdout", "") or "").strip().splitlines()
    return out[0].strip() if out else ""


def candidate_clone_paths(*, path: Optional[str] = None, project_dir: str = ".",
                          env: Optional[Mapping] = None) -> list:
    """dlc-meta 클론일 **가능성이 있는** 경로들(신뢰 순서, 중복 제거).

    명시 경로 > env > 배포 디렉토리 이웃 > cwd 이웃 > 홈. 자동 탐색은 어디까지나 편의이며,
    확실히 하려면 ``--dlc-meta <경로>`` 를 준다.
    """
    environ = os.environ if env is None else env
    raw: list = []
    if path:
        raw.append(path)
    from_env = str(environ.get(DLC_META_ENV, "") or "").strip()
    if from_env:
        raw.append(from_env)
    base = os.path.abspath(project_dir or ".")
    parent = os.path.dirname(base)
    cwd = os.path.abspath(os.getcwd())
    for root in (base, parent, cwd, os.path.dirname(cwd), os.path.expanduser("~")):
        if root:
            raw.append(os.path.join(root, DLC_META_DIRNAME))

    out: list = []
    seen: set = set()
    for p in raw:
        norm = os.path.abspath(os.path.expanduser(p))
        if norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def is_git_clone(path: str) -> bool:
    """그 디렉토리가 git 작업 트리인가(worktree 는 ``.git`` 이 파일이라 exists 로 본다)."""
    return bool(path) and os.path.isdir(path) and os.path.exists(os.path.join(path, ".git"))


def discover_dlc_meta_url(*, path: Optional[str] = None, project_dir: str = ".",
                          env: Optional[Mapping] = None,
                          runner: Optional[Callable] = None) -> tuple:
    """dlc-meta 원격 URL 을 찾는다 → ``(URL, 찾은 클론 경로, 안내 목록)``.

    ⚠️ **명시 경로는 조용히 넘어가지 않는다** — ``--dlc-meta`` 로 준 경로가 git 클론이
    아니거나 origin 이 없으면, 자동 탐색으로 슬쩍 다른 레포를 집는 대신 그 사실을
    안내에 남긴다(사람이 준 경로가 틀렸다는 것이 정보다).
    """
    notes: list = []
    candidates = candidate_clone_paths(path=path, project_dir=project_dir, env=env)
    explicit = os.path.abspath(os.path.expanduser(path)) if path else ""

    for candidate in candidates:
        if not is_git_clone(candidate):
            if candidate == explicit:
                notes.append(f"--dlc-meta 로 준 경로가 git 클론이 아닙니다: {candidate}")
            continue
        raw = read_origin_url(candidate, runner=runner)
        if not raw:
            if candidate == explicit:
                notes.append(f"{candidate} 에서 origin 원격을 읽지 못했습니다"
                             f"(git 미설치이거나 origin 이 없습니다).")
            continue
        url, url_notes = sanitize_repo_url(raw)
        notes.extend(url_notes)
        return url, candidate, notes

    return "", "", notes


# ---------------------------------------------------------------------------
# 답변 자동 채움
# ---------------------------------------------------------------------------


def needs_fill(value: Any) -> bool:
    """이 답변 값을 자동으로 채워야 하는가(없음·빈값·예시 자리표시자)."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or bool(_PLACEHOLDER.search(value))
    return False


def autofill_answers(flat: Mapping, *, dlc_meta_path: Optional[str] = None,
                     project_dir: str = ".", env: Optional[Mapping] = None,
                     runner: Optional[Callable] = None) -> AutofillReport:
    """평탄화된 답변에 자동 채움을 적용한다(원본 불변).

    Args:
        flat: ``{점 표기 키: 값}``(:func:`app.setup_validate.flatten_answers` 결과).
            ⚠️ 중첩 매핑을 그대로 주면 안 된다 — 같은 항목이 두 표현으로 갈라진다.
        dlc_meta_path: dlc-meta 클론 경로(``--dlc-meta``). 없으면 자동 탐색.
        project_dir: 배포 디렉토리(탐색 기준점).
        env/runner: 대역 주입(테스트·CI).

    Returns:
        :class:`AutofillReport`.

    채우는 항목:
        - ``run.dlc_meta_repo_url`` — 클론의 origin 에서.
        - ``forge.kind`` — 위 URL 의 호스트가 **스스로 밝힐 때만**
          (:func:`app.forge.infer_kind_from_url`). 설치자가 답했으면 건드리지 않는다.
          이 값이 틀리면 토큰 헤더·MR/PR 용어·엔드포인트가 통째로 어긋난다.
    """
    answers = dict(flat or {})
    report = AutofillReport(answers=answers)

    if not needs_fill(answers.get("run.dlc_meta_repo_url")):
        return report

    url, clone_path, notes = discover_dlc_meta_url(
        path=dlc_meta_path, project_dir=project_dir, env=env, runner=runner)
    report.notes.extend(notes)
    if not url:
        report.notes.append(
            "dlc-meta 원격 URL 을 찾지 못했습니다 — `--dlc-meta <클론 경로>` 로 경로를 주거나 "
            "run.dlc_meta_repo_url 을 직접 채우세요. (그대로 두면 검증이 막습니다 — 예시 URL 이 "
            "남으면 사내 토큰이 남의 호스트로 나갈 수 있습니다.)"
        )
        return report

    answers["run.dlc_meta_repo_url"] = url
    report.filled.append(Filled("run.dlc_meta_repo_url", url, "git-remote",
                                f"{clone_path} 의 origin"))

    # forge 종류 — URL 이 스스로 밝힐 때만, 그리고 설치자가 답하지 않았을 때만.
    if needs_fill(answers.get("forge.kind")):
        kind = forge_mod.infer_kind_from_url(url)
        if kind:
            answers["forge.kind"] = kind
            report.filled.append(Filled(
                "forge.kind", kind, "inferred",
                "dlc-meta URL 의 호스트에서 판정"))

    return report
