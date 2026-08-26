# E2E.md — jira-auto-dispatcher 배포 후 E2E 검증 시나리오

> **목적.** central이 서버에 배포된 뒤, 운영자/사용자가 **직접 온보딩**하고 **실 Jira 티켓**으로
> 전(全) 플로우를 검증한다. 설치는 [INSTALL.md](INSTALL.md), 온프렘 배포 상세는
> [DEPLOY.md](DEPLOY.md), "실행 = 풀 퍼미션 동의" 선언과 보안 태세는 [SECURITY.md](SECURITY.md).
>
> 이 문서는 **검증 런북**이다 — 각 단계에 **조작 → 기대 결과 → 실패 시 확인 로그**를 짝지어 둔다.
> 라이브 조작은 사람이 수행한다(자동 실행 금지). 스모크/관측 보조 스크립트는
> [`scripts/smoke-deployed.sh`](scripts/smoke-deployed.sh)·[`scripts/observe-job.sh`](scripts/observe-job.sh).
>
> ### 본문의 `§n` 표기에 대하여 (외부 참조)
>
> 아래 본문에 나오는 `§4`·`§5`·`§10` 같은 절 번호는 원 운영자가 자기 **사설 `dlc-meta`
> 레포**에 두고 쓰던 동작 계약 문서(`RECURSIVE-DISPATCH.md`)의 절 번호다. **그 문서는 이
> 저장소에 포함되지 않는다** — 배포마다 dlc-meta 레포가 다르기 때문이다. 외부 독자는 이
> 표기를 *해석할 필요 없는 각주*로 넘기고, 같은 계약을 이 저장소 안에서 확인하려면:
>
> | 알고 싶은 것 | 이 저장소의 위치 |
> |---|---|
> | 2-역할(central/worker) 구조·HTTP 디스패치 프로토콜 | [docs/DISPATCHER-DEV.md](docs/DISPATCHER-DEV.md) |
> | 레포락 스케줄러·자원 어드미션(옛 §4) | `app/scheduler.py` 모듈 docstring |
> | 취소/재오픈 전이(옛 §10) | `app/queue.py`·`app/status_watcher.py` docstring |
> | 산출 게이트(브랜치/MR 초안까지, 자동머지 없음 — 옛 §5) | [SECURITY.md](SECURITY.md) §3.1 |
> | 실행 코어 설계 배경 | [docs/DESIGN-*.md](docs/) (히스토리) |

---

## 0. 준비 — 접속·헬스·UI 확인

### 0.1 사전 조건

- INSTALL.md(또는 온프렘이면 DEPLOY.md 1~5단계) 완료 — 이미지 빌드 → `config.yaml` →
  시크릿(`service/jira-token`, `.env` 의 `WORKER_SHARED_SECRET`) → `docker compose up -d`.
- 서버 SSH 접근(`<deploy-user>@<서버>`). 로컬(갈래 A)이면 SSH 없이 그냥 로컬 셸이다.
- 관리 UI는 **신뢰 네트워크 한정**이므로 SSH 터널로 연다:

  ```bash
  ssh -L 8787:localhost:8787 <deploy-user>@<서버>   # 로컬 http://localhost:8787 → 서버 central
  ```

- **본인 노트북**에서 `claude setup-token`(Max, long-lived) 발급값을 미리 준비(온보딩 입력용).
- 자기 **Jira accountId**(assignee 매핑 키), Jira API 토큰, (선택) forge PAT(GitLab/GitHub) 준비.

### 0.2 조작 — 헬스·UI

```bash
# (터널 연결 상태에서 로컬 셸)
curl -fsS http://localhost:8787/healthz          # 헬스
# 스모크 스크립트로 한 번에:
bash scripts/smoke-deployed.sh http://localhost:8787
```

브라우저로 `http://localhost:8787` 접속 → 관리 콘솔(온보딩 폼 / 등록 사용자 / 잡 현황) 렌더 확인.

### 0.3 기대 결과

- `GET /healthz` → HTTP 200, 본문 `{"status":"ok","role":"central"}`.
- `GET /api/users` → HTTP 200, JSON 배열(초기엔 `[]`).
- `GET /api/jobs` → HTTP 200, JSON 배열(초기엔 `[]`).
- `docker ps`(서버)에서 `jad-central`·`jad-socket-proxy`가 **Up**. (worker는 아직 없음.)
- UI 3개 섹션(온보딩·등록 사용자·잡 현황) 정상 렌더.

### 0.4 실패 시 확인 로그

```bash
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose ps'
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central'
# 폴러/워처/스케줄러 스레드 기동 여부(부팅 로그):
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs central | grep -iE "poll|watcher|scheduler|bind|8787"'
```

- `/healthz`가 안 뜨면 → central 컨테이너 미기동/포트 매핑(8787) 확인, 터널 재확인.
- `/api/users`가 500이면 → `config/config.yaml` 로드 실패(마운트 `./config:/app/config:ro`)·
  `secrets.base_dir` 미설정 여부 확인.

---

## 1. 온보딩 (사용자 직접) → worker 자동 spawn

> ⚠️ 실제 합류는 **2단**이다 — 여기(웹 등록) 말고 로컬에서 `ai-dlc-orchestrator` 의
> SETTER 를 합류 모드로 돌려 `dlc-meta` 를 clone 하는 단계가 앞에 있다
> ([INSTALL.md §8](INSTALL.md)). 이 문서는 **배포 플로우 검증**이 목적이라 웹 쪽만 다룬다.

### 1.1 조작 — 자격증명 입력 → 등록

UI 온보딩 폼(또는 `POST /onboard`)에 입력한다. 필드 목록의 **정본은 스키마 선언**
(`app/user_schema.py`)이고, UI 는 그것을 이 인스턴스 설정과 함께 렌더한다
(`GET /api/onboarding/guide` — forge 종류에 맞는 PAT 안내만 보인다).

**필수**: `username`, `jira_account_id`, `jira_email`, `jira_token`, `forge_token`,
`claude_setup_token`, `consent_full_permissions`.
**선택**: `git_name`/`git_email`(커밋 author 귀속), `autonomy_mode`, `permission_level`,
`scope`, `notify_user_id`.

- `forge_token` 은 **선택이 아니다**(브랜치 push·MR/PR 생성용 — GitLab/GitHub PAT,
  `forge.kind` 에 맞춰. 옛 이름 `gitlab_token` 도 계속 받는다). 없으면 워커가 커밋만 하고
  변경요청을 못 만드는 조용한 반쪽 동작이 된다.
- `consent_full_permissions` 는 **합류자 본인**의 풀 퍼미션 동의다. 서버가 강제하며
  (400) 동의 시각은 서버 수신 시각으로 레지스트리에 남는다.
- `jira_account_id` 를 모르면 UI 의 `내 accountId 조회` 버튼(= `POST /api/onboarding/whoami`)
  이 이메일+토큰으로 `GET /rest/api/3/myself` 를 대신 호출해 채워 준다.

- **autonomy_mode = B 권장(최초)** — 경량 1차(트리아지+브랜치+스캐폴딩+1차 시도+`runs` 저널).
  MR을 강행하지 않아 최초 검증에서 부작용이 작다(§5). A는 컨벤션이 성숙한 FE에서 나중에.
- **permission_level = bypass**(현재 유일 구현; SECURITY.md §5).
- 폼 제출(`POST /onboard`) 뒤 사용자는 **enabled=false(안전 기본)**로 등록된다(§SECURITY 3).

CLI로 확인만 할 때(값 노출 주의 — 실제 토큰은 UI로 입력 권장):

```bash
curl -sS -X POST http://localhost:8787/onboard \
  -H 'Content-Type: application/json' \
  -d '{"username":"<username>","jira_account_id":"<JIRA_ACCOUNT_ID>","jira_email":"you@example.com",
       "jira_token":"<JIRA_API_TOKEN>","claude_setup_token":"<SETUP_TOKEN>",
       "forge_token":"<FORGE_PAT>","git_name":"<git-name>","git_email":"you@example.com",
       "autonomy_mode":"B","scope":"<PROJECT_KEY>","consent_full_permissions":true}'
# 기대: HTTP 201 {"status":"ok","username":"<username>","enabled":false,
#                 "consent_accepted_at":"<서버 수신 시각 ISO-8601>"}
# 검증 실패 시: HTTP 400 {"error":...,"missing":[...],"findings":[{"key":"<필드>",...}]}
```

### 1.2 조작 — 활성화(enable) → worker spawn

UI 등록 사용자 표에서 해당 사용자 **enable** 버튼(또는 `POST /users/<username>/enable`).
central이 per-user 볼륨 `jad-<username>` 보장 + 사전 인가 `settings.json` 기록 +
`jad-worker-<username>` 컨테이너를 같은 image·`jad-net`·비-root(1000:1000)로 spawn 한다(DEPLOY §6).

```bash
curl -sS -X POST http://localhost:8787/users/<username>/enable
# 기대: {"status":"enabled","container":"running"}
```

### 1.3 기대 결과

- `POST /onboard` → 201, 응답에 **토큰 값 없음**(참조만 저장). 시크릿은 서버
  `secrets/<username>/`에 0600으로 기록(jira-token·forge-token·claude-oauth-token).
- `GET /api/users`에 사용자 1건, 최초 `enabled=false` → enable 후 `enabled=true`.
- enable → `{"status":"enabled","container":"running"}`.
- 서버 `docker ps`에 **`jad-worker-<username>` Up**:

  ```bash
  ssh <deploy-user>@<서버> 'docker ps --filter name=jad-worker-'
  ```
- worker 로그에 폴링 루프 기동(및 claude 준비):

  ```bash
  ssh <deploy-user>@<서버> 'docker logs jad-worker-<username> --tail=50'
  ```
- worker 내부 헬스:

  ```bash
  ssh <deploy-user>@<서버> 'docker exec jad-worker-<username> curl -fsS http://localhost:8787/healthz'
  # {"status":"ok","role":"worker","user":"<username>"}
  ```

### 1.4 실패 시 확인 로그

- enable이 **502 `spawn_error`** → central이 docker(프록시)로 컨테이너를 못 띄운 것:
  ```bash
  ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -i spawn'
  # socket-proxy 정합 확인(config spawn.docker_host=tcp://socket-proxy:2375, DEPLOY §2·§5):
  ssh <deploy-user>@<서버> 'docker logs jad-socket-proxy --tail=50'
  ```
- worker가 떴다 바로 죽으면(Restarting/Exited) → claude 인증 실패 가능(setup-token 만료/오타):
  ```bash
  ssh <deploy-user>@<서버> 'docker logs jad-worker-<username> --tail=100'
  ```
  토큰 재발급 후 같은 username으로 재온보딩 → `container/stop`→`start`로 재기동(DEPLOY §6).
- 온보딩이 **400 필수필드 누락** → 응답 `missing` 배열 확인. **409** → 이미 등록된 username
  (다른 이름으로 재시도하거나 기존 레코드 사용).

---

## 2. 정상 플로우 — 티켓 생성 → 감지 → 디스패치 → 실행 → 완료

> 전진축(§2·§4): central 폴러가 신규 티켓을 감지 → dedup claim → 담당자 매핑 →
> 스케줄러가 레포락 판단 후 dispatch → worker가 `GET /dispatch/<user>/next`로 수령 →
> `claude -p`(오케스트레이터) 자율 실행 → 채널 F(`POST .../status`)로 회신.

### 2.1 조작 — 테스트 티켓 생성(사용자 직접)

감시 대상 Jira 프로젝트(`config.jira.project`)에서 **자기 자신이 담당자(assignee)** 인
테스트 티켓을 만든다.

- 상태 = `해야 할 일`(match.statuses). assignee accountId = 온보딩한 `jira_account_id`와 일치.
- 요약/설명은 **작고 안전한 스코프**로(예: 특정 레포 README 한 줄 추가). 실제 코드에 영향이
  가역적인 소규모 변경 권장.
- ⚠️ **트리거=작업 티켓 불변식**(§6) — worker는 이 티켓에 바인딩하고 새 사이클 티켓을 만들지 않는다.

### 2.2 관측 지점(단계별)

| 단계 | 관측 지점 | 무엇을 본다 |
|---|---|---|
| 감지·claim | `docker compose logs central | grep -i poll` | 폴링 주기(기본 60s)·티켓 claim 흔적 |
| 매핑 | central 로그 | assignee accountId → 등록 사용자 매핑(enabled만) |
| enqueue·dispatch | `GET /api/jobs` | 잡이 `queued` → `running`(레포락 획득) |
| 수령 | `docker logs jad-worker-<user>` | `GET /dispatch/<user>/next` 200 수신 + `claude -p` 실행 |
| Jira 착수 전이 | Jira 티켓 | `해야 할 일` → `진행 중` (worker 안 오케스트레이터가 사용자 토큰으로 전이) |
| 산출 | forge | 브랜치 `auto/<TICKET>` 생성(커밋 author = 사용자 identity) |
| MR/PR(A모드만) | forge | MR/PR 초안 생성(사용자 forge 토큰). **B모드는 MR 없음**(1차 산출+저널) |
| 완료 전이 | Jira 티켓 | (성공 시) `진행 중` → `완료` |
| 회신 | `GET /api/jobs` | 잡 `status=done`, `mr_url`(A) 채워짐 |

관측 보조:

```bash
bash scripts/observe-job.sh <TICKET> <user> http://localhost:8787   # /api/jobs 폴링 추적
```

### 2.3 기대 결과

- `/api/jobs`에서 해당 티켓 잡의 상태 전이: `queued` → `running` → `done`.
- Jira 티켓: `해야 할 일` → `진행 중` → (성공 시) `완료`.
  - ⚠️ **착수(진행 중) 전이 시 필수필드**: `duedate`, `customfield_10015`(시작 날짜).
    **완료 전이 전 필수필드**: `customfield_10186`, `customfield_10187`. 이 채움/전이는
    worker 안 오케스트레이터가 ISSUE-TRACKER-ADAPTER 규율로 수행한다(누락 시 전이 실패 → 2.4).
- forge(GitLab/GitHub): `auto/<TICKET>` 브랜치 존재. 커밋 author = 사용자 이름/이메일
  (README "per-user attribution").
- autonomy=A면 MR 초안 + `/api/jobs`의 `mr_url` 채워짐. **자동 머지는 없다**(사람 리뷰 게이트).
- autonomy=B면 MR 없이 1차 산출 + `runs/<TICKET>/` 저널.

### 2.4 실패 시 확인 로그

- 티켓을 만들었는데 **잡이 안 생김**(`/api/jobs` 비어 있음):
  ```bash
  ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -iE "poll|claim|map|assignee"'
  ```
  - assignee accountId 불일치 / 사용자 `enabled=false` / 상태가 `해야 할 일`이 아님 /
    프로젝트가 `config.jira.project` 와 다름 중 하나. 폴링 주기(60s) 대기했는지 확인.
- 잡이 `running`인데 worker가 안 받음:
  ```bash
  ssh <deploy-user>@<서버> 'docker logs jad-worker-<user> --tail=100'
  ```
  - `X-Worker-Secret` 불일치(401) → central `.env`와 worker env의 `WORKER_SHARED_SECRET` 정합 확인.
  - claude 인증 실패 → setup-token 재발급/재온보딩(1.4).
- Jira 전이 실패(진행 중/완료로 안 넘어감):
  - **필수필드 누락**이 가장 흔함(duedate·10015 착수 / 10186·10187 완료). worker 로그의
    실행 요약(`log_summary`)·`GET /api/jobs`의 해당 잡 로그 확인.
- 잡이 `failed`로 회신:
  ```bash
  ssh <deploy-user>@<서버> 'docker logs jad-worker-<user> --tail=200'
  ```
  `/api/jobs`의 `log_summary`(시크릿 마스킹됨)에서 원인 파악.

---

## 3. 한도/재개 (관측만)

> 토큰 한도(Max 롤링)는 사용자별로 걸린다. worker가 감지해 `interrupted`+`reset_at`으로
> 회신하고, reset 시각에 `--resume`으로 재개한다(DEPLOY §6 주의·worker.py). **만료(재인증 필요)
> 와 다르다** — 재발급 불필요.

### 3.1 조작

- 별도 조작 없이 **관측만** 한다(한도는 자연 발생). 강제 재현은 하지 않는다.
- 재개를 수동으로 앞당기려면 UI 잡 현황의 **재개** 버튼 = `POST /api/resume`(reset_at 도래분
  즉시 재적격 tick). central 스케줄러도 주기 tick(기본 30s)으로 자동 재적격.

### 3.2 기대 결과

- 한도 시 `/api/jobs`에서 잡 `status=interrupted` + `reset_at`(ISO8601) 채워짐.
- UI 잡 현황에 해당 행 **재개** 버튼 노출(interrupted일 때만).
- reset 시각(+`resume.reset_buffer_sec`=120s) 이후 worker가 `--resume`으로 이어서 실행 →
  잡이 다시 `running` → 최종 `done`.

### 3.3 실패 시 확인 로그

```bash
ssh <deploy-user>@<서버> 'docker logs jad-worker-<user> --tail=100 | grep -iE "limit|resume|reset"'
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=50 central | grep -i tick'
bash scripts/observe-job.sh <TICKET> <user>    # reset_at·status 추적
```

- reset 이후에도 재개 안 됨 → `POST /api/resume` 수동 tick 시도. 그래도면 worker 생존 여부
  (`docker ps`)와 claude 인증 유효성 확인.

---

## 4. 취소/롤백 & 재오픈 (§10)

> 신호 = **Jira 상태**. `취소됨`으로 전이 = 중단 요청(롤백 O). `완료`(정상)와 statusCategory가
> 같으므로(둘 다 done) **상태 이름**으로만 구분한다(§10.1). 상태 감시축(`status_watcher`)이
> 전진축과 별개 루프로 감지한다(§10.2).

### 4.1 조작 — 취소

2단계에서 **`running`(또는 `queued`)** 상태인 티켓을 Jira에서 **`취소됨`으로 전이**한다.

- 실행 중 잡을 겨냥하려면 잡이 `running`인 동안(위 2.2 관측) 전이한다.

### 4.2 기대 결과 (§10.3·§10.4)

- **큐 대기(queued/interrupted)였던 잡** → 즉시 `cancelled`(드롭) + 레포락 해제 + dedup 해제.
  롤백 대상 없음(아직 산출 없음).
- **실행 중(running)이던 잡** → central이 worker에 **취소 플래그** 세팅(제어 채널
  `GET /dispatch/<user>/<job>/control`이 `{"cancel":true}`) → worker의 claude subprocess **abort**
  → worker **롤백**(best-effort): 로컬/원격 `auto/<TICKET>` 브랜치 삭제 + MR 있으면 close →
  `cancelled` 회신 → central이 레포락 해제 + dedup 해제로 확정.
- `/api/jobs`에서 잡 `status=cancelling`(잠시) → `cancelled`. `audit_refs`에
  `branch_deleted`/`mr_closed` 흔적.

### 4.3 관측 지점

| 관측 | 지점 |
|---|---|
| 취소 감지 | `docker compose logs central | grep -i "취소\|cancel"`(status_watcher JQL `status="취소됨"`) |
| worker abort | `docker logs jad-worker-<user> | grep -iE "cancel|abort|terminate|rollback"` |
| 롤백 결과 | forge에서 `auto/<TICKET>` 브랜치 삭제·MR/PR closed 확인 |
| 락/dedup 해제 | `/api/jobs` 잡 `cancelled` + 같은 레포 대기 잡이 있으면 dispatch 진행 |

```bash
bash scripts/observe-job.sh <TICKET> <user>    # cancelling → cancelled 추적
```

### 4.4 조작 — 재오픈 (취소됨 → 해야 할 일)

취소 확정된 그 티켓을 Jira에서 다시 **`해야 할 일`로 전이**한다.

- 기대: 상태 감시축의 재오픈 감지가 **재-claim + 재-enqueue**(같은 티켓, 새 실행으로 초기화 —
  session_id/reset_at/mr_url/cancel_requested 비움) → 잡 `queued` → `running`으로 재실행(§10.4).
- **트리거=작업 티켓 불변식 유지** — 새 티켓을 만들지 않고 같은 티켓에 재바인딩(§6).

관측:

```bash
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -iE "reopen|재오픈|enqueue"'
bash scripts/observe-job.sh <TICKET> <user>    # cancelled → queued → running
```

### 4.5 실패 시 확인 로그

- 취소가 안 잡힘: status_watcher가 도는지 + JQL 대상인지 확인.
  ```bash
  ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs central | grep -i status_watcher'
  ```
  - 잡이 이미 종결(done/failed)이거나 추적 대상이 아니면 취소는 멱등 스킵된다(정상).
- 롤백 안 됨(브랜치/MR 잔존): worker 로그의 롤백 요약 확인. 브랜치/MR이 아직 없으면 스킵이
  정상(best-effort). forge 토큰 유효성(close 권한) 확인.
- 재오픈 무반응: 재오픈은 **취소 확정(cancelled) 잡의 티켓**만 대상. 잡이 cancelled인지
  (`/api/jobs`) + assignee가 여전히 enabled 사용자인지 확인. reopen 워터마크(updated) 이후
  전이인지 확인.

---

## 4B. 담당자 변경 = 핸드오프(체크포인트, 롤백X) + 이관

> **재배정 ≠ 취소.** 취소(`취소됨`)는 abort + **롤백**(WIP 폐기, §4). 담당자 변경은
> abort가 아니라 **핸드오프** — 실행 중 잡의 WIP를 **checkpoint(커밋·push로 보존)** 한 뒤
> 소유권만 X→Y로 넘긴다(롤백 없음). 두 플로우는 신호·상태·산출이 전부 다르다.

### 4B.1 조작 — 담당자 변경

2단계에서 X가 처리 중인 티켓의 **담당자(assignee)를 다른 enabled 사용자 Y로 변경**한다.
(웹훅이 켜져 있으면 즉시, 아니면 폴러 백스톱이 감지. ⚠️ 실행 중 티켓은 `진행 중`이라
match.statuses 밖이지만 **웹훅 경로는 상태 게이트와 무관하게 재배정을 감지**한다.)

### 4B.2 기대 결과

- **큐 대기(queued/interrupted, WIP 없음)** → X 슬롯을 **Y로 재-소유**하고 즉시 재-dispatch
  (Y 미가용이면 드롭 + park). 롤백/체크포인트 없음(아직 산출 없음).
- **실행 중(running, WIP 존재)** → central이 worker에 **핸드오프 플래그**(제어 채널
  `GET /dispatch/<user>/<job>/control` 이 `{"cancel":false,"action":"handoff"}`) → worker의
  claude subprocess abort → worker **checkpoint**(롤백 아님): `git add -A` + commit(비면 스킵)
  + `git push <branch>` + 이관 저널 노트 → `handed_off` 회신 → central이 **롤백 없이** 레포락
  해제 후, 같은 티켓/브랜치로 **Y에게 continue 잡을 dispatch**(continue_from_wip 힌트로
  "이전 담당자 WIP 리뷰 후 이어서 완성"). dedup은 이관이므로 유지.
- **Y가 enabled 사용자가 아님** → X는 **checkpoint로 WIP 보존**하되 dispatch하지 않고
  **park**(브랜치 그대로) — 이후 Y enable/재배정 시 재트리거.

### 4B.3 관측 지점

| 관측 | 지점 |
|---|---|
| 재배정 감지 | `docker compose logs central | grep -i "담당자 변경\|handoff\|handed_off"` |
| worker checkpoint | `docker logs jad-worker-<X> | grep -iE "handoff|checkpoint|handed_off"` |
| WIP 보존 | forge에서 `auto/<TICKET>` 브랜치가 **삭제되지 않고** 커밋이 push됐는지(롤백 아님) |
| 이관 dispatch | `/api/jobs`에서 잡 `handing_off` → (Y로) `running`, `continue_from_wip=true` |

- **잡 상태 흐름**: 실행 중 `running` → `handing_off`(checkpoint 회신 대기, 레포락 유지)
  → Y로 `queued`→`running`(이관) 또는 `handed_off`(park, 종결).

### 4B.4 실패 시 확인

- 재배정이 안 잡힘: 웹훅이 켜졌는지 + Y가 X와 **다른** 사용자로 매핑되는지(같은 담당자면
  중복 트리거로 흡수). 폴러 경로는 티켓이 match.statuses 안일 때만(큐 대기 등) 감지한다.
- 롤백이 일어남(브랜치 삭제): 이는 **취소**(`취소됨`) 플로우다. 담당자 변경은 롤백하지
  않는다 — 티켓이 `취소됨`으로 전이되지 않았는지 확인(재배정≠취소).
- 이관 후 Y가 새 브랜치를 만듦: continue_from_wip 힌트/브랜치 유지 프롬프트 확인.

---

## 4C. 자원 인지형 어드미션 (고정 잡 수 cap 대체)

> central은 dispatch 여부를 **잡 수**가 아니라 **호스트 서버의 실제 자원 상태**
> (가용 메모리 + CPU 부하)로 판단한다. 여유가 있으면 준비된 잡을 dispatch, 압박이면
> 큐에 대기. 자원이 넉넉하면 **한 사용자가 (서로 다른 레포에서) 여러 잡을 동시에** 굴린다
> (예: Y가 metapage-frontend + marketplace-frontend를 한 번에). **per-user 잡 수 cap도,
> 전역 잡 수 cap도 없다** — 스로틀은 오직 자원이다. 전에는 고정 카운트 cap
> (`concurrency_per_worker`/`global_concurrency`)이었고, 그 게이팅을 이 모델이 대체한다.

### 4C.1 어드미션 규칙 (정확히 무엇을 보나)

- **메모리(PRIMARY, 즉각 신호)** — `/proc/meminfo` 의 `MemAvailable`(호스트 값. 컨테이너
  안에서도 네임스페이스가 아닌 **호스트** 메모리를 보고한다 = 서버 상태). in-flight 잡의
  예약을 빼서 유효 가용치를 만든다:
  `effective_available_mb = MemAvailable_mb − active_job_count × per_job_mem_reserve_mb`
  → `effective_available_mb ≥ min_free_mem_mb` 여야 admit.
- **부하(SECONDARY, 거친 천장)** — `/proc/loadavg` 1분 부하 ÷ `os.cpu_count()`:
  `loadavg_1min / max(1, ncpu) < max_load_per_core` 여야 admit.
- **한 tick 내 버스트 비-과다커밋** — 한 tick에서 준비된 잡을 하나씩 admit하되, admit마다
  in-flight 수(=예약)를 다시 반영해 헤드룸을 재계산한다. loadavg는 1/5/15분 지연 평균이라
  부하가 오르기 전 버스트를 과다 admit하기 쉬운데, **메모리 예약**이 이를 선제 차단한다.
  → 메모리는 즉각·PRIMARY, loadavg-per-core는 2차 거친 상한.
- **정확성은 별개** — 전역 **레포락**(같은 레포 = 전 사용자 직렬)은 그대로다. 이는 잡 수
  cap이 아니라 **공유 워크스페이스 충돌 방지**다. dedup 게이트·`next_for_user(exclude=)`도 유지.
- **토큰/레이트는 central 관심사 아님** — 각 per-user worker 컨테이너의 오케스트레이터가
  자기 계정의 토큰/레이트를 스스로 관리한다. central은 이를 모델링하지 않는다.
- **worker**: central이 dispatch한 잡을 **모두** 동시에 스레드 풀로 실행한다(진짜 스로틀은
  위 자원 어드미션). worker는 runaway 방지용 **안전 상한**(`worker_max_concurrency`, 기본 64
  — 정책 cap 아님)만 둔다. 각 잡은 기존 `_process_job` 경로 그대로(자기 상태회신·제어/취소/
  핸드오프·재개·completed 캐시). 잡이 하나뿐일 때의 동작은 단일 잡 경로와 동치다.

### 4C.2 조작 — 서로 다른 레포 티켓 여러 개를 한 사용자에게

```bash
# 사용자 Y에게 서로 다른 레포로 매핑되는 티켓들을 '해야 할 일'로 만든다(또는 웹훅 트리거).
#   예: <TICKET-A> → metapage-frontend, <TICKET-B> → marketplace-frontend, ...
# central이 서버 자원이 허용하는 만큼 running으로 dispatch하는지 관측한다.
curl -s "$CENTRAL/api/jobs" | jq '.[] | {ticket, user, status, target_repos}'

# 서버 자원 상태(어드미션 입력)를 눈으로 확인(central 컨테이너/호스트에서):
grep MemAvailable /proc/meminfo ; cat /proc/loadavg ; nproc
```

### 4C.3 기대 결과

- **자원 여유** 시: Y의 여러 잡이 **동시에 `running`** (서로 다른 `target_repos`) — 개수 상한
  없음(한 사용자여도). worker 로그: 잡들이 나란히 진행(각자 `진행중` → `완료`). 스레드명 `jad-job-<TICKET>`.
- **메모리 압박**(유효 가용 < `min_free_mem_mb`) 또는 **부하 압박**(load/core ≥ `max_load_per_core`)
  시: 준비된 잡이 `queued`로 대기. central 로그에 `queue: mem pressure ...` / `queue: load pressure ...`.
- **in-flight 예약**: 예) 프로브 4GB·예약 1GB/잡·하한 1.5GB면 3개 admit 후 4번째는 `queued`
  (`4096 − 3×1024 = 1024 < 1536`). 한 잡이 끝나 예약이 회수되면 다음 tick에 대기분이 admit.
- **같은 레포** 두 잡(같은 사용자여도)은 자원과 무관하게 여전히 **직렬**(전역 레포락) —
  하나 `running`, 하나 `queued`.

### 4C.4 관측 지점

| 단계 | 확인 | 기대 |
|---|---|---|
| 서버 자원 | `grep MemAvailable /proc/meminfo` · `cat /proc/loadavg` | 어드미션 입력(호스트 메모리·부하) |
| central admit | `GET /api/jobs` | 자원 여유 시 다른-레포 잡 다수 동시 `running`(사용자 무관) |
| central queue | central 로그 | 압박 시 `queue: mem pressure` / `queue: load pressure` |
| worker fetch | `GET /dispatch/Y/next?exclude=<처리중 티켓>` | 처리 중 잡을 뺀 **다른** running 잡 반환 |
| 동시 실행 | `docker logs jad-worker-Y` | 여러 잡의 `진행중`이 겹쳐서 관측 |
| 예약 회수 | 한 잡 `완료` 후 | 대기분이 다음 tick에 `running`으로 |

### 4C.5 설정

```yaml
admission:
  min_free_mem_mb: 1536         # 유효 가용 메모리 하한(MB). 미만이면 큐잉(메모리 압박).
  per_job_mem_reserve_mb: 1024  # in-flight 잡 1개당 예약 메모리(MB). loadavg 지연 선제 보정.
  max_load_per_core: 0.9        # CPU 코어당 1분 loadavg 상한(2차 거친 천장). 이상이면 큐잉.
run:
  worker_max_concurrency: 64    # worker 동시 실행 안전 상한(runaway 백스톱, 정책 cap 아님).
```

### 4C.6 실패 시 확인

- 자원이 넉넉한데 잡이 직렬로만 돎: `admission.min_free_mem_mb`/`max_load_per_core`가 너무
  빡빡하지 않은지, `/proc` 프로브가 실제 호스트 값을 읽는지(컨테이너 마운트) 확인. worker가
  같은 잡을 반복 수령하면 `?exclude=` 배제가 안 걸린 것(구 central 배포).
- 자원이 빠듯한데 계속 admit(과다 커밋): `per_job_mem_reserve_mb`가 실제 잡 메모리보다 작지
  않은지, 프로브가 호스트가 아닌 컨테이너 로컬 값을 읽고 있지 않은지 확인.
- 같은 레포인데 병렬로 돎(있어선 안 됨): 전역 레포락/`target_repos` 해석 확인 — 이 변경은
  레포락을 **바꾸지 않는다**. 서로 다른 레포로 매핑됐는지 REPO-MAP 점검.

---

## 4D. central 자기 토큰/레이트 관리 (축1 — 쿨다운 + pending + 드레인)

> central은 그 자체가 AI 오케스트레이터다 — **자기** Claude 토큰(env `CLAUDE_CODE_OAUTH_TOKEN`)으로
> 신규 티켓의 **레포 해석**(`app/repo_resolver.py`, 폴러/웹훅의 `trigger_ticket`/`poll_once`)을 한다.
> 그 토큰이 레이트/사용량 한도에 걸려도 **이벤트를 잃지 않고**, 결정적 작업은 멈추지 않으며,
> 회복 시 자동으로 밀린 일을 처리해야 한다. ⚠️ **워커** 토큰/레이트는 각 worker 컨테이너
> 오케스트레이터의 몫이다(여기 범위 밖) — central은 오직 **자기 것**만 관리한다.

### 4D.1 무엇을 보장하나

- **이벤트 유실 없음**: 한도 쿨다운 중 도착한 웹훅/폴 티켓도 **수신·추적**된다 — dedup 게이트
  claim + 담당자 매핑까지는 하고(AI 불필요), claude 호출만 건너뛴 채 **pending(미해석 대기)**
  집합에 보관한다(`state/pending_resolution.json`으로 영속 → 재시작에도 유실 없음).
- **claude 안 두드림**: 한도 감지(429/rate limit/usage limit/overloaded/quota) 시 쿨다운을 건다
  (`ai_cooldown_default_sec`, 기본 120초). 연속 한도엔 백오프가 배증(× 2^(n-1), `ai_cooldown_max_sec`
  900초로 캡)하고, 응답에 reset/retry-after가 있으면 그 시각을 우선한다. 한 번 성공하면 리셋.
- **결정적 작업은 계속**: 이미 해석된 잡의 dispatch·서버 자원 어드미션·레포락/완료 관리
  (`app/scheduler.py` `tick()`)는 쿨다운과 **무관하게** 그대로 돈다 — 쿨다운은 오직 claude
  레포-해석 단계만 막는다. 이미 실행 중인 워커도 자기 토큰으로 계속 일한다.
- **자동 회복(드레인)**: 쿨다운이 풀리면 폴러 루프(`run_forever`)의 드레인 단계가 pending
  티켓을 저장된 이슈로 재해석(claude) → 잡 생성 → 스케줄링에 태운다. 배치 상한으로 한꺼번에
  몰아치지 않는다. 회복 후에도 한도가 재발하면 쿨다운을 재-무장하고 나머지는 pending으로 남긴다.

### 4D.2 조작 — 한도 재현/관측 (관측 위주)

라이브에서 한도를 강제하긴 어렵다. 실제 한도가 발생했을 때(또는 토큰을 일시적으로 무효화해
유사 오류를 유도했을 때) 아래 로그를 관측한다. 그동안 **결정적 dispatch가 계속되는지**가 핵심.

```bash
# central 로그에서 축1 신호 관측:
ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose logs central | \
  grep -iE "central AI (레이트|한도|쿨다운)|pending 보관|drain dispatch"'
# pending 대기 집합(영속 파일):
ssh <deploy-user>@<서버> 'docker compose exec central cat /app/state/pending_resolution.json'
```

### 4D.3 기대 결과

- 한도 중: `... pending 보관(미해석·미디스패치): <TICKET>` 로그 + `pending_resolution.json`에 티켓 존재.
  같은 티켓은 dedup claim이 유지되어 폴/웹훅 재수신에도 **중복 디스패치되지 않는다**(유실도 없음).
- 한도 중에도 **이미 해석된 다른 잡**은 자원이 허용하는 만큼 `running`으로 계속 dispatch된다
  (스케줄러 tick은 쿨다운과 무관).
- 회복 후: `drain dispatch: <TICKET> → user=... repos=[...]` 로그 + `pending_resolution.json`에서
  해당 티켓이 사라지고 `/api/jobs`에 잡이 등장.

### 4D.4 설정

```yaml
run:
  ai_cooldown_default_sec: 120   # 한도 감지 시 기본 쿨다운(초). 응답 reset/retry-after 있으면 우선.
  ai_cooldown_max_sec: 900       # 연속 한도 백오프(배증) 상한(초).
```

### 4D.5 실패 시 확인

- 한도인데 티켓이 사라짐(유실 의심): `pending_resolution.json`와 `dedup.json`(claim 유지)을 확인.
  claim이 풀려 있으면 매핑 단계에서 release된 것(미등록/비활성 담당자) — 정상.
- 회복됐는데 드레인이 안 됨: 폴러 스레드 생존(`grep poll`)·쿨다운 만료 여부·`repo_resolution: llm`인지
  확인. static 모드에는 pending 개념이 없다(정적 룩업이라 AI 불필요).
- 한도 중 결정적 dispatch가 멈춤(있어선 안 됨): 스케줄러 tick 루프가 쿨다운을 참조하지 않는지
  — 이 변경은 스케줄러를 **건드리지 않는다**. 자원 어드미션(4C)/레포락 쪽을 별도로 점검.

---

## 5. 정리 (cleanup)

테스트 산출을 되돌리고 상태를 비운다.

### 5.1 Jira

- 테스트 티켓을 `완료` 또는 `취소됨`으로 종결(또는 삭제 권한 있으면 삭제).
- 재오픈 테스트로 활성 상태로 남은 티켓이 없는지 확인.

### 5.2 forge(GitLab/GitHub)

- 테스트로 생성된 `auto/<TICKET>` 브랜치 삭제(롤백이 안 한 잔존분).
- 열린 MR 초안 close.

### 5.3 서버(central/worker) — 선택

```bash
# 특정 사용자 worker 중지(테스트 계정 정리 시): UI disable 또는
curl -sS -X POST http://localhost:8787/users/<user>/disable    # {"status":"disabled","container":"stopped"}
# 개별 컨테이너 확인
ssh <deploy-user>@<서버> 'docker ps --filter name=jad-worker-'
```

- 상태 저장(jobs/watermark/dedup/registry/pending_resolution)은 명명 볼륨 `jad-state`에 영속한다. 완전 초기화가
  필요하면(테스트 잔재 제거) 운영자가 의도적으로 볼륨을 비운다(주의 — 실사용 데이터 포함).
  ```bash
  # ⚠️ 파괴적 — 테스트 전용 환경에서만.
  ssh <deploy-user>@<서버> 'cd /opt/jira-auto-dispatcher && docker compose down && docker volume rm jad-state'
  ```

---

## 부록 A — 엔드포인트 빠른 참조

| 엔드포인트 | 메서드 | 용도 | 인증 |
|---|---|---|---|
| `/healthz` | GET | 헬스(role 표기) | 없음 |
| `/api/users` | GET | 등록 사용자 목록 | 없음 — **노출 금지**(신뢰 네트워크 한정) |
| `/api/users/<user>/enabled` | POST | enabled 토글(spawn 없음) | 없음 |
| `/api/jobs` | GET | 전 사용자 잡 현황 | 없음 |
| `/api/resume` | POST | reset_at 도래분 즉시 재적격(수동 tick) | 없음 |
| `/onboard` | POST | 사용자 등록(자격증명) | 없음 |
| `/users/<user>/enable` | POST | 활성화 + worker spawn | 없음 |
| `/users/<user>/disable` | POST | 비활성화 + worker stop | 없음 |
| `/users/<user>/autonomy` | POST | A\|B 전환 | 없음 |
| `/users/<user>/container/<start\|stop>` | POST | worker 컨테이너 제어 | 없음 |
| `/dispatch/<user>/next` | GET | (worker) 다음 잡 수령 | `X-Worker-Secret` |
| `/dispatch/<user>/<job>/status` | POST | (worker) 채널 F 회신 | `X-Worker-Secret` |
| `/dispatch/<user>/<job>/control` | GET | (worker) 제어 폴링 `{"cancel":bool,"action":"none\|cancel\|handoff"}` | `X-Worker-Secret` |

> ⚠️ `/dispatch/*`는 worker 전용(공유 시크릿 필요) — E2E 검증은 UI/관리 API와 관측으로 하고,
> dispatch 라우트를 사람이 직접 호출하지 않는다(worker가 담당).

## 부록 B — 잡 상태 & Jira 상태 대응

- **잡 상태**(내부): `queued` → `running` → `done`|`failed`; 한도 시 `interrupted`(→재개);
  취소 시 `cancelling` → `cancelled`(재오픈 시 다시 `queued`);
  담당자 변경 시 `handing_off` → (Y로) `queued`|`running`(이관) 또는 `handed_off`(park). ⚠️ 취소는
  롤백(WIP 폐기), 핸드오프는 checkpoint(WIP 보존) — **재배정 ≠ 취소**.
- **Jira 상태**(예시 — 이름은 인스턴스마다 다르다, `config.jira.*_statuses`):
  `해야 할 일` → `진행 중` → `완료` (+ **`취소됨`**; 범주는 완료지만 이름으로
  중단 구분). match.statuses = `["해야 할 일"]`가 트리거 대상.

## 부록 C — 주기(참고)

| 루프 | 기본 주기 | 출처 |
|---|---|---|
| Jira 폴러/상태 워처 | 60s | `config.jira.poll_interval_sec` |
| worker 잡 폴링 | 5s | `WORKER_POLL_INTERVAL_SEC` |
| worker 취소 제어 폴링 | 3s | `WORKER_CONTROL_POLL_SEC` |
| 스케줄러 tick(재개 재적격) | 30s | `start_central_background(tick_interval_sec)` |
| 재개 버퍼 | 120s | `config.resume.reset_buffer_sec` |
