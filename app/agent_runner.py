"""에이전트 러너 — `claude -p`(오케스트레이터) 실행 계약 구성(워커 전용).

역할:
    worker가 수신한 잡 1건을, 그 잡의 소유 사용자 정체성으로 오케스트레이터
    (`claude` CLI)를 자율 실행하도록 커맨드/환경/프롬프트를 조립한다. per-user
    attribution(git author=사용자, Jira actor=사용자 토큰, MR 생성자=사용자
    GitLab 토큰)의 실제 주입 지점이 여기다.

역할 소속: **worker**.

구현 Phase: **Phase 5** (워커 + 에이전트 실행).

실행 계약(설계 기준):
    cwd = config.run.orchestrator_repo   # 오케스트레이터 정체성으로 동작
    claude -p "<프롬프트>"
        --session-id <ticket-uuid>            # 티켓 결정적 UUID = 재개 키
        --output-format stream-json           # 라인 단위 이벤트(한도 감지)
        --dangerously-skip-permissions        # 승인할 사람 없음(사내망 한정)
    재개: claude -p --resume <session-id>

프롬프트 골자(티켓 컨텍스트):
    "HAN-XXX 티켓을 오케스트레이터로서 수행하라. 이 티켓이 곧 작업 티켓이다
     (새 티켓 생성 금지). autonomy_mode=<A|B>. 브랜치는 auto/HAN-XXX 를 쓰고,
     시작 시 '진행 중', 완료 시 '완료'로 전이하라. 완료되면 MR을 생성하라."

per-user 정체성 주입(실행 직전):
    - git:   git config user.name=<identity.git_name>,
             git config user.email=<identity.git_email>  (orchestrator_repo에)
    - Jira:  actor 토큰 env (오케스트레이터 Jira 어댑터가 소비)
    - GitLab:MR 생성자 토큰 env
    - Claude:CLAUDE_CODE_OAUTH_TOKEN(setup-token) — 컨테이너 env로 이미 존재하나
             값 재확인/전달
    ⚠️ GitHub은 central(나)만 — worker는 GitHub 토큰을 받지 않는다.

Popen 관용(claude-hacker/worker에서 계승):
    text=True, encoding='utf-8', errors='replace', bufsize=1
    ANSI 이스케이프 제거 정규식은 worker.ANSI_ESCAPE 를 계승해 사용.
"""

from __future__ import annotations

from typing import Optional


class AgentRunner:
    """사용자 정체성으로 `claude -p` 실행 계약 구성(스텁)."""

    def __init__(self, config) -> None:
        """설정 주입(run.* 파라미터·바이너리·모드).

        TODO(Phase 5): config.run 보관(claude_bin, orchestrator_repo 등).
        """
        self.config = config

    def build_prompt(self, job) -> str:
        """티켓 컨텍스트로 오케스트레이터 프롬프트 문자열 구성.

        TODO(Phase 5): 티켓키·autonomy_mode·브랜치/전이/MR 지침을 담은 프롬프트.
        (새 티켓 생성 금지 = 트리거 티켓이 곧 작업 티켓)
        """
        raise NotImplementedError("TODO(Phase 5): build_prompt")

    def build_command(self, job, resume: bool = False) -> list:
        """claude 실행 인자 리스트 구성(신규/재개 분기).

        TODO(Phase 5): claude_bin -p <prompt> --session-id <uuid>
        --output-format stream-json --dangerously-skip-permissions.
        resume=True면 --resume <session-id>.
        """
        raise NotImplementedError("TODO(Phase 5): build_command")

    def build_env(self, job, user) -> dict:
        """사용자 정체성 주입 env 조립(Jira/GitLab/Claude 토큰).

        TODO(Phase 5): CLAUDE_CODE_OAUTH_TOKEN + Jira/GitLab actor 토큰을
        secrets_ref에서 해석해 env dict로. GitHub 토큰은 넣지 않는다.
        """
        raise NotImplementedError("TODO(Phase 5): build_env")

    def apply_git_identity(self, user) -> None:
        """orchestrator_repo에 사용자 git author 정체성 주입(실행 직전).

        TODO(Phase 5): git config user.name/user.email = identity.*.
        """
        raise NotImplementedError("TODO(Phase 5): apply_git_identity")

    def run(self, job, user) -> Optional[dict]:
        """잡 1건을 사용자 정체성으로 실행하고 결과/상태를 반환.

        TODO(Phase 5): apply_git_identity → Popen(build_command, build_env,
        cwd=orchestrator_repo, text/utf-8/replace) → stream-json 라인 파싱을
        worker로 넘김(또는 여기서 처리). 결과 dict(status/branch/mr_url/
        session_id/reset_at) 반환.
        """
        raise NotImplementedError("TODO(Phase 5): run")
