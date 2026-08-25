"""파일럿 Tier-2 서브에이전트(central 전용) — Phase 3b-2.

역할(설계 §4 3b-2 + §5 D1–D4 확정):
    **한 종류의 Tier-2 노드**(per-user, D2). 한 파일럿 사용자의 티켓 배치를 받아
        1) 3b-0 상태를 조회해 **순서/페이싱을 제안**(propose),
        2) 각 티켓을 **그 사용자 본인의 Tier-3 컨테이너**(기존 디스패치 경로)로 위임
           (dispatch — 레포락/어드미션은 **파이썬이 강제**, D1),
        3) 3b-1 **명시적 id correlation** 으로 결과를 회수(collect),
        4) 완료 회신을 **리뷰**해 (초기엔 로그로) **보고**,
        5) 큐 드레인 시 **종료**(persistent-but-ephemeral, D2).
    실제 작업/자격은 여전히 사용자 컨테이너(Tier-3)에 남는다(enabler 4) — Tier-2는
    센트럴 파이썬 안에서 **스케줄링/조율 판단만** 한다.

**스왑 가능 인터페이스**(:class:`Tier2Runner`):
    구체 러너를 뒤에서 바꿀 수 있게 얇은 인터페이스로 감싼다. 이 MR은 **SDK 없이도**
    가치 있는 조각(자원툴 + 결정적 MOCK 러너)을 안전하게 착지시키고, **SDK 백엔드 러너**
    (:class:`SdkTier2Runner`)는 **오너의 의존성 확정 후** 얹을 **pending 조각**으로 명시한다.
        - :class:`MockTier2Runner` — SDK 불요. 자원툴을 그대로 조합해 propose→dispatch→
          collect를 **결정적**으로 수행(CI 검증 가능). 실제 에이전트 추론은 없다.
        - :class:`SdkTier2Runner` — 실제 Claude Agent SDK 인프로세스 에이전트(D3-a).
          현재 **미배선**: ``claude-agent-sdk`` 의존성/이미지 결정이 오너 확정 전이라,
          import/실행 시 명확한 :class:`Tier2SdkUnavailable` 를 던진다(조용한 실패 금지).

⚠️ 피처 플래그 OFF가 기본: :func:`build_pilot_tier2` 는 ``run.tier2_pilot_user`` 가
비어 있으면 **None**을 돌려주고, 아무 것도 배선/기동하지 않는다 → 디스패치 동작 무변경.
플래그가 켜져도 이 스캐폴드 단계에서는 러너를 **자동 실행하지 않는다**(수동/후속 기동).

역할 소속: **central**. POLICY-ENCODING: 이 파일은 UTF-8(BOM 없음)·LF.
"""

from __future__ import annotations

import logging
from typing import List, Optional, Protocol, runtime_checkable

from app import queue as q
from app.tier2_tools import Tier2Tools

log = logging.getLogger("jad.tier2")


class Tier2SdkUnavailable(RuntimeError):
    """SDK 백엔드 러너가 요청됐으나 ``claude-agent-sdk`` 의존성이 미확정/미설치."""


@runtime_checkable
class Tier2Runner(Protocol):
    """Tier-2 러너 인터페이스 — 구체 백엔드(mock/SDK)를 뒤에서 스왑하기 위한 경계.

    구현체는 자원툴만으로 propose→dispatch→collect→report를 수행하고, 드레인 시
    종료한다. ``run_once`` 는 **한 번의 드레인 시도**(현재 적격분 처리 + 회수 1패스)를
    돌리고 보고 dict를 반환한다 — CI에서 블로킹 없이 검증 가능한 단위.
    """

    user: str

    def run_once(self) -> dict:
        """현재 적격 배치를 propose→dispatch→collect하고 보고 dict를 반환."""
        ...


class MockTier2Runner:
    """결정적 MOCK Tier-2 러너 — SDK 불요, 자원툴만 조합(CI 검증용).

    실제 LLM 추론 대신 **투명한 휴리스틱**으로 순서를 제안한다(감사 가능):
    interrupted_ready(재적격)을 queued보다 먼저, 그 안에서는 ticket id 사전순.
    이는 "에이전트 제안 / 파이썬 강제"(D1)의 제안측 자리끼움(placeholder)이며, 실제
    SDK 러너로 교체될 때 이 순서 제안이 에이전트 판단으로 바뀐다. 강제(레포락/어드미션)는
    양쪽 모두 파이썬 tick이 담당하므로 교체해도 안전 불변식은 동일하다.
    """

    def __init__(self, tools: Tier2Tools) -> None:
        self.tools = tools
        self.user = tools.user

    # -- 제안(propose) --

    @staticmethod
    def propose_order(eligible: List[dict]) -> List[dict]:
        """적격 티켓의 **처리 순서 제안**(결정적 휴리스틱, 부작용 0).

        재적격(interrupted_ready)을 신규(queued)보다 우선(진행 중이던 작업 먼저 마무리),
        동순위는 ticket id 사전순 → 완전 결정적. 실제 SDK 러너에서 이 자리를 에이전트
        판단이 대체한다.
        """
        def key(e: dict):
            reason_rank = 0 if e.get("reason") == "interrupted_ready" else 1
            return (reason_rank, str(e.get("ticket", "")))

        return sorted(eligible, key=key)

    # -- 한 드레인 패스 --

    def run_once(self) -> dict:
        """한 배치 드레인 시도: propose→dispatch(제안)→collect(1패스)→report.

        블로킹하지 않는다 — dispatch는 파이썬 강제(defer 가능)를, collect는 **현재**
        해소된 결과만 회수한다. 미완 위임은 pending으로 남고 drained=False로 보고된다
        (실 러너/스케줄러 tick이 후속 완료를 채운다). 반환 dict가 이 라운드의 보고서다.
        """
        eligible = self.tools.eligible_tickets()
        order = self.propose_order(eligible)
        proposed = [e.get("ticket") for e in order]

        dispatched: List[dict] = []
        correlation_ids: List[str] = []
        for e in order:
            ticket = e.get("ticket")
            try:
                res = self.tools.dispatch(ticket)
            except (KeyError, ValueError) as exc:  # 교차 사용자/소실 잡은 건너뛴다(격리)
                log.warning("Tier-2[%s] dispatch 건너뜀 %s: %s", self.user, ticket, exc)
                continue
            dispatched.append(res)
            correlation_ids.append(res["correlation_id"])

        results = self.tools.collect(correlation_ids) if correlation_ids else {}
        reviewed = self._review(results)
        pending = self.tools.pending_ids()

        report = {
            "user": self.user,
            "backend": "mock",
            "proposed_order": proposed,
            "rationale": "interrupted_ready 우선 + ticket id 사전순(결정적 휴리스틱)",
            "dispatched": dispatched,
            "correlation_ids": correlation_ids,
            "results": results,
            "reviewed": reviewed,
            "pending_ids": pending,
            "drained": not pending,
        }
        log.info("Tier-2[%s] run_once: 제안 %d · 디스패치 %d · 미해소 %d",
                 self.user, len(proposed), len(dispatched), len(pending))
        return report

    @staticmethod
    def _review(results: dict) -> dict:
        """완료 회신 리뷰 요약(초기엔 로그/보고용) — terminal 결과의 성/실 + mr/로그경로.

        완료 회신의 ``cycle_log_path`` (§1.5)와 mr_url을 surfacing한다 — 실제 SDK 러너의
        "리뷰" 자리끼움. 미완은 건너뛴다.
        """
        out: dict = {}
        for cid, r in (results or {}).items():
            if not r.get("terminal"):
                continue
            payload = r.get("result") or {}
            out[cid] = {
                "ticket": payload.get("ticket"),
                "job_status": r.get("job_status"),
                "ok": r.get("job_status") == q.DONE,
                "mr_url": payload.get("mr_url"),
                "cycle_log_path": payload.get("cycle_log_path"),
            }
        return out


class SdkTier2Runner:
    """실제 Claude Agent SDK 인프로세스 Tier-2 러너(D3-a) — **pending(미배선)**.

    의도한 배선(오너의 의존성 확정 후):
        - ``from claude_agent_sdk import tool, create_sdk_mcp_server, ClaudeSDKClient,
          ClaudeAgentOptions`` (pip ``claude-agent-sdk``, Python ≥3.10, 플랫폼 휠에
          네이티브 바이너리 번들 · env ``CLAUDE_CODE_OAUTH_TOKEN`` 인증 — 센트럴 기보유).
        - :class:`Tier2Tools` 의 read_state/eligible_tickets/dispatch/collect 를 각각
          ``@tool`` 핸들러로 감싸 **인프로세스 MCP 서버**(``create_sdk_mcp_server``)로
          등록(``mcp_servers`` + ``allowed_tools``). **커스텀 도구는 이 자원 ops 뿐**(D3).
        - 서브에이전트 spawn은 SDK 네이티브(``agents``/Task) — 파이썬 도구로 만들지 않는다.
        - 지속 세션(``ClaudeSDKClient``)으로 일이 있는 동안 살아 있다가 드레인 시 종료(D2).

    지금은 ``claude-agent-sdk`` 의존성/이미지 결정이 **오너 확정 전**이라 조용히 추측 설치
    하지 않는다(작업 지시 · POLICY). 생성/실행 시 :class:`Tier2SdkUnavailable` 를 던져
    "아직 미배선"임을 분명히 한다. import는 지연(lazy)이라 이 모듈 로드가 SDK를 끌어오지
    않는다(플래그 OFF·mock 경로가 SDK 없이도 완전 동작).
    """

    def __init__(self, tools: Tier2Tools, *, model: Optional[str] = None) -> None:
        self.tools = tools
        self.user = tools.user
        self.model = model
        # 지연 import — SDK 유무를 여기서만 확인. 미설치면 명확히 실패.
        try:  # pragma: no cover - SDK 미설치가 CI 기본이라 실행되지 않음
            import claude_agent_sdk  # noqa: F401
        except ImportError as exc:
            raise Tier2SdkUnavailable(
                "SDK 백엔드 러너 미배선: 'claude-agent-sdk' 미설치. 오너의 의존성/이미지 "
                "확정(§5 D3) 후 requirements.txt·Dockerfile에 추가하고 이 러너를 활성화하세요. "
                "그 전에는 MockTier2Runner(SDK 불요)를 사용합니다."
            ) from exc

    def run_once(self) -> dict:  # pragma: no cover - 미배선 경로
        raise Tier2SdkUnavailable(
            "SdkTier2Runner.run_once 미구현 — SDK 배선은 오너 확정 후 후속 MR에서."
        )


# ---------------------------------------------------------------------------
# 팩토리 + 조건부 빌드(피처 플래그)
# ---------------------------------------------------------------------------


def make_tier2_runner(tools: Tier2Tools, *, backend: str = "mock",
                      model: Optional[str] = None) -> Tier2Runner:
    """백엔드 이름으로 러너를 만든다. 기본 'mock'(SDK 불요).

    backend='sdk' 는 명시적으로 요청할 때만 SDK 러너를 시도한다(미설치면
    :class:`Tier2SdkUnavailable`). 그 외 이름은 ValueError.
    """
    b = (backend or "mock").strip().lower()
    if b == "mock":
        return MockTier2Runner(tools)
    if b == "sdk":
        return SdkTier2Runner(tools, model=model)
    raise ValueError(f"알 수 없는 Tier-2 백엔드: {backend!r} (mock|sdk)")


def build_pilot_tier2(components: dict, *, backend: str = "mock") -> Optional[dict]:
    """피처 플래그를 읽어 파일럿 Tier-2 조각을 **조건부** 조립.

    ``config.run.tier2_pilot_user`` 가 비어 있으면(기본 OFF) **None**을 돌려주고 아무
    것도 만들지 않는다 → Tier-2 경로 완전 비활성, 디스패치 동작 무변경. 값이 있으면 그
    사용자에 바인딩된 :class:`Tier2Tools` + 러너(기본 mock)를 만들어 dict로 돌려준다.

    ⚠️ **자동 실행하지 않는다** — 여기서는 조립만. 러너 기동(루프/스레드)은 SDK 배선 및
    오너 확정 후 후속에서 얹는다(이 MR은 스캐폴드 착지).

    반환: {"user", "tools", "runner", "backend"} 또는 None(OFF).
    """
    cfg = components.get("config")
    dispatcher = components.get("dispatcher")
    pilot = (getattr(getattr(cfg, "run", None), "tier2_pilot_user", "") or "").strip()
    if not pilot:
        return None
    if dispatcher is None:
        log.warning("Tier-2 파일럿(%s) 활성이나 dispatcher 미조립 — 스킵", pilot)
        return None
    tools = Tier2Tools(dispatcher, pilot)
    runner = make_tier2_runner(tools, backend=backend)
    log.info("Tier-2 파일럿 조립: user=%s backend=%s (자동 실행 안 함 — 스캐폴드)",
             pilot, backend)
    return {"user": pilot, "tools": tools, "runner": runner, "backend": backend}
