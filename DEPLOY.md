# DEPLOY.md — 온프렘 서버 배포 상세 런북

> **범위.** 이 문서는 [INSTALL.md](INSTALL.md) 의 **갈래 C(온프렘 서버)** 본문이다 —
> 사설망의 리눅스 서버에 SSH 로 들어가 운영자가 **순서대로 따라 실행**하는 런북.
> 개인 노트북 평가(갈래 A)나 클라우드 VM(갈래 B)이면 INSTALL.md 를 먼저 읽어라.
> 어느 갈래든 공통인 준비물(토큰·`config.yaml`)과 프로파일별 값 차이는 INSTALL.md 가 정본이다.
>
> ⚠️ 이 시스템은 도구 권한을 가진 자율 에이전트(RCE 표면)를 헤드리스로 돌린다 —
> **신뢰 네트워크 한정**, 인터넷 노출 금지. 무엇에 동의하는지와 보안 통제의 정본은
> [SECURITY.md](SECURITY.md) 다(**배포 전 필독**).

---

## 0. 대상·전제

아래 `<서버>`·`<deploy-user>` 는 당신의 환경 값으로 바꿔 읽는다. 이 런북 예시는
배포 경로로 `/opt/jira-auto-dispatcher` 를 쓴다(다른 경로를 쓰면 그에 맞춰 바꾼다).

| 항목 | 값 |
|---|---|
| 대상 서버 | `<서버>` — 이 시스템 **전용 호스트** 권장(SECURITY.md §4) |
| SSH 계정 | `<deploy-user>` (docker 실행 권한 필요) |
| 필요 런타임 | Docker Engine + Docker Compose v2 (`docker compose`) |
| 이미지 | `jira-auto-dispatcher:latest` (central·worker 공용 단일 이미지) |
| 네트워크 | `jad-net` (compose가 생성; 동적 worker가 이름으로 합류) |
| 관리 UI 포트 | `8787` (⚠️ 신뢰 네트워크 한정 — 인터넷 노출 금지) |

계약 정합(이 3개는 `Dockerfile`·`docker-compose.yml`·`config/config.yaml`·`app/spawner.py`가
모두 동일해야 한다):

- **image** = `jira-auto-dispatcher:latest`
- **network** = `jad-net`
- **central DNS** = `http://central:8787` (compose 서비스 키가 `central`)

---

## 1. 이미지 빌드 / 전송

배포 방식은 둘 중 하나. **(A) 서버에서 직접 빌드**를 권장한다(가장 단순).

### (A) 서버에서 빌드 (권장)

```bash
# 로컬에서 소스 동기화(git 없이 rsync 예시 — .dockerignore와 무관하게 런타임/시크릿 제외)
rsync -az --delete \
  --exclude '.git' --exclude 'state/' --exclude 'secrets/' --exclude 'workspace/' \
  --exclude 'orchestrator/' --exclude 'dlc-meta/' --exclude 'dataspace_docs/' \
  --exclude 'config/config.yaml' --exclude '.env' \
  ./ <deploy-user>@<서버>:/opt/jira-auto-dispatcher/

# 서버에서 빌드
ssh <deploy-user>@<서버>
cd /opt/jira-auto-dispatcher
docker build -t jira-auto-dispatcher:latest .
docker run --rm --entrypoint claude jira-auto-dispatcher:latest --version   # 레이어 검증
```

### (B) 로컬 빌드 → tar 전송 (서버 빌드 불가 시)

```bash
docker build -t jira-auto-dispatcher:latest .
docker save jira-auto-dispatcher:latest | gzip > jad.tar.gz
scp jad.tar.gz <deploy-user>@<서버>:/opt/jira-auto-dispatcher/
ssh <deploy-user>@<서버> 'gunzip -c /opt/jira-auto-dispatcher/jad.tar.gz | docker load'
```

> `workspace/` 를 제외하는 이유: 런타임 클론(오케스트레이터·dlc-meta·docs 레포)은
> **공유 워크스페이스 named 볼륨**이 소유한다(5단계) — 소스 동기화로 덮으면 안 된다.
> 옛 배포가 리포 루트에 남긴 `orchestrator/`·`dlc-meta/`·`dataspace_docs/` 잔재도 함께 제외한다.
>
> ⚠️ `docker push`/레지스트리 사용은 이 런북 범위 밖(각자 정책 따름). 여기서는
> 로컬 빌드/전송만 다룬다.
>
> **재배포(2회차 이후)**: 위 rsync 로 소스를 갱신한 뒤 `deploy/redeploy-central.sh` 를
> 서버에서 실행하면 이미지 재빌드 → central 만 recreate → `/healthz` 폴링 → 실패 시
> 롤백까지 한 번에 한다. worker 컨테이너(`jad-worker-<user>`)는 건드리지 않는다.
>
> ```bash
> ssh <deploy-user>@<서버> \
>   "bash /opt/jira-auto-dispatcher/deploy/redeploy-central.sh /opt/jira-auto-dispatcher"
> ```

---

## 2. config.yaml 준비 (시크릿 값 아님)

`config/config.example.yaml` → `config/config.yaml` 로 복사해 채운다. 시크릿 **값**은
넣지 않는다(파일 참조만).

```bash
cd /opt/jira-auto-dispatcher
cp config/config.example.yaml config/config.yaml
```

온프렘은 `deploy.profile: onprem_server` 다. **socket-proxy를 쓰는 기본 구성이면 반드시**
`deploy.docker_host` 를 프록시로 맞춘다. 호스트 절대경로를 적을 항목은 없다 — worker 에
거는 마운트는 전부 named 볼륨이고, config·시크릿·알림 웹훅은 central 이 컨테이너 스펙에
**주입**한다(`app/inject.py`, INSTALL §2.2):

```yaml
deploy:
  profile: onprem_server
  docker_host: tcp://socket-proxy:2375         # ← socket-proxy 기본 구성. (직결 시 unix:///var/run/docker.sock)
  secrets_base_dir: /run/secrets
  workspace_volume: jad-workspace

spawn:
  image: jira-auto-dispatcher:latest
  network: jad-net
  central_url: http://central:8787
  run_as: "1000:1000"
```

> 레거시 키(`spawn.docker_host`·`spawn.workspace_volume`·`secrets.base_dir`)도 계속 읽으므로
> 기존 `config.yaml` 은 그대로 둬도 동작한다. 제거된 `host_deploy_dir`(과 env
> `HOST_DEPLOY_DIR`)이 남아 있어도 **무시**된다 — 지우지 않아도 무해하다.

`config/config.yaml`은 compose가 `/app/config:ro` 로 마운트한다(gitignore·이미지 미포함).

---

## 3. 시크릿 배치 (`secrets.base_dir`)

**central** 컨테이너는 `SECRETS_DIR=/run/secrets` 로 주입되고, 호스트 `./secrets/` 를
마운트한다. 여기에 **service 시크릿**(central 공용)과 **per-user 시크릿**을 파일로 둔다 —
이 저장 위치는 예나 지금이나 같다.
⚠️ **central은 rw로 마운트한다** — 온보딩 UI가 새 사용자 시크릿을 `secrets.base_dir/<user>/`
에 직접 쓰기 때문(`:ro`면 온보딩이 `Read-only file system` 500으로 실패).
호스트 `./secrets/` 소유는 컨테이너 uid(1000:1000)에 맞춘다.

**worker** 는 이 디렉토리를 **마운트하지 않는다.** central 이 spawn 시 그 사용자 몫만
컨테이너 스펙에 실어 보내고(`app/inject.py`), worker 가 부팅 시 자기 `/run/secrets`
tmpfs(RAM 전용, 0600)에 기록한다. 그래서 워커 사이에는 공유 부모 디렉토리 자체가 없다
— A 가 B 의 시크릿에 닿을 경로가 존재하지 않는다. 알림 웹훅도 같은 채널로 **파일 하나만**
간다(`service/` 디렉토리 전체는 절대 안 간다 — 거기엔 central watcher 의 Jira 토큰이 있다).

```bash
cd /opt/jira-auto-dispatcher
mkdir -p secrets/service

# (a) central Jira watcher 토큰 (config.jira.watcher_token_file = service/jira-token)
printf '%s' 'ATLASSIAN_API_TOKEN_값' > secrets/service/jira-token

# (b) 웹훅을 쓸 때만 — 웹훅 공유 시크릿 (config.webhook.secret_ref)
# printf '%s' '웹훅시크릿' > secrets/service/webhook-secret

# (c) 알림을 쓸 때만 — 웹훅 URL (config.notifier.webhook_ref).
#     provider별 URL 획득법은 config/config.example.yaml 의 notifier: 절 주석이 정본.
# printf '%s' 'https://...' > secrets/service/notifier-webhook

# 권한 하드닝 — 시크릿은 소유자만.
chmod -R go-rwx secrets
find secrets -type f -exec chmod 600 {} \;
```

- **worker 공유시크릿**(`WORKER_SHARED_SECRET`, dispatch HTTP 인증)은 파일이 아니라
  **env**로 주입한다. ⚠️ **직접 만들지 않는 것이 기본이다** — `python -m app.setup render`
  (INSTALL.md §2.5)가 `.env`에 없으면 만들고(0600), **이미 있으면 손대지 않는다**(멱등).
  재생성하면 떠 있는 워커가 전부 `X-Worker-Secret` 401로 죽으므로 바꾸지 않는다.
  그 명령을 쓰지 않는 배포라면 손으로:

  ```bash
  # /opt/jira-auto-dispatcher/.env  (compose가 자동 로드; 0600 권장)
  echo "WORKER_SHARED_SECRET=$(openssl rand -hex 32)" >> .env   # ⚠️ >> (기존 .env 보존)
  chmod 600 .env
  ```

  값이 있는지는 `python -m app.setup doctor --only worker_secret` 이 확인한다(값은 출력하지
  않는다). 없으면 dispatch 엔드포인트가 **무인증으로 열린다**는 경고가 뜬다.

- **per-user 시크릿**(각자 Jira/forge 토큰·claude setup-token)은 여기서 손으로 만들지
  않는다 — **온보딩(5단계)**에서 관리 UI가 `secrets/<username>/` 하위에 0600으로 기록한다.

- **central LLM 레포 리졸버(`run.repo_resolution: llm`)**: central이 새 티켓의
  `target_repos` 를 dlc-meta `REPO-MAP.md` + 티켓 내용으로 `claude` 에게 판단시켜 채운다
  (→ 스케줄러가 서로 다른 레포는 병렬, 같은 레포는 직렬). 이를 위해 central 컨테이너에
  **두 가지**를 셋업한다:
  1. **central claude 인증** — central 컨테이너 env `CLAUDE_CODE_OAUTH_TOKEN` 에
     `claude setup-token` 값을 주입한다(워커의 per-user 토큰과 별개인 central 자신의
     판단용 토큰). `.env` 에 두거나(compose 자동 로드) compose의 central 서비스
     `environment:` 로 넘긴다. 작은 판단 호출이라 사용량 부담은 낮다.
     ```bash
     # /opt/jira-auto-dispatcher/.env  (0600)
     echo "CLAUDE_CODE_OAUTH_TOKEN=$(cat /path/to/central-claude-token)" >> .env
     ```
  2. **central dlc-meta 접근** — central이 최신 `REPO-MAP.md` 를 읽도록, dlc-meta를
     **공유 워크스페이스 볼륨**(`jad-workspace` → `/app/workspace`) 안 단일 클론
     (`run.repo_map_path`, 기본 `/app/workspace/dlc-meta`)으로 두고 리졸브 전에 pull한다.
     pull에 쓸 **central forge 토큰**(GitLab PAT / GitHub PAT)은 `secrets/service/forge-token`
     파일(0600)에 두고 `forge.token_ref: service/forge-token` 로 참조만 건다(값 아님).
     레거시 키 `run.repo_resolver_gitlab_token_ref` 도 계속 읽으므로 기존 배포는 그대로 둬도 된다.
     ```bash
     printf '%s' 'FORGE_READ_TOKEN_값' > secrets/service/forge-token
     chmod 600 secrets/service/forge-token
     ```
     토큰이 없으면 pull을 생략하고 기존(stale) 체크아웃만 읽으며, `REPO-MAP.md` 자체가
     없으면 조용히 정적 `repo_map`(config) 폴백으로 동작한다(best-effort — 폴러 무중단).
     `repo_resolution: static` 으로 두면 이 기능을 끄고 기존 정적 룩업만 쓴다.

---

## 4. 인바운드 포트 (웹훅 쓸 때만)

- **폴링만** 쓰면(`webhook.enabled: false`) central이 Jira로 **아웃바운드**만 하므로
  인바운드 개방 **불필요**. 관리 UI(8787)는 신뢰 네트워크에서만 접근한다(SSH 터널 권장).
- **웹훅**을 켜면(`webhook.enabled: true`) Jira → central 로의 인바운드가 필요하다.
  리버스 프록시(TLS)를 앞단에 두고 `webhook.path` **하나만** 노출, 나머지(8787 관리 UI)는
  절대 외부 노출하지 않는다. 방화벽에서 출처를 Jira IP로 제한한다.

관리 UI 접근은 포트를 여는 대신 SSH 터널을 권장:

```bash
ssh -L 8787:localhost:8787 <deploy-user>@<서버>   # 로컬 http://localhost:8787
```

---

## 5. 기동 (`docker compose up -d`)

```bash
cd /opt/jira-auto-dispatcher
docker compose up -d              # socket-proxy + central 기동
docker compose ps
docker compose logs -f central    # 부팅 로그(설정 로드·poller 스레드) 확인
```

- 기본 구성은 **socket-proxy + central** 두 서비스를 띄운다. central은 docker.sock을
  직접 보지 않고 `tcp://socket-proxy:2375` 로만 컨테이너를 조작한다(2단계 config 정합 필수).
- 직결 대안(비권장)은 `docker-compose.yml` 주석 참조(socket-proxy 제거 + docker.sock 직접
  마운트 + `docker_host: unix:///var/run/docker.sock`).

### 공유 워크스페이스 (단일 클론·단일 pull·락)

central·모든 worker는 **하나의 named 볼륨**(`jad-workspace` → `/app/workspace`,
`run.workspace_dir`)을 공유하고, 오케스트레이터 레포(`orchestrator`/`dlc-meta`/
`docs`)와 타겟 레포를 그 하위에 **한 벌만** 클론한다(설계 §4 "공유 워크스페이스
하나 = 단일 클론·단일 pull 지점"). 예전처럼 워커마다 자기 컨테이너에 N중 클론·N번 pull
하지 않는다.

- **볼륨 공유**: compose가 `jad-workspace` 를 `name: jad-workspace` 로 고정 선언하고,
  spawner가 워커에 **같은 이름의 named 볼륨**을 `run.workspace_dir` 로 마운트한다
  (`config.deploy.workspace_volume`, 기본 `jad-workspace`). named 볼륨이라 호스트 경로와
  무관하게 볼륨명으로 docker가 해석한다.
- **경로 파생**: `run.orchestrator_repo`/`dlc_meta_repo`/`docs_repo` 를 비우면
  `<workspace_dir>/orchestrator|dlc-meta|docs` 로 자동 파생한다(명시하면 존중).
  ⚠️ 파생 디렉토리명이 옛 `dataspace_docs` → 범용 `docs` 로 바뀌었다. 경로를 **명시한**
  기존 배포는 그 값을 그대로 존중하므로 영향이 없고, 비워 둔 배포만 새 디렉토리에 다시
  clone 한다(읽기 전용 참고 레포라 안전).
- **pull 락**: 여러 워커가 공유 클론을 per-job pull하므로 clone/pull을 레포별 파일락
  (`<repo>.lock`, 공유 볼륨 위 → 컨테이너 간 `flock`)으로 감싸 **직렬화**한다
  (clone-if-absent+pull-if-present 가 락 안에서 원자적). 서로 다른 레포는 병렬.
- **동일 타겟레포 쓰기 안전**: central 스케줄러의 레포락이 같은 타겟레포 동시 잡을 이미
  직렬화하므로, 공유 타겟레포 클론이라도 한 번에 한 job만 만진다(브랜치 `auto/<ticket>` 로
  per-job 격리).

> ⚠️ **기존 배포에서 이관**: 공유 워크스페이스로 바꾸면 central·워커 모두 recreate 해야
> 새 볼륨 마운트가 반영된다. 워커는 배포가 자동 reconcile(6단계 참고)하거나 UI stop→start
> 로 재생성한다. 예전 per-user 클론(`jad-<user>` 볼륨 안이 아니라 각 워커 사설 경로)은
> 더는 쓰지 않으며, 최초 잡에서 공유 워크스페이스에 다시 클론된다.

---

## 6. 사용자 온보딩 → worker 동적 spawn

관리 UI(`http://localhost:8787`, SSH 터널)에서 사용자마다 자격증명을 입력한다. central이
시크릿을 `secrets/<username>/` 에 0600으로 저장하고 레지스트리에 참조만 남긴 뒤
(`enabled=false` 안전 기본), **활성화 시** Docker SDK로 worker 컨테이너를 동적 spawn 한다.

> ⚠️ **웹 등록은 합류의 2단 중 하나다.** 합류자는 로컬에서 `ai-dlc-orchestrator` 의
> SETTER 를 합류 모드로 돌려 `dlc-meta` 를 clone 해야 그 사람의 로컬 오케스트레이터가
> 정체성을 갖는다. 팀원에게는 [INSTALL.md §8](INSTALL.md) 을 준다.

온보딩 폼 필드 — **정본은 스키마 선언**(`app/user_schema.py`)이며 관리 UI 는 그것을 이
인스턴스 설정과 함께 렌더한다(`GET /api/onboarding/guide`). 아래 표는 요약이다:

| 필드 | 필수 | 내용 |
|---|---|---|
| `username` | ✔ | 내부 식별자(컨테이너·볼륨·시크릿 경로 키) |
| `jira_account_id` | ✔ | 담당자 매핑 키(티켓 assignee accountId). 폼의 `내 accountId 조회` 버튼이 `GET /rest/api/3/myself` 로 대신 찾아 준다(`POST /api/onboarding/whoami`) |
| `jira_email` | ✔ | Jira actor 이메일(Basic auth) |
| `jira_token` | ✔ | 사용자 Jira API 토큰 → `secrets/<user>/jira-token` |
| `forge_token` | ✔ | 브랜치 push·MR/PR 생성용 → `secrets/<user>/forge-token` (옛 폼 필드 이름 `gitlab_token` 도 계속 받는다). **선택이 아니다** — 없으면 워커가 커밋만 하고 변경요청을 못 만드는 조용한 반쪽 동작이 된다 |
| `claude_setup_token` | ✔ | `claude setup-token` 발급 값 → `secrets/<user>/claude-oauth-token` |
| `consent_full_permissions` | ✔ | **본인**의 풀 퍼미션 동의(체크박스). 서버가 강제하며(400) 수신 시각이 레지스트리 `consent.accepted_at` 에 남는다. 설치자의 `consent.full_permissions` 로 갈음하지 않는다 |
| `git_name`/`git_email` | | 커밋 author 귀속(⚠️ `git_email` 은 forge 에 인증된 이메일이어야 연결된다) |
| `autonomy_mode` | | A(완전자율) / B(경량 1차, 기본) |
| `notify_user_id` | | 완료 알림 @멘션용 채널 사용자 id(옛 이름 `google_chat_user_id`) |

활성화(worker 기동): UI의 enable 버튼 = `POST /users/<username>/enable` →
central이 per-user 볼륨 `jad-<username>` 보장 + 사전 인가 `settings.json` 기록 +
`jad-worker-<username>` 컨테이너를 같은 image·`jad-net`·비-root(1000:1000)로 spawn.

> ⚠️ **claude 인증(중요)**: 각 worker는 온보딩의 `claude setup-token`
> (`CLAUDE_CODE_OAUTH_TOKEN` env)으로 인증한다. Max 구독 기반의 long-lived 토큰이다.
> - **재인증·만료 대응**: 토큰이 만료/폐기되면 worker의 `claude`가 인증 실패한다.
>   해당 사용자가 로컬에서 `claude setup-token` 을 **재발급**해 관리 UI로 다시 온보딩
>   (같은 username 재입력)하면 `secrets/<user>/claude-oauth-token` 이 갱신된다. 이후
>   `POST /users/<username>/container/stop` → `.../start` 로 worker를 재기동해 새 토큰을 싣는다.
> - per-user 볼륨 `jad-<username>` 이 `~/.claude` 세션을 영속하므로 토큰만 유효하면
>   재기동으로 세션이 이어진다.
> - 토큰 한도(롤링)는 사용자별로 걸린다 — worker가 감지해 `interrupted`+`reset_at`으로
>   회신하고 central 스케줄러가 reset 시각에 재개한다(만료와 다름; 재발급 불필요).

---

## 7. 검증 체크리스트

```bash
# (1) central 헬스
curl -fsS http://localhost:8787/healthz          # {"status":"ok","role":"central"}

# (2) 폴러 동작(로그에 폴링 주기·claim 흔적)
docker compose logs --tail=50 central | grep -i poll

# (3) socket-proxy 경유 확인(central이 sock 직접 마운트 안 함)
docker inspect jad-central --format '{{json .Mounts}}'   # docker.sock 바인드가 없어야 함(기본 구성)

# (4) 온보딩·활성화 후 worker 컨테이너 기동
docker ps --filter name=jad-worker-               # jad-worker-<user> 가 Up
docker logs jad-worker-<user> --tail=50           # 폴링 루프·claude 준비 로그

# (5) worker 헬스(내부망)
docker exec jad-worker-<user> curl -fsS http://localhost:8787/healthz
```

**엔드투엔드 (테스트 티켓)**:

1. 감시 대상 Jira 프로젝트(`config.jira.project`)에서 테스트 티켓을 만들어 온보딩된
   사용자에게 할당하고 트리거 상태(`config.jira.trigger_statuses`)로 둔다.
2. central 로그에서 감지→claim→dispatch, 해당 worker 로그에서 잡 수신→`claude -p` 실행 확인.
3. 산출물 검증: 사용자 정체성의 브랜치(`auto/<TICKET>`) + MR/PR 생성(사용자 forge 토큰).
4. 티켓/잡 상태가 `done`(또는 한도 시 `interrupted`+`reset_at`)로 회신되는지 확인.

전 플로우를 단계별 기대결과와 함께 검증하는 상세 런북은 [E2E.md](E2E.md) 다.

---

## 8. 보안 (요약 — 정본은 SECURITY.md)

- **신뢰 네트워크 한정**: 관리 UI(8787)·worker는 인터넷 노출 금지. UI 접근은 SSH 터널 권장.
- **socket-proxy 권장**: central은 docker.sock 직결 대신 프록시로 최소 권한
  (CONTAINERS/IMAGES/NETWORKS/VOLUMES + POST)만 사용. 직결은 호스트 root 동치.
- **비-root**: 이미지는 uid 1000(app)으로 실행하고 worker도 `run_as: 1000:1000`.
- **시크릿**: 값은 `secrets/` 에 0600, read-only 마운트. 이미지에 굽지 않는다(.dockerignore).
  Jira/forge 토큰은 worker에 **파일 경로**로만 넘긴다(값은 claude setup-token만 env 주입).
- **가역성/폭주 방지**: dedup 게이트 + central **서버 자원 어드미션**(메모리+부하로 dispatch
  조절, 잡 수 cap 아님) + worker 안전 상한(`worker_max_concurrency`, 기본 64) + 결정적 브랜치
  (`auto/<TICKET>`) + MR 게이트(사람 리뷰).

전체 위협 모델과 "실행 = 풀 퍼미션 동의" 선언은 **[SECURITY.md](SECURITY.md)** 참조.

---

## 9. 운영 (참고)

```bash
docker compose restart central          # 설정 변경 반영(config.yaml 수정 후)
docker compose down                     # central+socket-proxy 중지(worker는 별도)
docker ps --filter name=jad-worker-     # 동적 worker 목록
docker stop jad-worker-<user>           # 개별 worker 중지(또는 UI disable)
```

- config.yaml·시크릿 변경 후에는 `docker compose restart central`. central 은 부팅 시
  각 워커에 **구워진 주입 페이로드**(config·시크릿)를 지금 값과 대조해, 달라졌으면 그
  워커를 재생성한다 — 활성 잡이 있으면 그 잡이 끝난 뒤로 미룬다(드레인). 그래서 별도
  조작 없이 다음 재기동에서 새 값이 반영된다.
- 이미지 갱신(재빌드) 시 `docker compose up -d --build central` 후, 기존 worker는 central
  이 같은 reconcile 로 재생성한다(수동으로 하려면 UI stop→start 또는
  `docker rm -f jad-worker-<user>` 후 재활성화).

### 9.1 업그레이드 — `host_deploy_dir` 제거 (기존 배포)

worker 의 bind 마운트가 전부 사라지면서 `deploy.host_deploy_dir` / env `HOST_DEPLOY_DIR`
이 **없어졌다**(INSTALL §2.2). 기존 배포의 이관 절차는 사실상 없다:

1. **시크릿을 옮기지 않는다.** `./secrets/` 는 central 이 계속 쓰는 그대로다 — 바뀐 것은
   *워커에게 전달하는 방법*뿐이다.
2. `docker compose build && docker compose up -d` 로 **central 과 이미지를 같이** 올린다
   (central·worker 는 같은 이미지다). central 이 부팅 시 낡은 워커를 감지해 재생성한다.
3. `.env` 의 `HOST_DEPLOY_DIR=`, `config.yaml` 의 `deploy.host_deploy_dir:` 는 **지워도 되고
   둬도 된다** — 아무도 읽지 않는다. `python -m app.setup validate` 가 "선언되지 않은
   항목" 경고로 남아 있음을 알려 준다.
4. 확인: `docker inspect jad-worker-<user> --format '{{json .HostConfig.Binds}}'` 가
   `null` 이면(=bind 없음) 새 방식으로 뜬 것이다. central 로그에 `주입 config
   materialize` / `주입 시크릿 materialize` 라인이 워커 부팅마다 남는다.

> ⚠️ **부분 업그레이드 금지**: 새 central + 낡은 워커 이미지 조합이면, 워커가 주입 env 를
> 해석하지 못해 config 없이 부팅한다(크래시 루프 — 조용하지 않고 로그에 바로 뜬다).
> 위 2번처럼 이미지를 함께 올리면 발생하지 않는다.
