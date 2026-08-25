"""설치 **자동 채움** — 사람에게 물을 이유가 없는 값을 기계가 채운다.

역할:
    :mod:`app.setup_schema` 가 "무엇을 묻는가"를 선언하고 :mod:`app.setup_validate` 가
    "그 답이 맞는가"를 강제한다면, 이 모듈은 그 앞자리에서 **애초에 묻지 않아도 되는
    항목을 답으로 만들어** 준다. 설치자가 손으로 옮겨 적는 값은 그 자체가 오설정의
    원천이고(이 리포에서 가장 자주 재발한 실패 모드), 특히 아래 둘은 사람이 정할 근거가
    전혀 없다:

    1. ``run.dlc_meta_repo_url`` — 이 시스템의 설치는 ``ai-dlc-orchestrator`` 프레임워크의
       SETTER 가 dlc-meta 레포를 만들어 원격에 push 한 **직후**에 이어진다. 즉 설치 시점에
       그 클론이 이미 로컬에 있고, 원격 URL 은 ``git -C <클론> remote get-url origin``
       한 줄이면 알 수 있다. 예전에는 이 값을 채우지 않으면 예시 파일의
       ``https://gitlab.example.com/<your-group>/dlc-meta.git`` 이 **그대로 남았다** —
       비어 있는 것도 아니라 형식상 유효해 보여 눈으로 넘어간다.
    2. ``WORKER_SHARED_SECRET`` — central↔worker HTTP 인증의 공유 시크릿. 사람이 고를
       이유가 없는 **랜덤값**이다(``secrets.token_hex``).

두 값의 성격이 다르고, 그래서 **가는 곳이 다르다**:
    - dlc-meta URL 은 시크릿이 아니라 설정이다 → 답변에 채워 ``config.yaml`` 로 간다.
    - 공유 시크릿은 **값**이다 → 이 리포의 규율("config 에는 값이 아니라 참조")대로
      ``config.yaml`` 에 **절대 쓰지 않는다.** 지금 이 값이 실제로 흐르는 경로는
      ``.env`` → compose ``environment.WORKER_SHARED_SECRET`` → :mod:`app.config` env
      오버라이드 → :mod:`app.spawner` 가 워커에 재주입이므로, 그 경로의 시작점인
      ``.env`` 에 쓴다.

멱등성(중요):
    공유 시크릿은 **이미 있으면 절대 덮어쓰지 않는다.** 재생성하면 떠 있는 워커가 전부
    ``X-Worker-Secret`` 401 로 죽는다. 그래서 판정은 "값이 있는가"이고, 있으면 아무 것도
    하지 않는다(호스트 env 가 이미 갖고 있어도 마찬가지).

시크릿 규율(절대 규칙):
    생성한 시크릿 **값은 반환값·로그·표준출력·JSON 출력 어디에도 싣지 않는다.** 이 모듈이
    돌려주는 것은 "만들었는가 / 어디에 있는가"뿐이다.

의존성 주입:
    git 호출(``runner``)·난수 생성기(``generator``)·환경(``env``)은 전부 주입 가능하다 —
    CI(ubuntu, 네트워크 없음)에서 대역만으로 전 경로를 테스트한다.

POLICY-ENCODING: 이 파일과 이 모듈이 만드는 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import os
import re
import secrets as _secrets
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

#: worker 공유 시크릿이 사는 env 이름(compose·spawner·config 가 공유하는 계약).
WORKER_SECRET_ENV = "WORKER_SHARED_SECRET"

#: 그 값을 담는 파일(compose 프로젝트 디렉토리 기준).
DEFAULT_ENV_FILE = ".env"

#: 생성 시 바이트 수(hex 로 그 2배 길이가 된다 — 32바이트 = 64자).
SECRET_BYTES = 32

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


@dataclass(frozen=True)
class SecretOutcome:
    """worker 공유 시크릿 확보 결과. ⚠️ **값은 담지 않는다.**

    Attributes:
        status: ``generated``(새로 만들어 파일에 씀) · ``kept_file``(파일에 이미 있음) ·
            ``kept_env``(호스트 env 가 이미 갖고 있음) · ``failed``(쓸 수 없었음).
        path: 값이 사는 파일 경로(``kept_env`` 면 "").
        detail: 사람이 읽는 한 줄.
    """

    status: str
    path: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        """이 배포에 공유 시크릿이 존재하는가."""
        return self.status in ("generated", "kept_file", "kept_env")

    def to_dict(self) -> dict:
        return {"status": self.status, "path": self.path, "detail": self.detail}


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


# ---------------------------------------------------------------------------
# worker 공유 시크릿(.env)
# ---------------------------------------------------------------------------


def _parse_env_file(path: str) -> tuple:
    """``.env`` 를 줄 목록으로 읽고 대상 키의 줄 인덱스·값을 찾는다.

    Returns:
        ``(줄 목록, 대상 줄 인덱스 또는 None, 그 줄의 값)``. 파일이 없으면 ``([], None, "")``.
    """
    if not os.path.exists(path):
        return [], None, ""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().replace("\r\n", "\n").split("\n")
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() != WORKER_SECRET_ENV:
            continue
        value = value.strip().strip("'\"")
        return lines, idx, value
    return lines, None, ""


def read_env_file_secret(path: str = DEFAULT_ENV_FILE) -> bool:
    """그 ``.env`` 에 **비어 있지 않은** worker 공유 시크릿이 있는가.

    ⚠️ 값을 돌려주지 않는다 — 존재 여부만 말한다(:mod:`app.setup_doctor` 가 이걸 쓴다).
    """
    _lines, idx, value = _parse_env_file(path)
    return idx is not None and bool(value)


def _write_env_file(path: str, lines: list) -> None:
    """``.env`` 를 0600 으로 쓴다(UTF-8·LF — POLICY-ENCODING).

    ⚠️ 시크릿 **값**이 들어 있는 파일이므로 권한을 조인다. 윈도우는 chmod 가 사실상
    무시되지만(INSTALL §2.3) 실패로 보지 않는다.
    """
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    def _opener(p, flags):
        return os.open(p, flags, 0o600)

    body = "\n".join(lines)
    if not body.endswith("\n"):
        body += "\n"
    with open(path, "w", encoding="utf-8", newline="", opener=_opener) as fh:
        fh.write(body)
    try:
        os.chmod(path, 0o600)
    except OSError:  # 윈도우 등 — 무해
        pass


def ensure_worker_shared_secret(env_path: str = DEFAULT_ENV_FILE, *,
                                env: Optional[Mapping] = None,
                                generator: Optional[Callable] = None) -> SecretOutcome:
    """central↔worker 공유 시크릿을 **없을 때만** 만들어 ``.env`` 에 둔다(멱등).

    판정 순서(있으면 아무 것도 하지 않는다 — 재생성하면 떠 있는 워커가 전부 401):
        1. 호스트 env 에 이미 값이 있다 → ``kept_env``. compose 가 그 값을 그대로 넘긴다.
        2. ``.env`` 에 비어 있지 않은 값이 있다 → ``kept_file``.
        3. 없다 → ``secrets.token_hex`` 로 만들어 그 파일에 쓴다 → ``generated``.
           (키는 있는데 값이 비어 있으면 **그 줄을 채운다** — 중복 줄을 만들지 않는다.)

    Args:
        env_path: ``.env`` 경로(compose 프로젝트 디렉토리 기준).
        env: 환경 매핑(기본 ``os.environ``).
        generator: 난수 hex 생성기 ``(bytes:int) -> str``(테스트 주입).

    Returns:
        :class:`SecretOutcome`. ⚠️ **값은 담기지 않는다.**
    """
    environ = os.environ if env is None else env
    if str(environ.get(WORKER_SECRET_ENV, "") or "").strip():
        return SecretOutcome("kept_env", "",
                             f"호스트 env {WORKER_SECRET_ENV} 에 이미 값이 있어 "
                             f"파일을 만들지 않았습니다.")

    lines, idx, value = _parse_env_file(env_path)
    if idx is not None and value:
        return SecretOutcome("kept_file", env_path,
                             f"{env_path} 에 이미 값이 있어 그대로 둡니다"
                             f"(재생성하면 떠 있는 워커가 전부 401 이 됩니다).")

    gen = generator if generator is not None else _secrets.token_hex
    line = f"{WORKER_SECRET_ENV}={gen(SECRET_BYTES)}"
    if idx is not None:
        lines[idx] = line
    else:
        while lines and not lines[-1].strip():
            lines.pop()
        if lines:
            lines.append("# central↔worker dispatch HTTP 인증 공유 시크릿(설치 관문이 생성)")
        lines.append(line)
    try:
        _write_env_file(env_path, lines)
    except OSError as exc:
        return SecretOutcome("failed", env_path,
                             f"{env_path} 에 쓸 수 없습니다({exc.strerror}) — "
                             f"직접 만드세요: {WORKER_SECRET_ENV}=$(openssl rand -hex 32)")
    return SecretOutcome("generated", env_path,
                         f"{WORKER_SECRET_ENV} 를 새로 생성해 {env_path} 에 저장했습니다"
                         f"(값은 출력하지 않습니다).")
