"""워커 — claude CLI(오케스트레이터) 자율 실행 + 한도 감지 + 재개.

역할:
    큐에서 잡을 꺼내 `claude -p`로 오케스트레이터를 자율 실행한다. 실행은
    stream-json으로 파싱하며, 토큰 한도(Max 롤링)를 감지하면 잡을 interrupted로
    두고 reset_at을 기록한다(스케줄러가 리셋시각에 재개).

구현 Phase: **Phase 5** (워커 + 스케줄러).

실행 커맨드(설계 기준):
    claude -p "<프롬프트/티켓 컨텍스트>"
        --session-id <deterministic>          # 재개 키
        --output-format stream-json           # 라인 단위 JSON 이벤트
        --dangerously-skip-permissions        # 승인할 사람 없음(사내망 한정)
    재개:
        claude -p --resume <session-id>       # 세션 재개
        (폴백) --from-pr <PR#> 또는 저널+브랜치로 컨텍스트 복원

Popen 관용(claude-hacker에서 계승):
    text=True, encoding='utf-8', errors='replace', bufsize=1
    ANSI 이스케이프 제거 정규식(stream-json이 아닌 부수 출력 정리용).

⚠️ 보안:
    --dangerously-skip-permissions = 도구권한 자율 에이전트 = RCE 표면.
    사내망·신뢰 환경 한정. 동시성=1로 폭주 방지.
    학습층은 개발서버에서 read-only(자기출력 학습 금지) — CLAUDE.md 참조.
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
    """오케스트레이터 자율 실행 워커(스텁)."""

    def __init__(self, config, job_queue, gate) -> None:
        """의존성 주입(설정·큐·게이트).

        TODO(Phase 5): 참조 보관 + 동시성/정지 이벤트 준비.
        """
        self.config = config
        self.queue = job_queue
        self.gate = gate
        self._stop = None  # threading.Event (Phase 5)

    def build_command(self, job, resume: bool = False) -> list:
        """claude 실행 인자 리스트 구성(신규/재개 분기).

        TODO(Phase 5): claude_bin -p ... --session-id/--resume/--output-format
        /--dangerously-skip-permissions 조립.
        """
        raise NotImplementedError("TODO(Phase 5): build_command")

    def run_job(self, job) -> None:
        """단일 잡 실행 — Popen 스트리밍 + stream-json 파싱 + 상태 전이.

        TODO(Phase 5): Popen(text/utf-8/replace) → 라인별 JSON 파싱 →
        한도 감지 시 LimitReached → interrupted/done/failed 전이.
        """
        raise NotImplementedError("TODO(Phase 5): run_job")

    def parse_reset_at(self, event_or_text) -> Optional[str]:
        """stream-json 이벤트/텍스트에서 한도 리셋시각 파싱.

        TODO(Phase 5): 한도 메시지 패턴 → reset_at(ISO/epoch) 추출.
        """
        raise NotImplementedError("TODO(Phase 5): parse_reset_at")

    def run_forever(self) -> None:
        """큐 소비 루프(백그라운드 스레드 진입점).

        TODO(Phase 5): next_queued → run_job 반복 + 예외 격리.
        """
        raise NotImplementedError("TODO(Phase 5): run_forever")

    def stop(self) -> None:
        """루프 정지 신호.

        TODO(Phase 5): stop 이벤트 set + 실행 중 프로세스 정리.
        """
        raise NotImplementedError("TODO(Phase 5): stop")
