"""워커 — 중앙 폴링 → 에이전트 실행 → 상태 회신 + 한도 감지/재개(워커 전용).

역할:
    사용자별 동적 컨테이너(ROLE=worker DISPATCH_USER=<user>)로 뜨는 실행체.
    Jira를 직접 보지 않고 CENTRAL_URL을 HTTP 폴링해 자기 잡을 받아, 그 사용자
    정체성으로 오케스트레이터(`claude -p`, agent_runner)를 자율 실행하고, 상태/
    로그를 중앙에 회신한다. 토큰 한도(Max 롤링)를 감지하면 interrupted+reset_at
    으로 회신하고 재개를 스케줄한다.

역할 소속: **worker**.

구현 Phase: **Phase 5** (워커 + 에이전트 실행).

환경(스포너가 주입):
    ROLE=worker
    DISPATCH_USER=<username>                # 이 워커가 대리하는 사용자
    CENTRAL_URL=http://central:8787         # 잡 수신/회신 대상
    CLAUDE_CODE_OAUTH_TOKEN=<setup-token>   # 사용자 Claude 인증(Max)

central↔worker HTTP 프로토콜:
    GET  {CENTRAL_URL}/dispatch/<DISPATCH_USER>/next
        → 다음 잡(JSON) 또는 204(대기).
    POST {CENTRAL_URL}/dispatch/<DISPATCH_USER>/<job>/status
        → {status, log?, reset_at?, branch?, session_id?, mr_url?, error?}

실행 위임:
    agent_runner.AgentRunner.run(job, user)로 `claude -p` 계약을 조립·실행.
    stream-json 라인 파싱은 여기(또는 runner)에서 하며, 한도 감지 시
    LimitReached(reset_at)로 interrupted 전이.

Popen 관용(claude-hacker에서 계승):
    text=True, encoding='utf-8', errors='replace', bufsize=1
    ANSI 이스케이프 제거 정규식(stream-json이 아닌 부수 출력 정리용).

⚠️ 보안:
    --dangerously-skip-permissions = 도구권한 자율 에이전트 = RCE 표면.
    사내망·신뢰 환경 한정. 동시성=1(concurrency_per_worker)로 폭주 방지.
"""

from __future__ import annotations

import re
from typing import Optional

# claude-hacker에서 계승: 터미널 ANSI 이스케이프 제거 정규식
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


class LimitReached(Exception):
    """토큰 한도 도달 신호 — reset_at을 실어 interrupted 전이에 사용."""

    def __init__(self, reset_at: Optional[str] = None) -> None:
        super().__init__("token limit reached")
        self.reset_at = reset_at


class Worker:
    """중앙 폴링 기반 사용자 워커(스텁)."""

    def __init__(self, config, agent_runner) -> None:
        """설정 + 에이전트 러너 주입. DISPATCH_USER/CENTRAL_URL은 env에서.

        TODO(Phase 5): config·runner 보관, env(DISPATCH_USER, CENTRAL_URL) 로드,
        requests.Session + 정지 이벤트 준비.
        """
        self.config = config
        self.runner = agent_runner
        self.user = None        # env DISPATCH_USER (Phase 5)
        self.central_url = None  # env CENTRAL_URL (Phase 5)
        self._stop = None        # threading.Event (Phase 5)

    def fetch_next(self) -> Optional[dict]:
        """중앙에서 다음 잡 수신(GET /dispatch/<user>/next).

        TODO(Phase 5): requests.get(next_url) → 잡 JSON 또는 None(204).
        """
        raise NotImplementedError("TODO(Phase 5): fetch_next")

    def report_status(self, job_id: str, payload: dict) -> None:
        """중앙에 상태/로그 회신(POST /dispatch/<user>/<job>/status).

        TODO(Phase 5): requests.post(status_url, json=payload).
        """
        raise NotImplementedError("TODO(Phase 5): report_status")

    def run_job(self, job, resume: bool = False) -> None:
        """단일 잡 실행 — agent_runner 위임 + stream-json 파싱 + 상태 회신.

        TODO(Phase 5): runner.run(job, user) → 라인별 JSON 파싱 → 한도 감지 시
        LimitReached → interrupted(reset_at)/done/failed 회신.
        """
        raise NotImplementedError("TODO(Phase 5): run_job")

    def parse_reset_at(self, event_or_text) -> Optional[str]:
        """stream-json 이벤트/텍스트에서 한도 리셋시각 파싱.

        TODO(Phase 5): 한도 메시지 패턴 → reset_at(ISO/epoch) 추출.
        """
        raise NotImplementedError("TODO(Phase 5): parse_reset_at")

    def run_forever(self) -> None:
        """중앙 폴링 루프(worker 프로세스 진입점).

        TODO(Phase 5): stop까지 fetch_next → run_job 반복 + 예외 격리 + 백오프.
        """
        raise NotImplementedError("TODO(Phase 5): run_forever")

    def stop(self) -> None:
        """루프 정지 신호.

        TODO(Phase 5): stop 이벤트 set + 실행 중 프로세스 정리.
        """
        raise NotImplementedError("TODO(Phase 5): stop")
