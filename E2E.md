# E2E.md — jira-auto-dispatcher 개발서버 E2E 검증 시나리오

> **목적.** central이 개발서버(61.96.103.14)에 배포된 뒤, 운영자/사용자가 **직접 온보딩**하고
> **실 Jira(HAN) 티켓**으로 전(全) 플로우를 검증한다. 배포 자체는 [DEPLOY.md](DEPLOY.md), 보안
> 인가 근거는 [SECURITY.md](SECURITY.md), 전체 동작 계약은
> `dlc-meta/RECURSIVE-DISPATCH.md`(이하 §는 그 문서 절 번호)가 정본이다.
>
> 이 문서는 **검증 런북**이다 — 각 단계에 **조작 → 기대 결과 → 실패 시 확인 로그**를 짝지어 둔다.
> 라이브 조작은 사람이 수행한다(자동 실행 금지). 스모크/관측 보조 스크립트는
> [`scripts/smoke-deployed.sh`](scripts/smoke-deployed.sh)·[`scripts/observe-job.sh`](scripts/observe-job.sh).

---

## 0. 준비 — 접속·헬스·UI 확인

### 0.1 사전 조건

- DEPLOY.md 1~5단계 완료(이미지 빌드 → config.yaml → 시크릿(service/jira-token, .env의
  `WORKER_SHARED_SECRET`) → `docker compose up -d`).
- 개발서버 SSH 접근(`yhchoi@61.96.103.14`).
- 관리 UI는 **사내망 한정**이므로 SSH 터널로 연다:

  ```bash
  ssh -L 8787:localhost:8787 yhchoi@61.96.103.14   # 로컬 http://localhost:8787 → 서버 central
  ```

- **본인 노트북**에서 `claude setup-token`(Max, long-lived) 발급값을 미리 준비(온보딩 입력용).
- 자기 **Jira accountId**(assignee 매핑 키), Jira API 토큰, (선택) GitLab PAT 준비.

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
ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose ps'
ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central'
# 폴러/워처/스케줄러 스레드 기동 여부(부팅 로그):
ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs central | grep -iE "poll|watcher|scheduler|bind|8787"'
```

- `/healthz`가 안 뜨면 → central 컨테이너 미기동/포트 매핑(8787) 확인, 터널 재확인.
- `/api/users`가 500이면 → `config/config.yaml` 로드 실패(마운트 `./config:/app/config:ro`)·
  `secrets.base_dir` 미설정 여부 확인.

---

## 1. 온보딩 (사용자 직접) → worker 자동 spawn

### 1.1 조작 — 자격증명 입력 → 등록

UI 온보딩 폼(또는 `POST /onboard`)에 입력한다. **필수**: `username`, `jira_account_id`,
`jira_email`, `jira_token`, `claude_setup_token`. **선택**: `gitlab_token`(MR 생성용),
`git_name`/`git_email`(커밋 author 귀속), `autonomy_mode`, `permission_level`, `scope`.

- **autonomy_mode = B 권장(최초)** — 경량 1차(트리아지+브랜치+스캐폴딩+1차 시도+`runs` 저널).
  MR을 강행하지 않아 최초 검증에서 부작용이 작다(§5). A는 컨벤션이 성숙한 FE에서 나중에.
- **permission_level = bypass**(현재 유일 구현; SECURITY.md §5).
- 폼 제출(`POST /onboard`) 뒤 사용자는 **enabled=false(안전 기본)**로 등록된다(§SECURITY 3).

CLI로 확인만 할 때(값 노출 주의 — 실제 토큰은 UI로 입력 권장):

```bash
curl -sS -X POST http://localhost:8787/onboard \
  -H 'Content-Type: application/json' \
  -d '{"username":"yh.choi","jira_account_id":"712020:...","jira_email":"yh.choi@interxlab.com",
       "jira_token":"<JIRA_API_TOKEN>","claude_setup_token":"<SETUP_TOKEN>",
       "gitlab_token":"<GITLAB_PAT>","git_name":"yunhyuk-choi","git_email":"yh.choi@interxlab.com",
       "autonomy_mode":"B","scope":"HAN"}'
# 기대: HTTP 201 {"status":"ok","username":"yh.choi","enabled":false}
```

### 1.2 조작 — 활성화(enable) → worker spawn

UI 등록 사용자 표에서 해당 사용자 **enable** 버튼(또는 `POST /users/<username>/enable`).
central이 per-user 볼륨 `jad-<username>` 보장 + 사전 인가 `settings.json` 기록 +
`jad-worker-<username>` 컨테이너를 같은 image·`jad-net`·비-root(1000:1000)로 spawn 한다(DEPLOY §6).

```bash
curl -sS -X POST http://localhost:8787/users/yh.choi/enable
# 기대: {"status":"enabled","container":"running"}
```

### 1.3 기대 결과

- `POST /onboard` → 201, 응답에 **토큰 값 없음**(참조만 저장). 시크릿은 서버
  `secrets/<username>/`에 0600으로 기록(jira-token·gitlab-token·claude-oauth-token).
- `GET /api/users`에 사용자 1건, 최초 `enabled=false` → enable 후 `enabled=true`.
- enable → `{"status":"enabled","container":"running"}`.
- 서버 `docker ps`에 **`jad-worker-yh.choi` Up**:

  ```bash
  ssh yhchoi@61.96.103.14 'docker ps --filter name=jad-worker-'
  ```
- worker 로그에 폴링 루프 기동(및 claude 준비):

  ```bash
  ssh yhchoi@61.96.103.14 'docker logs jad-worker-yh.choi --tail=50'
  ```
- worker 내부 헬스:

  ```bash
  ssh yhchoi@61.96.103.14 'docker exec jad-worker-yh.choi curl -fsS http://localhost:8787/healthz'
  # {"status":"ok","role":"worker","user":"yh.choi"}
  ```

### 1.4 실패 시 확인 로그

- enable이 **502 `spawn_error`** → central이 docker(프록시)로 컨테이너를 못 띄운 것:
  ```bash
  ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -i spawn'
  # socket-proxy 정합 확인(config spawn.docker_host=tcp://socket-proxy:2375, DEPLOY §2·§5):
  ssh yhchoi@61.96.103.14 'docker logs jad-socket-proxy --tail=50'
  ```
- worker가 떴다 바로 죽으면(Restarting/Exited) → claude 인증 실패 가능(setup-token 만료/오타):
  ```bash
  ssh yhchoi@61.96.103.14 'docker logs jad-worker-yh.choi --tail=100'
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

Jira(HAN)에서 **자기 자신이 담당자(assignee)** 인 테스트 티켓을 만든다.

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
| 산출 | GitLab | 브랜치 `auto/<TICKET>` 생성(커밋 author = 사용자 identity) |
| MR(A모드만) | GitLab | MR 초안 생성(사용자 GitLab 토큰). **B모드는 MR 없음**(1차 산출+저널) |
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
- GitLab: `auto/<TICKET>` 브랜치 존재. 커밋 author = 사용자 이름/이메일(per-user attribution §7).
- autonomy=A면 MR 초안 + `/api/jobs`의 `mr_url` 채워짐. **자동 머지는 없다**(사람 리뷰 게이트).
- autonomy=B면 MR 없이 1차 산출 + `runs/<TICKET>/` 저널.

### 2.4 실패 시 확인 로그

- 티켓을 만들었는데 **잡이 안 생김**(`/api/jobs` 비어 있음):
  ```bash
  ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -iE "poll|claim|map|assignee"'
  ```
  - assignee accountId 불일치 / 사용자 `enabled=false` / 상태가 `해야 할 일`이 아님 /
    프로젝트가 HAN이 아님(config.jira.project) 중 하나. 폴링 주기(60s) 대기했는지 확인.
- 잡이 `running`인데 worker가 안 받음:
  ```bash
  ssh yhchoi@61.96.103.14 'docker logs jad-worker-<user> --tail=100'
  ```
  - `X-Worker-Secret` 불일치(401) → central `.env`와 worker env의 `WORKER_SHARED_SECRET` 정합 확인.
  - claude 인증 실패 → setup-token 재발급/재온보딩(1.4).
- Jira 전이 실패(진행 중/완료로 안 넘어감):
  - **필수필드 누락**이 가장 흔함(duedate·10015 착수 / 10186·10187 완료). worker 로그의
    실행 요약(`log_summary`)·`GET /api/jobs`의 해당 잡 로그 확인.
- 잡이 `failed`로 회신:
  ```bash
  ssh yhchoi@61.96.103.14 'docker logs jad-worker-<user> --tail=200'
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
ssh yhchoi@61.96.103.14 'docker logs jad-worker-<user> --tail=100 | grep -iE "limit|resume|reset"'
ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=50 central | grep -i tick'
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
| 롤백 결과 | GitLab에서 `auto/<TICKET>` 브랜치 삭제·MR closed 확인 |
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
ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs --tail=100 central | grep -iE "reopen|재오픈|enqueue"'
bash scripts/observe-job.sh <TICKET> <user>    # cancelled → queued → running
```

### 4.5 실패 시 확인 로그

- 취소가 안 잡힘: status_watcher가 도는지 + JQL 대상인지 확인.
  ```bash
  ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose logs central | grep -i status_watcher'
  ```
  - 잡이 이미 종결(done/failed)이거나 추적 대상이 아니면 취소는 멱등 스킵된다(정상).
- 롤백 안 됨(브랜치/MR 잔존): worker 로그의 롤백 요약 확인. 브랜치/MR이 아직 없으면 스킵이
  정상(best-effort). GitLab 토큰 유효성(close 권한) 확인.
- 재오픈 무반응: 재오픈은 **취소 확정(cancelled) 잡의 티켓**만 대상. 잡이 cancelled인지
  (`/api/jobs`) + assignee가 여전히 enabled 사용자인지 확인. reopen 워터마크(updated) 이후
  전이인지 확인.

---

## 5. 정리 (cleanup)

테스트 산출을 되돌리고 상태를 비운다.

### 5.1 Jira

- 테스트 티켓을 `완료` 또는 `취소됨`으로 종결(또는 삭제 권한 있으면 삭제).
- 재오픈 테스트로 활성 상태로 남은 티켓이 없는지 확인.

### 5.2 GitLab

- 테스트로 생성된 `auto/<TICKET>` 브랜치 삭제(롤백이 안 한 잔존분).
- 열린 MR 초안 close.

### 5.3 서버(central/worker) — 선택

```bash
# 특정 사용자 worker 중지(테스트 계정 정리 시): UI disable 또는
curl -sS -X POST http://localhost:8787/users/<user>/disable    # {"status":"disabled","container":"stopped"}
# 개별 컨테이너 확인
ssh yhchoi@61.96.103.14 'docker ps --filter name=jad-worker-'
```

- 상태 저장(jobs/watermark/dedup/registry)은 명명 볼륨 `jad-state`에 영속한다. 완전 초기화가
  필요하면(테스트 잔재 제거) 운영자가 의도적으로 볼륨을 비운다(주의 — 실사용 데이터 포함).
  ```bash
  # ⚠️ 파괴적 — 테스트 전용 환경에서만.
  ssh yhchoi@61.96.103.14 'cd /opt/jira-auto-dispatcher && docker compose down && docker volume rm jad-state'
  ```

---

## 부록 A — 엔드포인트 빠른 참조

| 엔드포인트 | 메서드 | 용도 | 인증 |
|---|---|---|---|
| `/healthz` | GET | 헬스(role 표기) | 없음 |
| `/api/users` | GET | 등록 사용자 목록 | 없음(사내망) |
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
| `/dispatch/<user>/<job>/control` | GET | (worker) 취소 제어 폴링 | `X-Worker-Secret` |

> ⚠️ `/dispatch/*`는 worker 전용(공유 시크릿 필요) — E2E 검증은 UI/관리 API와 관측으로 하고,
> dispatch 라우트를 사람이 직접 호출하지 않는다(worker가 담당).

## 부록 B — 잡 상태 & Jira 상태 대응

- **잡 상태**(내부): `queued` → `running` → `done`|`failed`; 한도 시 `interrupted`(→재개);
  취소 시 `cancelling` → `cancelled`(재오픈 시 다시 `queued`).
- **Jira 상태**(HAN): `해야 할 일` → `진행 중` → `완료` (+ **`취소됨`**; 범주는 완료지만 이름으로
  중단 구분). match.statuses = `["해야 할 일"]`가 트리거 대상.

## 부록 C — 주기(참고)

| 루프 | 기본 주기 | 출처 |
|---|---|---|
| Jira 폴러/상태 워처 | 60s | `config.jira.poll_interval_sec` |
| worker 잡 폴링 | 5s | `WORKER_POLL_INTERVAL_SEC` |
| worker 취소 제어 폴링 | 3s | `WORKER_CONTROL_POLL_SEC` |
| 스케줄러 tick(재개 재적격) | 30s | `start_central_background(tick_interval_sec)` |
| 재개 버퍼 | 120s | `config.resume.reset_buffer_sec` |
