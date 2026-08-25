"""계층별 operating frame(프롬프트 = 데이터) 보관 + 주입 헬퍼(설계 §3·§6.5).

프랙탈 디스패치 설계의 **살(intent framing)** 은 코드가 아니라 프롬프트로 잡는다
(설계 §1.3). 각 계층의 operating frame 을 이 패키지의 편집 가능한 데이터 파일
(``*.md``)로 보관하고, 파이썬(app/session_manager.py)이 세션/서브 구동 시 **핵심
지시(core instruction) 앞에 조합해 주입**한다. 강제·격리·수명·좀비방지 같은 기계적
보장은 여기 문구가 아니라 파이썬/OS(tini·killpg)가 담당한다.

프레임 파일:
    - ``central_agent_frame.md``   — 센트럴 에이전트 프레임(설계 §3.1, P2).
    - ``user_sub_frame.md``        — 사용자별 서브에이전트(센트럴 측) 프레임(설계 §3.2, P2).
    - ``container_agent_frame.md`` — 사용자 컨테이너 에이전트 프레임(설계 §3.3).
    - ``repo_sub_frame.md``        — 레포별 서브에이전트(leaf) 프레임(설계 §3.4).

공개 API(순수):
    - ``central_agent_frame()`` / ``user_sub_frame()``    센트럴 계층 프레임(캐시, P2).
    - ``container_agent_frame()`` / ``repo_sub_frame()``  프레임 텍스트(캐시).
    - ``compose(frame, core_instruction)``                프레임 + 핵심 지시 조합.
    - ``extract_completion_report(text)``                 리치 완료-리포트 블록 추출.
    - ``REPORT_BEGIN`` / ``REPORT_END``                   완료-리포트 마커 접두.

POLICY-ENCODING: 프레임 파일은 UTF-8(BOM 없음)·LF 로 읽는다.
"""

from __future__ import annotations

import os
import re
from typing import Optional

# 리치 완료-리포트 마커 접두(컨테이너 프레임이 이 마커로 리포트를 감싸도록 지시한다).
# 상위 관찰자(P1=파이썬 캡처, P2=센트럴 서브)가 이 마커로 티켓 리포트를 상관·추출한다.
# 형식: ``<BEGIN> <티켓키>`` ... 리포트 본문 ... ``<END> <티켓키>``.
REPORT_BEGIN = "===JAD-COMPLETION-REPORT-BEGIN"
REPORT_END = "===JAD-COMPLETION-REPORT-END"

_PROMPTS_DIR = os.path.dirname(os.path.abspath(__file__))

# 프레임 텍스트 캐시(파일 IO 1회). 파일은 인스턴스 산출물이 아니라 소스이므로 캐시 안전.
_FRAME_CACHE: dict = {}

# 완료-리포트 블록 추출용 정규식(BEGIN…END, DOTALL). 티켓키는 마커 뒤 토큰에서 뽑는다.
_REPORT_BLOCK_RE = re.compile(
    re.escape(REPORT_BEGIN) + r"(?P<begin_tail>[^\n]*)\n(?P<body>.*?)"
    + re.escape(REPORT_END) + r"[^\n]*",
    re.DOTALL,
)


def load_frame(name: str) -> str:
    """``app/prompts/<name>`` 프레임 파일을 UTF-8 로 읽어 반환(캐시).

    ``name`` 은 확장자 포함 파일명(예: ``container_agent_frame.md``). 파일 부재는
    ``FileNotFoundError`` 로 명확히 실패시킨다(조용한 빈 프레임 방지).
    """
    if name in _FRAME_CACHE:
        return _FRAME_CACHE[name]
    path = os.path.join(_PROMPTS_DIR, name)
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    _FRAME_CACHE[name] = text
    return text


def central_agent_frame() -> str:
    """센트럴 에이전트 operating frame(설계 §3.1, P2) 텍스트."""
    return load_frame("central_agent_frame.md")


def user_sub_frame() -> str:
    """사용자별 서브에이전트(센트럴 측) operating frame(설계 §3.2, P2) 텍스트."""
    return load_frame("user_sub_frame.md")


def container_agent_frame() -> str:
    """사용자 컨테이너 에이전트 operating frame(설계 §3.3) 텍스트."""
    return load_frame("container_agent_frame.md")


def repo_sub_frame() -> str:
    """레포별 서브에이전트(leaf) operating frame(설계 §3.4) 텍스트."""
    return load_frame("repo_sub_frame.md")


def compose(frame: str, core_instruction: str) -> str:
    """operating frame + 핵심 지시(core instruction)를 하나의 주입 문자열로 조합.

    프레임(행동 규약)을 앞에, 핵심 지시(이번에 무엇을 할지)를 뒤에 두고 구분선으로
    가른다. 어느 한쪽이 비면 다른 한쪽만 반환한다(방어적).
    """
    f = (frame or "").strip()
    c = (core_instruction or "").strip()
    if not f:
        return c
    if not c:
        return f
    return f + "\n\n---\n\n# 이번 핵심 지시\n\n" + c


def extract_completion_report(text: Optional[str]) -> "Optional[tuple[str, str]]":
    """텍스트에서 **첫** 리치 완료-리포트 블록을 추출 → ``(티켓키, 리포트본문)``.

    컨테이너 프레임이 방출하도록 지시한 ``BEGIN <티켓키> … END`` 블록을 찾는다.
    티켓키는 BEGIN 마커 뒤 꼬리(``=== 제거 후 첫 토큰``)에서 뽑는다. 블록이 없으면
    None. 리포트 본문은 마커를 제외한 안쪽 텍스트(양끝 공백 strip).
    """
    if not text:
        return None
    m = _REPORT_BLOCK_RE.search(text)
    if not m:
        return None
    begin_tail = m.group("begin_tail") or ""
    # 꼬리에서 트레일링 '=' 와 공백을 걷어낸 첫 토큰이 티켓키(예: " PROJ-1===" → "PROJ-1").
    ticket = begin_tail.strip().strip("=").strip()
    ticket = ticket.split()[0] if ticket else ""
    body = (m.group("body") or "").strip()
    return ticket, body


__all__ = [
    "REPORT_BEGIN",
    "REPORT_END",
    "central_agent_frame",
    "compose",
    "container_agent_frame",
    "extract_completion_report",
    "load_frame",
    "repo_sub_frame",
    "user_sub_frame",
]
