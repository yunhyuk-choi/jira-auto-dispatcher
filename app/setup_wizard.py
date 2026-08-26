"""``python -m app.setup wizard`` — 대화로 값을 캐내는 **설치 진입점**(껍데기).

왜 이것이 있는가:
    설치 리허설(빈 디렉토리 → ``INSTALL.md`` 만 보고 → ``docker compose up``)은 성공했지만
    사람이 손으로 한 일이 많았다: ``answers.json`` 을 직접 작성하고, ``deploy.secrets_base_dir``
    을 빠뜨려 ``validate`` 에 막히고, 시크릿 파일 3개를 손으로 만들고, ``discover`` 출력을
    눈으로 읽어 옮겨 적고, ``validate``→``render``→``doctor``→``compose up`` 순서를 스스로
    알아야 했다. 이 모듈은 **그 손일만** 없앤다.

이 모듈이 하는 일 / 하지 않는 일:
    - **한다**: 질문·기본값 제시·후보 선택·시크릿 파일 저장·답변 파일 저장(중단/재개)·
      단계 순서 안내.
    - **하지 않는다**: 검증·산출·판정을 **구현하지 않는다.** 전부 기존 라이브러리를 부른다 —
      :mod:`app.setup_autofill`(묻지 않아도 되는 값) · :mod:`app.setup_discover`(인스턴스 조회) ·
      :mod:`app.setup_validate`(검증) · :mod:`app.setup_render`(config.yaml 생성) ·
      :mod:`app.setup_doctor`(실측 진단). 질문 문구조차 :mod:`app.setup_schema` 의 선언에서
      나온다(스키마가 늘면 질문도 늘고, 여기 문구를 따로 고칠 일이 없다).

    **강제성의 원천은 대화가 아니라 기계적 게이트다.** 대화가 무엇을 건너뛰든,
    ``validate`` 가 통과하지 않으면 ``config.yaml`` 은 생성되지 않고 종료코드는 non-zero 다.
    이 모듈은 그 게이트를 **우회하는 경로를 하나도 만들지 않는다.**

대화형은 **편의지 유일 경로가 아니다**:
    ``INSTALL.md`` 의 수동 절차(예시 파일 복사 → 손으로 채움 → ``validate``/``render``)는
    그대로 유효하다. 이 마법사가 만드는 답변 파일은 그 수동 경로가 쓰는 것과 **같은 형식**
    이라, 언제든 마법사를 그만두고 ``python -m app.setup validate setup-answers.json`` 로
    이어갈 수 있다.

시크릿 취급(이 리포의 규율):
    입력받은 토큰 **값**은 ``config.yaml`` 에도, 답변 파일에도, 화면에도, 로그에도 쓰지
    않는다. 값은 :func:`app.inject.write_secret` 이 ``<시크릿 루트>/<ref>`` 에 0600 으로
    저장하고, 답변에는 **참조(상대 경로)만** 남는다. 입력은 :mod:`getpass` 로 받아 터미널
    에코조차 남기지 않는다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF. 답변 파일도 같은 규칙으로 쓴다.
"""

from __future__ import annotations

import getpass
import json
import os
import secrets as _secrets
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from app import (inject, setup_autofill, setup_discover, setup_doctor,
                 setup_render, setup_schema as S, setup_skill, setup_validate)

#: 종료코드 — :mod:`app.setup` 과 **같은 계약**(테스트가 드리프트를 잡는다).
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2

#: 답변 파일 기본 이름(배포 디렉토리 기준). 중단·재개의 매개체이며 gitignore 된다.
DEFAULT_ANSWERS_PATH = "setup-answers.json"

#: 호스트 쪽 시크릿 루트 기본값(배포 디렉토리 기준). ``docker-compose.yml`` 이
#: ``./secrets:/run/secrets`` 로 마운트하므로, 설정에 적히는 컨테이너 경로
#: (``deploy.secrets_base_dir``)와 **파일을 실제로 쓰는 이 경로**는 다르다.
DEFAULT_SECRETS_DIRNAME = "secrets"

#: 풀 퍼미션 동의 항목 — 질문 문구를 첫 질문과 되묻기가 **같이** 쓴다(사람이 같은 것을
#: 두 번째 볼 때 다른 문장이면 다른 질문으로 읽는다).
CONSENT_KEY = "consent.full_permissions"
CONSENT_PROMPT = "위 위험을 이해했고 동의합니다"

#: 프로파일 하나에서 파생되므로 **묻지 않는** 항목(:data:`app.setup_schema.PROFILE_DEFAULTS`).
#: ``--all`` 을 주면 묻는다. ⚠️ ``deploy.secrets_base_dir`` 은 여기 없다 — required 이고
#: ``local`` 프로파일 파생값이 빈 문자열이라, 묻지 않으면 리허설에서처럼 ``validate`` 에
#: 막힌다(그게 사람이 손으로 막혔던 바로 그 자리다).
_PROFILE_DERIVED = ("deploy.docker_host", "deploy.workspace_volume")


# ---------------------------------------------------------------------------
# 대화 입출력 — **주입 가능**(테스트는 대역으로만 돈다)
# ---------------------------------------------------------------------------


def _default_secret_reader(prompt: str) -> str:
    """시크릿 입력 — 터미널이면 에코 없이, 아니면 **멈추지 않고** 평범하게 읽는다.

    ⚠️ 윈도우의 :func:`getpass.getpass` 는 stdin 이 파이프여도 **콘솔을 직접** 읽는다
    (``msvcrt.getwch``). 그래서 ``echo ... | python -m app.setup wizard`` 같은 실행이
    아무 출력 없이 영원히 멈춘다 — 실제로 그렇게 걸렸다. 자동화·스크립트 설치를 막을
    이유가 없으므로, 터미널이 아니면 값을 가릴 수 없다는 **사실을 알리고** 그냥 읽는다.
    """
    try:
        interactive = sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):   # 닫힌/특수 스트림
        interactive = False
    if interactive:
        return getpass.getpass(prompt)
    print("  ⚠️ 터미널이 아니라 입력을 가릴 수 없습니다 — 값이 화면·로그에 보일 수 있습니다.")
    return input(prompt)


class WizardIO:
    """질문·출력의 단일 창구. 실제 입출력은 전부 주입 가능한 콜러블이다.

    이렇게 쪼개는 이유는 테스트다. "필수를 빠뜨리면 진행이 막힌다" 같은 계약은 입력
    시퀀스를 대역으로 넣어야 고정할 수 있고, CI 는 TTY 도 네트워크도 없는 ubuntu 에서
    돈다.

    Args:
        reader: ``(prompt) -> str``. 기본은 :func:`input`.
        secret_reader: ``(prompt) -> str``. 기본은 :func:`getpass.getpass`
            (터미널 에코 없음 — 시크릿 값이 화면·스크롤백에 남지 않는다).
        writer: ``(text) -> None``. 기본은 :func:`print`.
    """

    def __init__(self, *, reader: Optional[Callable] = None,
                 secret_reader: Optional[Callable] = None,
                 writer: Optional[Callable] = None) -> None:
        self._reader = reader or (lambda prompt: input(prompt))
        self._secret_reader = secret_reader or _default_secret_reader
        self._writer = writer or (lambda text: print(text))

    # --- 출력 ---------------------------------------------------------------

    def say(self, text: str = "") -> None:
        """한 줄 출력."""
        self._writer(text)

    def heading(self, text: str) -> None:
        """단계 제목(사람이 '지금 어디쯤인가'를 잃지 않게)."""
        self._writer("")
        self._writer(f"── {text} " + "─" * max(0, 60 - len(text)))

    # --- 입력 ---------------------------------------------------------------

    def ask(self, prompt: str, *, default: Any = "", help_text: str = "",
            required: bool = False) -> str:
        """자유 입력. ``required`` 면 빈 답을 **받지 않고 다시 묻는다**.

        빈 입력은 ``default`` 를 뜻한다(그래서 제안값을 그대로 받는 것이 Enter 한 번이다).
        """
        shown = "" if default in (None, "") else f" [{default}]"
        first = True
        while True:
            if first and help_text:
                self._say_help(help_text)
            first = False
            raw = str(self._reader(f"{prompt}{shown}: ") or "").strip()
            if raw:
                return raw
            if default not in (None, ""):
                return str(default)
            if not required:
                return ""
            self._writer("  ⚠️ 필수 항목입니다 — 값을 입력하세요(건너뛸 수 없습니다).")

    def ask_secret(self, prompt: str, *, help_text: str = "") -> str:
        """시크릿 **값** 입력(에코 없음). 빈 입력은 '지금은 건너뛴다'는 뜻이다."""
        if help_text:
            self._say_help(help_text)
        return str(self._secret_reader(f"{prompt} (입력은 화면에 보이지 않습니다): ") or "").strip()

    def ask_bool(self, prompt: str, *, default: bool = True,
                 help_text: str = "") -> bool:
        """예/아니오."""
        marker = "Y/n" if default else "y/N"
        first = True
        while True:
            if first and help_text:
                self._say_help(help_text)
            first = False
            raw = str(self._reader(f"{prompt} [{marker}]: ") or "").strip().lower()
            if not raw:
                return default
            if raw in ("y", "yes", "예", "ㅇ", "1", "true"):
                return True
            if raw in ("n", "no", "아니오", "아니요", "ㄴ", "0", "false"):
                return False
            self._writer("  ⚠️ y 또는 n 으로 답하세요.")

    def ask_choice(self, prompt: str, options: list, *, default_index: Optional[int] = None,
                   help_text: str = "", other_label: str = "") -> Any:
        """번호로 고르기. ``options`` 는 ``(라벨, 값)`` 목록.

        ``other_label`` 을 주면 마지막에 '직접 입력' 항목을 붙이고, 고르면 자유 입력을
        받아 **문자열 그대로** 돌려준다(자동 선택이 확정이 되지 않게 하는 탈출구다).
        """
        if help_text:
            self._say_help(help_text)
        for idx, (label, _value) in enumerate(options, start=1):
            mark = " ←기본" if default_index is not None and idx - 1 == default_index else ""
            self._writer(f"   {idx}) {label}{mark}")
        other_idx = 0
        if other_label:
            other_idx = len(options) + 1
            self._writer(f"   {other_idx}) {other_label}")
        default_shown = "" if default_index is None else f" [{default_index + 1}]"
        while True:
            raw = str(self._reader(f"{prompt}{default_shown}: ") or "").strip()
            if not raw and default_index is not None:
                return options[default_index][1]
            if raw.isdigit():
                num = int(raw)
                if 1 <= num <= len(options):
                    return options[num - 1][1]
                if other_idx and num == other_idx:
                    return self.ask("   직접 입력", required=True)
            self._writer("  ⚠️ 목록의 번호를 입력하세요.")

    def ask_multi(self, prompt: str, options: list, *, default_values: tuple = (),
                  help_text: str = "", required: bool = False) -> list:
        """번호 여러 개(쉼표 구분)로 고르기 → 고른 **값들의 목록**."""
        if help_text:
            self._say_help(help_text)
        for idx, (label, _value) in enumerate(options, start=1):
            self._writer(f"   {idx}) {label}")
        shown = ", ".join(str(v) for v in default_values)
        while True:
            raw = str(self._reader(
                f"{prompt} (쉼표로 여러 개)" + (f" [{shown}]" if shown else "") + ": "
            ) or "").strip()
            if not raw:
                if default_values:
                    return list(default_values)
                if not required:
                    return []
                self._writer("  ⚠️ 필수 항목입니다 — 최소 하나를 고르세요.")
                continue
            picked: list = []
            bad = False
            for token in raw.split(","):
                token = token.strip()
                if not token.isdigit() or not (1 <= int(token) <= len(options)):
                    bad = True
                    break
                picked.append(options[int(token) - 1][1])
            if bad or not picked:
                self._writer("  ⚠️ 목록의 번호를 쉼표로 구분해 입력하세요(예: 1,3).")
                continue
            return picked

    # --- 내부 ---------------------------------------------------------------

    def _say_help(self, help_text: str) -> None:
        for line in str(help_text).strip().splitlines():
            self._writer(f"  · {line.strip()}")


# ---------------------------------------------------------------------------
# 옵션 · 세션
# ---------------------------------------------------------------------------


@dataclass
class WizardOptions:
    """마법사 실행 옵션(전부 CLI 인자에서 온다)."""

    answers_path: str = DEFAULT_ANSWERS_PATH
    project_dir: str = "."
    config_path: str = setup_render.DEFAULT_OUTPUT_PATH
    template: str = setup_render.DEFAULT_TEMPLATE_PATH
    dlc_meta: str = ""
    secrets_dir: str = ""          # 비면 <project_dir>/secrets
    autofill: bool = True
    ask_all: bool = False          # 선택 항목까지 전부 묻는다
    use_discover: bool = True      # Jira 인스턴스 조회(네트워크)
    use_doctor: bool = True        # 실측 진단(네트워크·도커)

    @property
    def secrets_root(self) -> str:
        """시크릿 **파일**이 실제로 쓰이는 호스트 경로."""
        return self.secrets_dir or os.path.join(self.project_dir or ".",
                                                DEFAULT_SECRETS_DIRNAME)


@dataclass
class _Session:
    """진행 중 상태 — 답변(평탄한 점 표기)과 그 저장 책임."""

    io: WizardIO
    options: WizardOptions
    answers: dict = field(default_factory=dict)
    now: Callable = None            # -> ISO-8601 문자열
    token: Callable = None          # -> 고엔트로피 문자열
    notes: list = field(default_factory=list)

    def set(self, key: str, value: Any) -> None:
        """답변 하나를 기록하고 **즉시 파일에 저장**한다(중단 대비)."""
        self.answers[key] = value
        self.save()

    def get(self, key: str, default: Any = None) -> Any:
        return self.answers.get(key, default)

    def save(self) -> None:
        """답변을 파일로 남긴다 — 이 파일이 곧 '이어서 하기'의 매개체다.

        ⚠️ 시크릿 **값**은 절대 담기지 않는다(담기는 것은 참조 문자열뿐이다).
        """
        write_answers(self.options.answers_path, self.answers)


# ---------------------------------------------------------------------------
# 답변 파일 입출력 — 수동 경로(``validate answers.json``)와 **같은 형식**
# ---------------------------------------------------------------------------


def nest_answers(flat: dict) -> dict:
    """평탄한 점 표기 답변 → 중첩 매핑(사람이 읽고 손으로 고칠 수 있는 모양).

    ``INSTALL.md`` §2.5 의 손으로 쓰는 ``answers.json`` 과 같은 모양이라, 마법사를 그만두고
    수동 CLI 로 이어갈 수 있다(대화형은 유일 경로가 아니다).
    """
    out: dict = {}
    for key, value in (flat or {}).items():
        parts = str(key).split(".")
        node = out
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
    return out


def read_answers(path: str) -> dict:
    """저장된 답변을 평탄한 점 표기로 읽는다(없거나 깨졌으면 빈 dict).

    깨진 파일에 죽지 않는다 — 설치 도중 편집기로 손대다 깨뜨리는 일이 흔하고, 그때
    마법사가 죽으면 여태 모은 답이 아니라 **사람의 의욕**이 사라진다.
    """
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return setup_validate.flatten_answers(raw)


def write_answers(path: str, flat: dict) -> None:
    """답변을 UTF-8(BOM 없음)·LF JSON 으로 저장(POLICY-ENCODING)."""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(nest_answers(flat), fh, ensure_ascii=False, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# 스키마 기반 질문 — 문구·타입·허용값의 원천은 :mod:`app.setup_schema`
# ---------------------------------------------------------------------------


#: 안내 문구 상한(글자). 스키마 설명은 문서용이라 길다 — 대화에 문서를 통째로 부으면
#: 사람은 읽지 않고 넘긴다. 자세한 내용은 config.example.yaml 주석이 정본이다.
_HELP_LIMIT = 200


def _help_for(f: S.SchemaField) -> str:
    """스키마 설명을 질문 안내 문구로 — **첫 문장만**(길면 자른다)."""
    text = " ".join(str(f.description or "").split())
    if len(text) <= _HELP_LIMIT:
        return text
    cut = text.find("다. ")          # 한국어 종결 — 문장 경계에서 자른다
    if 0 < cut + 2 <= _HELP_LIMIT:
        return text[:cut + 2] + " …"
    return text[:_HELP_LIMIT].rstrip() + " …"


def ask_field(s: _Session, key: str, *, default: Any = None,
              required: Optional[bool] = None, prompt: str = "") -> Any:
    """스키마 선언 하나를 질문으로 바꿔 묻고, 답을 기록한다.

    타입별 위젯은 :attr:`app.setup_schema.SchemaField.type` 이 정한다 — 질문 문구·허용값·
    예시가 전부 스키마에서 오므로, 스키마가 늘어도 이 모듈은 그대로다.
    """
    f = S.get_field(key)
    if f is None:  # 스키마에 없는 키를 물을 이유가 없다(오타 방어)
        raise KeyError(f"스키마에 없는 키입니다: {key}")
    label = prompt or f"{key}"
    is_required = f.required if required is None else required
    # 기본값 우선순위: 이미 답한 값(재개) > 호출부가 준 제안 > 스키마 기본값.
    current = s.get(key)
    if current not in (None, "", [], {}):
        fallback = current
    elif default is not None:
        fallback = default
    else:
        fallback = f.default

    if f.type == S.FieldType.BOOL:
        value = s.io.ask_bool(label, default=bool(fallback), help_text=_help_for(f))
    elif f.type == S.FieldType.ENUM:
        options = [(str(c), c) for c in f.choices]
        default_index = None
        for idx, (_lbl, val) in enumerate(options):
            if val == fallback:
                default_index = idx
        value = s.io.ask_choice(label, options, default_index=default_index,
                                help_text=_help_for(f))
    elif f.type == S.FieldType.INT:
        while True:
            raw = s.io.ask(label, default=fallback, help_text=_help_for(f),
                           required=is_required)
            if raw == "" and not is_required:
                value = fallback
                break
            try:
                value = int(str(raw).strip())
                break
            except ValueError:
                s.io.say("  ⚠️ 정수를 입력하세요.")
    elif f.type in (S.FieldType.STRING_LIST, S.FieldType.NAMED_REF_LIST):
        shown = ", ".join(_ref_label(x) for x in (fallback or []))
        raw = s.io.ask(label + " (쉼표로 여러 개)", default=shown,
                       help_text=_help_for(f), required=is_required)
        value = [x.strip() for x in str(raw).split(",") if x.strip()]
        if not value and fallback:
            value = list(fallback)
    else:
        hint = _help_for(f)
        if f.example not in (None, "", [], {}) and not isinstance(f.example, (dict, list)):
            hint += f" (예: {f.example})"
        value = s.io.ask(label, default=fallback or "", help_text=hint,
                         required=is_required)
    s.set(key, value)
    return value


def _ref_label(item: Any) -> str:
    """``{id, name}`` 또는 문자열 → 사람이 읽는 한 조각."""
    if isinstance(item, dict):
        return str(item.get("name", "") or item.get("id", ""))
    return str(item)


# ---------------------------------------------------------------------------
# 시크릿 — 값은 파일로, 설정에는 참조만
# ---------------------------------------------------------------------------


def ensure_secret(s: _Session, key: str, *, what: str, generate: bool = False) -> None:
    """시크릿 참조를 묻고(기본값 제시), 그 **값**을 0600 파일로 저장한다.

    이미 파일이 있으면 **묻지 않는다** — 재개 시 같은 토큰을 두 번 붙여넣게 하지 않는다.
    값을 지금 못 구하면 건너뛸 수 있고(중단·재개), 그 경우 ``doctor --only secrets`` 가
    부재를 잡는다 — 마법사가 판정을 흉내내지 않는다.
    """
    f = S.get_field(key)
    ref = ask_field(s, key, default=(f.example if f is not None else ""),
                    required=True, prompt=f"{key} (시크릿 파일 참조)")
    root = s.options.secrets_root
    try:
        path = inject.secret_dest(root, str(ref))
    except Exception:  # noqa: BLE001 — 경로 조립 실패는 참조가 이상하다는 뜻
        s.io.say(f"  ⚠️ 참조가 이상합니다: {ref!r}")
        return

    if os.path.exists(path) and os.path.getsize(path) > 0:
        s.io.say(f"  이미 있습니다: {path} — 그대로 씁니다(다시 묻지 않습니다).")
        return

    if generate:
        value = s.token()
        s.io.say("  무작위 값을 생성합니다 — 사람이 정할 이유가 없는 값입니다.")
    else:
        value = s.io.ask_secret(f"  {what} 값",
                                help_text=f"값은 {path} 에 0600 으로 저장되고, "
                                          f"설정에는 참조({ref})만 들어갑니다. "
                                          f"지금 없으면 Enter 로 건너뛰고 나중에 그 파일에 "
                                          f"직접 저장해도 됩니다.")
    if not value:
        s.io.say(f"  ⏭️ 건너뜀 — 나중에 이 파일에 값을 저장하세요: {path}")
        s.notes.append(f"시크릿 값 미저장: {path} ({what})")
        return
    try:
        written = inject.write_secret(root, str(ref), value)
    except inject.InjectError as exc:
        s.io.say(f"  ⚠️ 저장하지 못했습니다: {exc}")
        s.notes.append(f"시크릿 저장 실패: {ref}")
        return
    s.io.say(f"  저장: {written} (0600) — 값은 어디에도 출력하지 않습니다.")


# ---------------------------------------------------------------------------
# 단계들
# ---------------------------------------------------------------------------


def _step_intro(s: _Session) -> None:
    s.io.say("jira-auto-dispatcher 설치 마법사")
    s.io.say("")
    s.io.say("이 마법사는 값을 **묻기만** 합니다 — 검증·생성·진단은 기존 CLI")
    s.io.say("(`python -m app.setup validate|render|doctor`)가 그대로 합니다.")
    s.io.say(f"답변은 {s.options.answers_path} 에 계속 저장되니, 중간에 그만두고")
    s.io.say("나중에 같은 명령을 다시 실행하면 이어서 진행합니다.")
    s.io.say(f"시크릿 값은 {s.options.secrets_root} 아래 0600 파일로만 저장합니다"
             " — 설정·답변 파일·화면에는 남기지 않습니다.")
    if s.answers:
        s.io.say("")
        s.io.say(f"이전에 저장된 답변 {len(s.answers)}개를 불러왔습니다"
                 " — 각 질문의 [기본값]이 그 값입니다(Enter 로 유지).")


def _step_consent(s: _Session) -> None:
    """풀 퍼미션 동의 — 가장 먼저 묻는다(동의 없이는 나머지를 물을 이유가 없다)."""
    s.io.heading("0. 풀 퍼미션 동의")
    s.io.say("이 시스템은 사람의 매 단계 승인 없이 파일 쓰기·셸 실행·git push 권한을 가진")
    s.io.say("코딩 에이전트를 헤드리스로 실행합니다. 관리 UI(8787)와 worker 는 그 자체로")
    s.io.say("원격 코드 실행 표면이라 인터넷에 노출하면 안 됩니다(정본: SECURITY.md).")
    agreed = ask_field(s, CONSENT_KEY, default=bool(s.get(CONSENT_KEY, False)),
                       prompt=CONSENT_PROMPT)
    if agreed and not s.get("consent.accepted_at"):
        s.set("consent.accepted_at", s.now())


def _step_jira(s: _Session) -> None:
    """Jira 접속에 필요한 최소값 — 이것만 있으면 ``discover`` 가 돈다."""
    s.io.heading("1. Jira 인스턴스")
    ask_field(s, "jira.base_url", required=True)
    ask_field(s, "jira.project", required=True)
    ask_field(s, "jira.watcher_email", required=True)
    ensure_secret(s, "jira.watcher_token_file", what="Jira 감시 계정 API 토큰")


def _step_deploy(s: _Session) -> None:
    s.io.heading("5. 배포 형태")
    profile = ask_field(s, "deploy.profile", required=True)
    derived = S.profile_derived_values(profile)   # {점 표기 키: 파생값}
    shown = " · ".join(f"{k}={v}" for k, v in derived.items()
                       if k != "deploy.secrets_base_dir")
    if shown:
        s.io.say(f"  프로파일에서 파생됩니다(묻지 않습니다): {shown}")
    # ⚠️ secrets_base_dir 은 required 인데 local 프로파일은 파생값을 주지 않는다
    #    (그 값은 env SECRETS_DIR 로 온다). 묻지 않으면 리허설에서처럼 validate 에 막힌다.
    s.io.say("  시크릿 루트는 **컨테이너 관점 경로**입니다 — compose 가 "
             f"{s.options.secrets_root} 를 그 자리에 마운트합니다.")
    ask_field(s, "deploy.secrets_base_dir",
              default=derived.get("deploy.secrets_base_dir") or "/run/secrets",
              required=True)
    if s.options.ask_all:
        for key in _PROFILE_DERIVED:
            ask_field(s, key, default=derived.get(key))


def _step_forge(s: _Session) -> None:
    s.io.heading("4. 코드 호스팅(forge)")
    inferred = s.get("forge.kind")
    if inferred:
        s.io.say(f"  dlc-meta 원격 URL 에서 {inferred} 로 판정했습니다 — 아래에서 바꿀 수 있습니다.")
    ask_field(s, "forge.kind", required=True)
    if s.options.ask_all:
        ask_field(s, "forge.base_url")
    ensure_secret(s, "forge.token_ref",
                  what="central 서비스 forge 토큰(GitLab PAT / GitHub PAT)")


def _step_webhook(s: _Session) -> None:
    s.io.heading("6. Jira 웹훅 수신(선택)")
    enabled = ask_field(s, "webhook.enabled", default=True,
                        prompt="웹훅 수신을 켤까요(끄면 폴링만 씁니다)")
    if not enabled:
        return
    ensure_secret(s, "webhook.secret_ref", what="Jira 웹훅 수신 토큰", generate=True)
    ref = s.get("webhook.secret_ref", "")
    s.io.say("  Jira 쪽 Automation/웹훅이 같은 값을 헤더 X-Jira-Webhook-Token 으로 보내야 합니다.")
    s.io.say(f"  값이 필요하면 직접 읽으세요: {inject.secret_dest(s.options.secrets_root, str(ref))}")


def _step_notifier(s: _Session) -> None:
    s.io.heading("7. 완료 알림(기본 꺼짐)")
    provider = ask_field(s, "notifier.provider", required=True)
    if provider and provider != "none":
        ensure_secret(s, "notifier.webhook_ref", what="알림 incoming webhook URL")


def _step_docs_repo(s: _Session) -> None:
    s.io.heading("8. 설계 문서 레포(선택)")
    if not s.io.ask_bool("에이전트가 참고할 설계 문서 레포가 있습니까", default=False,
                         help_text="없으면 그냥 건너뜁니다 — 프로비저닝을 조용히 skip 합니다."):
        s.set("run.docs_repo_url", "")
        return
    ask_field(s, "run.docs_repo_url")


def _step_dlc_meta(s: _Session) -> None:
    """dlc-meta 원격 URL — **묻지 않는 값**. 자동으로 못 찾을 때만 묻는다.

    설치는 프레임워크(SETTER)가 dlc-meta 를 만들어 push 한 직후에 이어지므로 그 클론이
    이미 로컬에 있다 — :func:`app.setup_autofill.autofill_answers` 가 origin 에서 읽는다.
    """
    s.io.heading("3. dlc-meta 레포(자동)")
    if _try_autofill_dlc_meta(s):
        return
    s.io.say("  자동으로 찾지 못했습니다 — 클론 경로를 주거나 URL 을 직접 적으세요.")
    path = s.io.ask("  dlc-meta 클론 경로(모르면 Enter — URL 을 직접 묻습니다)")
    if path:
        s.options.dlc_meta = path
        if _try_autofill_dlc_meta(s):
            return
    ask_field(s, "run.dlc_meta_repo_url", required=True)


def _try_autofill_dlc_meta(s: _Session) -> bool:
    """자동 채움을 한 번 시도하고 결과를 알린다 → 채웠으면 True."""
    report = _autofill(s)
    for line in (report.format_text() or "").splitlines():
        s.io.say("  " + line)
    url = report.answers.get("run.dlc_meta_repo_url", "")
    if setup_autofill.needs_fill(url):
        return False
    s.set("run.dlc_meta_repo_url", url)
    # forge.kind 도 이 URL 에서 파생된다(설치자가 답하지 않았을 때만 — autofill 규율).
    for filled in report.filled:
        if filled.key == "forge.kind" and not s.get("forge.kind"):
            s.set("forge.kind", filled.value)
    s.io.say(f"  run.dlc_meta_repo_url = {url} (묻지 않았습니다)")
    return True


def _autofill(s: _Session):
    """묻지 않아도 되는 값 채우기 — 판정은 :mod:`app.setup_autofill` 이 한다."""
    if not s.options.autofill:
        return setup_autofill.AutofillReport(answers=dict(s.answers))
    return setup_autofill.autofill_answers(
        dict(s.answers), dlc_meta_path=(s.options.dlc_meta or None),
        project_dir=s.options.project_dir)


# ---------------------------------------------------------------------------
# discover — 눈으로 옮겨 적지 않기 위한 단계
# ---------------------------------------------------------------------------


def _config_for_discover(s: _Session):
    """지금까지의 답변으로 **조회 전용** 설정 객체를 만든다(파일을 만들지 않는다).

    ``discover`` 는 ``jira.base_url``·``watcher_email``·``watcher_token_file`` 만 있으면
    돌게 설계돼 있다. 시크릿 루트는 **호스트 경로**로 덮어쓴다 — 설정에 적히는
    ``/run/secrets`` 는 컨테이너 관점이라 호스트에서는 토큰 파일을 못 찾는다.
    """
    from app.config import load_config_from_dict

    raw = nest_answers({
        "jira.base_url": s.get("jira.base_url", ""),
        "jira.project": s.get("jira.project", ""),
        "jira.watcher_email": s.get("jira.watcher_email", ""),
        "jira.watcher_token_file": s.get("jira.watcher_token_file", ""),
        "jira.trigger_statuses": s.get("jira.trigger_statuses", []) or [],
        "jira.cancel_statuses": s.get("jira.cancel_statuses", []) or [],
        "jira.optout_labels": s.get("jira.optout_labels", []) or [],
        "jira.custom_fields": s.get("jira.custom_fields", {}) or {},
        "deploy.secrets_base_dir": s.options.secrets_root,
    })
    return load_config_from_dict(raw)


def _step_discover(s: _Session, discover_fn: Callable) -> None:
    """인스턴스를 조회해 **후보를 보여주고 고르게** 한다(자동 선택은 확인만 받는다)."""
    s.io.heading("2. Jira 인스턴스 조회 (값을 옮겨 적지 않기 위해)")
    if not s.options.use_discover:
        s.io.say("  --no-discover — 조회를 건너뜁니다(값을 직접 입력합니다).")
        _ask_statuses_manually(s)
        return
    try:
        cfg = _config_for_discover(s)
        result = discover_fn(cfg, project_dir=s.options.project_dir)
    except Exception as exc:  # noqa: BLE001 — 조회 실패로 설치를 멈추지 않는다
        s.io.say(f"  ⚠️ 조회하지 못했습니다: {type(exc).__name__}: {exc}")
        s.io.say("  값을 직접 입력합니다(나중에 `python -m app.setup discover` 로 확인하세요).")
        _ask_statuses_manually(s)
        return

    for section in result.sections:
        s.io.say(f"  [{section.status}] {section.name}: {section.summary}")
        if section.hint:
            s.io.say(f"      ↳ {section.hint}")

    _pick_account(s, result)
    _pick_statuses(s, result)
    _pick_transition(s, result)
    _pick_custom_fields(s, result)


def _pick_account(s: _Session, result) -> None:
    section = result.get("account")
    data = (section.data if section is not None else {}) or {}
    account_id = str(data.get("account_id", "") or "")
    if account_id:
        s.io.say(f"  감시 계정 accountId: {account_id} (사용자 온보딩에 필요합니다)")


def _status_options(result) -> list:
    section = result.get("statuses")
    if section is None or section.status != setup_discover.STATUS_OK:
        return []
    out: list = []
    for entry in (section.data or {}).get("statuses") or []:
        name = str(entry.get("name", "") or "")
        sid = str(entry.get("id", "") or "")
        if not name:
            continue
        label = f"{name} (id={sid}, {entry.get('category', '') or '?'})"
        out.append((label, {"id": sid, "name": name} if sid else name))
    return out


def _pick_statuses(s: _Session, result) -> None:
    options = _status_options(result)
    if not options:
        s.io.say("  상태 목록을 얻지 못했습니다 — 직접 입력합니다.")
        _ask_statuses_manually(s)
        return
    s.io.say("")
    picked = s.io.ask_multi(
        "  착수(트리거) 상태를 고르세요", options,
        default_values=tuple(s.get("jira.trigger_statuses") or ()),
        required=True,
        help_text="이 상태로 티켓이 들어오면 자동 착수합니다. 이름이 어긋나면 폴러는 "
                  "에러 없이 아무 티켓도 찾지 못합니다 — 그래서 목록에서 고릅니다.")
    s.set("jira.trigger_statuses", picked)
    cancelled = s.io.ask_multi(
        "  '취소' 상태를 고르세요(없으면 Enter)", options,
        default_values=tuple(s.get("jira.cancel_statuses") or ()),
        help_text="이 상태로 바뀌면 추적 중인 잡을 즉시 취소합니다.")
    if cancelled:
        s.set("jira.cancel_statuses", cancelled)


def _ask_statuses_manually(s: _Session) -> None:
    """조회를 못 했을 때의 폴백 — 그래도 required 는 required 다."""
    ask_field(s, "jira.trigger_statuses", required=True)


def _pick_transition(s: _Session, result) -> None:
    section = result.get("transitions")
    if section is None or section.status != setup_discover.STATUS_OK:
        return
    data = section.data or {}
    selected = data.get("done_selected") or {}
    transitions = data.get("transitions") or []
    if selected.get("id"):
        s.io.say("")
        s.io.say(f"  완료 전이를 id={selected['id']} ({selected.get('name')}) 로 "
                 f"자동 선택했습니다.")
        if s.io.ask_bool("  이대로 쓸까요", default=True):
            s.set("jira.done_transition_names",
                  [{"id": str(selected["id"]), "name": str(selected.get("name", ""))}])
            return
    if not transitions:
        return
    options = [(f"{t.get('name')} → {t.get('to_name')} (id={t.get('id')})",
                {"id": str(t.get("id", "")), "name": str(t.get("name", ""))})
               for t in transitions]
    options.append(("(고르지 않음 — 이름으로 찾게 둔다)", None))
    picked = s.io.ask_choice("  완료 전이를 고르세요", options,
                             default_index=len(options) - 1,
                             help_text="완료 전이는 id 로 실행되는데 사람은 이름으로 압니다. "
                                       "고르면 id 와 이름을 함께 적어 둡니다.")
    if picked:
        s.set("jira.done_transition_names", [picked])


def _pick_custom_fields(s: _Session, result) -> None:
    """커스텀필드 — **자동 선택도 확정이 아니다.** 확인받고, 후보에서 고를 수 있게 한다."""
    section = result.get("custom_fields")
    if section is None or section.status != setup_discover.STATUS_OK:
        return
    logical = (section.data or {}).get("logical_keys") or {}
    if not logical:
        return
    s.io.say("")
    s.io.say("  Jira 커스텀필드 id 는 인스턴스마다 다릅니다 — 조회 결과로 채웁니다.")
    chosen = dict(s.get("jira.custom_fields") or {})
    for key, desc, fallback in S.JIRA_CUSTOM_FIELD_KEYS:
        entry = logical.get(key) or {}
        selected = str(entry.get("selected", "") or "")
        candidates = entry.get("candidates") or []
        current = chosen.get(key) or selected
        if selected and s.io.ask_bool(
                f"  {key}({desc}) → {selected} 로 채웠습니다. 맞습니까",
                default=True):
            chosen[key] = selected
            continue
        options = [(f"{c.get('name')} — {c.get('id')} [{c.get('tier')}]", str(c.get("id", "")))
                   for c in candidates if c.get("id")]
        options.append((f"(기본값 유지: {fallback})", fallback))
        picked = s.io.ask_choice(
            f"  {key}({desc}) 를 고르세요", options,
            default_index=len(options) - 1, other_label="직접 id 입력",
            help_text=f"현재 값: {current or '(없음)'} — 부분일치로 잘못 고르면 착수 시 "
                      f"엉뚱한 필드에 날짜가 박힙니다.")
        if picked:
            chosen[key] = picked
    if chosen:
        s.set("jira.custom_fields", chosen)


# ---------------------------------------------------------------------------
# 검증 · 산출 · 진단 — 전부 기존 라이브러리
# ---------------------------------------------------------------------------


def _step_validate(s: _Session):
    """``setup_validate`` 로 게이트를 통과할 때까지 — 통과 못 하면 여기서 끝난다.

    Returns:
        :class:`app.setup_validate.ValidationResult` (``ok`` 가 False 면 호출부는 반드시
        non-zero 로 끝낸다) — 판정은 이 모듈이 아니라 검증기가 한다.
    """
    s.io.heading("9. 검증 (python -m app.setup validate 와 같은 게이트)")
    while True:
        report = _autofill(s)
        result = setup_validate.validate_answers(report.answers)
        s.io.say(result.format_text())
        if result.ok:
            return result
        askable = [f.key for f in result.errors if S.get_field(f.key) is not None]
        if not askable:
            return result
        if not s.io.ask_bool("  지금 고칠까요(아니오면 여기서 멈추고 답변은 저장됩니다)",
                             default=True):
            return result
        before = dict(s.answers)
        for key in askable:
            f = S.get_field(key)
            if f is not None and f.secret_ref:
                ensure_secret(s, key, what=f"{key} 의 값")
            elif key == CONSENT_KEY:
                ask_field(s, key, required=True, prompt=CONSENT_PROMPT)
            else:
                ask_field(s, key, required=True)
        if s.answers == before:
            # 되묻기가 아무것도 바꾸지 못했다 — 같은 질문을 무한히 반복하지 않는다.
            # (동의를 거부한 경우가 대표적이다: 답이 바뀌지 않는 한 게이트는 계속 막는다.)
            s.io.say("  값이 그대로입니다 — 여기서 멈춥니다.")
            return result


def _step_render(s: _Session, result) -> Optional[str]:
    """``setup_render`` 로 config.yaml 생성."""
    s.io.heading("10. config.yaml 생성 (python -m app.setup render)")
    out = s.options.config_path
    force = False
    if os.path.exists(out):
        if not s.io.ask_bool(f"  {out} 이 이미 있습니다 — 덮어쓸까요(.bak 로 백업합니다)",
                             default=False):
            s.io.say("  생성하지 않았습니다. 답변은 저장돼 있으니 나중에 이어서 하거나")
            s.io.say(f"  `python -m app.setup render {s.options.answers_path}` 로 직접 만드세요.")
            return None
        force = True
    try:
        rendered = setup_render.render_config(result.explicit,
                                              template_path=s.options.template)
        backup = setup_render.write_config(rendered.text, out, force=force)
    except setup_render.RenderError as exc:
        s.io.say(f"  ⚠️ 생성 실패: {exc}")
        return None
    s.io.say(f"  생성: {out}" + (f" (기존 파일 백업: {backup})" if backup else ""))
    s.io.say(rendered.format_text())
    return out


def _step_doctor(s: _Session, config_path: str, doctor_fn: Callable) -> bool:
    """``setup_doctor`` 실측 — 판정도 문구도 전부 진단기 것이다."""
    s.io.heading("11. 실측 진단 (python -m app.setup doctor)")
    if not s.options.use_doctor:
        s.io.say("  --no-doctor — 건너뜁니다. 기동 전에 `python -m app.setup doctor` 를 돌리세요.")
        return True
    from app.config import ConfigError, load_config

    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        s.io.say(f"  ⚠️ 생성된 설정을 로드하지 못했습니다: {exc}")
        return False
    try:
        results = doctor_fn(cfg, config_path=config_path,
                            project_dir=s.options.project_dir)
    except Exception as exc:  # noqa: BLE001 — 진단 자체가 죽어도 설치 결과는 남긴다
        s.io.say(f"  ⚠️ 진단을 마치지 못했습니다: {type(exc).__name__}: {exc}")
        return False
    s.io.say(setup_doctor.format_results(results))
    return all(r.ok for r in results)


def _step_skill(s: _Session) -> None:
    """프로젝트 스킬 생성(**편의 · 실패해도 설치를 막지 않는다**).

    ⚠️ 이 단계는 게이트가 **아니다.** 리포가 추적하는 템플릿에서 개인 `.claude/skills/`
    를 만들어 주는 것뿐이고(:mod:`app.setup_skill`), 권한이 없거나 파일시스템이 읽기
    전용이면 경고 한 줄만 남기고 그대로 진행한다 — 스킬은 ``claude`` 를 쓸 때만 의미
    있는 선택지이며, 설치의 1차 진입점은 이 마법사(=CLI) 자신이다.
    (짝 프레임워크의 EX-15 / C FALLBACK — degraded 로 계속, 중단 금지 — 와 같은 사상.)

    기존 파일은 **덮어쓰지 않는다** — 직접 고친 스킬을 조용히 날리지 않기 위해서다
    (덮어쓰려면 사람이 ``python -m app.setup skill --force`` 를 명시해야 한다).
    """
    s.io.heading("12. 프로젝트 스킬 (선택 — claude 를 쓸 때만)")
    try:
        report = setup_skill.install_best_effort(s.options.project_dir)
    except Exception as exc:  # noqa: BLE001 — 이 단계의 실패가 설치를 멈추게 두지 않는다
        s.io.say(f"  ⚠️ 스킬을 만들지 못했습니다({type(exc).__name__}: {exc}) — "
                 f"설치와는 무관하므로 그대로 진행합니다.")
        return
    if not report.templates:
        s.io.say("  설치할 스킬 템플릿이 없습니다 — 건너뜁니다.")
        return
    s.io.say(report.format_text())
    if not report.ok:
        s.io.say("  ⚠️ 스킬은 편의일 뿐입니다 — 설치 자체는 위 결과 그대로 유효합니다.")


def _step_next(s: _Session, config_path: Optional[str], healthy: bool) -> None:
    s.io.heading("다음 단계")
    if config_path is None:
        s.io.say("  config.yaml 이 아직 없습니다 — 위 안내대로 마무리하세요.")
        return
    if not healthy:
        s.io.say("  ⚠️ 진단에 실패한 검사가 있습니다. 고친 뒤 다시:")
        s.io.say("      python -m app.setup doctor")
    for note in s.notes:
        s.io.say(f"  ⚠️ 남은 일: {note}")
    s.io.say("  1) docker compose up -d")
    s.io.say("  2) curl -fsS http://127.0.0.1:8787/healthz")
    s.io.say("  3) docker compose exec central python -m app.setup doctor   # 컨테이너 관점")
    s.io.say("  4) 관리 UI(http://127.0.0.1:8787) 에서 사용자 온보딩")
    s.io.say("  ⚠️ 관리 UI 와 worker 는 RCE 표면입니다 — 인터넷에 노출하지 마세요(SECURITY.md).")


# ---------------------------------------------------------------------------
# 진입점
# ---------------------------------------------------------------------------


def _default_now() -> str:
    """동의 시각(ISO-8601, 로컬 타임존)."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run_wizard(io: WizardIO, options: WizardOptions, *,
               discover_fn: Optional[Callable] = None,
               doctor_fn: Optional[Callable] = None,
               now_fn: Optional[Callable] = None,
               token_fn: Optional[Callable] = None) -> int:
    """마법사 본체 → 종료코드(:data:`EXIT_OK` / :data:`EXIT_GATE_FAILED`).

    Args:
        io: 대화 입출력(:class:`WizardIO`) — 테스트는 대역을 넣는다.
        options: 실행 옵션(:class:`WizardOptions`).
        discover_fn: ``(cfg, project_dir=...) -> DiscoveryResult``.
            기본 :func:`app.setup_discover.discover`(네트워크).
        doctor_fn: ``(cfg, config_path=..., project_dir=...) -> [CheckResult]``.
            기본 :func:`app.setup_doctor.run_checks`(네트워크·도커).
        now_fn/token_fn: 시각·난수 대역(결정적 테스트용).

    중단(Ctrl-C·Ctrl-D)은 실패가 아니다 — 여태 모은 답을 저장하고 이어서 할 방법을
    알린 뒤 non-zero 로 끝낸다(설치가 **완료되지 않았음**은 종료코드로 남긴다).
    """
    session = _Session(
        io=io, options=options,
        answers=read_answers(options.answers_path),
        now=now_fn or _default_now,
        token=token_fn or (lambda: _secrets.token_hex(32)),
    )
    try:
        return _run(session,
                    discover_fn or setup_discover.discover,
                    doctor_fn or setup_doctor.run_checks)
    except (EOFError, KeyboardInterrupt):
        session.save()
        io.say("")
        io.say(f"중단했습니다 — 여태 모은 답변은 {options.answers_path} 에 있습니다.")
        io.say("같은 명령을 다시 실행하면 이어서 진행합니다.")
        return EXIT_GATE_FAILED


def _run(s: _Session, discover_fn: Callable, doctor_fn: Callable) -> int:
    """단계 순서 — 리허설에서 사람이 스스로 알아야 했던 그 순서."""
    _step_intro(s)
    _step_consent(s)
    _step_jira(s)
    _step_discover(s, discover_fn)
    _step_dlc_meta(s)
    _step_forge(s)
    _step_deploy(s)
    _step_webhook(s)
    _step_notifier(s)
    _step_docs_repo(s)

    result = _step_validate(s)
    if not result.ok:
        s.io.say("")
        s.io.say(f"검증을 통과하지 못해 config.yaml 을 만들지 않았습니다"
                 f" — 답변은 {s.options.answers_path} 에 남아 있습니다.")
        return EXIT_GATE_FAILED

    config_path = _step_render(s, result)
    healthy = _step_doctor(s, config_path, doctor_fn) if config_path else False
    _step_skill(s)          # 편의 — 실패해도 종료코드에 영향을 주지 않는다
    _step_next(s, config_path, healthy)
    if config_path is None:
        return EXIT_GATE_FAILED
    return EXIT_OK if healthy else EXIT_GATE_FAILED
