# jira-auto-dispatcher

> ## ⚠️ 이 소프트웨어를 실행한다 = 자율 에이전트에게 풀 퍼미션을 준다
>
> 이 시스템은 **사람의 매 단계 승인 없이**, 파일 쓰기·셸 실행·`git push` 권한을 가진
> 코딩 에이전트(`claude`)를 **헤드리스로** 돌린다(`--dangerously-skip-permissions` 상당).
> 승인 다이얼로그를 눌러 줄 사람이 없다는 것이 전제이므로, **권한을 죽이는 선택지는 없다** —
> 그게 이 시스템의 동작 원리다.
>
> **따라서 이 소프트웨어를 기동하는 행위 자체가, 당신이 붙인 토큰의 권한 범위 안에서
> 에이전트가 무엇을 하든 그 결과에 동의한 것이다.** 에이전트는 당신이 준 forge 토큰으로
> 브랜치를 밀고, 당신이 준 Jira 토큰으로 티켓을 전이시키고, 워커 컨테이너 안에서 임의의
> 명령을 실행한다. 이 동의는 `config.yaml` 의 `consent.full_permissions: true` 로 **명시**해야
> 한다(기본 `false`).
>
> **관리 UI(8787)와 worker 컨테이너는 그 자체로 원격 코드 실행(RCE) 표면이다.**
> - 🚫 **인터넷에 노출하지 말 것.** 퍼블릭 IP·포트포워딩·리버스 프록시로 열지 말 것.
> - ✅ **신뢰 네트워크 한정** — 사설망/VPN 안에서만. 원격 접근이 필요하면 **SSH 터널**을 쓴다.
> - ✅ 전용 계정·전용 토큰으로 돌리고, 권한 범위를 필요한 레포로 좁힌다.
>
> 위험 모델·보완 통제의 정본은 **[SECURITY.md](SECURITY.md)**, 설치는 **[INSTALL.md](INSTALL.md)**.

## 설치 — 여기서 시작

```bash
python -m app.setup wizard      # 대화로 물어보고 config.yaml·시크릿·진단까지 이끈다
```

물어본 값은 `setup-answers.json` 에 계속 저장되므로 **중간에 그만두고 나중에 이어서**
해도 된다. 검증·생성·판정은 전부 설치 관문 CLI(`python -m app.setup validate|render|
doctor`)가 하고, 통과하지 못하면 `config.yaml` 이 만들어지지 않는다.

**이 CLI 가 1차 진입점이다 — `claude` 가 없어도 설치가 끝난다.** 손으로 채우는 절차도
[INSTALL.md](INSTALL.md) 에 그대로 유효하다.

<details><summary>선택 — <code>claude</code> 를 쓴다면 슬래시 커맨드로도 시작할 수 있다</summary>

```bash
python -m app.setup skill          # skill-templates/ → 내 .claude/skills/ (멱등)
python -m app.setup skill --list   # 어떤 스킬이 있는지만 보기
```

만들어진 스킬은 `claude` 세션에서 `/install-jira-auto-dispatcher` 로 부른다. 스킬도 결국
같은 CLI(`validate`/`render`/`doctor`)를 태우므로 **게이트는 하나다.**

`.claude/` 아래는 그 머신의 **개인 영역**이라 리포가 추적하지 않는다(세션 인증·로컬 설정이
섞이는 자리다). 추적되는 것은 `skill-templates/` 의 템플릿뿐이고 — 템플릿을 하나 더 넣으면
코드 수정 없이 설치 대상이 하나 더 된다 — 실제 파일은 머신마다 위 명령으로 만든다.
만들지 못해도(권한 없음·읽기전용 FS) **설치에는 아무 지장이 없다** — 스킬은 편의지 필수
경로가 아니다.

</details>

Jira(`jira.project` + `jira.projects` 로 지정, 사용자별로는 온보딩의 `scope` 로 좁힌다)
티켓이 **등록 사용자**에게 새로 할당되면 이를 감지해, **그 사용자
정체성으로** 오케스트레이터(`claude` CLI = ai-dlc-orchestrator)를 자율 실행해
브랜치·MR을 만드는 시스템이다.

> ⚠️ **Jira Cloud 전용** — 티켓 감지는 `POST /rest/api/3/search/jql` 로 하는데, 이 경로는
> Jira Cloud 에만 있다(Server/Data Center 에는 없고, 옛 `/rest/api/2/search` 는 Cloud 에서
> 삭제됐다). Server/DC 는 검증할 수단이 없어 **지원하지 않는다** — 그런 사이트를 가리키면
> "티켓 없음"처럼 조용히 넘어가지 않고 원인을 말하는 에러로 실패한다.
> 커스텀필드 id·완료 전이 id 처럼 **인스턴스마다 다른 값**은 코드 상수가 아니라 설정
> (`config/config.example.yaml` 의 `jira:` 섹션)에서 받는다 — 알아내는 방법도 그 주석에 있다.

단일 도커 이미지를 env `ROLE`로 두 역할로 분기한다:

- **central** (상시 컨테이너, 이 프로젝트): Jira 폴링/웹훅 → dedup 게이트 →
  티켓 담당자를 등록 사용자에 매핑 → 그 사용자 worker에 잡 배포. + 사용자
  레지스트리/온보딩/관리 UI + 사용자 worker 컨테이너 동적 spawn(Docker SDK).
- **worker** (사용자별 동적 컨테이너, `ROLE=worker DISPATCH_USER=<user>`): Jira를
  직접 보지 않고, **중앙에 아무것도 요청하지 않는다**. 그냥 살아 있는 실행 자리다 —
  중앙이 `docker exec` 로 그 컨테이너 안에 세션을 **밀어 넣고**, 그 사용자 정체성
  (Claude 계정·Jira 토큰·forge 토큰·git author)으로 `claude -p` 가 돈다.
  토큰 한도 감지 시 interrupted + reset_at 으로 기록되고 재개한다.

베이스는 `claude-web-wrapper`(claude-hacker): Flask로 `claude` CLI를 감싼 웹 래퍼
(브라우저 로그인 + `claude -p` 스트리밍)에서 재사용 자산을 이식했다.

## per-user attribution (완전 사용자 귀속)

모든 산출물이 실제 사용자에게 귀속된다:

- **git 커밋 author** = 사용자 (`git config user.name/email` = 레지스트리 identity)
- **Jira actor** = 사용자 Jira 토큰
- **MR/PR 생성자** = 사용자 forge 토큰(GitLab PAT / GitHub PAT — `forge.kind` 에 따라)
- 반대로 **central 서비스 자신의 토큰**(`forge.token_ref`)은 central 전용이다. worker 는
  이 토큰을 상속받지 않는다 — 산출물이 서비스 계정이 아니라 **실제 사용자**에게 귀속되도록
  worker env 에서 명시적으로 제거한다(`app/agent_runner.py`).

## Claude 인증

각 사용자가 로컬에서 `claude setup-token`(Max, long-lived)으로 토큰을 발급 →
온보딩 폼에 붙여넣기 → worker가 `CLAUDE_CODE_OAUTH_TOKEN` env로 사용한다.
브라우저 방식(`claude auth login`, `auth_login.py`)은 **폴백**으로 유지한다.

## 아키텍처

```text
                        ┌─────────────────────── central (상시) ───────────────────────┐
Jira(jira.project)          │                                                              │
  │ (1) 신규 할당 감지   │   poller ── high-watermark JQL 폴링                           │
  ├── poller ───────────┼──▶ gate  ── 단일 원자적 dedup claim                           │
  └── webhook(옵션) ─────┤    │                                                         │
                        │    ▼ (2) 담당자 account_id → registry 매핑(enabled만)         │
                        │  registry ── 등록 사용자 CRUD(state/registry.json)            │
                        │    │                                                         │
                        │    ▼ (3) dispatch.enqueue(user, job) → scheduler              │
                        │  central_session ── 상주 라이브 세션(사용자별 서브 스폰)      │
                        │  spawner ── 온보딩 시 worker 컨테이너 동적 spawn(Docker SDK)  │
                        └────┼─────────────────────────────────────────────────────────┘
                             │ (4) docker exec — 중앙이 워커 안으로 **밀어 넣는다**
              ┌──────────────┼──────────────┐  ...사용자마다 1개
        ┌─────▼─────┐  ┌─────▼─────┐   worker (동적, ROLE=worker DISPATCH_USER=<u>)
        │ worker A  │  │ worker B  │   ── 상주만 한다(/healthz) → 주입된 claude -p 실행
        └───────────┘  └───────────┘      토큰 한도 감지 → interrupted(reset_at) → 재개
```

- **영속(state/)**: jobs·watermark·dedup·registry를 JSON으로 영속(central만).
  worker는 무상태 실행체다.
- **디스패치 seam 은 하나뿐**: 중앙 → `docker exec`(프랙탈 푸시). 워커가 중앙을 폴링하던
  옛 HTTP 경로(`app/worker.py::worker_loop` + `GET /dispatch/<user>/next` 계열)는 프랙탈
  경로와 **이중 실행**(같은 티켓 두 번 → 중복 MR·브랜치·Jira 코멘트)을 일으켜 삭제됐다.
- **웹훅-레디**: 폴러가 기본. 웹훅은 켜면 동일 게이트/매핑으로 수렴(중복 안전).
- **재개**: 중앙이 interrupted 잡을 reset_at에 사용자 큐로 재-enqueue → 그 worker가
  `claude -p --resume`로 이어간다(야간 드레인 / UI "지금 재개" 동일 경로).

## HTTP 표면

| 메서드 | 경로 | 방향 | 내용 |
|---|---|---|---|
| GET | `/healthz` | 프로브 | 역할/사용자 헬스 (워커 컨테이너의 **유일한** 서빙 표면) |
| POST | `/webhook/jira` | Jira→central | 이벤트 구동 단일 티켓 트리거(헤더 토큰 인증, 폴링은 백스톱) |
| POST | `/onboard` | UI→central | 사용자 등록 + worker spawn |

> ⚠️ **은퇴**: `GET /dispatch/<user>/next` · `POST /dispatch/<user>/<job>/status` ·
> `GET /dispatch/<user>/<job>/control` 과 그 `X-Worker-Secret` 인증은 레거시 워커 폴링
> 프로토콜이었고, 이중 실행의 원인이라 소비자(`app/worker.py`)와 함께 제거됐다. 그래서
> `WORKER_SHARED_SECRET` 과 `CENTRAL_URL`(`spawn.central_url`)도 함께 은퇴했다 — 옛
> `.env`·`config.yaml` 에 남아 있어도 조용히 무시된다.

## 실행

```bash
pip install -r requirements.txt

# 설정 준비(시크릿 값은 넣지 말 것 — 파일 참조만)
python -m app.setup wizard            # 대화로 채우기(권장)
# 또는 손으로: cp config/config.example.yaml config/config.yaml

# central (관리 콘솔 + 감시/디스패치)
ROLE=central python -m app.main       # http://127.0.0.1:8787

# worker (보통 central이 동적 spawn; 수동 기동 시)
# ⚠️ 워커는 중앙 주소를 알 필요가 없다 — 중앙이 docker exec 로 밀어 넣는다.
ROLE=worker DISPATCH_USER=<username> \
  CLAUDE_CODE_OAUTH_TOKEN=... python -m app.main
```

> 위는 개발용 직접 실행이다. **실제 설치는 docker compose 기준**이며 절차는
> [INSTALL.md](INSTALL.md) 를 따른다(로컬 / 클라우드 VM / 온프렘 서버 3갈래).

## ⚠️ 리스크 (설계상 중요)

> 🔒 **보안 태세의 정본은 [SECURITY.md](SECURITY.md).** worker는 사람 승인 없이 bypass
> 사전 인가로 도구 권한 에이전트를 헤드리스 실행한다 — 그 구조적 근거와 보완 통제
> (신뢰 네트워크 한정·MR 게이트·가역성·격리·비-root·시크릿 ro·`permission_level` 조임)를
> SECURITY.md 가 선언문 형태로 기술한다.

- **docker.sock 특권**: central이 worker를 spawn하려면 docker.sock에 접근한다 —
  사실상 호스트 root 권한과 동치인 특권 상승 표면이다. 완화책: docker-socket-proxy를
  앞단에 두고 CONTAINERS/POST만 최소 허용(`deploy.docker_host=tcp://socket-proxy:2375`),
  central 비-root 실행, 신뢰 네트워크 한정. (compose에 프록시 골격 주석 포함)
- **RCE 표면**: worker는 `claude -p ... --dangerously-skip-permissions`로 도구 권한을
  가진 자율 에이전트를 실행한다. 승인할 사람이 없는 자율 실행 전제이므로 권한을
  죽일 수 없다 → **신뢰 네트워크 한정**, 인터넷 노출 금지. 폭주 방지는
  central **서버 자원 어드미션**(메모리+부하로 dispatch 조절) + worker 안전 상한
  (`worker_max_concurrency`, 기본 64) + dedup + 결정적 브랜치(`auto/<TICKET>`) 가역성으로 한다.
- **서버 용량**: 사용자마다 상시 worker 컨테이너 1개(mem_limit 기본 4g)가 뜬다.
  사용자 수 × 리소스가 호스트 용량을 압박할 수 있다(스케일 상한/유휴 정지 정책 필요).
- **per-user Max**: 각 사용자가 개별 Claude Max 구독(setup-token)을 붙여야 한다.
  토큰 한도(롤링)는 사용자별로 따로 걸리며, worker가 감지해 reset_at까지 재개를 미룬다.

## 연동 레포 (런타임 clone, gitignore됨)

central·모든 worker 가 **하나의 공유 워크스페이스 볼륨**(`run.workspace_dir`, 기본
`/app/workspace`)을 마운트하고 그 아래 **한 벌만** 클론한다. 아래 경로를 config 에서
비워 두면 `workspace_dir` 하위로 자동 파생된다.

| config 키 | 역할 | 파생 경로 | 필수 |
|---|---|---|---|
| `run.orchestrator_repo` (+`_url`) | 오케스트레이터 프레임워크(`claude`가 이 정체성으로 동작) | `<workspace_dir>/orchestrator` | **필수** |
| `run.dlc_meta_repo` (+`_url`) | 거버넌스·에이전트 오버레이·런 저널 + `REPO-MAP.md` | `<workspace_dir>/dlc-meta` | **필수** |
| `run.docs_repo` (+`_url`) | 설계 문서 저장소 | `<workspace_dir>/docs` | 선택 — URL 이 비면 조용히 skip |

> `docs_repo` 의 옛 이름은 `dataspace_docs_repo` 였다(특정 프로젝트의 레포 이름).
> 레거시 키는 계속 읽으므로 기존 `config.yaml` 은 그대로 둬도 동작한다.

## 설정

`config/config.example.yaml`이 스키마 정본이다(시스템 수준 설정, 시크릿 없음).
`config/config.yaml`(gitignore됨)로 복사해 채운다. 사용자별 트리거 목록은 config가
아니라 **레지스트리**(`state/registry.json`, 온보딩 UI로 채움)가 소유한다 —
스키마 예시는 `config/registry.example.json`. 시크릿(Jira/forge/Claude 토큰,
웹훅 시크릿)은 값이 아니라 **파일 참조**(`secrets.base_dir` 상대 경로)로만 다룬다.

배포 형태(로컬 / 클라우드 VM / 온프렘)에 따라 달라지는 값은 `deploy.profile` 하나로
파생된다 — 무엇이 달라지는지는 [INSTALL.md](INSTALL.md) 의 프로파일 표를 본다.

### 알림(선택)

기본은 **끔**(`notifier.provider: none`) — 알림 없이도 시스템은 완전히 동작한다.
지원 provider 는 `none | google_chat | slack | generic_webhook` 이고, **provider 별 웹훅
URL 획득 절차·페이로드 모양·멘션 문법은 `config/config.example.yaml` 의 `notifier:` 절
주석이 단일 원천**이다(여기에 복제하지 않는다 — 복제하면 갈라진다). 웹훅 URL 은 값이
아니라 파일 참조(`notifier.webhook_ref`)로만 둔다. 담당자 멘션 id 는 config 가 아니라
레지스트리의 사용자별 `notify_user_id` 다.

## 문서 맵

| 문서 | 언제 |
|---|---|
| [INSTALL.md](INSTALL.md) | **설치 — 여기서 시작.** 로컬 / 클라우드 VM / 온프렘 3갈래 |
| [DEPLOY.md](DEPLOY.md) | 온프렘 서버 배포 상세 런북(INSTALL 갈래 C 의 본문) |
| [SECURITY.md](SECURITY.md) | 무엇에 동의하는가 + 보안 태세·보완 통제 |
| [E2E.md](E2E.md) | 배포 후 실 티켓으로 전 플로우 검증하는 런북 |
| [docs/DISPATCHER-DEV.md](docs/DISPATCHER-DEV.md) | 이 디스패처 자체를 개발·수정할 때의 온보딩 |
| [docs/DESIGN-*.md](docs/) | 설계 기록(히스토리) |
