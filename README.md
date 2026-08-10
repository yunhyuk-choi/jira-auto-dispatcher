# jira-auto-dispatcher

Jira(HAN) 티켓이 **등록 사용자**에게 새로 할당되면 이를 감지해, **그 사용자
정체성으로** 오케스트레이터(`claude` CLI = ai-dlc-orchestrator)를 자율 실행해
브랜치·MR을 만드는 시스템이다.

단일 도커 이미지를 env `ROLE`로 두 역할로 분기한다:

- **central** (상시 컨테이너, 이 프로젝트): Jira 폴링/웹훅 → dedup 게이트 →
  티켓 담당자를 등록 사용자에 매핑 → 그 사용자 worker에 잡 배포. + 사용자
  레지스트리/온보딩/관리 UI + 사용자 worker 컨테이너 동적 spawn(Docker SDK).
- **worker** (사용자별 동적 컨테이너, `ROLE=worker DISPATCH_USER=<user>`): Jira를
  직접 보지 않는다. 중앙을 HTTP 폴링 → 잡 수신 → 그 사용자 정체성(Claude 계정·
  Jira 토큰·GitLab 토큰·git author)으로 `claude -p` 실행 → 상태/로그 회신.
  토큰 한도 감지 시 interrupted + reset_at으로 회신하고 재개한다.

베이스는 `claude-web-wrapper`(claude-hacker): Flask로 `claude` CLI를 감싼 웹 래퍼
(브라우저 로그인 + `claude -p` 스트리밍)에서 재사용 자산을 이식했다.

## per-user attribution (완전 사용자 귀속)

모든 산출물이 실제 사용자에게 귀속된다:

- **git 커밋 author** = 사용자 (`git config user.name/email` = 레지스트리 identity)
- **Jira actor** = 사용자 Jira 토큰
- **MR 생성자** = 사용자 GitLab 토큰
- **GitHub** 만 central(=나) 소유. worker는 GitHub 토큰을 받지 않는다.

## Claude 인증

각 사용자가 로컬에서 `claude setup-token`(Max, long-lived)으로 토큰을 발급 →
온보딩 폼에 붙여넣기 → worker가 `CLAUDE_CODE_OAUTH_TOKEN` env로 사용한다.
브라우저 방식(`claude auth login`, `auth_login.py`)은 **폴백**으로 유지한다.

## 아키텍처

```text
                        ┌─────────────────────── central (상시) ───────────────────────┐
Jira(HAN)               │                                                              │
  │ (1) 신규 할당 감지   │   poller ── high-watermark JQL 폴링                           │
  ├── poller ───────────┼──▶ gate  ── 단일 원자적 dedup claim                           │
  └── webhook(옵션) ─────┤    │                                                         │
                        │    ▼ (2) 담당자 account_id → registry 매핑(enabled만)         │
                        │  registry ── 등록 사용자 CRUD(state/registry.json)            │
                        │    │                                                         │
                        │    ▼ (3) dispatch.enqueue(user, job)                          │
                        │  dispatch ── 사용자별 잡 큐 + HTTP                            │
                        │    │  GET /dispatch/<user>/next   POST /dispatch/<u>/<job>/status
                        │  spawner ── 온보딩 시 worker 컨테이너 동적 spawn(Docker SDK)  │
                        └────┼─────────────────────────────────────────────────────────┘
                             │ (4) HTTP (worker가 폴링/회신)
              ┌──────────────┼──────────────┐  ...사용자마다 1개
        ┌─────▼─────┐  ┌─────▼─────┐   worker (동적, ROLE=worker DISPATCH_USER=<u>)
        │ worker A  │  │ worker B  │   ── central 폴링 → agent_runner → claude -p → 회신
        └───────────┘  └───────────┘      토큰 한도 감지 → interrupted(reset_at) → 재개
```

- **영속(state/)**: jobs·watermark·dedup·registry를 JSON으로 영속(central만).
  worker는 무상태 실행체다.
- **웹훅-레디**: 폴러가 기본. 웹훅은 켜면 동일 게이트/매핑으로 수렴(중복 안전).
- **재개**: 중앙이 interrupted 잡을 reset_at에 사용자 큐로 재-enqueue → 그 worker가
  `claude -p --resume`로 이어간다(야간 드레인 / UI "지금 재개" 동일 경로).

## central ↔ worker HTTP 프로토콜

| 메서드 | 경로 | 방향 | 내용 |
|---|---|---|---|
| GET | `/dispatch/<user>/next` | worker→central | 다음 잡 1건(JSON) 수신, running 전이. 없으면 204 |
| POST | `/dispatch/<user>/<job>/status` | worker→central | `{status, log?, reset_at?, branch?, session_id?, mr_url?, error?}` 회신 |
| GET | `/healthz` | 프로브 | 역할/사용자 헬스 |
| POST | `/onboard` | UI→central | 사용자 등록 + worker spawn (Phase 3) |

## 실행

```bash
pip install -r requirements.txt

# 설정 준비(시크릿 값은 넣지 말 것 — 파일 참조만)
cp config/config.example.yaml config/config.yaml

# central (관리 콘솔 + 감시/디스패치)
ROLE=central python -m app.main       # http://127.0.0.1:8787

# worker (보통 central이 동적 spawn; 수동 기동 시)
ROLE=worker DISPATCH_USER=yh.choi CENTRAL_URL=http://central:8787 \
  CLAUDE_CODE_OAUTH_TOKEN=... python -m app.main
```

> 현재는 스캐폴딩 단계다. 동작하는 것은 ROLE 분기 + 관리 UI 렌더 + 브라우저
> 로그인(`/start-login`·`/complete-login`) + `/healthz` 뿐이며, 레지스트리/디스패치/
> 스포너/폴러/워커/스케줄러 등 핵심 로직은 이후 Phase에서 구현된다(각 모듈
> docstring의 Phase 마커 참조).

## ⚠️ 리스크 (설계상 중요)

> 🔒 **보안 태세 & 자율 실행 인가 기록은 [SECURITY.md](SECURITY.md) 정본.** worker는 사람
> 승인 없이 bypass 사전 인가로 도구 권한 에이전트를 헤드리스 실행한다 — 그 인가 근거와
> 보완 통제(사내망 한정·MR 게이트·가역성·격리·비-root·시크릿 ro·`permission_level` 조임)는
> SECURITY.md에 명시적으로 기록돼 있다.

- **docker.sock 특권**: central이 worker를 spawn하려면 docker.sock에 접근한다 —
  사실상 호스트 root 권한과 동치인 특권 상승 표면이다. 완화책: docker-socket-proxy를
  앞단에 두고 CONTAINERS/POST만 최소 허용(`spawn.docker_host=tcp://socket-proxy:2375`),
  central 비-root 실행, 사내망 한정. (compose에 프록시 골격 주석 포함)
- **RCE 표면**: worker는 `claude -p ... --dangerously-skip-permissions`로 도구 권한을
  가진 자율 에이전트를 실행한다. 승인할 사람이 없는 자율 실행 전제이므로 권한을
  죽일 수 없다 → **사내망·신뢰 환경 한정**, 외부 노출 금지. 폭주 방지는
  `concurrency_per_worker: 1` + dedup + 결정적 브랜치(`auto/<TICKET>`) 가역성으로 한다.
- **서버 용량**: 사용자마다 상시 worker 컨테이너 1개(mem_limit 기본 4g)가 뜬다.
  사용자 수 × 리소스가 개발서버 용량을 압박할 수 있다(스케일 상한/유휴 정지 정책 필요).
- **per-user Max**: 각 사용자가 개별 Claude Max 구독(setup-token)을 붙여야 한다.
  토큰 한도(롤링)는 사용자별로 따로 걸리며, worker가 감지해 reset_at까지 재개를 미룬다.

## 연동 레포 (런타임 clone, gitignore됨)

| 레포 | 역할 | 컨테이너 경로(config `run`) |
|---|---|---|
| ai-dlc-orchestrator | 오케스트레이터(`claude`가 이 정체성으로 동작) | `/app/orchestrator` |
| dlc-meta | PR 거버넌스·에이전트 오버레이·런 저널(3층) | `/app/dlc-meta` |
| dataspace_docs | 설계 문서 저장소 | `/app/dataspace_docs` |

## 설정

`config/config.example.yaml`이 스키마 정본이다(시스템 수준 설정, 시크릿 없음).
`config/config.yaml`(gitignore됨)로 복사해 채운다. 사용자별 트리거 목록은 config가
아니라 **레지스트리**(`state/registry.json`, 온보딩 UI로 채움)가 소유한다 —
스키마 예시는 `config/registry.example.json`. 시크릿(Jira/GitLab/Claude 토큰,
웹훅 시크릿)은 값이 아니라 **파일 참조**(`secrets.base_dir` 상대 경로)로만 다룬다.
