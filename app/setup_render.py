"""config.yaml 렌더러 — **예시 파일을 템플릿으로** 쓰고 주석을 보존한다.

역할:
    검증(:mod:`app.setup_validate`)을 통과한 답변으로 ``config/config.yaml`` 을 만든다.

왜 문자열 조립이 아니라 템플릿인가:
    ``config/config.example.yaml`` 은 단순한 예시가 아니라 **사용자 안내의 단일 원천**이다
    — 각 항목이 무엇이고, 왜 필요하고, 값을 어디서 얻는지가 전부 그 파일 주석에 있다.
    ``yaml.safe_dump`` 로 새로 뱉으면 그 안내가 **전부 사라진다**. 설치자는 config.yaml 을
    나중에 반드시 다시 열어 고치는데(운영은 계속 바뀐다), 그때 아무 설명이 없는 파일을
    마주하게 된다. 그래서 이 모듈은 예시 파일을 **원문 그대로 두고 값만 바꾼다**:

        주석·빈 줄·항목 순서·정렬은 한 글자도 건드리지 않고,
        ``key: <값>`` 의 **값 자리만** 치환한다.

    (round-trip YAML 라이브러리(ruamel 등)를 새로 의존성에 넣지 않는 이유이기도 하다 —
    이 리포의 런타임 의존성은 5개뿐이고, 설치 도구 하나 때문에 늘릴 값어치가 없다.
    대신 렌더 결과를 ``yaml.safe_load`` 로 **되읽어 검증**한다 — :func:`_verify_roundtrip`.)

답한 것만 쓴다(중요):
    렌더러는 :attr:`app.setup_validate.ValidationResult.explicit` — 설치자가 **실제로 답한**
    항목만 쓴다. 스키마 기본값까지 파일에 박으면 ``deploy.profile`` 하나로 나머지가 파생되는
    설계(:data:`app.setup_schema.PROFILE_DEFAULTS`)가 무력화된다. 예: 프로파일을
    ``cloud_vm`` 으로 고른 사람이 ``docker_host`` 를 답하지 않았는데 스키마 기본값
    (``unix:///var/run/docker.sock``)이 파일에 박히면, 파생돼야 할 socket-proxy 설정이
    조용히 소켓 직결로 바뀐다.

시크릿 규율:
    ``secret=True`` 필드는 **절대 쓰지 않는다**(현재 스키마엔 없지만 방어). 참조 자리에
    값이 온 경우는 검증기가 이미 막았고, 렌더러도 한 번 더 거부한다.

POLICY-ENCODING: 생성 파일은 UTF-8(BOM 없음)·LF. 이 파일도 마찬가지.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app import setup_schema as S

#: 템플릿 = 사람이 읽는 예시(주석의 단일 원천).
DEFAULT_TEMPLATE_PATH = "config/config.example.yaml"

#: 산출 위치(= :data:`app.config.DEFAULT_CONFIG_PATH`).
DEFAULT_OUTPUT_PATH = "config/config.yaml"

#: ``key:`` / ``key: value`` 줄. 리스트 항목(``- x``)·주석 줄은 매치되지 않는다.
_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_.\-]*)[ \t]*:(?=[ \t]|$)(.*)$")

#: 남아 있으면 안 되는 예시 자리표시자(``<PROJECT_KEY>`` 등).
_PLACEHOLDER = re.compile(r"<[^<>\s][^<>]*>")

#: 따옴표 없이 쓰면 YAML 이 다른 타입으로 읽어 버리는 문자열들.
_YAML_RESERVED = {
    "true", "false", "yes", "no", "on", "off", "null", "none", "~", "y", "n",
}

#: 숫자로 읽힐 문자열(따옴표가 필요하다 — 예: done_transition_id: "41").
_NUMERIC = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")

#: **날짜/시각으로 읽힐** 문자열. YAML 1.1 은 ``2026-08-25T09:00:00+09:00`` 을 따옴표 없이
#: 두면 ``datetime`` 객체로 읽는다 — ``consent.accepted_at`` 이 정확히 그 모양이라
#: 따옴표를 씌우지 않으면 문자열이 아닌 값이 되어 버린다(실제로 밟은 함정).
_TIMESTAMPISH = re.compile(r"^\d{4}-\d{1,2}-\d{1,2}([T ].*)?$")


class RenderError(Exception):
    """렌더 실패(템플릿 부재·시크릿 값 감지·round-trip 불일치 등)."""


@dataclass
class RenderResult:
    """렌더 결과 + 사람에게 알려야 할 것들.

    Attributes:
        text: 완성된 config.yaml 본문(LF).
        replaced: 템플릿의 기존 줄에서 **값만 바꾼** 키들.
        inserted: 템플릿에 자리가 없어 **새로 넣은** 키들(예: ``jira.watcher_email``).
        unanswered: 설치자가 답하지 않아 **템플릿 값 그대로 남은** 스키마 항목들.
            오류는 아니지만(선택 항목이거나 기본값이 맞을 수 있다) 알고는 있어야 한다 —
            특히 ``jira.custom_fields`` 처럼 예시 값이 *다른 조직의 id* 인 항목이 있다.
        placeholders: 산출물에 아직 ``<...>`` 자리표시자가 남은 줄 ``(줄번호, 원문)``.
            렌더러는 스키마 항목만 채우므로, 스키마가 묻지 않는 예시 값
            (``run.dlc_meta_repo_url`` 등)은 사람이 직접 고쳐야 한다.
    """

    text: str = ""
    replaced: list = field(default_factory=list)
    inserted: list = field(default_factory=list)
    unanswered: list = field(default_factory=list)
    placeholders: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """기계가 읽는 요약(본문은 빼고 — 호출부가 이미 들고 있다)."""
        return {
            "replaced": list(self.replaced),
            "inserted": list(self.inserted),
            "unanswered": list(self.unanswered),
            "placeholders": [{"line": ln, "text": tx} for ln, tx in self.placeholders],
        }

    def format_text(self) -> str:
        """사람이 읽는 요약."""
        lines = [
            f"값 치환 {len(self.replaced)}건, 신규 추가 {len(self.inserted)}건 "
            f"(템플릿 주석은 그대로 보존)."
        ]
        if self.inserted:
            lines.append("  추가: " + ", ".join(self.inserted))
        if self.unanswered:
            lines.append(
                "  ⚠️ 답하지 않아 예시 값이 남은 항목(확인 필요): "
                + ", ".join(self.unanswered)
            )
        if self.placeholders:
            lines.append("  ⚠️ 아직 자리표시자가 남은 줄(직접 채우세요):")
            for ln, tx in self.placeholders:
                lines.append(f"      {ln}: {tx.strip()}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# YAML 스칼라 표기
# ---------------------------------------------------------------------------


def _needs_quote(s: str) -> bool:
    """이 문자열을 따옴표 없이 쓰면 YAML 이 다르게 읽는가."""
    if s == "" or s != s.strip():
        return True
    if s.lower() in _YAML_RESERVED or _NUMERIC.match(s) or _TIMESTAMPISH.match(s):
        return True
    if s[0] in "#&*!|>%@`\"'[]{},?:-":
        return True
    if ": " in s or " #" in s or s.endswith(":"):
        return True
    return any(ch in s for ch in ("\n", "\r", "\t"))


def _quote(s: str) -> str:
    """큰따옴표 표기(YAML double-quoted — 백슬래시·따옴표 이스케이프)."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def format_scalar(value: Any) -> str:
    """블록 컨텍스트의 값 표기(따옴표는 필요할 때만 — 템플릿 문체 유지)."""
    if value is None:
        return '""'
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_format_flow_item(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(
            f"{_format_flow_item(k)}: {_format_flow_item(v)}" for k, v in value.items()
        ) + "}"
    s = str(value)
    return _quote(s) if _needs_quote(s) else s


def _format_flow_item(value: Any) -> str:
    """플로우(``[a, b]``·``{k: v}``) 안의 원소 표기.

    문자열은 **항상** 따옴표로 감싼다 — 플로우 안에서는 공백·쉼표·괄호가 구분자로 읽혀
    조용히 다른 값이 되기 쉽고(``[해야 할 일]``), 템플릿의 표기(``["해야 할 일"]``)와도
    맞는다.
    """
    if isinstance(value, str):
        return _quote(value)
    return format_scalar(value)


# ---------------------------------------------------------------------------
# 템플릿 파싱(줄 단위 — 주석·정렬을 원문 그대로 남기기 위해)
# ---------------------------------------------------------------------------


def _split_comment(rest: str) -> tuple:
    """``key:`` 뒤 나머지를 ``(값 부분, 주석 부분)`` 으로 나눈다(YAML 규칙).

    따옴표 안의 ``#`` 은 주석이 아니고, 주석 ``#`` 은 공백 뒤에 온다.
    """
    in_single = in_double = False
    i = 0
    while i < len(rest):
        ch = rest[i]
        if in_single:
            if ch == "'":
                in_single = False
        elif in_double:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_double = False
        elif ch == "'":
            in_single = True
        elif ch == '"':
            in_double = True
        elif ch == "#" and (i == 0 or rest[i - 1] in " \t"):
            return rest[:i], rest[i:]
        i += 1
    return rest, ""


@dataclass
class _Slot:
    """템플릿에서 값 하나가 사는 자리."""

    lineno: int          # 0-기준 줄 인덱스
    indent: str
    key: str
    path: str            # 점 표기 경로
    value: str           # 값 원문(주석 제외, 양끝 공백 제거)
    comment: str         # 주석 원문("" 또는 "# ...")
    comment_col: int     # 주석이 시작하던 열(정렬 유지용, 없으면 -1)
    block_parent: bool   # 값이 비어 있고 자식 블록을 가지는 자리


def _parse_template(lines: list) -> dict:
    """줄 목록 → ``{점 표기 경로: _Slot}``. 들여쓰기 스택으로 경로를 추적한다."""
    slots: dict = {}
    stack: list = []  # [(indent_len, key)]
    for idx, line in enumerate(lines):
        m = _KEY_LINE.match(line)
        if not m:
            continue
        indent, key, rest = m.group(1), m.group(2), m.group(3)
        depth = len(indent)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        path = ".".join([k for _d, k in stack] + [key])
        value_part, comment = _split_comment(rest)
        value = value_part.strip()
        slots[path] = _Slot(
            lineno=idx, indent=indent, key=key, path=path, value=value,
            comment=comment.rstrip(),
            # 주석이 시작하던 열 — 값 길이가 바뀌어도 주석 정렬을 지키려고 기억해 둔다.
            comment_col=(len(line) - len(comment.rstrip()) if comment else -1),
            # 값이 비어 있으면 자식 블록을 가진 자리다(``jira:`` · ``custom_fields:``).
            block_parent=(value == ""),
        )
        if value == "":
            stack.append((depth, key))
    return slots


def _compose_line(slot: _Slot, new_value: str) -> str:
    """``indent + key: + 값 + (원래 열의 주석)`` 한 줄을 만든다(정렬 최대한 유지)."""
    head = f"{slot.indent}{slot.key}: {new_value}"
    if not slot.comment:
        return head
    pad = max(slot.comment_col - len(head), 1)
    return head + " " * pad + slot.comment


# ---------------------------------------------------------------------------
# 렌더링
# ---------------------------------------------------------------------------


def _child_lines(lines: list, parent: _Slot) -> tuple:
    """블록 부모의 자식 줄 범위 ``(시작, 끝)``(끝은 배타적)."""
    depth = len(parent.indent)
    start = parent.lineno + 1
    end = start
    for idx in range(start, len(lines)):
        line = lines[idx]
        if not line.strip():
            end = idx + 1
            continue
        cur_indent = len(line) - len(line.lstrip())
        if cur_indent <= depth:
            break
        end = idx + 1
    # 블록 끝에 붙은 빈 줄은 블록 소유가 아니다(다음 섹션의 여백).
    while end > start and not lines[end - 1].strip():
        end -= 1
    return start, end


def _rewrite_map_block(lines: list, parent: _Slot, mapping: Mapping) -> list:
    """STRING_MAP 블록을 답변으로 다시 쓴다(자식별 주석은 살려서).

    규칙:
        - 템플릿에 있던 자식 키가 답변에도 있으면 **그 줄의 값만** 바꾼다(주석 보존).
        - 템플릿에만 있는 자식 키는 **지운다.** 예시 값은 *다른 조직의 커스텀필드 id* 라
          남겨 두면 조용한 오작동이 된다(스키마 의미상 "키 없음"과 ``""`` 는 다르다).
        - 답변에만 있는 키는 블록 끝에 추가한다.
        - 자식 키 바로 위의 전체줄 주석은 그 키와 운명을 같이한다(키가 지워지면 함께).
    """
    start, end = _child_lines(lines, parent)
    child_indent = parent.indent + "  "
    known: dict = {}
    for idx in range(start, end):
        m = _KEY_LINE.match(lines[idx])
        if m and len(m.group(1)) > len(parent.indent):
            known[m.group(2)] = idx

    out: list = []
    pending: list = []
    emitted: set = set()
    for idx in range(start, end):
        line = lines[idx]
        m = _KEY_LINE.match(line)
        if not m or len(m.group(1)) <= len(parent.indent):
            pending.append(line)          # 주석·빈 줄 — 다음 키의 생사에 따라간다
            continue
        key = m.group(2)
        if key in mapping:
            out.extend(pending)
            indent, rest = m.group(1), m.group(3)
            value_part, comment = _split_comment(rest)
            slot = _Slot(
                lineno=idx, indent=indent, key=key, path="", value=value_part.strip(),
                comment=comment.rstrip(),
                comment_col=(len(line) - len(comment.rstrip()) if comment else -1),
                block_parent=False,
            )
            out.append(_compose_line(slot, format_scalar(mapping[key])))
            emitted.add(key)
        # 답변에 없는 키 → 줄도 pending 주석도 버린다.
        pending = []
    for key, value in mapping.items():
        if key not in emitted:
            out.append(f"{child_indent}{key}: {format_scalar(value)}")
    out.extend(pending)                    # 블록 끝에 남은 주석은 보존
    if not out:
        out.append(f"{child_indent}{{}}")  # 빈 매핑도 문법적으로 유효해야 한다
    return out


def _insert_position(lines: list, parent_path: str, slots: Mapping) -> Optional[tuple]:
    """새 키를 넣을 자리 ``(줄 인덱스, 들여쓰기)``. 부모 섹션이 없으면 None."""
    parent = slots.get(parent_path)
    if parent is None or not parent.block_parent:
        return None
    start, end = _child_lines(lines, parent)
    # ⚠️ **직계 자식만** 본다 — 손자(``custom_fields`` 안의 ``start_date``)를 마지막 키로
    # 잡으면 들여쓰기가 한 단계 깊어져 엉뚱한 블록 안에 새 키가 들어간다.
    child_indent: Optional[str] = None
    last_key_line: Optional[int] = None
    for idx in range(start, end):
        m = _KEY_LINE.match(lines[idx])
        if not m or len(m.group(1)) <= len(parent.indent):
            continue
        if child_indent is None:
            child_indent = m.group(1)
        if len(m.group(1)) == len(child_indent):
            last_key_line = idx
    if child_indent is None or last_key_line is None:
        return start, parent.indent + "  "
    # 마지막 직계 자식이 블록을 가지면(예: ``custom_fields:``) 그 블록 전체를 건너뛴다.
    idx = last_key_line + 1
    while idx < end:
        line = lines[idx]
        if line.strip() and (len(line) - len(line.lstrip())) <= len(child_indent):
            break
        idx += 1
    return idx, child_indent


def render_config(
    values: Mapping,
    *,
    template_text: Optional[str] = None,
    template_path: str = DEFAULT_TEMPLATE_PATH,
) -> RenderResult:
    """답변으로 config.yaml 본문을 만든다(템플릿 주석 보존).

    Args:
        values: 설치자가 **실제로 답한** 항목 ``{점 표기 키: 값}``
            (:attr:`app.setup_validate.ValidationResult.explicit`). 스키마에 없는 키는
            무시한다 — 이 렌더러는 스키마가 아는 자리만 건드린다.
        template_text: 템플릿 본문을 직접 줄 때(테스트·임베드용).
        template_path: 템플릿 파일 경로(기본 ``config/config.example.yaml``).

    Returns:
        :class:`RenderResult`.

    Raises:
        RenderError: 템플릿을 읽을 수 없거나, 시크릿 **값**을 쓰려 하거나,
            산출물을 되읽었을 때 의도한 값이 아닌 경우(:func:`_verify_roundtrip`).
    """
    if template_text is None:
        if not os.path.exists(template_path):
            raise RenderError(
                f"템플릿을 찾을 수 없습니다: {template_path} "
                f"(이 파일이 주석=사용자 안내의 단일 원천입니다)"
            )
        with open(template_path, "r", encoding="utf-8") as fh:
            template_text = fh.read()

    lines = template_text.replace("\r\n", "\n").split("\n")
    slots = _parse_template(lines)

    # 스키마가 아는 항목만, 선언 순서대로 처리한다(출력 결정성).
    wanted: list = []
    for f in S.iter_fields():
        if f.key not in values:
            continue
        if f.secret:
            raise RenderError(
                f"{f.key} 는 시크릿 **값**이라 config.yaml 에 쓸 수 없습니다 "
                f"(시크릿 파일로 저장하고 참조만 두세요)."
            )
        wanted.append((f, values[f.key]))

    result = RenderResult()
    # 블록 재작성·삽입은 줄 인덱스를 바꾸므로, 먼저 '치환 계획'을 모아 뒤에서부터 적용한다.
    replacements: list = []   # (start, end, [새 줄들], 키, 종류)
    for f, value in wanted:
        slot = slots.get(f.key)
        if slot is not None and slot.block_parent and isinstance(value, Mapping):
            start, end = _child_lines(lines, slot)
            replacements.append((start, end, _rewrite_map_block(lines, slot, value),
                                 f.key, "replace"))
            result.replaced.append(f.key)
            continue
        if slot is not None and slot.block_parent:
            # 블록으로 적혀 있던 리스트 등 → 부모 줄에 플로우 표기로 접어 넣는다.
            start, end = _child_lines(lines, slot)
            replacements.append((slot.lineno, end,
                                 [_compose_line(slot, format_scalar(value))],
                                 f.key, "replace"))
            result.replaced.append(f.key)
            continue
        if slot is not None:
            replacements.append((slot.lineno, slot.lineno + 1,
                                 [_compose_line(slot, format_scalar(value))],
                                 f.key, "replace"))
            result.replaced.append(f.key)
            continue
        # 템플릿에 자리가 없다 — 부모 섹션 끝에 새로 넣는다.
        parent_path = f.key.rsplit(".", 1)[0]
        pos = _insert_position(lines, parent_path, slots)
        if pos is None:
            raise RenderError(
                f"{f.key} 를 넣을 자리를 템플릿에서 찾지 못했습니다"
                f"(섹션 {parent_path!r} 부재) — 템플릿과 스키마가 어긋났습니다."
            )
        idx, child_indent = pos
        replacements.append((idx, idx,
                             [f"{child_indent}{f.key.rsplit('.', 1)[1]}: "
                              f"{format_scalar(value)}"],
                             f.key, "insert"))
        result.inserted.append(f.key)

    for start, end, new_lines, _key, _kind in sorted(replacements, key=lambda r: -r[0]):
        lines[start:end] = new_lines

    text = "\n".join(lines)
    _verify_roundtrip(text, wanted)

    result.text = text
    result.unanswered = [f.key for f in S.iter_fields() if f.key not in values]
    result.placeholders = _find_placeholders(text)
    return result


def _find_placeholders(text: str) -> list:
    """산출물에서 ``<...>`` 자리표시자가 **값 자리에** 남은 줄 ``[(줄번호, 원문)]``.

    주석은 무시한다 — 주석 속 ``<deploy-user>`` 는 설명이지 값이 아니다. 스키마가 묻지
    않는 예시 값(``run.dlc_meta_repo_url`` 등)이 그대로 남는 것을 잡으라고 있는 검사다.
    """
    out: list = []
    for i, line in enumerate(text.split("\n")):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_LINE.match(line)
        body = _split_comment(m.group(3) if m else line)[0]
        if _PLACEHOLDER.search(body):
            out.append((i + 1, line))
    return out


def _verify_roundtrip(text: str, wanted: list) -> None:
    """산출물을 되읽어 **의도한 값이 실제로 그 자리에 있는지** 확인한다.

    줄 단위 편집은 주석을 지키는 대신 문법적으로 취약하다 — 그 취약함을 사람이 아니라
    파서가 잡게 한다. 하나라도 어긋나면 파일을 쓰기 전에 실패한다(조용한 오설정 금지).
    """
    import yaml  # 지연 import — 이 모듈을 파싱 없이 쓰는 소비처가 있을 수 있다

    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise RenderError(f"렌더 결과가 올바른 YAML 이 아닙니다: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RenderError("렌더 결과의 최상위가 매핑이 아닙니다.")

    for f, value in wanted:
        node: Any = loaded
        for part in f.key.split("."):
            if not isinstance(node, dict) or part not in node:
                raise RenderError(f"렌더 결과에 {f.key} 가 없습니다(템플릿 편집 실패).")
            node = node[part]
        expected = list(value) if isinstance(value, (list, tuple)) else value
        if isinstance(value, Mapping):
            expected = dict(value)
        if node != expected:
            raise RenderError(
                f"렌더 결과의 {f.key} 값이 의도와 다릅니다"
                f"(되읽은 타입 {type(node).__name__}) — 템플릿 편집 실패."
            )


# ---------------------------------------------------------------------------
# 파일 쓰기(파괴적 동작 방지)
# ---------------------------------------------------------------------------


def write_config(text: str, path: str = DEFAULT_OUTPUT_PATH, *,
                 force: bool = False, backup: bool = True) -> str:
    """렌더 결과를 파일로 쓴다. 이미 있으면 **먼저 백업**하고, 없으면 그냥 만든다.

    Args:
        text: :attr:`RenderResult.text`.
        path: 산출 경로.
        force: 기존 파일이 있을 때 덮어쓸지. False 면 :class:`RenderError`.
        backup: 덮어쓰기 전 ``<path>.bak-<타임스탬프>`` 로 백업할지.

    Returns:
        만든 백업 파일 경로(백업하지 않았으면 "").

    POLICY-ENCODING: UTF-8(BOM 없음)·LF 로 쓴다(``newline=""`` + 명시 ``\\n``).
    """
    backup_path = ""
    if os.path.exists(path):
        if not force:
            raise RenderError(
                f"이미 파일이 있습니다: {path} — 덮어쓰려면 --force 를 주세요"
                f"(그때 {path}.bak-<타임스탬프> 로 백업합니다)."
            )
        if backup:
            backup_path = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
            with open(path, "r", encoding="utf-8") as fh:
                previous = fh.read()
            with open(backup_path, "w", encoding="utf-8", newline="") as fh:
                fh.write(previous)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    body = text if text.endswith("\n") else text + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(body.replace("\r\n", "\n"))
    return backup_path
