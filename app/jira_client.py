"""Jira REST 클라이언트(중앙 전용).

역할:
    Jira Cloud REST로 이슈를 조회/전이/코멘트하고, JQL로 신규 할당 티켓을
    검색한다. 인증은 Basic auth(email:token). 중앙은 감시 토큰
    (jira.watcher_token_file, secrets.base_dir 상대)으로만 Jira를 읽는다.

역할 소속: **central** (worker는 Jira를 직접 보지 않는다). 상태 전이 주체:
    - **진행중(in-progress) 전이**는 **디스패처가 워커 착수 시** "디스패치 유저" 토큰으로
      수행한다(worker_dispatch.run_dispatch — 착수 시점을 아는 결정적 주체가 디스패처뿐).
    - **완료(done) 전이**는 worker가 실행하는 오케스트레이터/사용자가 "사용자" 토큰으로
      수행한다(범위 밖).

구현 Phase: **Phase 2** (Jira 연동).

엔드포인트(사실 검증됨 2026-08):
    - GET  /rest/api/2/issue/{key}                 이슈 상세
    - PUT  /rest/api/2/issue/{key}                 필드 갱신(set_fields)
    - GET  /rest/api/2/issue/{key}/transitions     가능한 전이 목록
    - POST /rest/api/2/issue/{key}/transitions     상태 전이
    - POST /rest/api/2/issue/{key}/comment         코멘트
    - POST /rest/api/3/search/jql                  JQL 검색(신 API, nextPageToken)
    ⚠️ /rest/api/2/search·/rest/api/3/search 는 Atlassian이 삭제 — 사용 금지.

⚠️ **Jira Cloud 전용**:
    위 JQL 검색 경로(``POST /rest/api/3/search/jql``)는 **Jira Cloud 에만** 있다 —
    Jira Server/Data Center 에는 존재하지 않는다(그렇다고 옛 ``/rest/api/2/search`` 로
    돌아갈 수도 없다. Cloud 에서 삭제됐다). Server/DC 는 검증할 수단이 없어 **추측
    구현을 넣지 않았다** — 대신 Server/DC 로 보이는 응답(404/405/410, JSON 이 아닌
    로그인/에러 HTML, 정체불명 응답 형태)을 만나면 **조용히 빈 결과를 돌려주지 않고**
    무엇이 문제인지 말하는 :class:`JiraError` 로 올린다. 빈 결과는 폴러에게 "새 티켓이
    없다"로 읽혀 아무 일도 일어나지 않는데, 그게 가장 나쁜 실패 모드다.

⚠️ **인스턴스마다 다른 값**(커스텀필드 id·완료 전이 id):
    아래 모듈 상수는 **이 코드가 처음 운영된 인스턴스의 값일 뿐**이다. 커스텀필드 id 와
    전이 id 는 Jira 인스턴스마다 다르므로, 남의 Jira 에 그대로 보내면 400/404 가 난다.
    그래서 정본은 설정(``jira.custom_fields``·``jira.done_transition_id``·
    ``jira.done_transition_names`` — :mod:`app.setup_schema` 선언, :mod:`app.config` 파서)이고,
    이 모듈 상수는 **설정이 없을 때의 폴백**이다(하위호환 — 기존 배포는 아무것도 안 바꿔도
    오늘과 동일하게 동작). 설정 주입은 :meth:`JiraClient.from_config` 를 쓴다.

    자기 인스턴스의 값을 알아내는 법:
        - 커스텀필드 id: ``GET /rest/api/3/field`` (또는 ``GET /rest/api/2/issue/{key}`` 응답)
        - 전이 id/이름:  ``GET /rest/api/2/issue/{key}/transitions``

시크릿 규율:
    토큰은 ``session.auth`` 에만 실린다. 로그·예외 메시지·``repr`` 어디에도 토큰을 넣지
    않는다(:meth:`JiraClient.__repr__` 가 마스킹한다).
"""

from __future__ import annotations

import logging
import unicodedata
from typing import Any, Iterable, Optional

import requests

log = logging.getLogger("jad.jira")

# ---------------------------------------------------------------------------
# 인스턴스별 기본값(= 설정 미지정 시의 폴백. 정본은 config 의 jira.* 다)
# ---------------------------------------------------------------------------

# 커스텀 필드 상수 — 이 코드가 처음 운영된 인스턴스의 값. 다른 인스턴스에서는 다르다.
FIELD_START_DATE = "customfield_10015"   # 시작날짜(착수)
FIELD_DUE_DATE = "duedate"               # 마감일(착수)
FIELD_ACTUAL_START = "customfield_10187"  # 실제 시작일(완료)
FIELD_ACTUAL_END = "customfield_10186"    # 실제 종료일(완료)
DONE_TRANSITION_ID = "41"                 # 완료 전이(그 인스턴스의 값 — 레거시 폴백 전용)

#: 논리 키 → 오늘의 기본 커스텀필드 id. 논리 키 목록의 정본은
#: :data:`app.setup_schema.JIRA_CUSTOM_FIELD_KEYS` 이며,
#: ``tests/test_setup_schema.py`` 가 두 곳의 드리프트를 잡는다.
DEFAULT_CUSTOM_FIELDS: dict = {
    "start_date": FIELD_START_DATE,
    "due_date": FIELD_DUE_DATE,
    "actual_start": FIELD_ACTUAL_START,
    "actual_end": FIELD_ACTUAL_END,
}

#: 완료 전이를 **이름**으로 찾을 때의 기본 후보(설정 ``jira.done_transition_names`` 기본과 동일).
DEFAULT_DONE_TRANSITION_NAMES: tuple = ("완료", "Done")

#: JQL 검색 경로(Jira Cloud 전용 — 모듈 docstring 참조).
SEARCH_JQL_PATH = "/rest/api/3/search/jql"

#: 이 경로가 **없을 때** Jira 가 내는 상태코드들(= Server/DC 강한 신호).
_ENDPOINT_ABSENT_STATUS = (404, 405, 410)


def _normalize_name(name: Any) -> str:
    """이름 정규화 — NFC · 모든 공백 제거 · casefold.

    "완료"/"완 료", "Done"/"done" 같은 표기 차이를 흡수한다(worker_dispatch 의 진행중
    전이 매칭과 같은 규칙).
    """
    s = unicodedata.normalize("NFC", str(name or ""))
    return "".join(s.split()).casefold()


def _describe_transitions(transitions: Any) -> str:
    """전이 목록을 사람이 읽는 한 줄로(에러 메시지용 — 무엇을 고를 수 있었는지 보여준다)."""
    parts: list = []
    for t in transitions or []:
        if not isinstance(t, dict):
            continue
        to = t.get("to") or {}
        parts.append(f"id={t.get('id')} name={t.get('name')!r} to={to.get('name')!r}")
    return "; ".join(parts) or "(가능한 전이 없음)"


class JiraError(Exception):
    """Jira REST 호출 실패(4xx/5xx 또는 네트워크)."""

    def __init__(self, message: str, status_code: Optional[int] = None, body: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class JiraClient:
    """Jira Cloud REST 클라이언트.

    인스턴스별 값(커스텀필드 id·완료 전이)은 **생성자 주입**이며, 주지 않으면 모듈 상수로
    폴백한다(하위호환). 설정에서 만들 때는 :meth:`from_config` 를 쓴다.
    """

    def __init__(
        self,
        base_url: str,
        email: str,
        token: str,
        timeout: int = 30,
        *,
        custom_fields: Optional[dict] = None,
        done_transition_id: str = "",
        done_transition_names: Optional[Iterable] = None,
    ) -> None:
        """base_url·email·token(=Basic auth 자격)로 세션 구성.

        Basic auth = (email, token). 한글 body는 항상 json= 로 보낸다.

        Args:
            custom_fields: 논리 키(``start_date``·``due_date``·``actual_start``·
                ``actual_end``) → **이 인스턴스의** 필드 id. 해석 규칙은
                :meth:`_resolve_custom_fields` 참조 — 키를 **안 주면** 모듈 상수 폴백
                (하위호환), **빈 값으로 주면** "이 인스턴스엔 없는 필드"로 보고 그 필드를
                아예 전송하지 않는다.
            done_transition_id: 완료 전이의 숫자 id(문자열). 주면 이름 탐색 없이 그걸 쓴다.
            done_transition_names: id 대신 이름으로 완료 전이를 찾을 때의 후보 목록.
                ``None``(미주입) 이면 **레거시 모드**로 본다 — 이름으로도 못 찾았을 때
                모듈 상수 :data:`DONE_TRANSITION_ID` 로 폴백한다(기존 배포 무변경 보장).
        """
        self.base_url = base_url.rstrip("/")
        self.email = email
        self._token = token
        self.timeout = timeout
        # --- 인스턴스별 필드/전이 식별(설정 주입 → 없으면 모듈 상수 폴백) ---
        self.custom_fields = self._resolve_custom_fields(custom_fields)
        self.done_transition_id = str(done_transition_id or "").strip()
        # 설정이 하나라도 주입됐는가 — 레거시 폴백(DONE_TRANSITION_ID) 허용 여부를 가른다.
        self._done_configured = bool(self.done_transition_id) or done_transition_names is not None
        names = (list(done_transition_names) if done_transition_names is not None
                 else list(DEFAULT_DONE_TRANSITION_NAMES))
        self.done_transition_names = [str(n) for n in names if str(n).strip()]
        self.session = requests.Session()
        self.session.auth = (email, token)
        self.session.headers.update(
            {"Accept": "application/json", "Content-Type": "application/json"}
        )

    def __repr__(self) -> str:
        """토큰을 절대 노출하지 않는 repr(로그·예외에 객체가 실려도 안전하게)."""
        return f"JiraClient(base_url={self.base_url!r}, email={self.email!r}, token=***)"

    # ------------------------------------------------------------------
    # 설정 주입
    # ------------------------------------------------------------------

    @classmethod
    def from_config(
        cls,
        config: Any,
        email: str,
        token: str,
        *,
        base_url: str = "",
        timeout: int = 30,
    ) -> "JiraClient":
        """``config.jira.*`` 의 인스턴스별 값을 주입해 클라이언트를 만든다(권장 경로).

        읽는 키: ``jira.base_url``·``jira.custom_fields``·``jira.done_transition_id``·
        ``jira.done_transition_names``. 값이 비어 있으면 각각 모듈 상수로 폴백한다.

        Args:
            config: :class:`app.config.AppConfig`(또는 같은 속성을 흉내내는 객체 —
                테스트 대역도 그대로 통과하도록 ``getattr`` 로만 접근한다).
            base_url: 명시하면 ``config.jira.base_url`` 보다 우선(디스패처처럼 이미
                base_url 을 손에 쥔 호출부용).
        """
        jira_cfg = getattr(config, "jira", None)
        url = base_url or (getattr(jira_cfg, "base_url", "") or "")
        names = getattr(jira_cfg, "done_transition_names", None)
        return cls(
            url,
            email,
            token,
            timeout=timeout,
            custom_fields=getattr(jira_cfg, "custom_fields", None) or None,
            done_transition_id=getattr(jira_cfg, "done_transition_id", "") or "",
            done_transition_names=list(names) if names else None,
        )

    @staticmethod
    def _resolve_custom_fields(overrides: Optional[dict]) -> dict:
        """논리 키 → 이 인스턴스의 필드 id 확정(3분기).

        1. 키가 **없다** → 모듈 상수 기본값을 쓴다(하위호환 — 기존 배포 무변경).
        2. 키가 **비어 있지 않은 값**으로 있다 → 그 값을 쓴다.
        3. 키가 **빈 값**(``""``/``None``)으로 있다 → "이 인스턴스엔 그 필드가 없다"는
           명시 의사표시로 보고 **결과에서 제외**한다 → 그 필드는 아예 전송되지 않는다.
           (남의 Jira 에 없는 커스텀필드를 보내면 400 이 난다. 명시 빈 값을 "미설정"으로
           되돌려 기본값을 밀어넣지 않는 것이 :func:`app.config._pick` 의 규율과 정합.)

        스키마에 없는 논리 키도 값이 있으면 그대로 실어 준다(후속 확장 여지).
        """
        ov = dict(overrides or {})
        resolved: dict = {}
        for logical, default_id in DEFAULT_CUSTOM_FIELDS.items():
            if logical in ov:
                field_id = str(ov.get(logical) or "").strip()
                if field_id:
                    resolved[logical] = field_id
                # else: 명시 빈 값 → 비활성(키를 넣지 않는다)
            else:
                resolved[logical] = default_id
        for logical, field_id in ov.items():
            if logical in DEFAULT_CUSTOM_FIELDS:
                continue
            field_id = str(field_id or "").strip()
            if field_id:
                resolved[logical] = field_id
        return resolved

    def field_id(self, logical_key: str) -> Optional[str]:
        """논리 키의 이 인스턴스 필드 id. 비활성(미설정)이면 ``None``."""
        return self.custom_fields.get(logical_key) or None

    def _fields_payload(self, pairs: Iterable) -> tuple:
        """``[(논리키, 값), ...]`` → ``({필드id: 값}, [건너뛴 논리키])``.

        비활성 필드는 payload 에 **넣지 않는다** — 없는 커스텀필드를 보내 400 을 맞는
        대신 그 필드만 빼되, 무엇을 뺐는지는 반환값·로그로 드러낸다(조용한 실패 아님).
        """
        fields: dict = {}
        skipped: list = []
        for logical, value in pairs:
            fid = self.field_id(logical)
            if fid:
                fields[fid] = value
            else:
                skipped.append(logical)
        return fields, skipped

    # ------------------------------------------------------------------
    # 저수준 요청
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise JiraError(f"{method} {path} 요청 실패: {exc}") from exc
        if resp.status_code >= 400:
            body: Any
            try:
                body = resp.json()
            except ValueError:
                body = resp.text
            raise JiraError(
                f"{method} {path} → HTTP {resp.status_code}: {body}",
                status_code=resp.status_code,
                body=body,
            )
        return resp

    @staticmethod
    def _json_or_empty(resp: requests.Response) -> dict:
        if resp.status_code == 204 or not (resp.content or b"").strip():
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # ------------------------------------------------------------------
    # 이슈 읽기/쓰기
    # ------------------------------------------------------------------

    def get_issue(self, key: str, fields: Optional[list] = None) -> dict:
        """이슈 상세 GET (/rest/api/2/issue/{key})."""
        params = {}
        if fields:
            params["fields"] = ",".join(fields)
        resp = self._request("GET", f"/rest/api/2/issue/{key}", params=params or None)
        return self._json_or_empty(resp)

    def set_fields(self, key: str, fields: dict) -> None:
        """이슈 필드 PUT (/rest/api/2/issue/{key}). body={fields:...}."""
        self._request("PUT", f"/rest/api/2/issue/{key}", json={"fields": fields})

    # 하위호환 별칭(기존 스텁 시그니처 유지).
    def update_issue(self, key: str, fields: dict) -> None:
        """set_fields 별칭(하위호환)."""
        self.set_fields(key, fields)

    def list_transitions(self, key: str) -> list:
        """가능한 전이 목록 GET (/rest/api/2/issue/{key}/transitions)."""
        resp = self._request("GET", f"/rest/api/2/issue/{key}/transitions")
        return self._json_or_empty(resp).get("transitions", [])

    # 하위호환 별칭.
    def get_transitions(self, key: str) -> list:
        """list_transitions 별칭(하위호환)."""
        return self.list_transitions(key)

    def transition(self, key: str, transition_id: str, fields: Optional[dict] = None) -> None:
        """상태 전이 POST (/rest/api/2/issue/{key}/transitions).

        fields 를 함께 넘기면 전이와 동시에 필드를 기입한다(완료 전이 시 실제
        시작/종료일 등).
        """
        body: dict = {"transition": {"id": str(transition_id)}}
        if fields:
            body["fields"] = fields
        self._request("POST", f"/rest/api/2/issue/{key}/transitions", json=body)

    # 하위호환 별칭.
    def transition_issue(self, key: str, transition_id: str) -> None:
        """transition 별칭(하위호환)."""
        self.transition(key, transition_id)

    def add_comment(self, key: str, body: str) -> None:
        """코멘트 POST (/rest/api/2/issue/{key}/comment). 한글 body는 json=."""
        self._request("POST", f"/rest/api/2/issue/{key}/comment", json={"body": body})

    # ------------------------------------------------------------------
    # 착수/완료 전이 헬퍼(POLICY-ISSUE-TRACKING 필수 필드 선기입)
    # ------------------------------------------------------------------

    def start_work(self, key: str, start_date: str, due_date: str) -> dict:
        """착수 필수 필드(시작날짜+마감일) 기입.

        상태 전이(→ 진행중)는 **디스패처가 워커 착수 시** 디스패치 유저 토큰으로 별도
        수행한다(worker_dispatch.run_dispatch — 런타임 발견·멱등). 이 헬퍼는 필수 필드만
        선기입한다(YYYY-MM-DD).

        해당 필드가 이 인스턴스에 없다고 설정된 경우(``jira.custom_fields`` 에 빈 값)
        그 필드는 **보내지 않는다**. 둘 다 비활성이면 PUT 자체를 생략한다(빈 갱신 금지).

        Returns:
            ``{"updated": bool, "skipped_fields": [논리키...]}`` — 무엇을 뺐는지 드러낸다.
        """
        fields, skipped = self._fields_payload(
            (("start_date", start_date), ("due_date", due_date))
        )
        if skipped:
            log.info(
                "jira: 착수 필드 일부를 전송하지 않습니다(이 인스턴스에 미설정) key=%s skipped=%s",
                key, skipped,
            )
        if not fields:
            return {"updated": False, "skipped_fields": skipped}
        self.set_fields(key, fields)
        return {"updated": True, "skipped_fields": skipped}

    def resolve_done_transition_id(self, key: str) -> str:
        """이 인스턴스의 '완료' 전이 id 를 확정한다(우선순위 고정).

        1. **설정된 id**(``jira.done_transition_id``) — 있으면 네트워크 접근 없이 그대로.
        2. **이름 매칭** — ``GET .../transitions`` 의 전이명/목표 상태명이
           ``jira.done_transition_names`` 후보와 (정규화 후) 일치하면 그 전이.
        3. **done 카테고리 단일 후보** — 이름은 몰라도 ``statusCategory.key == "done"``
           인 전이가 **정확히 하나**면 모호성이 없으니 그걸 쓴다.
        4. **레거시 폴백**(경고 로그) — (a) 모듈 상수 :data:`DONE_TRANSITION_ID` 가 이 이슈의
           done 카테고리 전이 중 하나면 그걸 쓰고(설정 스키마가 약속한 폴백. done 전이일
           때만이라 엉뚱한 id 를 고르지 않는다), (b) 설정이 전혀 주입되지 않은 클라이언트
           (옛 생성 경로)는 전이 목록이 비어 있어도 그 상수를 쓴다 — 기존 배포 무변경 보장.
        5. 그래도 못 정하면 **가능한 전이 목록을 담은** :class:`JiraError`.
           (조용히 실패하거나 아무 id 나 찍어 400 을 맞게 두지 않는다.)

        Raises:
            JiraError: 완료 전이를 확정할 수 없을 때(가능한 전이 목록·설정 방법 포함).
        """
        if self.done_transition_id:
            return self.done_transition_id

        transitions = self.list_transitions(key)
        wanted = {_normalize_name(n) for n in self.done_transition_names}
        done_category: list = []
        for t in transitions or []:
            if not isinstance(t, dict):
                continue
            to = t.get("to") or {}
            if _normalize_name(t.get("name")) in wanted or _normalize_name(to.get("name")) in wanted:
                return str(t.get("id"))
            if ((to.get("statusCategory") or {}).get("key") or "").strip().lower() == "done":
                done_category.append(t)

        if len(done_category) == 1:
            chosen = done_category[0]
            log.info(
                "jira: 완료 전이를 이름으로 못 찾아 done 카테고리 단일 후보를 사용합니다 "
                "key=%s id=%s to=%s",
                key, chosen.get("id"), (chosen.get("to") or {}).get("name"),
            )
            return str(chosen.get("id"))

        # 모듈 상수 id 가 **실제로 이 이슈의 done 카테고리 전이 중 하나**면 그걸 쓴다
        # (설정 스키마가 약속한 "그래도 못 찾으면 모듈 상수" 폴백. 단 done 전이일 때만 —
        # 남의 인스턴스에서 우연히 같은 id 가 엉뚱한 전이인 경우를 고르지 않기 위해).
        legacy = next(
            (t for t in done_category if str(t.get("id")) == DONE_TRANSITION_ID), None
        )
        if legacy is not None:
            log.warning(
                "jira: 완료 전이를 이름으로 못 찾아 모듈 상수 id(%s)로 폴백합니다 — "
                "config.yaml 의 jira.done_transition_id / jira.done_transition_names 를 "
                "이 인스턴스 값으로 채우세요. key=%s", DONE_TRANSITION_ID, key,
            )
            return DONE_TRANSITION_ID

        if not self._done_configured and DONE_TRANSITION_ID:
            log.warning(
                "jira: 완료 전이를 이름으로 찾지 못해 레거시 기본 id(%s)로 폴백합니다 — "
                "config.yaml 의 jira.done_transition_id / jira.done_transition_names 를 "
                "이 인스턴스 값으로 채우세요. key=%s",
                DONE_TRANSITION_ID, key,
            )
            return DONE_TRANSITION_ID

        reason = (
            "완료(done) 카테고리 전이가 여럿이라 자동 선택하지 않았습니다"
            if len(done_category) > 1 else
            "이름이 일치하는 전이도, done 카테고리 단일 후보도 없습니다"
        )
        raise JiraError(
            f"{key}: 완료 전이를 확정할 수 없습니다 — {reason}. "
            f"찾던 이름 후보={self.done_transition_names}. "
            f"가능한 전이: {_describe_transitions(transitions)}. "
            f"config.yaml 의 jira.done_transition_id 에 위 목록의 id 를 적거나, "
            f"jira.done_transition_names 에 이 워크플로우의 완료 전이 이름을 적으세요 "
            f"(실측: GET /rest/api/2/issue/{key}/transitions)."
        )

    def transition_done(
        self,
        key: str,
        actual_start: str,
        actual_end: str,
        transition_id: Optional[str] = None,
    ) -> str:
        """완료 전이 — 실제 시작/종료일(YYYY-MM-DD) 선기입 후 done 전이.

        전이와 동시에 필드를 넣는다(단일 트랜잭션). ``transition_id`` 를 명시하면 그대로
        쓰고, 생략하면 :meth:`resolve_done_transition_id` 가 설정 id → 이름 매칭 →
        (실패 시) 명확한 에러 순으로 확정한다. 실제 시작/종료일 필드가 이 인스턴스에
        없다고 설정돼 있으면 그 필드는 보내지 않는다.

        Returns:
            실제로 사용한 전이 id(로그·감사용).
        """
        tid = str(transition_id) if transition_id else self.resolve_done_transition_id(key)
        fields, skipped = self._fields_payload(
            (("actual_start", actual_start), ("actual_end", actual_end))
        )
        if skipped:
            log.info(
                "jira: 완료 필드 일부를 전송하지 않습니다(이 인스턴스에 미설정) key=%s skipped=%s",
                key, skipped,
            )
        self.transition(key, tid, fields=fields or None)
        return tid

    # ------------------------------------------------------------------
    # JQL 검색(Jira Cloud 전용 신 API + nextPageToken 자동 페이지네이션)
    # ------------------------------------------------------------------

    def _cloud_only_error(self, exc: JiraError) -> JiraError:
        """검색 실패가 "이 엔드포인트가 없다"면 Cloud 전용임을 알려주는 에러로 바꾼다.

        Server/DC 에는 ``/rest/api/3/search/jql`` 이 없어 404(또는 405/410)가 난다. 원문
        HTTP 에러만 올리면 설치자는 "권한 문제인가?"를 헤매게 되므로 원인을 지목해 준다.
        그 밖의 실패(401/403/5xx 등)는 원문 그대로 올린다.
        """
        if exc.status_code not in _ENDPOINT_ABSENT_STATUS:
            return exc
        return JiraError(
            f"JQL 검색 엔드포인트({SEARCH_JQL_PATH})가 이 사이트에 없습니다"
            f"(HTTP {exc.status_code}). 이 경로는 **Jira Cloud 전용**이며 Jira "
            f"Server/Data Center 에는 존재하지 않습니다 — 이 시스템은 현재 Jira Cloud 만 "
            f"지원합니다. jira.base_url 이 Cloud 사이트"
            f"(보통 https://<사이트>.atlassian.net)를 가리키는지 확인하세요. "
            f"현재 base_url={self.base_url!r}. 원문: {exc}",
            status_code=exc.status_code,
            body=exc.body,
        )

    def _ensure_search_payload(self, payload: Any, resp: Any) -> None:
        """검색 응답이 Cloud 형태인지 확인 — 아니면 **빈 결과 대신 에러**.

        Server/DC 는 이 경로에서 로그인/에러 **HTML** 을 200 으로 주기도 한다. 그러면
        JSON 파싱이 실패해 ``{}`` 가 되고, 그대로 두면 폴러가 "새 티켓 없음"으로 읽는다
        — 아무 일도 안 일어나는데 아무도 모르는 최악의 실패다. 그래서 알아볼 수 없는
        형태는 실패시킨다.
        """
        if isinstance(payload, dict) and (
            "issues" in payload or "nextPageToken" in payload or "isLast" in payload
        ):
            return
        snippet = (getattr(resp, "text", "") or "")[:200]
        raise JiraError(
            f"JQL 검색 응답을 알아볼 수 없습니다({SEARCH_JQL_PATH} 가 issues 를 담은 JSON 을 "
            f"돌려주지 않았습니다). 이 경로는 **Jira Cloud 전용**입니다 — Server/Data Center "
            f"이거나 인증이 로그인 페이지로 리다이렉트됐을 수 있습니다. "
            f"'티켓 없음'으로 오인되지 않도록 빈 결과 대신 실패시킵니다. "
            f"base_url={self.base_url!r} HTTP {getattr(resp, 'status_code', '?')} "
            f"응답 앞부분={snippet!r}",
            status_code=getattr(resp, "status_code", None),
        )

    def search_jql_page(
        self,
        jql: str,
        fields: Optional[list] = None,
        max_results: int = 50,
        next_page_token: Optional[str] = None,
    ) -> dict:
        """단일 페이지 JQL 검색 POST (/rest/api/3/search/jql). **Jira Cloud 전용**.

        반환: 원 응답 dict({issues, nextPageToken?, isLast?, ...}).

        Raises:
            JiraError: 엔드포인트 부재(Server/DC 신호) 또는 알아볼 수 없는 응답 형태.
                조용한 빈 결과 반환은 하지 않는다(모듈 docstring 참조).
        """
        body: dict = {"jql": jql, "maxResults": max_results}
        if fields is not None:
            body["fields"] = fields
        if next_page_token:
            body["nextPageToken"] = next_page_token
        try:
            resp = self._request("POST", SEARCH_JQL_PATH, json=body)
        except JiraError as exc:
            raise self._cloud_only_error(exc) from exc
        payload = self._json_or_empty(resp)
        self._ensure_search_payload(payload, resp)
        return payload

    def search_jql(
        self,
        jql: str,
        fields: Optional[list] = None,
        max_results: int = 50,
    ) -> dict:
        """JQL 검색 — nextPageToken을 따라 전 페이지를 모아 반환. **Jira Cloud 전용**.

        반환: ``{"issues": [...전체...], "total": N}``.
        max_results 는 페이지 크기로 사용한다(전체 상한이 아님).
        """
        all_issues: list = []
        token: Optional[str] = None
        seen_tokens: set = set()
        while True:
            page = self.search_jql_page(jql, fields=fields, max_results=max_results, next_page_token=token)
            all_issues.extend(page.get("issues", []) or [])
            token = page.get("nextPageToken")
            # isLast=True 또는 토큰 소진 시 종료. 토큰 반복(무한루프) 방어.
            if page.get("isLast") is True or not token or token in seen_tokens:
                break
            seen_tokens.add(token)
        return {"issues": all_issues, "total": len(all_issues)}
