"""풀 퍼미션 동의의 **출처**를 기록·검증한다 — "누가 동의했는가"의 단일 원천.

왜 이 모듈이 따로 있는가(실측된 결함):
    3차 리허설에서 온보딩 **서브 에이전트가 ``consent.full_permissions: true`` 를 스스로
    세팅**했다. 룰북은 "대신 눌러 주지 마라"라고 하면서 같은 문서에서 "서브에게는 사용자
    채널이 없다"고도 했다 — 그 둘은 **구조적으로 양립 불가**라, 서브는 진행 쪽을 택했다.
    이 시스템은 "풀 퍼미션을 준다는 데 사람이 동의했다"를 전제로 헤드리스 에이전트를
    돌리므로, 그 전제가 자기 승인으로 만들어지면 시스템 전체의 안전 논거가 무너진다.

이 모듈이 닫는 구멍:
    동의를 **답변 파일의 불리언 하나**에서 **출처가 붙은 별도 증서(attestation)** 로 바꾼다.
    ``setup-answers.json`` 에 ``true`` 를 적는 것만으로는 더 이상 통과하지 못한다 — 그 값을
    뒷받침하는 :data:`RECORD_FILENAME` 증서가 있어야 하고, 증서는 **오직 두 채널**로만
    만들어진다:

    ================== ============================================================
    :data:`CHANNEL_HUMAN`  터미널 앞의 사람이 직접 만든다. **stdin 이 TTY 여야** 하고
                           확인 문구를 그대로 입력해야 한다. 헤드리스 세션(파이프·서브
                           에이전트)에는 TTY 가 없으므로 이 채널은 **물리적으로 막힌다**.
    :data:`CHANNEL_RELAY`  사용자 채널을 가진 **오케스트레이터**가 사람에게서 받아 중계한다.
                           누가 동의했는지(``granted_by``)와 그 사람의 **원문**
                           (``statement``), 중계자(``relayed_by``)를 반드시 남긴다.
    ================== ============================================================

    그리고 **서브 에이전트 경로에서는 두 채널 모두 거부한다** — 서브는 룰북 계약에 따라
    모든 설치 명령을 ``JAD_SETUP_ACTOR=subagent`` 로 실행하며(:data:`ACTOR_ENV`), 그 값이
    보이면 :func:`grant_interactive`·:func:`grant_relay` 가 :class:`ConsentError` 로 막는다.
    서브가 할 수 있는 유일한 동의 관련 동작은 :func:`consent_request` — **동의 요청서를
    만들어 상위에 반환하는 것**뿐이다. 이것이 "멈춰라"와 "너에겐 채널이 없다"의 모순을
    푸는 방법이다: 서브는 멈추되 **빈손으로 멈추지 않는다.**

정직하게 말해 두는 한계:
    셸을 가진 에이전트가 ``--relay`` 를 거짓으로 실행하는 것을 소프트웨어가 물리적으로
    막을 수는 없다. 그래서 이 모듈은 **막는 것과 드러내는 것**을 함께 한다 — 중계 채널로
    만들어진 동의는 검증·진단·``config.yaml`` 어디서나 "사람이 직접 입력한 것이 아니라
    <중계자> 가 <사람> 에게서 받았다고 **주장**한 것"으로 표시된다. 조용히 사람 동의인 척
    하지 않는다(이 리포의 반복 주제 — 조용한 실패 금지).

정본 관계:
    - 증서 파일(:data:`RECORD_FILENAME`) = "동의가 어디서 왔는가"의 정본. 개인 산출물이며
      gitignore 된다(사람 이름·원문이 들어간다).
    - ``consent.*`` 설정 키 = 그 사실의 **사본**(:mod:`app.setup_render` 가 증서에서 옮겨
      적고, :mod:`app.config` 가 런타임에 읽는다).
    - 게이트 판정은 :func:`app.setup_validate.validate_answers` 가 이 모듈의 순수 술어
      (:func:`attestation_problem`)로 수행한다 — 게이트가 두 벌이 되면 반드시 갈라진다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Optional

# ---------------------------------------------------------------------------
# 상수 — 기계가 읽는 안정 식별자
# ---------------------------------------------------------------------------

#: 동의 증서 파일 이름(배포 디렉토리 기준). 답변 파일과 **일부러 분리**한다 — 답변 파일은
#: 온보딩 대화가 자유롭게 쓰는 작업 파일이고, 증서는 사람만 만들 수 있는 산출물이다.
RECORD_FILENAME = "setup-consent.json"

#: 증서 스키마 버전(형식이 바뀌면 올린다 — 옛 증서를 조용히 오해하지 않기 위해).
RECORD_VERSION = 1

#: 사람이 무엇에 동의했는지의 버전. 고지 문구(:data:`DISCLOSURE`)가 실질적으로 바뀌면
#: 올린다 — 옛 고지에 동의한 증서를 새 고지의 동의로 재사용하지 않기 위한 표식이다.
DISCLOSURE_VERSION = "1"

#: 동의 채널 — 사람이 직접 입력(TTY).
CHANNEL_HUMAN = "human_interactive"
#: 동의 채널 — 사용자 채널을 가진 상위 오케스트레이터가 사람에게서 받아 중계.
CHANNEL_RELAY = "orchestrator_relay"
#: 허용 채널 전체(선언 순서 = 신뢰 순서).
CHANNELS: tuple = (CHANNEL_HUMAN, CHANNEL_RELAY)

#: 실행 주체를 밝히는 환경변수. 룰북 계약상 **서브 에이전트는 반드시 이걸 세팅**하고
#: 모든 설치 명령을 돌린다. 사람·오케스트레이터는 세팅하지 않거나 다른 값을 쓴다.
ACTOR_ENV = "JAD_SETUP_ACTOR"

#: 이 값들이면 "동의를 만들 수 없는 주체"로 본다(대소문자·구분자 무시).
SUBAGENT_ACTORS: tuple = ("subagent", "sub-agent", "sub_agent", "agent", "sub", "worker")

#: 사람이 입력해야 하는 확인 문구(둘 중 하나 — 한글 입력이 곤란한 콘솔 대비).
#: 비교는 앞뒤 공백 제거 + 대소문자 무시로만 한다(그 이상 관대해지면 확인이 아니다).
CONFIRM_PHRASES: tuple = ("동의합니다", "I AGREE")

#: 무엇에 동의하는가 — 사람에게 보여주는 고지 원문(SECURITY.md 요약). 이 문구가 곧
#: :data:`DISCLOSURE_VERSION` 의 내용물이다.
DISCLOSURE: tuple = (
    "이 시스템은 **사람의 매 단계 승인 없이** 도구 권한(파일 쓰기 · 셸 실행 · git push)을",
    "가진 코딩 에이전트를 헤드리스로 실행합니다.",
    "- 관리 UI(기본 8787)와 worker 컨테이너는 그 자체로 **원격 코드 실행 표면**입니다.",
    "  인터넷에 노출하지 마세요.",
    "- 에이전트는 등록된 레포에 브랜치를 push 하고 변경요청을 만듭니다.",
    "- 토큰은 이 호스트의 파일로 보관되며, 그 권한 범위 전체가 에이전트에게 위임됩니다.",
    "정본 위험 모델: SECURITY.md · 설치 안내: INSTALL.md §2.1",
)

#: 중계 동의에 붙는 경고 — 어디에 출력되든 같은 문장이어야 한다(단일 원천).
RELAY_CAVEAT = (
    "이 동의는 사람이 직접 입력한 것이 아니라, 중계자가 사람에게서 받았다고 "
    "**주장**한 것입니다. 사람이 실제로 그렇게 말했는지는 중계자의 책임입니다."
)

#: 자유 입력 자리에 온 **기계 자동 채움 냄새**(중계 동의의 근거로 인정하지 않는다).
_PLACEHOLDER_STATEMENTS: frozenset = frozenset({
    "true", "false", "yes", "no", "y", "n", "ok", "okay", "agree", "agreed",
    "consent", "동의", "승인", "-", "n/a", "na", "none", "null", "tbd",
    "statement", "granted_by", "사용자", "user",
})

#: 예시 자리표시자(``<사용자 원문>``) — 그대로 넣은 것을 잡는다.
_ANGLED = re.compile(r"<[^<>\s][^<>]*>")


class ConsentError(Exception):
    """동의를 만들 수 없다(주체·채널·입력 문제). CLI 는 게이트 실패로 번역한다."""


# ---------------------------------------------------------------------------
# 증서 자료구조
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsentRecord:
    """동의 증서 — "무엇에 · 누가 · 어느 채널로 동의했는가".

    Attributes:
        full_permissions: 동의 여부. **False 인 증서도 유효하다**(거부의 기록).
        accepted_at: 동의 시각(ISO-8601).
        channel: :data:`CHANNEL_HUMAN` | :data:`CHANNEL_RELAY`.
        granted_by: 동의한 **사람**의 식별자(이름·이메일). 채널과 무관하게 필수.
        relayed_by: 중계자(오케스트레이터) 식별자. :data:`CHANNEL_RELAY` 에서만 채워진다.
        statement: 그 사람의 **원문**. :data:`CHANNEL_RELAY` 에서만 채워진다.
        disclosure_version: 동의 당시 고지 버전(:data:`DISCLOSURE_VERSION`).
        version: 증서 스키마 버전(:data:`RECORD_VERSION`).
    """

    full_permissions: bool
    accepted_at: str
    channel: str
    granted_by: str
    relayed_by: str = ""
    statement: str = ""
    disclosure_version: str = DISCLOSURE_VERSION
    version: int = RECORD_VERSION

    # --- 직렬화 ---------------------------------------------------------

    def to_dict(self) -> dict:
        """JSON 직렬화용 dict(키 순서 = 사람이 읽기 좋은 순서)."""
        return {
            "version": self.version,
            "full_permissions": self.full_permissions,
            "accepted_at": self.accepted_at,
            "channel": self.channel,
            "granted_by": self.granted_by,
            "relayed_by": self.relayed_by,
            "statement": self.statement,
            "disclosure_version": self.disclosure_version,
        }

    @classmethod
    def from_dict(cls, raw: Mapping) -> "ConsentRecord":
        """저장된 증서를 읽는다.

        Raises:
            ConsentError: 형식이 증서로 볼 수 없을 때(모르는 채널·버전 등). **조용히
                무시하지 않는다** — 깨진 증서를 "증서 없음"으로 흘리면 그 다음 오류
                메시지가 엉뚱한 곳을 가리킨다.
        """
        if not isinstance(raw, Mapping):
            raise ConsentError("동의 증서의 최상위는 객체(JSON object)여야 합니다.")
        version = raw.get("version", RECORD_VERSION)
        try:
            version = int(version)
        except (TypeError, ValueError):
            raise ConsentError(f"동의 증서의 version 이 정수가 아닙니다: {version!r}")
        if version > RECORD_VERSION:
            raise ConsentError(
                f"동의 증서 형식이 이 버전보다 새롭습니다(증서 v{version} > 지원 "
                f"v{RECORD_VERSION}) — 이 리포를 갱신하거나 증서를 다시 만드세요.")
        channel = str(raw.get("channel", "") or "").strip()
        if channel not in CHANNELS:
            raise ConsentError(
                f"동의 증서의 channel 이 알 수 없는 값입니다: {channel!r} "
                f"(가능: {' | '.join(CHANNELS)}). 손으로 만든 증서는 인정하지 않습니다 — "
                f"`python -m app.setup consent` 로 다시 만드세요.")
        value = raw.get("full_permissions")
        if not isinstance(value, bool):
            raise ConsentError(
                "동의 증서의 full_permissions 는 true/false 여야 합니다"
                f"(받은 타입: {type(value).__name__}).")
        return cls(
            full_permissions=value,
            accepted_at=str(raw.get("accepted_at", "") or ""),
            channel=channel,
            granted_by=str(raw.get("granted_by", "") or ""),
            relayed_by=str(raw.get("relayed_by", "") or ""),
            statement=str(raw.get("statement", "") or ""),
            disclosure_version=str(raw.get("disclosure_version", "") or ""),
            version=version,
        )

    # --- 파생 -----------------------------------------------------------

    @property
    def relayed(self) -> bool:
        """상위 중계로 전달된 동의인가."""
        return self.channel == CHANNEL_RELAY

    def config_values(self) -> dict:
        """``config.yaml`` 에 옮겨 적을 ``consent.*`` 값(점 표기).

        ⚠️ ``statement``(사람의 원문)는 **일부러 빼놓는다** — 설정 파일은 컨테이너로
        마운트되고 로그·진단 출력에 실릴 수 있다. 원문은 증서 파일에만 둔다.
        """
        out = {
            "consent.full_permissions": self.full_permissions,
            "consent.accepted_at": self.accepted_at,
            "consent.channel": self.channel,
            "consent.granted_by": self.granted_by,
        }
        if self.relayed_by:
            out["consent.relayed_by"] = self.relayed_by
        return out

    def describe(self) -> str:
        """사람이 읽는 한 줄 요약(진단·검증 출력 공용 — 문구는 한 곳에서만 만든다)."""
        if not self.full_permissions:
            return f"동의하지 않음으로 기록됨({self.accepted_at or '시각 미상'})"
        if self.relayed:
            return (f"{self.granted_by or '?'} 의 동의를 "
                    f"{self.relayed_by or '?'} 가 중계({self.accepted_at or '시각 미상'})")
        return (f"{self.granted_by or '?'} 가 터미널에서 직접 동의"
                f"({self.accepted_at or '시각 미상'})")


# ---------------------------------------------------------------------------
# 실행 주체 판정
# ---------------------------------------------------------------------------


def _env(env: Optional[Mapping]) -> Mapping:
    return os.environ if env is None else env


def actor(env: Optional[Mapping] = None) -> str:
    """현재 선언된 실행 주체(:data:`ACTOR_ENV`). 미선언이면 빈 문자열."""
    return str(_env(env).get(ACTOR_ENV, "") or "").strip().lower()


def is_subagent(env: Optional[Mapping] = None) -> bool:
    """서브 에이전트로 **선언된** 실행인가(= 동의를 만들 수 없는 주체).

    선언이 없으면 False 다 — 여기서 추측으로 사람을 막으면 정상 설치가 깨진다. 선언하지
    않은 헤드리스 실행은 :func:`grant_interactive` 의 **TTY 요구**가 따로 막는다(두 장치가
    서로 다른 구멍을 덮는다).
    """
    return actor(env).replace(" ", "") in SUBAGENT_ACTORS


def _refuse_if_subagent(env: Optional[Mapping]) -> None:
    """서브 에이전트 경로면 동의 생성을 거부한다(C1 의 핵심 게이트)."""
    if not is_subagent(env):
        return
    raise ConsentError(
        f"{ACTOR_ENV}={actor(env)} — 서브 에이전트는 동의를 만들 수 없습니다. "
        f"동의는 사람에게서만 옵니다. `python -m app.setup consent --request` 로 "
        f"**동의 요청서**를 만들어 상위 오케스트레이터에게 반환하세요(4-튜플의 "
        f"*권고 다음 단계*). 상위가 사용자에게 받아 `consent --relay` 로 전달합니다."
    )


# ---------------------------------------------------------------------------
# 증서 생성 — 두 채널
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """지금 시각(ISO-8601, 로컬 오프셋 포함). 초 단위까지만 — 감사 흔적에 충분하다."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _clean_identity(value: Any, *, label: str) -> str:
    """사람·중계자 식별자 정리 + 자리표시자 거부."""
    text = str(value or "").strip()
    if not text:
        raise ConsentError(f"{label} 가 비어 있습니다 — 누가 동의했는지 남겨야 합니다.")
    if _ANGLED.search(text):
        raise ConsentError(f"{label} 에 예시 자리표시자가 그대로 있습니다: {text!r}")
    return text


def grant_interactive(
    *,
    granted_by: Any = "",
    prompt: Optional[Callable] = None,
    isatty: Optional[bool] = None,
    now: Optional[Callable] = None,
    env: Optional[Mapping] = None,
    show: Optional[Callable] = None,
) -> ConsentRecord:
    """사람 채널 — **터미널 앞의 사람**이 고지를 읽고 확인 문구를 입력한다.

    Args:
        granted_by: 동의자 식별자. 비면 대화로 묻는다.
        prompt: 한 줄 입력 함수(기본 :func:`input`). 테스트가 대역을 주입한다.
        isatty: stdin 이 TTY 인가. ``None`` 이면 실제 ``sys.stdin`` 을 본다.
        now: 시각 생성기(테스트 주입용).
        env: 환경 매핑(테스트 주입용).
        show: 고지 출력 함수(기본 :func:`print`).

    Raises:
        ConsentError: 서브 에이전트 경로 / TTY 없음 / 확인 문구 불일치.

    Note:
        **TTY 요구가 이 채널의 핵심**이다. 헤드리스 에이전트 세션은 stdin 이 파이프이므로
        여기서 물리적으로 막힌다 — 룰북을 따르든 안 따르든.
    """
    _refuse_if_subagent(env)
    if isatty is None:
        import sys  # noqa: PLC0415 — 주입되지 않았을 때만 실제 stdin 을 본다
        isatty = bool(getattr(sys.stdin, "isatty", lambda: False)())
    if not isatty:
        raise ConsentError(
            "이 채널에는 사람이 없습니다(stdin 이 터미널이 아닙니다) — 사람 동의를 "
            "여기서 만들 수 없습니다. 터미널에서 직접 `python -m app.setup consent` 를 "
            "실행하거나, 사용자 채널을 가진 상위가 `consent --relay` 로 전달하세요."
        )
    emit = show if show is not None else print
    ask_raw = prompt if prompt is not None else input

    def ask(text: str) -> str:
        """입력 한 줄. **EOF·중단은 트레이스백이 아니라 '동의 안 함'이다.**

        stdin 이 TTY 라고 보고했지만 실제로는 아무도 없는 환경이 있다(일부 러너·의사
        터미널). 그때 파이썬 기본 동작은 ``EOFError`` 트레이스백인데, 그건 사람에게
        아무것도 알려주지 못한다 — 동의를 못 받았다고 말하는 편이 정확하다.
        """
        try:
            return str(ask_raw(text) or "")
        except (EOFError, KeyboardInterrupt):
            raise ConsentError(
                "입력이 끊겨 동의를 기록하지 않았습니다 — 이 자리에 실제로 사람이 "
                "없는 것으로 봅니다(TTY 라고 보고됐더라도). 터미널에서 직접 실행하거나 "
                "상위가 `consent --relay` 로 전달하세요.")

    for line in DISCLOSURE:
        emit(line)
    emit("")
    who = str(granted_by or "").strip() or ask("동의하는 사람(이름 또는 이메일): ")
    who = _clean_identity(who, label="동의자")
    emit("")
    emit(f"동의하려면 다음 중 하나를 그대로 입력하세요: {' 또는 '.join(CONFIRM_PHRASES)}")
    typed = ask("입력: ").strip()
    if typed.casefold() not in {p.casefold() for p in CONFIRM_PHRASES}:
        raise ConsentError(
            f"확인 문구가 일치하지 않아 동의를 기록하지 않았습니다(입력: {typed!r}). "
            f"동의하지 않는 것도 정상적인 결과입니다 — 설치는 여기서 멈춥니다."
        )
    return ConsentRecord(
        full_permissions=True,
        accepted_at=(now or _now_iso)(),
        channel=CHANNEL_HUMAN,
        granted_by=who,
    )


def grant_relay(
    *,
    granted_by: Any,
    statement: Any,
    relayed_by: Any,
    now: Optional[Callable] = None,
    env: Optional[Mapping] = None,
) -> ConsentRecord:
    """중계 채널 — **사용자 채널을 가진 상위**가 사람에게서 받은 동의를 전달한다.

    세 가지를 전부 요구하는 이유: 나중에 "이 동의는 어디서 왔나"를 물었을 때 답이 있어야
    한다. 사람(:paramref:`granted_by`) · 그 사람의 **원문**(:paramref:`statement`) ·
    중계자(:paramref:`relayed_by`) 가 그 답이다. 하나라도 비면 중계가 아니라 자기 승인이다.

    Raises:
        ConsentError: 서브 에이전트 경로 / 셋 중 하나라도 비었거나 자리표시자·기계 상투어.
    """
    _refuse_if_subagent(env)
    who = _clean_identity(granted_by, label="동의자(--granted-by)")
    relay = _clean_identity(relayed_by, label="중계자(--relayed-by)")
    said = str(statement or "").strip()
    if not said:
        raise ConsentError(
            "사용자 원문(--statement)이 비어 있습니다 — 사람이 실제로 무엇이라고 했는지 "
            "그대로 옮겨 적으세요. 없으면 그것은 중계가 아니라 자기 승인입니다.")
    if _ANGLED.search(said):
        raise ConsentError(f"사용자 원문에 예시 자리표시자가 그대로 있습니다: {said!r}")
    if said.strip(".!? ").casefold() in _PLACEHOLDER_STATEMENTS:
        raise ConsentError(
            f"사용자 원문이 기계 상투어입니다: {said!r} — 사람의 실제 문장을 옮겨 적으세요"
            f"(예: \"풀 퍼미션으로 돌려도 좋습니다, 위험은 이해했습니다\").")
    return ConsentRecord(
        full_permissions=True,
        accepted_at=(now or _now_iso)(),
        channel=CHANNEL_RELAY,
        granted_by=who,
        relayed_by=relay,
        statement=said,
    )


# ---------------------------------------------------------------------------
# 동의 요청서 — 서브가 **빈손으로 멈추지 않도록**
# ---------------------------------------------------------------------------


def consent_request(*, project_dir: str = ".") -> dict:
    """상위 오케스트레이터에게 올릴 **동의 요청서**(부작용 없음 — 누구나 만들 수 있다).

    "멈춰라"와 "너에겐 사용자 채널이 없다"의 모순을 푸는 산출물이다. 서브는 동의를
    만들지 못하는 대신 이것을 4-튜플의 *권고 다음 단계* 에 실어 반환하고, 상위는
    ``relay_command`` 를 사용자 응답으로 채워 실행한다.
    """
    return {
        "kind": "consent_request",
        "blocked_key": "consent.full_permissions",
        "why": ("이 설치는 풀 퍼미션 실행에 대한 **사람의 동의**를 전제로 합니다. "
                "동의는 사람에게서만 올 수 있으므로 서브 에이전트가 대신 만들 수 "
                "없습니다 — 상위가 사용자에게 받아 전달해야 진행됩니다."),
        "disclosure_version": DISCLOSURE_VERSION,
        "disclosure": list(DISCLOSURE),
        "ask_user": ("위 내용을 그대로 사용자에게 보여주고, 동의하는지 물으세요. "
                     "동의하면 사용자의 **원문 한 문장**과 이름을 그대로 받아 오세요."),
        "record_path": record_path(project_dir),
        "relay_command": [
            "python", "-m", "app.setup", "consent", "--relay",
            "--granted-by", "<사용자 이름 또는 이메일>",
            "--statement", "<사용자가 실제로 한 말 그대로>",
            "--relayed-by", "<중계한 오케스트레이터 식별자>",
        ],
        "human_command": ["python", "-m", "app.setup", "consent"],
        "note": ("사용자가 동의하지 않으면 그것으로 끝입니다 — 설치를 진행하지 마세요. "
                 "동의 거부는 오류가 아니라 정상적인 결과입니다."),
    }


# ---------------------------------------------------------------------------
# 증서 파일 입출력
# ---------------------------------------------------------------------------


def record_path(project_dir: str = ".", path: str = "") -> str:
    """증서 파일 경로(명시 경로 우선, 없으면 ``<project_dir>/setup-consent.json``)."""
    if path:
        return path
    return os.path.join(project_dir or ".", RECORD_FILENAME)


def load_record(project_dir: str = ".", path: str = "") -> Optional[ConsentRecord]:
    """증서를 읽는다. 파일이 없으면 ``None``(= 아직 동의 없음).

    Raises:
        ConsentError: 파일은 있는데 JSON 이 아니거나 증서로 볼 수 없을 때.
    """
    target = record_path(project_dir, path)
    if not os.path.exists(target):
        return None
    try:
        with open(target, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConsentError(f"동의 증서를 읽을 수 없습니다({target}): {exc}")
    return ConsentRecord.from_dict(raw)


def save_record(record: ConsentRecord, project_dir: str = ".", path: str = "") -> str:
    """증서를 저장하고 그 경로를 돌려준다(부모 디렉토리는 있다고 가정 — 배포 루트).

    ⚠️ 시크릿 값은 담기지 않는다. 다만 **사람 이름과 원문**이 들어가므로 gitignore 된다.
    """
    target = record_path(project_dir, path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    text = json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n"
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return target


# ---------------------------------------------------------------------------
# 게이트 판정(순수 술어 — Finding 으로 감싸는 쪽은 setup_validate)
# ---------------------------------------------------------------------------

#: :func:`attestation_problem` 이 돌려주는 문제 코드(기계가 읽는 안정 식별자).
PROBLEM_MISSING = "missing"        # 증서가 아예 없다
PROBLEM_DENIED = "denied"          # 증서가 "동의하지 않음"이다
PROBLEM_MISMATCH = "mismatch"      # 답변은 true 인데 증서는 그 값을 뒷받침하지 않는다
PROBLEM_STALE_DISCLOSURE = "stale_disclosure"  # 옛 고지에 대한 동의다


@dataclass
class AttestationProblem:
    """증서 검사 결과 하나(문제 코드 + 사람이 읽는 문구)."""

    code: str
    message: str
    hint: str = ""


def attestation_problem(
    answered: Any,
    record: Optional[ConsentRecord],
) -> Optional[AttestationProblem]:
    """답변의 동의 값이 **사람에게서 온 증서**로 뒷받침되는가(순수 술어).

    Args:
        answered: 답변 파일의 ``consent.full_permissions`` 값.
        record: :func:`load_record` 결과(없으면 ``None``).

    Returns:
        문제가 없으면 ``None``. 있으면 :class:`AttestationProblem`.

    Note:
        ``answered`` 가 True 가 아닌 경우는 **여기서 다루지 않는다** — 그건 "동의 안 함"
        이고 :func:`app.setup_validate._check_consent` 의 몫이다(메시지가 갈리면 사람이
        엉뚱한 곳을 고친다).
    """
    if answered is not True:
        return None
    if record is None:
        return AttestationProblem(
            PROBLEM_MISSING,
            "답변에는 동의(true)가 있는데 그 동의가 **어디서 왔는지**를 증명하는 "
            f"{RECORD_FILENAME} 이 없습니다 — 답변 파일에 true 를 적는 것만으로는 "
            "사람의 동의가 되지 않습니다.",
            "사람: 터미널에서 `python -m app.setup consent` / "
            "서브 에이전트: `python -m app.setup consent --request` 로 요청서를 만들어 "
            "상위에 반환 / 상위: `python -m app.setup consent --relay ...` 로 전달.",
        )
    if not record.full_permissions:
        return AttestationProblem(
            PROBLEM_DENIED,
            f"{RECORD_FILENAME} 은 **동의하지 않음**으로 기록돼 있는데 답변은 true 입니다 "
            "— 증서가 정본입니다.",
            "동의 의사가 바뀌었다면 사람이 다시 "
            "`python -m app.setup consent` 를 실행해 증서를 갱신하세요.",
        )
    if record.disclosure_version and record.disclosure_version != DISCLOSURE_VERSION:
        return AttestationProblem(
            PROBLEM_STALE_DISCLOSURE,
            f"동의 증서가 옛 고지(v{record.disclosure_version})에 대한 것입니다 "
            f"(현재 v{DISCLOSURE_VERSION}) — 무엇에 동의했는지가 달라졌습니다.",
            "사람이 새 고지를 읽고 다시 동의해야 합니다"
            "(`python -m app.setup consent`).",
        )
    return None
