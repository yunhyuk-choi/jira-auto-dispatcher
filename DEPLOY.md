# DEPLOY.md — jira-auto-dispatcher 개발서버 배포 절차

> 운영자가 **순서대로 따라 실행**하는 배포 런북이다. 대상은 사내 개발서버이며,
> 이 시스템은 도구 권한을 가진 자율 에이전트(RCE 표면)를 헤드리스로 돌린다 —
> **반드시 사내망 한정**, 외부 노출 금지. 보안 인가·통제의 정본은
> [SECURITY.md](SECURITY.md) 다(배포 전 필독).

---

## 0. 대상·전제

| 항목 | 값 |
|---|---|
| 개발서버 | **<DEV_SERVER_HOST>** |
| SSH 계정 | `<deploy-user>` |
| 필요 런타임 | Docker Engine + Docker Compose v2 (`docker compose`) |
| 이미지 | `jira-auto-dispatcher:latest` (central·worker 공용 단일 이미지) |
| 네트워크 | `jad-net` (compose가 생성; 동적 worker가 이름으로 합류) |
| 관리 UI 포트 | `8787` (⚠️ 사내망 한정) |

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
  --exclude 'config/config.yaml' \
  ./ <deploy-user>@<DEV_SERVER_HOST>:/opt/jira-auto-dispatcher/

# 서버에서 빌드
ssh <deploy-user>@<DEV_SERVER_HOST>
cd /opt/jira-auto-dispatcher
docker build -t jira-auto-dispatcher:latest .
docker run --rm --entrypoint claude jira-auto-dispatcher:latest --version   # 레이어 검증
```

### (B) 로컬 빌드 → tar 전송 (서버 빌드 불가 시)

```bash
docker build -t jira-auto-dispatcher:latest .
docker save jira-auto-dispatcher:latest | gzip > jad.tar.gz
scp jad.tar.gz <deploy-user>@<DEV_SERVER_HOST>:/opt/jira-auto-dispatcher/
ssh <deploy-user>@<DEV_SERVER_HOST> 'gunzip -c /opt/jira-auto-dispatcher/jad.tar.gz | docker load'
```

> ⚠️ `docker push`/레지스트리 사용은 이 런북 범위 밖(사내 정책 따름). 여기서는
> 로컬 빌드/전송만 다룬다.

---

## 2. config.yaml 준비 (시크릿 값 아님)

`config/config.example.yaml` → `config/config.yaml` 로 복사해 채운다. 시크릿 **값**은
넣지 않는다(파일 참조만).

```bash
cd /opt/jira-auto-dispatcher
cp config/config.example.yaml config/config.yaml
```

**socket-proxy를 쓰는 기본 구성이면 반드시** `spawn.docker_host` 를 프록시로 맞춘다:

```yaml
spawn:
  image: jira-auto-dispatcher:latest
  network: jad-net
  central_url: http://central:8787
  docker_host: tcp://socket-proxy:2375   # ← socket-proxy 기본 구성. (직결 시 unix:///var/run/docker.sock)
  run_as: "1000:1000"
```

`config/config.yaml`은 compose가 `/app/config:ro` 로 마운트한다(gitignore·이미지 미포함).

---

## 3. 시크릿 배치 (`secrets.base_dir`)

컨테이너는 `SECRETS_DIR=/run/secrets` 로 주입되고, 호스트 `./secrets/` 를 read-only로
마운트한다. 여기에 **service 시크릿**(central 공용)과 **per-user 시크릿**을 파일로 둔다.

```bash
cd /opt/jira-auto-dispatcher
mkdir -p secrets/service

# (a) central Jira watcher 토큰 (config.jira.watcher_token_file = service/jira-token)
printf '%s' 'ATLASSIAN_API_TOKEN_값' > secrets/service/jira-token

# (b) 웹훅을 쓸 때만 — 웹훅 공유 시크릿 (config.webhook.shared_secret_file)
# printf '%s' '웹훅시크릿' > secrets/service/webhook-secret

# 권한 하드닝 — 시크릿은 소유자만.
chmod -R go-rwx secrets
find secrets -type f -exec chmod 600 {} \;
```

- **worker 공유시크릿**(`WORKER_SHARED_SECRET`, dispatch HTTP 인증)은 파일이 아니라
  **env**로 주입한다. `.env` 에 두거나 셸 env로 export 한다:

  ```bash
  # /opt/jira-auto-dispatcher/.env  (compose가 자동 로드; 0600 권장)
  echo "WORKER_SHARED_SECRET=$(openssl rand -hex 24)" > .env
  chmod 600 .env
  ```

- **per-user 시크릿**(각자 Jira/GitLab 토큰·claude setup-token)은 여기서 손으로 만들지
  않는다 — **온보딩(5단계)**에서 관리 UI가 `secrets/<username>/` 하위에 0600으로 기록한다.

---

## 4. 인바운드 포트 (웹훅 쓸 때만)

- **폴링만** 쓰면(`webhook.enabled: false`, 기본) central이 Jira로 **아웃바운드**만 하므로
  인바운드 개방 **불필요**. 관리 UI(8787)는 사내망에서만 접근한다(SSH 터널 권장).
- **웹훅**을 켜면(`webhook.enabled: true`) Jira → central 로의 인바운드가 필요하다.
  사내 리버스 프록시(TLS)를 앞단에 두고 `webhook.path` 만 노출, 나머지(8787 관리 UI)는
  절대 외부 노출하지 않는다. 방화벽에서 출처를 Jira IP로 제한한다.

관리 UI 접근은 포트를 여는 대신 SSH 터널을 권장:

```bash
ssh -L 8787:localhost:8787 <deploy-user>@<DEV_SERVER_HOST>   # 로컬 http://localhost:8787
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

---

## 6. 사용자 온보딩 → worker 동적 spawn

관리 UI(`http://localhost:8787`, SSH 터널)에서 사용자마다 자격증명을 입력한다. central이
시크릿을 `secrets/<username>/` 에 0600으로 저장하고 레지스트리에 참조만 남긴 뒤
(`enabled=false` 안전 기본), **활성화 시** Docker SDK로 worker 컨테이너를 동적 spawn 한다.

온보딩 폼 필수 필드(`app/onboarding.py`):

| 필드 | 내용 |
|---|---|
| `username` | 내부 식별자(컨테이너·볼륨·시크릿 경로 키) |
| `jira_account_id` | 담당자 매핑 키(티켓 assignee accountId) |
| `jira_email` | Jira actor 이메일(Basic auth) |
| `jira_token` | 사용자 Jira API 토큰 → `secrets/<user>/jira-token` |
| `claude_setup_token` | `claude setup-token` 발급 값 → `secrets/<user>/claude-oauth-token` |
| (선택) `gitlab_token` | MR 생성용 → `secrets/<user>/gitlab-token` |
| (선택) `git_name`/`git_email` | 커밋 author 귀속 |

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

1. Jira(<PROJECT_KEY>)에서 테스트 티켓을 만들어 온보딩된 사용자에게 할당하고 매칭 상태
   (`config.match.statuses`, 예: `해야 할 일`)로 둔다.
2. central 로그에서 감지→claim→dispatch, 해당 worker 로그에서 잡 수신→`claude -p` 실행 확인.
3. 산출물 검증: 사용자 정체성의 브랜치(`auto/<TICKET>`) + MR 생성(사용자 GitLab 토큰).
4. 티켓/잡 상태가 `done`(또는 한도 시 `interrupted`+`reset_at`)로 회신되는지 확인.

---

## 8. 보안 (요약 — 정본은 SECURITY.md)

- **사내망 한정**: 관리 UI(8787)·worker는 외부 노출 금지. UI 접근은 SSH 터널 권장.
- **socket-proxy 권장**: central은 docker.sock 직결 대신 프록시로 최소 권한
  (CONTAINERS/IMAGES/NETWORKS/VOLUMES + POST)만 사용. 직결은 호스트 root 동치.
- **비-root**: 이미지는 uid 1000(app)으로 실행하고 worker도 `run_as: 1000:1000`.
- **시크릿**: 값은 `secrets/` 에 0600, read-only 마운트. 이미지에 굽지 않는다(.dockerignore).
  Jira/GitLab 토큰은 worker에 **파일 경로**로만 넘긴다(값은 claude setup-token만 env 주입).
- **가역성/폭주 방지**: dedup 게이트 + `concurrency_per_worker: 1` + 결정적 브랜치
  (`auto/<TICKET>`) + MR 게이트(사람 리뷰).

전체 위협 모델·자율 실행 인가 근거는 **[SECURITY.md](SECURITY.md)** 참조.

---

## 9. 운영 (참고)

```bash
docker compose restart central          # 설정 변경 반영(config.yaml 수정 후)
docker compose down                     # central+socket-proxy 중지(worker는 별도)
docker ps --filter name=jad-worker-     # 동적 worker 목록
docker stop jad-worker-<user>           # 개별 worker 중지(또는 UI disable)
```

- config.yaml·시크릿 변경 후에는 `docker compose restart central`. worker 토큰 갱신은
  6단계의 재기동 절차를 따른다.
- 이미지 갱신(재빌드) 시 `docker compose up -d --build central` 후, 기존 worker는
  UI에서 stop→start(또는 `docker rm -f jad-worker-<user>` 후 재활성화)로 새 이미지 반영.
