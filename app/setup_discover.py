"""Jira 인스턴스 **조회(discovery)** — 설치자가 값을 *타이핑하지 않게* 한다.

역할:
    :mod:`app.setup_validate` 는 "답변이 스키마에 맞는가"를 보고, :mod:`app.setup_doctor`
    는 "그 설정으로 실제로 붙는가"를 본다. 둘 다 **답이 이미 있다**는 전제 위에 서 있다.
    이 모듈은 그 앞자리 — **답을 어디서 얻는가**를 맡는다.

왜 필요한가(실제로 반복해서 밟은 함정):
    Jira 는 화면에 보이는 **표시명**과 API 가 쓰는 **id/name** 이 어긋날 수 있고, JQL 은
    name 으로 거는데 전이는 id 로 건다. 커스텀필드 id 와 전이 id 는 **인스턴스마다 완전히
    다르다** — ``customfield_10015`` 는 이 코드가 처음 운영된 인스턴스의 값일 뿐이다.
    그런데 ``render`` 로 만든 config.yaml 에는 그 예시 id 가 **형식상 유효한 모양 그대로**
    남아 눈으로 넘기기 쉽다. 그 결과가 조용한 오작동이다:

        - 상태 이름이 어긋나면 폴러는 **아무 티켓도 못 찾는다**(에러도 안 난다).
        - 없는 커스텀필드를 보내면 Jira 가 400 을 낸다(착수·완료가 통째로 막힌다).
        - 전이 id 가 남의 인스턴스 값이면 **엉뚱한 전이**를 실행할 수도 있다.

    사람이 눈으로 옮겨 적는 한 이 어긋남은 계속 재발한다. 그래서 설치 시점에 **그
    인스턴스를 실제로 조회해 존재하는 값만** 고르게 하고, 상태·전이는 **id 와 name 을
    함께** 남긴다(:func:`app.config._named_refs`). 그러면 불일치가 구조적으로 사라진다.

무엇을 조회하는가:
    ``custom_fields``  ``GET /rest/api/3/field`` — 논리 키별로 **후보를 추려 제시**한다.
    ``statuses``       ``GET /rest/api/3/project/{key}/statuses`` — 이슈 타입별 실제 상태.
    ``transitions``    ``GET /rest/api/2/issue/{key}/transitions`` — id·name·목표 상태.
    ``labels``         ``GET /rest/api/3/label`` — opt-out 라벨 참고(⚠️ 아래 주의).
    ``account``        ``GET /rest/api/3/myself`` — ``accountId``(사용자 온보딩에 필요).

⚠️ **자동 선택하지 않는다**(커스텀필드):
    이름이 **정확히** 일치하는 필드가 **딱 하나**일 때만 고른다. 비슷한 이름이 여럿이거나
    부분 일치뿐이면 **고르지 않고 후보만** 보여준다. 잘못 자동선택한 필드 id 는 검증도
    진단도 통과한 뒤 운영에서 400 으로 터지는데, 그때는 아무도 이 선택을 기억하지 못한다
    — "모르겠으면 사람에게 묻는다"가 언제나 싸다.

⚠️ **라벨은 조회보다 안내가 중요하다**:
    Jira 라벨은 미리 만들어 두지 않아도 티켓에 입력하는 순간 생긴다. 그래서 "이 라벨이
    목록에 없다"는 오류가 아니며, 설치자에게 정말 필요한 정보는 *그 라벨을 티켓에 붙이면
    자동화에서 제외된다* 는 규칙 쪽이다(:data:`OPTOUT_LABEL_GUIDE`).

의존성 주입:
    Jira 에 닿는 경로는 전부 ``client=`` 로 대역을 받는다(:mod:`app.setup_doctor` 와 같은
    규율) — CI 는 네트워크 없이 전 경로를 돈다.

시크릿 규율:
    토큰 **값**은 출력·로그·예외 어디에도 싣지 않는다. 클라이언트 생성은
    :func:`app.setup_doctor._jira_client` 를 재사용한다(이메일·토큰 해석 규칙이 두 벌이
    되면 갈라진다). ``accountId``·이메일은 시크릿이 아니라 **온보딩에 필요한 식별자**라
    그대로 보여 준다.

POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app import setup_schema as S
from app.jira_client import _normalize_name

#: 조회 항목 상태.
STATUS_OK = "ok"       # 조회 성공
STATUS_SKIP = "skip"   # 이 환경/설정에서는 조회할 수 없다(설정 부재 등)
STATUS_FAIL = "fail"   # 조회를 시도했으나 실패했다(→ non-zero exit)

#: 실행 순서 = 의존 순서(계정 → 필드 → 상태 → 전이 → 라벨).
SECTION_ORDER: tuple = ("account", "custom_fields", "statuses", "transitions", "labels")

#: 논리 커스텀필드 키 → **이름 후보**(한국어·영어). 이름이 조직마다 달라서 정답이 아니라
#: *후보를 추리는* 데만 쓴다. 정확히 일치하는 필드가 유일할 때만 자동 선택한다.
#: 논리 키 목록 자체의 정본은 :data:`app.setup_schema.JIRA_CUSTOM_FIELD_KEYS` 다.
FIELD_NAME_ALIASES: dict = {
    "start_date": ("시작날짜", "시작 날짜", "시작일", "착수일",
                   "start date", "startdate", "start"),
    "due_date": ("마감일", "마감 일자", "기한", "종료일",
                 "due date", "duedate", "due"),
    "actual_start": ("실제 시작일", "실제시작일", "실제 착수일", "실제 시작",
                     "actual start", "actual start date", "actual start time"),
    "actual_end": ("실제 종료일", "실제종료일", "실제 완료일", "실제 종료",
                   "actual end", "actual end date", "actual end time"),
}

#: 후보 등급. ``exact`` 만 자동 선택의 근거가 된다.
TIER_EXACT = "exact"        # 이름이 후보와 정확히 일치(공백·대소문자 무시)
TIER_PARTIAL = "partial"    # 후보가 이름 안에 들어 있다(참고용 — 자동 선택 근거 아님)

#: opt-out 라벨에 대해 설치자가 실제로 알아야 하는 것(조회 결과보다 이쪽이 중요하다).
OPTOUT_LABEL_GUIDE = (
    "이 라벨을 티켓에 붙이면 그 티켓은 자동화에서 제외된다 — 신규 착수를 하지 않고, "
    "이미 추적 중이면 취소로 수렴한다. Jira 라벨은 미리 만들어 둘 필요가 없다(티켓에 "
    "입력하는 순간 생성된다). 그러니 아래 목록에 없어도 정상이며, 팀에 '이 라벨을 붙이면 "
    "자동화가 손대지 않는다'는 사실을 알리는 쪽이 훨씬 중요하다."
)


class DiscoveryError(Exception):
    """조회 자체를 시작할 수 없을 때(알 수 없는 섹션 이름 등)."""


# ---------------------------------------------------------------------------
# 결과 자료구조
# ---------------------------------------------------------------------------


@dataclass
class Section:
    """조회 항목 하나의 결과.

    Attributes:
        name: :data:`SECTION_ORDER` 중 하나.
        status: :data:`STATUS_OK` | :data:`STATUS_SKIP` | :data:`STATUS_FAIL`.
        summary: 한 줄 요약(사람이 읽는 출력의 헤더).
        data: 기계가 읽는 조회 결과(``--json`` · 후속 대화형 온보딩·웹 UI 가 소비).
        hint: 실패·건너뜀일 때 무엇을 하면 되는지.
        lines: 사람이 읽는 본문(들여쓰기 없이 — 출력부가 붙인다).
    """

    name: str
    status: str
    summary: str
    data: dict = field(default_factory=dict)
    hint: str = ""
    lines: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """실패가 아니면 True(건너뜀은 실패가 아니다)."""
        return self.status != STATUS_FAIL

    def to_dict(self) -> dict:
        """JSON 직렬화용."""
        return {"name": self.name, "status": self.status, "summary": self.summary,
                "hint": self.hint, "data": self.data}


@dataclass
class DiscoveryResult:
    """조회 전체 결과 — CLI·후속 대화형 온보딩·웹 UI 공용 반환값."""

    sections: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """실패한 항목이 하나도 없으면 True(= 종료코드 0)."""
        return all(s.ok for s in self.sections)

    def get(self, name: str) -> Optional[Section]:
        """이름으로 항목 조회(없으면 None)."""
        return next((s for s in self.sections if s.name == name), None)

    def suggested_answers(self) -> dict:
        """조회로 **확정된 것만** 담은 답변 조각(점 표기 키 → 값).

        ⚠️ 확정하지 못한 것은 **넣지 않는다.** 추측으로 채운 값은 검증도 진단도 통과한 뒤
        운영에서 터지고, 그때는 그게 추측이었다는 사실이 남아 있지 않다. 비어 있는 자리는
        사람이 후보를 보고 고르라는 뜻이다.
        """
        out: dict = {}
        fields_section = self.get("custom_fields")
        if fields_section is not None and fields_section.status == STATUS_OK:
            picked = {k: v["selected"]
                      for k, v in (fields_section.data.get("logical_keys") or {}).items()
                      if v.get("selected")}
            if picked:
                out["jira.custom_fields"] = picked
        statuses = self.get("statuses")
        if statuses is not None and statuses.status == STATUS_OK:
            for key in ("trigger_statuses", "cancel_statuses"):
                refs = [{"id": e["id"], "name": e["name"]}
                        for e in (statuses.data.get("configured") or {}).get(key, [])
                        if e.get("id")]
                if refs:
                    out[f"jira.{key}"] = refs
        transitions = self.get("transitions")
        if transitions is not None and transitions.status == STATUS_OK:
            done = transitions.data.get("done_selected") or {}
            if done.get("id"):
                out["jira.done_transition_names"] = [
                    {"id": done["id"], "name": done["name"]}]
        return out

    def to_dict(self) -> dict:
        """기계가 읽는 출력(``--json``) — 후속 대화형 온보딩·웹 UI 의 입력."""
        return {
            "ok": self.ok,
            "sections": [s.to_dict() for s in self.sections],
            "suggested_answers": self.suggested_answers(),
        }

    def format_text(self) -> str:
        """사람이 읽는 출력."""
        mark = {STATUS_OK: "OK  ", STATUS_FAIL: "FAIL", STATUS_SKIP: "SKIP"}
        out: list = []
        for s in self.sections:
            out.append(f"[{mark[s.status]}] {s.name}: {s.summary}")
            out.extend(f"       {line}" for line in s.lines)
            if s.hint:
                out.append(f"       ↳ {s.hint}")
            out.append("")
        suggested = self.suggested_answers()
        out.append("[확정된 값 — config.yaml 의 jira 섹션에 그대로 넣으면 된다]")
        if suggested:
            out.extend("  " + line for line in _format_answer_block(suggested))
            out.append("  (여기 없는 항목은 확정하지 못했다는 뜻이다 — 위 후보를 보고 "
                       "직접 고르세요.)")
        else:
            out.append("  없음 — 확정할 수 있는 값이 없었습니다. 위 후보를 보고 "
                       "직접 고르세요.")
        if not self.ok:
            out.append("")
            out.append("실패한 조회가 있습니다 — 그대로 두면 설정을 실측 없이 "
                       "추측으로 채우게 됩니다.")
        return "\n".join(out)


def _format_answer_block(answers: dict) -> list:
    """확정 값을 config.yaml 에 붙여넣을 수 있는 줄들로 만든다(``jira:`` 블록).

    표기는 렌더러(:func:`app.setup_render.format_scalar`)를 **재사용**한다 — 따옴표
    규칙이 두 벌이 되면 한쪽이 낡는다(숫자로 읽히는 문자열 등).
    """
    from app.setup_render import format_scalar

    lines = ["jira:"]
    for key, value in answers.items():
        leaf = key.split(".", 1)[1]
        if isinstance(value, dict):
            lines.append(f"  {leaf}:")
            lines.extend(f"    {k}: {format_scalar(v)}" for k, v in value.items())
        else:
            lines.append(f"  {leaf}: {format_scalar(value)}")
    return lines


# ---------------------------------------------------------------------------
# 커스텀필드 후보 추리기(순수 — 네트워크 없이 테스트된다)
# ---------------------------------------------------------------------------


def _field_entry(raw: Any) -> dict:
    """``GET /field`` 원소 하나 → 우리가 쓰는 얕은 표현(모르는 키는 버린다)."""
    item = raw if isinstance(raw, dict) else {}
    schema = item.get("schema") if isinstance(item.get("schema"), dict) else {}
    return {
        "id": str(item.get("id", "") or ""),
        "name": str(item.get("name", "") or ""),
        "custom": bool(item.get("custom", False)),
        "type": str((schema or {}).get("type", "") or ""),
    }


def match_custom_field(fields: Any, logical_key: str,
                       aliases: Any = None) -> dict:
    """논리 키 하나에 대한 **후보 추림**(자동 선택은 유일 정확일치일 때만).

    Args:
        fields: ``GET /rest/api/3/field`` 원 응답(배열).
        logical_key: :data:`app.setup_schema.JIRA_CUSTOM_FIELD_KEYS` 의 논리 키.
        aliases: 이름 후보(기본 :data:`FIELD_NAME_ALIASES`).

    Returns:
        ``{"selected": id|None, "reason": 사유, "candidates": [...]}``.
        ``candidates`` 원소는 ``{id, name, custom, type, tier}`` 이며 정확일치가 앞이다.

    선택 규칙(**보수적으로**):
        - 이름이 정확히 일치하는 **필드가 하나** → 그것을 고른다(``reason="exact"``).
        - 정확일치가 **여럿**(서로 다른 필드) → 고르지 않는다(``reason="ambiguous"``).
          같은 이름의 필드가 여럿인 인스턴스가 실제로 있다(프로젝트별 커스텀필드).
        - 정확일치가 **없음** → 부분일치는 후보로만 보여주고 고르지 않는다
          (``reason="no_exact_match"``). 부분일치로 고르면 "실제 시작일"을 "시작일"로
          집는 사고가 난다.
    """
    names = tuple(aliases if aliases is not None
                  else FIELD_NAME_ALIASES.get(logical_key, ()))
    normalized_aliases = {_normalize_name(a) for a in names if str(a).strip()}
    exact: list = []
    partial: list = []
    for raw in list(fields or []):
        entry = _field_entry(raw)
        if not entry["id"] or not entry["name"]:
            continue
        norm = _normalize_name(entry["name"])
        if norm in normalized_aliases:
            exact.append({**entry, "tier": TIER_EXACT})
        elif any(alias and alias in norm for alias in normalized_aliases):
            partial.append({**entry, "tier": TIER_PARTIAL})

    candidates = exact + partial
    if len(exact) == 1:
        return {"selected": exact[0]["id"], "reason": TIER_EXACT, "candidates": candidates}
    if len(exact) > 1:
        return {"selected": None, "reason": "ambiguous", "candidates": candidates}
    return {"selected": None, "reason": "no_exact_match", "candidates": candidates}


def _configured_field_id(cfg: Any, logical_key: str) -> tuple:
    """설정(또는 오늘의 기본값)이 지목하는 필드 id → ``(id, 출처)``.

    출처는 ``"config"``(``jira.custom_fields`` 에 적혀 있다) 또는 ``"default"``
    (:data:`app.setup_schema.JIRA_CUSTOM_FIELD_KEYS` 의 오늘의 기본값 — 즉 **다른
    조직의 값**이 그대로 쓰이고 있다는 뜻이다).
    """
    configured = getattr(getattr(cfg, "jira", None), "custom_fields", None) or {}
    if logical_key in configured:
        return str(configured.get(logical_key) or "").strip(), "config"
    default = next((d for k, _desc, d in S.JIRA_CUSTOM_FIELD_KEYS if k == logical_key), "")
    return str(default or ""), "default"


# ---------------------------------------------------------------------------
# 개별 조회
# ---------------------------------------------------------------------------


def _skipped(name: str, reason: str, hint: str = "") -> Section:
    return Section(name, STATUS_SKIP, reason, hint=hint)


def _failed(name: str, exc: Any, base_url: str) -> Section:
    """:class:`app.jira_client.JiraError` → 상태코드별 안내(진단과 같은 표를 재사용)."""
    from app.setup_doctor import _jira_failure

    result = _jira_failure(name, exc, base_url)
    return Section(name, STATUS_FAIL, result.message, hint=result.hint)


def discover_account(client: Any, base_url: str = "") -> Section:
    """``GET /rest/api/3/myself`` — ``accountId`` 를 알려 준다.

    사용자 온보딩에 ``accountId`` 가 필요한데, 그걸 Jira UI 에서 찾는 것이 은근히 어렵다
    (설정 화면에 대놓고 있지 않다). 어차피 자격 확인을 위해 부르는 김에 같이 보여 준다.
    """
    from app.jira_client import JiraError

    try:
        me = client.myself() or {}
    except JiraError as exc:
        return _failed("account", exc, base_url)
    data = {
        "account_id": str(me.get("accountId", "") or ""),
        "display_name": str(me.get("displayName", "") or ""),
        "email": str(me.get("emailAddress", "") or ""),
        "active": bool(me.get("active", True)),
    }
    section = Section("account", STATUS_OK,
                      f"{data['display_name'] or '?'} ({data['email'] or '이메일 비공개'})",
                      data=data)
    section.lines = [
        f"accountId: {data['account_id'] or '(응답에 없음)'}",
        "이 값이 사용자 온보딩(레지스트리)의 Jira 계정 식별자입니다.",
    ]
    if not data["email"]:
        section.lines.append(
            "이메일이 비공개 설정이라 응답에 없습니다 — jira.watcher_email 은 "
            "토큰을 발급한 계정의 이메일을 직접 적으세요.")
    return section


def discover_custom_fields(client: Any, cfg: Any, base_url: str = "") -> Section:
    """``GET /rest/api/3/field`` — 논리 키별 후보를 추리고, **설정된 id 의 실재를 검증**한다.

    후자가 이 조회의 진짜 값어치다. ``render`` 결과에는 다른 조직의 커스텀필드 id 가
    예시 값 그대로 남는데, 비어 있지도 않고 형식도 유효해서 눈으로 넘어간다. 여기서
    "그 id 는 이 인스턴스에 없습니다"라고 말해 준다.
    """
    from app.jira_client import JiraError

    try:
        fields = client.list_fields()
    except JiraError as exc:
        return _failed("custom_fields", exc, base_url)

    by_id = {e["id"]: e for e in (_field_entry(f) for f in fields or []) if e["id"]}
    logical: dict = {}
    problems: list = []
    lines: list = []
    for key, desc, _default in S.JIRA_CUSTOM_FIELD_KEYS:
        match = match_custom_field(fields, key)
        configured_id, source = _configured_field_id(cfg, key)
        exists = bool(configured_id) and configured_id in by_id
        logical[key] = {
            "description": desc,
            "configured": {
                "id": configured_id,
                "source": source,
                "exists": exists,
                "name": by_id.get(configured_id, {}).get("name", ""),
            },
            "selected": match["selected"],
            "reason": match["reason"],
            "candidates": match["candidates"],
        }
        lines.append(f"{key} — {desc}")
        if match["selected"]:
            picked = next(c for c in match["candidates"] if c["id"] == match["selected"])
            lines.append(f"  → 확정: {picked['id']}  ({picked['name']}) "
                         f"— 이름이 정확히 일치하는 필드가 유일합니다")
        elif match["candidates"]:
            why = ("같은 이름의 필드가 여럿이라" if match["reason"] == "ambiguous"
                   else "이름이 정확히 일치하는 필드가 없어")
            lines.append(f"  → 자동 선택 안 함({why}) — 아래 후보 중에서 고르세요:")
            for c in match["candidates"][:8]:
                tag = "정확일치" if c["tier"] == TIER_EXACT else "부분일치"
                lines.append(f"     {c['id']:<22} {c['name']}  [{tag}"
                             f"{'' if c['custom'] else ', 시스템 필드'}"
                             f"{', ' + c['type'] if c['type'] else ''}]")
            if len(match["candidates"]) > 8:
                lines.append(f"     … 외 {len(match['candidates']) - 8}개(--json 으로 전부)")
        else:
            lines.append("  → 후보 없음 — 이 인스턴스에 그런 필드가 없을 수 있습니다. "
                         'jira.custom_fields 에 ""(빈 값)로 두면 그 필드를 전송하지 않습니다.')
        if configured_id and not exists:
            where = ("config.yaml 의 jira.custom_fields 값" if source == "config"
                     else "예시 기본값(= 다른 조직의 id)")
            problems.append(key)
            lines.append(f"  ⚠️ 현재 {where} 는 {configured_id!r} 인데 "
                         f"**이 인스턴스에 그런 필드가 없습니다** — 그대로 두면 착수·완료 "
                         f"시 Jira 가 400 을 냅니다.")
        elif configured_id and source == "default":
            lines.append(f"  ℹ️ 현재는 예시 기본값 {configured_id!r}"
                         f"({by_id[configured_id]['name']})가 쓰입니다 — 우연히 이 "
                         f"인스턴스에도 존재하지만, 의도한 필드인지 확인하세요.")
        else:
            # 기본값이 없는 논리 키(actual_start·actual_end)이거나 명시로 비운 경우.
            # **오류가 아니다** — 그 필드를 보내지 않을 뿐이다. 다만 워크플로우가 그것을
            # 요구하면 전이가 400 으로 막히므로, 채울 수 있다는 사실을 알려 준다.
            lines.append("  ℹ️ 현재 미설정 — 이 필드를 전송하지 않습니다. 이 워크플로우가 "
                         "그 필드를 요구한다면 위 후보에서 골라 채우세요"
                         "(요구하지 않으면 그대로 두는 것이 맞습니다).")

    data = {"logical_keys": logical, "field_count": len(by_id),
            "invalid_configured": problems}
    summary = (f"필드 {len(by_id)}개 조회 — "
               f"확정 {sum(1 for v in logical.values() if v['selected'])}/"
               f"{len(logical)}개")
    hint = ""
    if problems:
        summary += f" · ⚠️ 이 인스턴스에 없는 id {len(problems)}개"
        hint = ("위 ⚠️ 항목을 고치기 전에는 착수·완료 전이가 실패합니다. 후보에서 고르거나, "
                "이 인스턴스에 그 필드가 없다면 빈 값(\"\")으로 두어 전송하지 않게 하세요.")
    return Section("custom_fields", STATUS_OK, summary, data=data, hint=hint, lines=lines)


def _status_entry(raw: Any) -> dict:
    """상태 원소 → ``{id, name, category}``."""
    item = raw if isinstance(raw, dict) else {}
    category = item.get("statusCategory") if isinstance(item.get("statusCategory"), dict) else {}
    return {
        "id": str(item.get("id", "") or ""),
        "name": str(item.get("name", "") or ""),
        "category": str((category or {}).get("key", "") or ""),
    }


def discover_statuses(client: Any, cfg: Any, base_url: str = "") -> Section:
    """``GET /rest/api/3/project/{key}/statuses`` — 실제 상태 + **설정된 이름의 실재 검증**.

    트리거 상태 이름이 하나라도 어긋나면 폴러는 **에러 없이 아무것도 안 한다**. 그래서
    설정에 적힌 이름이 이 프로젝트에 실제로 있는지까지 본다.
    """
    from app.jira_client import JiraError

    jira = getattr(cfg, "jira", None)
    project = str(getattr(jira, "project", "") or "")
    if not project:
        return _skipped("statuses", "jira.project 가 비어 있습니다",
                        "감시할 프로젝트 키를 먼저 정하세요(이슈 키의 앞부분입니다).")
    try:
        raw_types = client.project_statuses(project)
    except JiraError as exc:
        return _failed("statuses", exc, base_url)

    issue_types: list = []
    all_statuses: dict = {}          # 이름 → 항목(중복 제거, 첫 등장 순서 유지)
    for raw in raw_types or []:
        item = raw if isinstance(raw, dict) else {}
        statuses = [_status_entry(s) for s in (item.get("statuses") or [])]
        statuses = [s for s in statuses if s["name"]]
        issue_types.append({
            "id": str(item.get("id", "") or ""),
            "name": str(item.get("name", "") or ""),
            "statuses": statuses,
        })
        for s in statuses:
            all_statuses.setdefault(s["name"], s)

    by_normalized = {_normalize_name(name): entry
                     for name, entry in all_statuses.items()}

    configured: dict = {}
    unknown: list = []
    for key in ("trigger_statuses", "cancel_statuses"):
        entries: list = []
        for name in list(getattr(jira, key, None) or []):
            hit = by_normalized.get(_normalize_name(name))
            entries.append({
                "name": str(name),
                "id": hit["id"] if hit else "",
                "exists": hit is not None,
                # 표기 차이(공백·대소문자)까지 흡수해 찾았다면 **실제 이름**을 알려 준다.
                "actual_name": hit["name"] if hit else "",
                "category": hit["category"] if hit else "",
            })
            if hit is None:
                unknown.append(f"{key}: {name}")
        configured[key] = entries

    lines: list = []
    for it in issue_types:
        names = ", ".join(f"{s['name']}({s['id']})" for s in it["statuses"])
        lines.append(f"{it['name']}: {names or '(상태 없음)'}")
    lines.append("")
    for key in ("trigger_statuses", "cancel_statuses"):
        for e in configured[key]:
            if not e["exists"]:
                lines.append(f"⚠️ jira.{key} 의 {e['name']!r} 은(는) 이 프로젝트에 "
                             f"없는 상태입니다.")
            elif e["actual_name"] != e["name"]:
                lines.append(f"⚠️ jira.{key} 의 {e['name']!r} 은(는) 실제로 "
                             f"{e['actual_name']!r} 입니다(표기가 다릅니다) — id "
                             f"{e['id']}.")
            else:
                lines.append(f"jira.{key}: {e['name']} → id {e['id']}")

    data = {"project": project, "issue_types": issue_types,
            "statuses": list(all_statuses.values()),
            "configured": configured, "unknown": unknown}
    summary = f"{project} 의 상태 {len(all_statuses)}종(이슈 타입 {len(issue_types)}개)"
    hint = ""
    if unknown:
        summary += f" · ⚠️ 설정에 없는 상태 이름 {len(unknown)}개"
        hint = ("설정의 상태 이름이 이 프로젝트에 없으면 폴러는 **에러 없이 아무 티켓도 "
                "찾지 못합니다**(가장 알아채기 어려운 실패). 위 목록의 이름으로 고치고, "
                "가능하면 {id, name} 형태로 적어 두세요.")
    return Section("statuses", STATUS_OK, summary, data=data, hint=hint, lines=lines)


def _transition_entry(raw: Any) -> dict:
    """전이 원소 → ``{id, name, to_id, to_name, to_category}``."""
    item = raw if isinstance(raw, dict) else {}
    to = item.get("to") if isinstance(item.get("to"), dict) else {}
    category = (to or {}).get("statusCategory")
    category = category if isinstance(category, dict) else {}
    return {
        "id": str(item.get("id", "") or ""),
        "name": str(item.get("name", "") or ""),
        "to_id": str((to or {}).get("id", "") or ""),
        "to_name": str((to or {}).get("name", "") or ""),
        "to_category": str((category or {}).get("key", "") or ""),
    }


def _sample_issue_key(client: Any, project: str) -> str:
    """전이를 실측할 이슈를 하나 고른다(없으면 "").

    전이 목록은 **이슈마다** 다를 수 있어(워크플로우·현재 상태·조건) 반드시 실제 이슈로
    본다. 최근 생성된 것 하나면 충분하다.
    """
    from app.jira_client import JiraError

    try:
        page = client.search_jql_page(f"project = {project} ORDER BY created DESC",
                                      fields=["key"], max_results=1)
    except JiraError:
        return ""
    issues = (page or {}).get("issues") or []
    return str((issues[0] or {}).get("key", "") or "") if issues else ""


def discover_transitions(client: Any, cfg: Any, base_url: str = "",
                         issue_key: str = "") -> Section:
    """``GET /rest/api/2/issue/{key}/transitions`` — **id·name·목표 상태를 함께** 얻는다.

    완료 전이는 **id 로** 실행되는데 사람은 **이름으로** 안다. 그 둘을 한 자리에서 보여
    주고, 설정된 ``jira.done_transition_id`` 가 이 이슈의 실제 전이인지도 확인한다.
    """
    from app.jira_client import JiraError

    jira = getattr(cfg, "jira", None)
    project = str(getattr(jira, "project", "") or "")
    key = str(issue_key or "").strip()
    sampled = False
    if not key:
        if not project:
            return _skipped("transitions", "실측할 이슈를 정할 수 없습니다"
                                           "(jira.project 도 --issue 도 없습니다)",
                            "--issue <ISSUE-KEY> 로 실제 티켓 하나를 지정하세요.")
        key = _sample_issue_key(client, project)
        sampled = True
    if not key:
        return _skipped("transitions", f"{project} 에서 표본 이슈를 찾지 못했습니다",
                        "티켓이 하나도 없는 새 프로젝트라면 정상입니다 — 첫 티켓을 만든 뒤 "
                        "`--issue <ISSUE-KEY>` 로 다시 조회하세요.")

    try:
        raw_transitions = client.list_transitions(key)
    except JiraError as exc:
        return _failed("transitions", exc, base_url)

    transitions = [_transition_entry(t) for t in raw_transitions or []]
    transitions = [t for t in transitions if t["id"]]
    wanted = {_normalize_name(n) for n in (getattr(jira, "done_transition_names", None) or [])}
    by_name = [t for t in transitions
               if _normalize_name(t["name"]) in wanted
               or _normalize_name(t["to_name"]) in wanted]
    done_category = [t for t in transitions if t["to_category"] == "done"]

    # 완료 전이 확정 — 런타임(JiraClient.resolve_done_transition_id)과 **같은 우선순위**로
    # 본다: 이름 매칭이 유일하면 그것, 아니면 done 카테고리 단일 후보. 둘 다 아니면 확정
    # 하지 않는다(여기서 아무거나 고르면 그게 그대로 config 에 박힌다).
    done_selected: dict = {}
    if len(by_name) == 1:
        done_selected = {**by_name[0], "reason": "name_match"}
    elif not by_name and len(done_category) == 1:
        done_selected = {**done_category[0], "reason": "single_done_category"}

    configured_id = str(getattr(jira, "done_transition_id", "") or "")
    configured_exists = any(t["id"] == configured_id for t in transitions)

    lines = [f"실측 이슈: {key}" + (" (최근 티켓에서 자동 표본)" if sampled else "")]
    for t in transitions:
        tag = " [done]" if t["to_category"] == "done" else ""
        lines.append(f"  id={t['id']:<6} {t['name']}  →  {t['to_name']}{tag}")
    if done_selected:
        why = ("이름이 일치하는 전이가 유일" if done_selected["reason"] == "name_match"
               else "done 카테고리 전이가 유일")
        lines.append(f"→ 완료 전이 확정: id={done_selected['id']} "
                     f"({done_selected['name']}) — {why}")
    else:
        lines.append("→ 완료 전이를 확정하지 않았습니다 — 위 목록에서 골라 "
                     "jira.done_transition_names 에 {id, name} 으로 적으세요.")
    if configured_id and not configured_exists:
        lines.append(f"⚠️ 설정된 jira.done_transition_id={configured_id!r} 는 이 이슈의 "
                     f"전이 목록에 없습니다 — 다른 인스턴스의 id 일 수 있습니다.")

    data = {
        "issue_key": key, "sampled": sampled, "transitions": transitions,
        "done_candidates": done_category, "done_selected": done_selected,
        "configured": {"done_transition_id": configured_id,
                       "exists": configured_exists,
                       "names": list(getattr(jira, "done_transition_names", None) or [])},
    }
    summary = f"{key} 의 전이 {len(transitions)}개(완료 후보 {len(done_category)}개)"
    hint = ""
    if configured_id and not configured_exists:
        summary += " · ⚠️ 설정된 완료 전이 id 가 목록에 없음"
        hint = ("전이 id 는 인스턴스마다 다릅니다. 위 목록의 id 로 바꾸거나, "
                "jira.done_transition_id 를 비우고 이름으로 찾게 하세요.")
    elif not done_selected:
        hint = ("완료 전이가 모호합니다 — 이대로면 완료 처리 시점에 "
                "'전이를 확정할 수 없다'는 에러로 실패합니다.")
    return Section("transitions", STATUS_OK, summary, data=data, hint=hint, lines=lines)


def discover_labels(client: Any, cfg: Any, base_url: str = "") -> Section:
    """``GET /rest/api/3/label`` — opt-out 라벨 참고 + **규칙 안내**(이쪽이 더 중요하다)."""
    from app.jira_client import JiraError

    try:
        payload = client.list_labels()
    except JiraError as exc:
        return _failed("labels", exc, base_url)

    labels = list((payload or {}).get("labels") or [])
    known = {_normalize_name(x) for x in labels}
    configured = [str(x) for x in (getattr(getattr(cfg, "jira", None),
                                           "optout_labels", None) or [])]
    entries = [{"name": name, "exists": _normalize_name(name) in known}
               for name in configured]

    lines = [OPTOUT_LABEL_GUIDE, ""]
    for e in entries:
        state = "이 인스턴스에 이미 있음" if e["exists"] else "아직 없음(문제 아님 — 붙이면 생성)"
        lines.append(f"jira.optout_labels: {e['name']}  — {state}")
    if not entries:
        lines.append("jira.optout_labels 가 비어 있습니다 — 자동화 제외 장치가 꺼져 있습니다.")
    sample = [x for x in labels[:10]]
    if sample:
        lines.append("")
        lines.append("인스턴스 라벨 표본: " + ", ".join(sample)
                     + (" …" if len(labels) > len(sample) else ""))

    data = {"labels": labels, "total": int((payload or {}).get("total") or len(labels)),
            "truncated": bool((payload or {}).get("truncated")),
            "optout_labels": entries, "guide": OPTOUT_LABEL_GUIDE}
    summary = f"라벨 {data['total']}개" + ("(목록 일부만 조회)" if data["truncated"] else "")
    return Section("labels", STATUS_OK, summary, data=data, lines=lines)


# ---------------------------------------------------------------------------
# 실행기
# ---------------------------------------------------------------------------


def discover(cfg: Any, *, project_dir: str = ".", only: tuple = (),
             issue_key: str = "", client: Any = None) -> DiscoveryResult:
    """설정이 가리키는 Jira 인스턴스를 조회한다(**부작용 없는 읽기만**).

    Args:
        cfg: 로드된 :class:`app.config.AppConfig`.
        project_dir: 배포 디렉토리(호스트 쪽 secrets 폴백 경로 계산 기준 — 진단과 동일).
        only: :data:`SECTION_ORDER` 의 부분집합만 조회(비면 전부).
        issue_key: 전이 실측에 쓸 이슈 키(비우면 최근 티켓에서 자동 표본).
        client: :class:`app.jira_client.JiraClient` 대역 주입(테스트·CI).

    Returns:
        :class:`DiscoveryResult`.

    Raises:
        DiscoveryError: ``only`` 에 알 수 없는 이름이 있을 때.
    """
    from app.setup_doctor import _jira_client

    wanted = tuple(only) or SECTION_ORDER
    unknown = [n for n in wanted if n not in SECTION_ORDER]
    if unknown:
        raise DiscoveryError(
            f"알 수 없는 조회 항목: {', '.join(unknown)} "
            f"(가능: {', '.join(SECTION_ORDER)})"
        )

    base_url = str(getattr(getattr(cfg, "jira", None), "base_url", "") or "")
    reason = ""
    if client is None:
        client, reason = _jira_client(cfg, project_dir)

    result = DiscoveryResult()
    runners = {
        "account": lambda: discover_account(client, base_url),
        "custom_fields": lambda: discover_custom_fields(client, cfg, base_url),
        "statuses": lambda: discover_statuses(client, cfg, base_url),
        "transitions": lambda: discover_transitions(client, cfg, base_url, issue_key),
        "labels": lambda: discover_labels(client, cfg, base_url),
    }
    for name in SECTION_ORDER:
        if name not in wanted:
            continue
        if client is None:
            result.sections.append(_skipped(
                name, reason or "Jira 클라이언트를 만들 수 없습니다",
                "조회하려면 jira.base_url · jira.watcher_email · watcher 토큰 파일이 "
                "모두 필요합니다(`python -m app.setup doctor --only secrets` 참조)."))
            continue
        try:
            result.sections.append(runners[name]())
        except Exception as exc:  # noqa: BLE001 — 한 조회의 사고가 전체를 죽이면 안 된다
            result.sections.append(Section(
                name, STATUS_FAIL, f"조회 중 예외: {type(exc).__name__}: {exc}",
                hint=f"이 항목만 다시 돌려 보세요: "
                     f"python -m app.setup discover --only {name}"))
    return result
