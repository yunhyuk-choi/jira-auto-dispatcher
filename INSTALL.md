# INSTALL.md — 설치 가이드 (로컬 / 클라우드 VM / 온프렘)

> **먼저 읽어라.** 이 소프트웨어를 기동한다는 것은 **자율 에이전트에게 풀 퍼미션을
> 부여하는 데 동의**하는 것이다 — 사람 승인 없이 셸·파일 쓰기·`git push` 를 하는
> 코딩 에이전트를 헤드리스로 돌린다. 관리 UI(8787)와 worker 는 **RCE 표면**이므로
> **인터넷에 노출하면 안 된다.** 무엇에 동의하는지의 정본은 [SECURITY.md](SECURITY.md).
>
> **⚠️ 이 문서는 "지금 실제로 동작하는 수동 절차"만 기술한다.** `config.example.yaml` 을
> 복사해 손으로 채우는 절차는 그대로 유효하며, 그 위에 **설정 관문 CLI**
> (`python -m app.setup` — 검증 / 생성 / 실측 진단, [§2.5](#25-설정-관문-cli--python--m-appsetup))가 있다.
> **대화형 온보딩 마법사는 아직 없다** — 값을 캐묻는 대화형 진행은 후속이고, 그때도
> 판정(검증·산출·진단)은 이 CLI 가 한다.

---

## 0. 어느 갈래인가

| 갈래 | 쓰는 경우 | 본문 |
|---|---|---|
| **A. 로컬** | 개인 노트북에서 평가·개발. 도커 소켓 직결. | §3 |
| **B. 클라우드 VM** | AWS EC2·GCE 등. ⚠️ **인바운드 전면 차단 + SSH 터널/VPN 필수** | §4 |
| **C. 온프렘 서버** | 사설망 리눅스 서버 상시 운영 | §5 → [DEPLOY.md](DEPLOY.md) |

§1(준비물)·§2(설정)·§6(검증)은 **세 갈래 공통**이다.

---

## 1. 준비물

### 1.1 호스트

- **Docker Engine + Docker Compose v2**(`docker compose`). 로컬이면 Docker Desktop 도 된다.
- 메모리: worker 1개당 `spawn.mem_limit`(기본 4g)가 상한이다. 사용자 수 × 4g + central
  여유를 확보한다. `admission.min_free_mem_mb`(기본 1536MB) 미만이면 dispatch 가 큐잉된다.
- ⚠️ **전용 호스트 권장** — central 의 docker 접근은 실효 호스트 root 동치다(SECURITY.md §4).

### 1.2 자격증명·URL (미리 손에 쥐고 시작한다)

| 준비물 | 어디서 | 어디에 넣나 |
|---|---|---|
| **Jira Cloud 사이트 URL** | `https://<your-org>.atlassian.net` (⚠️ **Cloud 전용** — Server/DC 미지원) | `jira.base_url` |
| **Jira 프로젝트 키** | 감시할 프로젝트 | `jira.project` |
| **Jira watcher 토큰** | Atlassian 계정 → API 토큰 발급 | 파일 `secrets/service/jira-token` ← `jira.watcher_token_file` |
| **forge 토큰(서비스용)** | GitLab PAT 또는 GitHub PAT | 파일 `secrets/service/forge-token` ← `forge.token_ref` |
| **dlc-meta 레포 원격 URL** | 당신이 만든 레포(비어 있어도 된다) | `run.dlc_meta_repo_url` |
| **오케스트레이터 프레임워크 레포 URL** | 공개 프레임워크 | `run.orchestrator_repo_url` |
| **설계 문서 레포 URL** *(선택)* | 없으면 비운다 → 조용히 skip | `run.docs_repo_url` |
| **worker 공유 시크릿** | `openssl rand -hex 24` 로 즉석 생성 | `.env` 의 `WORKER_SHARED_SECRET` |
| **사용자별 `claude setup-token`** | 각 사용자가 자기 PC 에서 `claude setup-token`(Max 구독) | **관리 UI 온보딩 폼**(설치 시점 아님) |
| **사용자별 Jira accountId·토큰·forge 토큰** | 각 사용자 | **관리 UI 온보딩 폼** |

> **per-user 자격증명은 설치 단계에서 만들지 않는다.** central 이 뜬 뒤 관리 UI 온보딩
> 폼으로 받아 `secrets/<username>/` 에 0600 으로 저장한다.

---

## 2. 공통 설정 — `config.yaml` 만들기

```bash
git clone <이 저장소> jira-auto-dispatcher
cd jira-auto-dispatcher
cp config/config.example.yaml config/config.yaml
```

`config/config.yaml` 은 gitignore 되고 이미지에도 굽지 않는다(compose 가 `/app/config:ro`
로 마운트). **시크릿 "값"은 절대 넣지 않는다 — 파일 참조만 둔다.**

### 2.1 반드시 손대는 것

```yaml
consent:
  full_permissions: true          # ← §0 경고를 읽고 이해했다는 명시 동의(기본 false)
  accepted_at: "2026-01-01T09:00:00+09:00"

deploy:
  profile: local | cloud_vm | onprem_server    # ← §2.2 표

jira:
  base_url: https://<your-org>.atlassian.net
  project: <PROJECT_KEY>
  trigger_statuses: ["해야 할 일"]   # ← 당신 워크플로우의 상태 **이름**
  cancel_statuses: ["취소됨"]

forge:
  kind: gitlab                    # gitlab | github — 토큰 URL·MR/PR 용어가 여기서 갈린다
  base_url: ""                    # self-hosted(사내 GitLab·GHE)면 그 base URL

run:
  orchestrator_repo_url: <프레임워크 레포 URL>
  dlc_meta_repo_url: <당신의 dlc-meta 레포 URL>
  docs_repo_url: ""               # 선택 — 비우면 skip
```

> Jira 커스텀필드 id·완료 전이 id 는 **인스턴스마다 다르다**. 알아내는 방법(`/rest/api/3/field`
> 조회 등)은 `config/config.example.yaml` 의 `jira:` 절 주석에 있다 — 여기 복제하지 않는다.

### 2.2 ⚠️ 갈래마다 달라지는 4개 값 (틀리면 worker 마운트가 조용히 깨진다)

`deploy.profile` 하나만 고르면 나머지가 파생되지만(`app/setup_schema.PROFILE_DEFAULTS`),
**`host_deploy_dir` 만은 자동으로 알 수 없어 직접 채워야 한다.**

| 키 | A. 로컬 | B. 클라우드 VM | C. 온프렘 |
|---|---|---|---|
| `deploy.profile` | `local` | `cloud_vm` | `onprem_server` |
| `deploy.docker_host` | `unix:///var/run/docker.sock` | `tcp://socket-proxy:2375` | `tcp://socket-proxy:2375` |
| `deploy.secrets_base_dir` | `""`(→ env `SECRETS_DIR`) | `/run/secrets` | `/run/secrets` |
| `deploy.workspace_volume` | `jad-workspace` | `jad-workspace` | `jad-workspace` |
| `deploy.host_deploy_dir` | **compose 로 띄우면 필수** | **필수** | **필수** |

> 세 갈래 모두 `host_deploy_dir` 은 **이 리포 디렉토리의 호스트 절대경로**다(central
> 컨테이너 내부 경로가 아니다). 비워 두면 "호스트 = central 파일시스템" 을 전제한 폴백으로
> 내려가는데(경고 로그가 남는다), **compose 로 central 을 컨테이너에 띄우는 순간 그 전제가
> 깨진다** — central 안의 `/run/secrets` 는 호스트에 없는 경로라 worker 바인드가 조용히
> 어긋난다. `docker compose` 를 쓰면 로컬이라도 채워라(`.env` 에 `HOST_DEPLOY_DIR=$PWD`).

**왜 `host_deploy_dir` 이 필요한가.** central 이 socket-proxy(또는 docker.sock)를 통해
worker 를 띄우면 그 컨테이너는 **형제(sibling)** 로 생성되고, 바인드 마운트의 source 는
central 컨테이너 내부가 아니라 **호스트 docker 데몬**이 해석한다. 그래서 worker 의
config·시크릿 마운트 source 는 반드시 **호스트 경로**여야 한다. 여기가 틀리면 worker 는
뜨지만 설정/토큰을 못 읽어 **조용히** 실패한다.

- env `HOST_DEPLOY_DIR` 로 주입해도 된다(compose 가 `.env` 를 자동 로드; env 가 우선).
- `workspace_volume` 은 **named 볼륨**이라 `host_deploy_dir` 과 무관하게 이름으로 해석된다
  — central·모든 worker 가 이 볼륨 하나를 공유해 레포를 **한 벌만** 클론한다.
- 레거시 키(`spawn.docker_host`·`spawn.host_deploy_dir`·`spawn.workspace_volume`·
  `secrets.base_dir`)도 계속 읽으므로 기존 `config.yaml` 은 그대로 둬도 동작한다.

### 2.3 호스트 OS 별 표기 (리눅스 / 맥 / 윈도우)

코드에는 플랫폼 분기가 없다 — 컨테이너 안은 항상 리눅스다. **다만 호스트 경로를 적는
두 값**(`deploy.host_deploy_dir`, `deploy.secrets_base_dir`)은 호스트 OS 표기를 따른다.

| 호스트 | `host_deploy_dir` 예 | 비고 |
|---|---|---|
| 리눅스 | `/opt/jira-auto-dispatcher` | 그대로 |
| 맥 | `/Users/<you>/jira-auto-dispatcher` | Docker Desktop 파일 공유 대상 경로여야 한다 |
| 윈도우(Docker Desktop) | `/c/Users/<you>/jira-auto-dispatcher` 또는 `C:/Users/<you>/jira-auto-dispatcher` | **슬래시(`/`)를 쓴다.** 백슬래시(`C:\...`)는 데몬이 해석하지 못한다 |
| 윈도우(WSL2 안에서 clone) | `/home/<you>/jira-auto-dispatcher` | WSL 파일시스템에 두는 편이 성능·권한 모두 낫다(권장) |

- `secrets_base_dir` 도 같은 규칙이다. 컨테이너 배포(갈래 B·C)면 이 값은 **컨테이너 경로**
  (`/run/secrets`)이므로 호스트 OS 와 무관하다.
- 시크릿 파일 권한 하드닝(`chmod 600`)은 리눅스/맥에서만 의미가 있다. 윈도우에서는 chmod 가
  무시되므로(코드도 `OSError` 를 무해하게 넘긴다) NTFS ACL 로 별도 보호해야 한다 —
  **윈도우 호스트는 개인 평가용으로만 쓰는 것을 권한다.**
- 텍스트 파일은 전부 **LF**로 정규화된다(`.gitattributes`). 윈도우에서 편집기 설정으로
  CRLF 를 강제하면 컨테이너 안 셸 스크립트가 깨질 수 있다.

### 2.4 알림 (선택 — 기본 꺼짐)

기본은 `notifier.provider: none` 이고, 알림 없이도 시스템은 완전히 동작한다.
지원 provider 는 `none | google_chat | slack | generic_webhook`.

**provider 별 웹훅 URL 획득 절차·페이로드 모양·멘션 문법은
`config/config.example.yaml` 의 `notifier:` 절 주석이 단일 원천**이다 — 여기에 복제하지
않는다(복제하면 갈라진다). 요지만:

- 웹훅 URL 은 **값이 아니라 파일 참조**다 → `notifier.webhook_ref`(기본
  `service/notifier-webhook`)가 가리키는 파일에 0600 으로 저장한다.
- 담당자 @멘션 id 는 config 가 아니라 **레지스트리의 사용자별 `notify_user_id`** 다
  (모양은 provider 마다 다르다 — `config/registry.example.json` 참조).
- 목록에 없는 provider 이름을 넣으면 **아무것도 발송하지 않고 경고만** 남긴다(오발송 방지).

### 2.5 설정 관문 CLI — `python -m app.setup`

손으로 채운 `config.yaml` 은 **아무도 검사해 주지 않는다** — 필수 항목을 빠뜨려도, 풀
퍼미션 동의를 켜지 않아도 부팅은 된다(경고만 남는다). 그 자리를 이 명령이 메운다.
종료코드가 곧 게이트다: **0 통과 / 1 게이트 실패 / 2 사용 오류.**

| 서브커맨드 | 하는 일 | 입력 → 출력 |
|---|---|---|
| `validate` | 수집한 답변을 `app/setup_schema.py` 선언에 대고 검증(누락·조건부 필수·허용값·타입·시크릿 값 혼입을 **전부 모아서** 보고) | 답변 JSON(파일 또는 stdin) → 사람용 출력 / `--json` |
| `render` | 검증을 통과한 답변으로 `config.yaml` 생성. **`config/config.example.yaml` 을 템플릿으로 써서 주석(=이 안내)을 그대로 물려준다** | 답변 JSON → `config/config.yaml`(`-o` 로 변경, 기존 파일은 `--force` + 자동 `.bak-*` 백업) |
| `doctor` | 그 설정으로 **실제로 붙는지** 실측: Jira 자격·JQL 경로, forge 토큰 권한, dlc-meta 원격 도달성, docker 접속, 알림 웹훅 참조, `host_deploy_dir` 함정 | `config/config.yaml` → 검사별 PASS/FAIL/WARN/SKIP + 고치는 법 |

```bash
cat > answers.json <<'JSON'
{
  "consent": {"full_permissions": true, "accepted_at": "2026-01-01T09:00:00+09:00"},
  "deploy":  {"profile": "cloud_vm", "host_deploy_dir": "/home/you/jira-auto-dispatcher",
              "secrets_base_dir": "/run/secrets"},
  "forge":   {"kind": "gitlab", "token_ref": "service/forge-token"},
  "jira":    {"base_url": "https://your-org.atlassian.net", "project": "PROJ",
              "trigger_statuses": ["해야 할 일"],
              "watcher_token_file": "service/jira-token",
              "watcher_email": "bot@your-org.example"},
  "notifier": {"provider": "none"}
}
JSON

python -m app.setup validate answers.json          # 통과 못 하면 non-zero
python -m app.setup render   answers.json          # → config/config.yaml (주석 보존)
python -m app.setup doctor                         # 실측 진단
```

- `render` 는 **답한 항목만** 쓴다 — 답하지 않은 값은 예시 파일의 값과 `deploy.profile`
  파생에 맡긴다(§2.2). 대신 "답하지 않아 예시 값이 남은 항목"과 아직 남은 `<...>`
  자리표시자를 출력에 나열하므로, 그 목록은 눈으로 확인한다.
- 시크릿은 **값이 아니라 참조**다. 참조 자리에 토큰·웹훅 URL 을 넣으면 `validate` 가
  막는다(그 값을 출력에 싣지 않는다).
- `doctor` 는 기본적으로 **알림을 발송하지 않는다.** 실제 발송까지 시험하려면
  `--send-test-notification`(팀 채널에 메시지가 남는다).
- 일부만 돌리려면 `--only`: `config, secrets, host_deploy_dir, jira_auth, jira_search,
  forge_token, dlc_meta, docker, notifier`.
- ⚠️ **호스트에서 돌릴 때와 컨테이너 안에서 돌릴 때 보이는 것이 다르다.** `/run/secrets`
  나 `tcp://socket-proxy:2375` 는 컨테이너 관점이라, 호스트에서는 해당 검사가 `SKIP` 으로
  나오고 안내가 붙는다. 기동 후에는 안에서 한 번 더 돌린다:
  ```bash
  docker compose exec central python -m app.setup doctor
  ```
- 이 CLI 는 **얇은 껍데기**다 — 검증·렌더·진단 로직은 `app/setup_validate.py` ·
  `app/setup_render.py` · `app/setup_doctor.py` 에 있고, 후속 웹 온보딩·대화형 에이전트가
  **같은 함수**를 재사용한다(게이트가 두 벌이 되면 반드시 갈라진다).

---

## 3. 갈래 A — 로컬 (개인/평가용)

가장 단순하다. docker.sock 을 직결하므로 **평가 목적에 한정**한다(호스트 root 동치).

```bash
# (1) 설정
cp config/config.example.yaml config/config.yaml
#     → deploy.profile: local / deploy.docker_host: unix:///var/run/docker.sock
#     → docker-compose.yml 의 socket-proxy 서비스·depends_on 을 주석 처리하고
#       central volumes 에 - /var/run/docker.sock:/var/run/docker.sock 한 줄을 추가한다
#       (compose 파일 안 "(대안) docker.sock 직결" 주석 참조)

# (2) 시크릿 — 값이 아니라 파일로
mkdir -p secrets/service
printf '%s' '<JIRA_API_TOKEN>'  > secrets/service/jira-token
printf '%s' '<FORGE_PAT>'       > secrets/service/forge-token
chmod -R go-rwx secrets && find secrets -type f -exec chmod 600 {} \;

# (3) 호스트 경로 + worker 공유 시크릿(dispatch HTTP 인증) + central 자기 claude 토큰
printf 'HOST_DEPLOY_DIR=%s\n' "$PWD" > .env    # ← compose 로 띄우면 로컬도 필요(§2.2)
printf 'WORKER_SHARED_SECRET=%s\n' "$(openssl rand -hex 24)" >> .env
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' '<claude setup-token 값>' >> .env
chmod 600 .env

# (4) 기동
docker compose up -d
docker compose logs -f central
```

- 관리 UI: `http://localhost:8787`. **포트를 다른 기기에 노출하지 말 것.**
- 로컬은 리소스가 곧 병목이다 — 사용자 1명(worker 1개, 4g)으로 시작해 본다.

---

## 4. 갈래 B — 클라우드 VM (AWS EC2 등)

> ## ⚠️ 여기서 실수하면 인터넷에 RCE 를 열어 두게 된다
>
> worker 는 승인 없이 임의 명령을 실행하는 에이전트이고, 관리 UI 는 그 worker 를 만드는
> 콘솔이다. **8787 을 인터넷에 여는 것은 공개 원격 셸을 여는 것과 같다.**

### 4.1 네트워크 (먼저 한다 — 기동 전에)

1. **보안그룹 인바운드를 전부 차단**한다. 8787 은 **어떤 소스에도 열지 않는다**
   (`0.0.0.0/0` 은 물론 "우리 사무실 IP" 도 열지 않는 것을 기본으로 한다).
2. SSH(22)만 **당신의 IP 또는 사설망**으로 제한해 연다. 가능하면 SSH 대신
   **AWS SSM Session Manager** 같은 에이전트 기반 접근을 쓴다(인바운드 0).
3. 관리 UI 는 **SSH 터널**로만 접근한다:
   ```bash
   ssh -L 8787:localhost:8787 <user>@<vm-host>     # 로컬 http://localhost:8787
   ```
   VPN 안이라면 VPN 사설 IP 로 바인딩해도 된다. **퍼블릭 IP 바인딩은 금지.**
4. 아웃바운드는 필요하다 — Jira Cloud, forge(GitLab/GitHub), `api.anthropic.com`.
5. 웹훅(`webhook.enabled: true`)을 꼭 써야 하면, 리버스 프록시(TLS)로 `webhook.path`
   **하나만** 노출하고 출처를 Jira IP 로 제한한다. **8787 은 절대 함께 열지 않는다.**
   폴링만 써도 기능은 동일하므로, 확신이 없으면 웹훅을 켜지 말고 폴링으로 간다.

### 4.2 기동

socket-proxy 경유가 **기본**이다(docker.sock 직결 금지 — compose 를 손대지 않는다).

```bash
# VM 에서
git clone <이 저장소> ~/jira-auto-dispatcher && cd ~/jira-auto-dispatcher
cp config/config.example.yaml config/config.yaml   # → §2 대로 채운다

# ⚠️ host_deploy_dir 필수 — 이 디렉토리의 호스트 절대경로
printf 'HOST_DEPLOY_DIR=%s\n' "$PWD" > .env
printf 'WORKER_SHARED_SECRET=%s\n' "$(openssl rand -hex 24)" >> .env
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' '<claude setup-token 값>' >> .env
chmod 600 .env

mkdir -p secrets/service
printf '%s' '<JIRA_API_TOKEN>' > secrets/service/jira-token
printf '%s' '<FORGE_PAT>'      > secrets/service/forge-token
chmod -R go-rwx secrets && find secrets -type f -exec chmod 600 {} \;

docker compose up -d
```

- 인스턴스 타입: 사용자 1명당 4g 를 잡는다. `admission` 이 자원 압박 시 dispatch 를
  큐잉하므로 터지지는 않지만, 작으면 그냥 느려진다.
- 디스크: 공유 워크스페이스 볼륨(`jad-workspace`)에 대상 레포들이 클론된다. 여유를 둔다.
- ⚠️ 이 VM 을 다른 서비스와 공유하지 않는다(SECURITY.md §4).

---

## 5. 갈래 C — 온프렘 서버

절차가 길어 별도 문서로 분리했다 — **[DEPLOY.md](DEPLOY.md)** 를 따른다.
거기서 다루는 것: 이미지 빌드/전송(rsync 또는 tar), `config.yaml`·시크릿 배치,
인바운드 정책, `docker compose up -d`, 공유 워크스페이스, 온보딩→worker spawn,
검증 체크리스트, 운영 명령, 재배포(`deploy/redeploy-central.sh` 무중단 교체).

§1·§2 의 준비물·설정은 그대로 적용된다.

---

## 6. 첫 기동 검증 (공통)

```bash
# (0) 기동 **전**: 설정 실측 진단(§2.5). 여기서 잡히는 것이 로그를 뒤지는 것보다 싸다
python -m app.setup doctor

# (1) 헬스 — {"status":"ok","role":"central"}
curl -fsS http://localhost:8787/healthz

# (2) 스모크(읽기 전용: /healthz + /api/users + /api/jobs + 컨테이너 상태)
bash scripts/smoke-deployed.sh http://localhost:8787

# (3) 부팅 로그 — 설정 로드·폴러/워처/스케줄러 스레드 기동
docker compose logs --tail=100 central

# (4) 기본 구성(socket-proxy)이면 central 에 docker.sock 바인드가 없어야 한다
docker inspect jad-central --format '{{json .Mounts}}'
```

기대: `smoke-deployed.sh` 종료코드 0, `docker ps` 에 `jad-central`(+기본 구성이면
`jad-socket-proxy`)이 Up. worker 는 아직 없다 — 온보딩 후에 생긴다.

**부팅 로그에 `consent.full_permissions 가 설정되지 않았습니다` 경고가 보이면**
§2.1 의 동의 값을 아직 안 켠 것이다. 부팅은 되지만 그대로 운영하지 말라
(`python -m app.setup doctor --only config` 가 같은 것을 실패로 잡는다).

기동 뒤에는 **컨테이너 안에서** 한 번 더 진단한다 — `/run/secrets`·`socket-proxy` 처럼
컨테이너 관점의 값들은 호스트에서 판정할 수 없어 그때만 `SKIP` 이 아닌 진짜 결과가 나온다:

```bash
docker compose exec central python -m app.setup doctor
```

### 6.1 사용자 온보딩 → worker 뜨기

관리 UI(`http://localhost:8787`)에서 사용자를 등록한다. 필요한 값과 활성화 절차는
[DEPLOY.md §6](DEPLOY.md) 에 표로 있다(세 갈래 공통). 요지:

1. 온보딩 폼 제출 → `secrets/<username>/` 에 0600 저장 + 레지스트리에 **참조만** 기록.
   안전 기본은 `enabled=false` 다.
2. UI 에서 enable → central 이 `jad-worker-<username>` 컨테이너를 비-root 로 spawn.
3. `docker ps --filter name=jad-worker-` 로 Up 확인.

### 6.2 실제 티켓으로 끝까지

배포가 살아 있는지가 아니라 **플로우가 도는지**를 검증하려면 [E2E.md](E2E.md) 를 따른다
(티켓 생성 → 감지 → dispatch → 실행 → 브랜치/MR → 완료 회신, 각 단계 기대결과·실패 로그 포함).

---

## 7. 자주 틀리는 것

> 아래 대부분은 `python -m app.setup doctor`(§2.5)가 **증상이 나기 전에** 잡는다 —
> 로그를 뒤지기 전에 먼저 돌려 본다.

| 증상 | 원인 | 잡는 검사 |
|---|---|---|
| worker 가 뜨는데 설정/토큰을 못 읽음 | `deploy.host_deploy_dir` 이 비었거나 **컨테이너 내부 경로**로 적혔다(§2.2) | `--only host_deploy_dir` |
| dlc-meta pull/push 가 안 됨 | 사설 GitLab 을 이 호스트에서 열 수 없거나 토큰 권한 부족 | `--only dlc_meta` |
| 시크릿 파일을 못 읽음 | 참조 경로 오타 · 파일 부재 · 값을 config 에 직접 적음 | `--only secrets` (+ `validate`) |
| 온보딩이 `Read-only file system` 500 | central 의 `./secrets` 마운트를 `:ro` 로 바꿨다 — central 은 **rw** 여야 한다 | — |
| 티켓이 감지되지 않음 | `jira.project`·`jira.trigger_statuses`(상태 **이름**)·assignee accountId 매핑 중 하나. 폴링 주기(기본 60s)도 기다렸는지 | `--only jira_search` (프로젝트 키까지) |
| "티켓 없음"처럼 조용히 넘어감 대신 에러 | Jira **Server/DC** 를 가리켰다 — Cloud 전용이다 | `--only jira_auth,jira_search` |
| 잡이 running 으로 안 넘어감 | 자원 어드미션이 큐잉 중일 수 있다(`admission.min_free_mem_mb`·`max_load_per_core`) | — |
| MR 작성자가 엉뚱한 사용자 | worker 가 앰비언트 자격증명으로 push 했다 — per-user forge 토큰 경로를 확인 | — |
| 알림이 안 감 | `notifier.provider` 가 `none` 이거나 목록 밖 값(경고만 남기고 미발송) | `--only notifier` |
| worker 를 못 띄움 | docker 엔드포인트 접속 실패(소켓 권한 · socket-proxy 미기동) | `--only docker` |
