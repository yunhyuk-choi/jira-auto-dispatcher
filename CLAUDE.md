# CLAUDE.md — jira-auto-dispatcher (이 레포에서 작업할 Claude 온보딩)

> 이 파일은 Claude Code 세션이 **이 리포지토리**에서 작업할 때 읽는 온보딩 문서다.
> 사람용 개요는 [README.md](README.md). 여기서는 *다른 Claude가 맥락을 빠르게 잡고
> 설계 불변식을 깨지 않도록* 의도·함정·경계를 기록한다.

## 이 시스템 = 2-역할 디스패처 (경계를 지켜라)

단일 도커 이미지를 env `ROLE`로 두 역할로 분기한다(`app/main.py`):

- **central** (상시 컨테이너): Jira(<PROJECT_KEY>) 신규 할당을 **감지** → dedup → 담당자를
  **등록 사용자에 매핑** → 그 사용자 worker에 잡 **배포**. + 사용자 레지스트리/
  온보딩/관리 UI + 사용자 worker 컨테이너 **동적 spawn**(Docker SDK).
- **worker** (사용자별 동적 컨테이너, `ROLE=worker DISPATCH_USER=<user>`): Jira를
  직접 보지 않는다. 중앙을 HTTP 폴링 → 잡 수신 → **그 사용자 정체성**으로
  오케스트레이터(`claude -p`)를 **기동** → 상태/로그 회신 → 한도로 끊기면 **재개**.

- **상태 전이·티켓 팔로우·브랜치/MR 생성은 앱이 흉내내지 말 것.** 그것은
  worker가 실행하는 **오케스트레이터가 사용자 토큰으로** 담당한다. 앱은
  "감지 → 매핑 → 디스패치 → 기동 → 재개"의 파이프라인만 책임진다.

## per-user attribution (완전 사용자 귀속) — 불변식

worker가 잡 실행 직전 그 사용자 정체성을 주입한다(`app/agent_runner.py`):

- **git 커밋 author** = 레지스트리 `identity.git_name/git_email`
- **Jira actor** = 사용자 Jira 토큰(env)
- **MR 생성자** = 사용자 GitLab 토큰(env)
- **Claude** = 사용자 `CLAUDE_CODE_OAUTH_TOKEN`(setup-token)
- **GitHub 은 central(=나)만.** worker에는 GitHub 토큰을 절대 주입하지 않는다.

## Claude 인증 = setup-token 온보딩

각 사용자가 로컬에서 `claude setup-token`(Max, long-lived)으로 발급 → 온보딩 폼에
붙여넣기 → worker가 `CLAUDE_CODE_OAUTH_TOKEN`으로 사용. 브라우저 로그인
(`claude auth login`, `auth_login.py`)은 **폴백**으로만 유지.

## 핵심 설계 불변식 (깨지 말 것)

1. **단일 원자 dedup 게이트** — 폴러·웹훅 둘 다 반드시 `gate.claim()`으로 수렴한다.
   어느 경로도 큐에 직접 넣지 않는다. 중복 트리거는 게이트가 흡수한다.
2. **매핑 게이트** — claim 통과 후 담당자 account_id를 registry로 사용자에 매핑한다.
   **enabled 사용자만** 디스패치하고, 미등록/비활성/미매핑이면 skip + 로그(큐잉 금지).
3. **루프 차단** — 무한 자동화 루프를 다음으로 막는다:
   - 트리거 티켓 = 작업 티켓 (오케스트레이터는 새 티켓을 만들지 않고 기존 티켓으로 처리)
   - dedup(같은 티켓 재트리거 차단)
   - `concurrency_per_worker: 1` (사용자별 자율 에이전트 폭주 방지)
   - 브랜치/MR 가역성 (사고 시 `auto/<TICKET>` 브랜치 삭제로 리셋)
4. **worker는 Jira를 직접 보지 않는다** — 오직 central↔worker HTTP로만 잡을 주고받는다.
   worker는 무상태 실행체다(영속은 central의 state/ 만).
5. **재개(resume)** — 중앙이 interrupted 잡을 reset_at에 **사용자 큐로 재-enqueue** →
   그 worker가 `claude -p --resume <session-id>`로 이어간다. 폴백은 런 저널 +
   결정적 브랜치. 사용자 `~/.claude` 볼륨 영속이 전제다.
6. **토큰 한도(Max 롤링)** — worker가 `--output-format stream-json`으로 한도를 감지하면
   `interrupted` + `reset_at`으로 회신한다. 재개는 리셋시각 스케줄러 / 야간 드레인 /
   UI "지금 재개" 중 하나로 트리거되며 모두 dispatcher 재-enqueue 경로다.
7. **시크릿은 값이 아니라 참조** — 레지스트리/config에 토큰 "값"을 넣지 않는다.
   시크릿 파일(secrets.base_dir 상대)만 참조하고 값은 런타임에 읽는다.

## central ↔ worker HTTP 프로토콜

| 메서드 | 경로 | 방향 | 내용 |
|---|---|---|---|
| GET | `/dispatch/<user>/next` | worker→central | 다음 잡 1건, running 전이. 없으면 204 |
| POST | `/dispatch/<user>/<job>/status` | worker→central | `{status, log?, reset_at?, branch?, session_id?, mr_url?, error?}` |
| POST | `/onboard` | UI→central | 사용자 등록 + worker spawn |
| GET | `/healthz` | 프로브 | 역할/사용자 헬스 |

## 레지스트리 스키마 (state/registry.json)

온보딩으로 채워지는 운영 데이터(gitignore). 필드: `username, display_name,
jira_account_id, jira_email, enabled(자동트리거 토글), autonomy_mode(A|B), agent,
per_repo{}, identity{git_name,git_email}, scope{projects:[]}, container{name,status},
secrets_ref{jira_token,gitlab_token,claude_oauth_token}`. 스키마 예시는
`config/registry.example.json`. **secrets_ref는 참조만**(값 금지).

## dlc-meta 3층 구조

| 층 | 경로 | 내용 |
|---|---|---|
| 공통 | `common/` | PR 거버넌스(프론트=컨벤션 성숙, 백엔드=WIP) |
| 사용자 오버레이 | `agents/<user>/` | 사용자별 에이전트 오버레이(registry `agent` 키) |
| 런 저널 | `runs/<ticket>/` | 티켓별 실행 저널(재개 폴백 컨텍스트) |

## 자율 모드 A/B

레지스트리 `autonomy_mode`로 분기한다(둘 다 구현 대상):

- **A = 완전자율 MR 초안** — 컨벤션이 성숙한 레포(예: portal-frontend)에 적합.
- **B = 경량 1차 + 로컬 완성** — 컨벤션 WIP 레포(예: portal-backend)에 적합.

레포별 오버라이드는 `per_repo`로 지정한다. worker의 `agent_runner`가 이 모드를
프롬프트에 실어 오케스트레이터에 전달한다.

## 구현 로드맵 (Phase 마커)

각 모듈 docstring에 담당 Phase가 명시돼 있다. 스캐폴딩(Phase 0)은 완료:

| Phase | 모듈 | 내용 |
|---|---|---|
| 0 (완료) | `auth_login.py`, `main.py`(ROLE 분기 골격), `templates/index.html` | 로그인 이식 + central/worker 분기 진입점 + 관리 UI 골격 |
| 1 | `config.py`, `state.py` | 설정 로드/검증(central 스키마) + state/*.json(+registry) 영속 |
| 2 | `jira_client.py` | Jira REST(issue/transition/comment/JQL, 감시 토큰) |
| 3 | `gate.py`, `queue.py`, `registry.py`, `dispatch.py` | dedup 게이트 + 잡 상태머신 + 레지스트리 CRUD + 사용자별 큐/HTTP + 온보딩 API |
| 4 | `poller.py`, `webhook.py` | high-watermark 폴링 + 사용자 매핑 + 웹훅 수렴 |
| 5 | `worker.py`, `agent_runner.py`, `scheduler.py`, `main.py`(배선) | 중앙 폴링 루프 + claude 실행/정체성 주입/한도/재개 + 스케줄러 |
| 6 | `spawner.py`, `Dockerfile`, `docker-compose.yml` | worker 동적 spawn(Docker SDK) + 컨테이너/배포 |

## ⚠️ 리스크

> 🔒 자율 full-permission 실행 인가 기록·보완 통제는 [SECURITY.md](SECURITY.md) 정본.

- **docker.sock 특권**: central의 spawner가 docker.sock에 접근 = 호스트 root 동치
  (특권 상승 표면). 완화책: docker-socket-proxy로 CONTAINERS/POST 최소 허용
  (`spawn.docker_host=tcp://socket-proxy:2375`), central 비-root, 사내망 한정.
- **RCE 표면**: worker의 `--dangerously-skip-permissions`는 도구 권한 자율 에이전트다.
  승인할 사람 없는 자율 실행이라 권한을 죽일 수 없다 → **사내망·신뢰 환경 한정**,
  외부 노출 금지. 폭주 방지는 `concurrency_per_worker: 1` + dedup + 브랜치 가역성.
- **서버 용량**: 사용자마다 상시 worker 컨테이너(mem_limit 기본 4g). 사용자 수 ×
  리소스가 개발서버를 압박 → 스케일 상한/유휴 정지 정책 필요.
- **per-user Max**: 사용자마다 개별 Claude Max(setup-token). 한도는 사용자별로 걸린다.

## 함정 (claude-hacker 계승)

- **인코딩(Popen)**: Windows(cp949)에서 `claude` UTF-8 출력 디코딩 실패를 막으려면
  모든 `subprocess.Popen`은 `text=True, encoding='utf-8', errors='replace'`.
  ANSI 이스케이프 제거 정규식은 `worker.ANSI_ESCAPE`로 계승.
- **커맨드명**: 로그인은 `claude auth login`(‘claude login’은 없음), 비대화형 실행은
  `claude -p <prompt>`, 토큰 발급은 `claude setup-token`.
- **로그인 코드 주입**: `claude auth login`은 `code#state`를 CLI **stdin에 붙여넣어**
  완료한다(폴백 경로). CLI가 TTY 전용으로 코드를 읽으면 pty(pywinpty) 우회 필요할 수 있음.
- **파일명**: 프론트 템플릿은 `templates/index.html`이다. `render_template('index.html')`과 일치.

## POLICY-ENCODING

이 레포에서 생성하는 모든 파일은 **UTF-8(BOM 없음)·LF**. 로케일 의존 셸 출력 금지,
생성 후 손상 검증.
