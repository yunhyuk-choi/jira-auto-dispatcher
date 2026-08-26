# INSTALL.md — 설치 가이드 (로컬 / 클라우드 VM / 온프렘)

> **먼저 읽어라.** 이 소프트웨어를 기동한다는 것은 **자율 에이전트에게 풀 퍼미션을
> 부여하는 데 동의**하는 것이다 — 사람 승인 없이 셸·파일 쓰기·`git push` 를 하는
> 코딩 에이전트를 헤드리스로 돌린다. 관리 UI(8787)와 worker 는 **RCE 표면**이므로
> **인터넷에 노출하면 안 된다.** 무엇에 동의하는지의 정본은 [SECURITY.md](SECURITY.md).
>
> **⚠️ 이 문서는 "지금 실제로 동작하는 수동 절차"를 기술한다.** `config.example.yaml` 을
> 복사해 손으로 채우는 절차는 그대로 유효하며, 그 위에 **설정 관문 CLI**
> (`python -m app.setup` — 검증 / 생성 / 실측 진단, [§2.5](#25-설정-관문-cli--python--m-appsetup))가 있다.
>
> **가장 쉬운 길은 `python -m app.setup wizard`** ([§2.6](#26-대화형-마법사--python--m-appsetup-wizard))
> 다 — 대화로 값을 묻고 시크릿 파일을 0600 으로 만들고 위 CLI 를 순서대로 태운다.
> 마법사는 **편의지 유일 경로가 아니다**: 판정(검증·산출·진단)은 여전히 이 CLI 가 하고,
> 아래 수동 절차는 언제든 그대로 쓸 수 있다.

---

## 0. 어느 갈래인가

> **당신이 설치자가 아니라 이미 도는 인스턴스에 합류하는 팀원이라면** — §1~§7 을 읽을 필요가
> 없다. [§8 팀원 합류](#8-팀원-합류-이미-도는-인스턴스에-참여하는-사람) 하나만 보면 된다
> (로컬 프레임워크 합류 + 웹 자격증명 등록의 2단).

| 갈래 | 쓰는 경우 | 본문 |
|---|---|---|
| **A. 로컬** | 개인 노트북에서 평가·개발(Docker Desktop 포함). | §3 |
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
| **Jira 프로젝트 키** | 감시할 대표 프로젝트(여러 개면 `jira.projects` 에 나머지) | `jira.project` |
| **Jira watcher 토큰** | Atlassian 계정 → API 토큰 발급 | 파일 `secrets/service/jira-token` ← `jira.watcher_token_file` |
| **forge 토큰(서비스용)** | GitLab PAT 또는 GitHub PAT | 파일 `secrets/service/forge-token` ← `forge.token_ref` |
| **dlc-meta 레포 원격 URL** | 당신이 만든 레포(비어 있어도 된다). ⚠️ **손으로 적지 않는다** — 설치 관문이 그 클론의 `origin` 에서 읽어 채운다(§2.5) | `run.dlc_meta_repo_url` |
| **오케스트레이터 프레임워크 레포 URL** | 공개 프레임워크 | `run.orchestrator_repo_url` |
| **설계 문서 레포 URL** *(선택)* | 없으면 비운다 → 조용히 skip | `run.docs_repo_url` |
| **사용자별 `claude setup-token`** | 각 사용자가 자기 PC 에서 `claude setup-token`(Max 구독) | **관리 UI 온보딩 폼**(설치 시점 아님) |
| **사용자별 Jira accountId·토큰·forge 토큰** | 각 사용자 | **관리 UI 온보딩 폼** |

> **per-user 자격증명은 설치 단계에서 만들지 않는다.** central 이 뜬 뒤 관리 UI 온보딩
> 폼으로 받아 `secrets/<username>/` 에 0600 으로 저장한다.
>
> ⚠️ 그리고 **합류자는 웹 등록만으로 끝나지 않는다** — 로컬에서 `ai-dlc-orchestrator`
> 프레임워크의 SETTER 를 합류 모드로 돌려 `dlc-meta` 를 clone 해야 그 사람의 로컬
> 오케스트레이터가 정체성을 갖는다. 2단 절차는 [§8 팀원 합류](#8-팀원-합류-이미-도는-인스턴스에-참여하는-사람).

---

## 2. 공통 설정 — `config.yaml` 만들기

```bash
git clone <이 저장소> jira-auto-dispatcher
cd jira-auto-dispatcher

python -m app.setup wizard            # 대화로 채우기(§2.6) — 아래 §2.1~§2.5 를 대신한다
# 또는 손으로:
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
  dlc_meta_repo_url: <설치 관문이 채운다 — §2.5 의 `--dlc-meta`>
  docs_repo_url: ""               # 선택 — 비우면 skip
```

> `run.dlc_meta_repo_url` 은 **묻지 않는 값**이다. 이 시스템의 설치는 `ai-dlc-orchestrator`
> 프레임워크의 SETTER 가 dlc-meta 를 만들어 원격에 push 한 **직후**에 이어지므로, 그 클론이
> 이미 로컬에 있다 — 설치 관문이 `git -C <클론> remote get-url origin` 으로 읽어 채운다(§2.5).
> 못 채우면 `validate` 가 막는다(예시 URL 을 그대로 남기지 않는다 — 그 값에서 forge
> `base_url`·`kind` 가 파생되므로 남의 조직 호스트가 남으면 사내 토큰이 그리로 나갈 수 있다).

> Jira 커스텀필드 id·완료 전이 id 는 **인스턴스마다 다르다**. 알아내는 방법(`/rest/api/3/field`
> 조회 등)은 `config/config.example.yaml` 의 `jira:` 절 주석에 있다 — 여기 복제하지 않는다.

### 2.2 갈래마다 달라지는 값 (프로파일 하나면 끝난다)

`deploy.profile` 하나만 고르면 나머지가 파생된다(`app/setup_schema.PROFILE_DEFAULTS`).
**설치자가 직접 계산해 넣어야 하는 호스트 경로는 없다.**

| 키 | A. 로컬 | B. 클라우드 VM | C. 온프렘 |
|---|---|---|---|
| `deploy.profile` | `local` | `cloud_vm` | `onprem_server` |
| `deploy.docker_host` | `tcp://socket-proxy:2375` | `tcp://socket-proxy:2375` | `tcp://socket-proxy:2375` |
| `deploy.secrets_base_dir` | `""`(→ env `SECRETS_DIR`) | `/run/secrets` | `/run/secrets` |
| `deploy.workspace_volume` | `jad-workspace` | `jad-workspace` | `jad-workspace` |

> **도커 접근은 세 갈래가 같다 — socket-proxy 경유.** 이 리포가 배포하는
> `docker-compose.yml` 은 socket-proxy 를 기본으로 선언하고 central 을 거기에 붙인다.
> 로컬(Docker Desktop 포함)에서도 **compose 를 한 글자도 고치지 않고** 그대로 뜬다.
> 소켓 직결(`unix:///var/run/docker.sock`)은 central 에 호스트 root 동치 권한을 주므로
> 기본값이 아니다 — 필요하면 §3 의 "고급" 대안으로 명시한다.
>
> `python -m app.setup render` 는 프로파일 파생값을 **실제로 `config.yaml` 에 써 넣는다.**
> 예전에는 답하지 않은 항목이 예시 파일 값(=다른 프로파일 값) 그대로 남아, 문서를 그대로
> 따랐는데 프로파일과 모순되는 설정이 나왔다.

> ⚠️ **`host_deploy_dir` 은 없어졌다.** 예전에는 "이 리포 디렉토리의 **호스트** 절대경로"를
> 설치자가 정확히 적어야 했고, 틀리면 worker 가 **에러 없이 뜬 채** 빈 디렉토리를 마운트해
> 한참 뒤 엉뚱한 실패로 나타났다. 이제 그 값이 필요한 자리가 하나도 없다(아래).
> 기존 `config.yaml`·`.env` 에 남아 있어도 **무시**되므로 지워도 되고 둬도 된다
> (`python -m app.setup validate` 가 "선언되지 않은 항목" 경고로 알려 준다).

**왜 없어도 되나.** central 이 socket-proxy(또는 docker.sock)를 통해 worker 를 띄우면 그
컨테이너는 **형제(sibling)** 로 생성되고, 바인드 마운트의 source 는 central 컨테이너 내부가
아니라 **호스트 docker 데몬**이 해석한다. 그래서 예전엔 worker 마운트에 호스트 경로가
필요했다. 지금 worker 에 거는 마운트는 **named 볼륨 2개뿐**이고(볼륨은 *이름* 으로
해석되므로 호스트 경로 개념이 없다), 나머지는 central 이 컨테이너 스펙에 실어 보낸다:

| worker 가 필요한 것 | 전달 방식 |
|---|---|
| `~/.claude`(인증/세션 영속) | named 볼륨 `jad-<user>` |
| 공유 워크스페이스(레포 한 벌) | named 볼륨 `jad-workspace` |
| `config/config.yaml` | **스폰 시 주입** → worker 가 부팅 시 `/app/config/config.yaml` 로 기록 |
| 그 사용자의 시크릿 + 사전 인가 settings | **스폰 시 주입** → worker 의 `/run/secrets`(tmpfs, RAM 전용) |
| 알림 웹훅 파일 하나 | **스폰 시 주입**(같은 채널) |

주입 계약의 단일 원천은 `app/inject.py` 다. central 은 여전히 자기 `./config` 와
`./secrets` 를 **자기 compose 마운트**로 읽는다 — 그건 compose CLI 가 호스트에서 해석하는
상대 경로라 애초에 틀릴 여지가 없다.

- 레거시 키(`spawn.docker_host`·`spawn.workspace_volume`·`secrets.base_dir`)도 계속 읽으므로
  기존 `config.yaml` 은 그대로 둬도 동작한다.

### 2.3 호스트 OS 별 표기 (리눅스 / 맥 / 윈도우)

코드에는 플랫폼 분기가 없다 — 컨테이너 안은 항상 리눅스다. 호스트 절대경로를 직접 적는
설정 항목은 이제 없다(§2.2). 남는 것은 `deploy.secrets_base_dir` 하나인데, 컨테이너 배포
(갈래 B·C)면 이 값은 **컨테이너 경로**(`/run/secrets`)라 호스트 OS 와 무관하고, 로컬 개발
(갈래 A)이면 그냥 로컬 디렉토리다(윈도우도 슬래시 표기: `C:/Users/<you>/.jad/secrets`).

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
| `discover` | **이 Jira 인스턴스에 실제로 있는 값**을 조회(커스텀필드 후보·프로젝트 상태·전이 id/name·라벨·`accountId`). 설정에 적힌 id·상태 이름이 **이 인스턴스에 실재하는지**까지 검증한다 | `config.yaml`(base_url·watcher_email·토큰만 있으면 됨) → 후보 목록 + 붙여넣을 `jira:` 블록 / `--json` |
| `validate` | 수집한 답변을 `app/setup_schema.py` 선언에 대고 검증(누락·조건부 필수·허용값·타입·시크릿 값 혼입을 **전부 모아서** 보고) | 답변 JSON(파일 또는 stdin) → 사람용 출력 / `--json` |
| `render` | 검증을 통과한 답변으로 `config.yaml` 생성. **`config/config.example.yaml` 을 템플릿으로 써서 주석(=이 안내)을 그대로 물려준다.** | 답변 JSON → `config/config.yaml`(`-o` 로 변경, 기존 파일은 `--force` + 자동 `.bak-*` 백업) |
| `doctor` | 그 설정으로 **실제로 붙는지** 실측: Jira 자격·JQL 경로, forge 토큰 권한, dlc-meta 원격 도달성, docker 접속, 알림 웹훅 참조 | `config/config.yaml` → 검사별 PASS/FAIL/WARN/SKIP + 고치는 법 |
| `skill` | (선택 · 게이트 아님) 리포가 추적하는 스킬 템플릿을 **내 로컬** `.claude/skills/` 로 펼친다 — `claude` 를 쓸 때만 의미 있다([§2.7](#27-선택-프로젝트-스킬--python--m-appsetup-skill)) | `skill-templates/` → `.claude/skills/<이름>/SKILL.md`(멱등, 덮어쓰기는 `--force`) |

```bash
cat > answers.json <<'JSON'
{
  "consent": {"full_permissions": true, "accepted_at": "2026-01-01T09:00:00+09:00"},
  "deploy":  {"profile": "cloud_vm",
              "secrets_base_dir": "/run/secrets"},
  "forge":   {"kind": "gitlab", "token_ref": "service/forge-token"},
  "jira":    {"base_url": "https://your-org.atlassian.net", "project": "PROJ",
              "trigger_statuses": ["해야 할 일"],
              "watcher_token_file": "service/jira-token",
              "watcher_email": "bot@your-org.example"},
  "notifier": {"provider": "none"},
  "webhook": {"enabled": true, "secret_ref": "service/jira-webhook"}
}
JSON
# ↑ run.dlc_meta_repo_url 이 없다 — 아래 --dlc-meta 가 채운다(사람이 적을 값이 아니다).

python -m app.setup validate answers.json --dlc-meta ../dlc-meta   # 통과 못 하면 non-zero
python -m app.setup render   answers.json --dlc-meta ../dlc-meta   # → config/config.yaml
python -m app.setup discover                       # 인스턴스 조회(아래 참조)
python -m app.setup doctor                         # 실측 진단
```

#### 자동으로 채워지는 값 — 묻지 않는다

| 값 | 어디서 오나 | 어디로 가나 |
|---|---|---|
| `run.dlc_meta_repo_url` | dlc-meta 클론의 `git remote get-url origin`. `--dlc-meta <경로>` 로 주거나 생략하면 흔한 위치(배포 디렉토리·그 상위·cwd·홈의 `dlc-meta`)를 탐색한다. env `DLC_META_DIR` 도 본다 | `config.yaml` (+ 호스트가 GitHub/GitLab 임을 스스로 밝히면 `forge.kind` 도 함께) |

> ⚠️ 예전에는 `render` 가 `.env` 에 `WORKER_SHARED_SECRET` 도 만들었다. **은퇴했다** —
> 그 값은 워커가 중앙의 dispatch HTTP 를 부를 때 쓰는 `X-Worker-Secret` 인증용이었는데,
> 그 경로가 프랙탈 seam(중앙 → `docker exec` 푸시)으로 대체되며 읽는 곳이 사라졌다.
> 옛 `.env` 에 값이 남아 있어도 무시되므로 지워도 되고 그냥 둬도 된다.

- 원격 URL 에 토큰이 박혀 있으면(`https://oauth2:glpat-…@…`) **자격정보를 떼고** 적는다.
  SSH 원격(`git@host:…`)은 https 로 바꿔 적고 그 사실을 알린다 — central 은 SSH 키가 아니라
  forge 토큰을 http(s) URL 에 실어 인증한다.
- 채우지 못하면 **예시 값을 남기지 않고 게이트가 막는다**(`validate` 가 `run.dlc_meta_repo_url`
  누락으로 실패). 그래도 누군가 손으로 예시 URL 을 되돌려 놓으면 `doctor --only config` 가
  자리표시자로 잡는다 — 그물이 두 겹이다.

#### `discover` — 값을 **손으로 옮겨 적지 않기** 위한 조회

여기가 남들이 가장 많이 막히고, 우리도 반복해서 밟은 자리다. Jira 는 화면에 보이는
**표시명**과 API 가 쓰는 **id/name** 이 어긋날 수 있고, JQL 은 name 으로 거는데 전이는
id 로 건다. 커스텀필드 id·전이 id 는 **인스턴스마다 완전히 다르다** — `customfield_10015`
는 이 코드가 처음 운영된 인스턴스의 값일 뿐이다. 그런데 `render` 결과에는 그 예시 id 가
**형식상 유효한 모양 그대로** 남아 눈으로 넘어간다. 그 결과가 조용한 오작동이다:

- 상태 이름이 어긋나면 폴러는 **에러 없이 아무 티켓도 못 찾는다**(가장 알아채기 어렵다).
- 이 인스턴스에 없는 커스텀필드를 보내면 Jira 가 400 → 착수·완료가 통째로 막힌다.

`discover` 는 `jira.base_url` · `jira.watcher_email` · `jira.watcher_token_file` 만 채운
`config.yaml` 로도 돌아간다(나머지를 채우기 **전에** 쓰라고 만든 것이다).

```bash
python -m app.setup discover                      # 전부
python -m app.setup discover --only custom_fields # 필드 후보만
python -m app.setup discover --issue PROJ-12      # 이 티켓으로 전이 실측(생략 시 최근 티켓)
python -m app.setup discover --json > jira.json   # 기계용(후속 온보딩·웹 UI가 소비)
```

- **자동 선택은 아낀다.** 커스텀필드는 이름이 *정확히* 일치하는 필드가 **유일할 때만**
  고른다. 비슷한 이름이 여럿이거나 부분 일치뿐이면 고르지 않고 **후보만** 보여준다 —
  잘못 고른 필드 id 는 검증도 진단도 통과한 뒤 운영에서 400 으로 터진다.
- **id 와 name 을 함께** 적어 준다. 출력 맨 아래의 `jira:` 블록을 그대로 붙여넣으면 된다:
  ```yaml
  jira:
    trigger_statuses: [{"id": "10000", "name": "해야 할 일"}]
    done_transition_names: [{"id": "41", "name": "완료"}]
  ```
  런타임 소비처는 예전처럼 **이름만** 보고, id 는 곁에 남아 진단이 짝을 검증한다.
  문자열 목록(`["해야 할 일"]`)도 계속 읽는다 — 기존 `config.yaml` 은 그대로 둬도 된다.
- **라벨은 조회보다 안내가 중요하다.** Jira 라벨은 미리 만들 필요가 없다(티켓에 입력하는
  순간 생성된다). 목록에 없어도 정상이며, 팀에 *"이 라벨을 붙이면 자동화가 손대지
  않는다"* 를 알리는 쪽이 훨씬 중요하다.
- `accountId` 도 함께 알려 준다 — 사용자 온보딩(레지스트리)에 필요한데 Jira UI 에서
  찾기가 은근히 어렵다.

- `render` 는 **답한 항목만** 쓴다 — 답하지 않은 값은 예시 파일의 값과 `deploy.profile`
  파생에 맡긴다(§2.2). 대신 "답하지 않아 예시 값이 남은 항목"과 아직 남은 `<...>`
  자리표시자를 출력에 나열하므로, 그 목록은 눈으로 확인한다.
- 시크릿은 **값이 아니라 참조**다. 참조 자리에 토큰·웹훅 URL 을 넣으면 `validate` 가
  막는다(그 값을 출력에 싣지 않는다).
- `doctor` 는 기본적으로 **알림을 발송하지 않는다.** 실제 발송까지 시험하려면
  `--send-test-notification`(팀 채널에 메시지가 남는다).
- 일부만 돌리려면 `--only`: `config, secrets, worker_secret, jira_auth,
  jira_search, forge_token, dlc_meta, docker, notifier`.
- ⚠️ **`forge_token` 검사는 갈 곳을 모르면 요청 자체를 하지 않는다(SKIP).** `forge.base_url`
  이 비어 있으면 설정된 레포 URL(`run.dlc_meta_repo_url` · `run.docs_repo_url`)의 호스트에서
  유도하고, 유도조차 못 하면 SaaS(gitlab.com)로 떨어지는 대신 건너뛴다 — 사내 PAT 가
  외부로 전송되는 것보다 검사를 못 하는 편이 낫다. SKIP 이 뜨면 `forge.base_url` 을
  적어 주면 된다(SaaS 를 쓴다면 `https://gitlab.com` 을 그대로 적어도 된다).
- ⚠️ **호스트에서 돌릴 때와 컨테이너 안에서 돌릴 때 보이는 것이 다르다.** `/run/secrets`
  나 `tcp://socket-proxy:2375` 는 컨테이너 관점이라, 호스트에서는 해당 검사가 `SKIP` 으로
  나오고 안내가 붙는다. 기동 후에는 안에서 한 번 더 돌린다:
  ```bash
  docker compose exec central python -m app.setup doctor
  ```
- 이 CLI 는 **얇은 껍데기**다 — 조회·검증·렌더·진단 로직은 `app/setup_discover.py` ·
  `app/setup_validate.py` · `app/setup_render.py` · `app/setup_doctor.py` 에 있고, 후속 웹 온보딩·대화형 에이전트가
  **같은 함수**를 재사용한다(게이트가 두 벌이 되면 반드시 갈라진다).

---

### 2.6 대화형 마법사 — `python -m app.setup wizard`

§2.5 의 네 명령을 **대화로** 태운다. 이 명령이 새로 만드는 게이트는 **없다** — 검증·산출·
판정은 그대로 `validate`/`render`/`doctor` 가 쓰는 라이브러리가 하고, 통과하지 못하면
`config.yaml` 은 생성되지 않고 종료코드도 non-zero 다.

```bash
python -m app.setup wizard
```

마법사가 대신 해 주는 것(= 설치 리허설에서 사람이 손으로 하던 것):

| 리허설에서 손으로 하던 일 | 마법사 |
|---|---|
| `answers.json` 을 직접 작성 | 대화로 묻고 `setup-answers.json` 에 저장 |
| `deploy.secrets_base_dir` 을 빠뜨려 `validate` 에 막힘 | 반드시 묻는다(프로파일이 채워 주지 않는 유일한 required) |
| 시크릿 파일 3개를 손으로 생성 | 값을 받아 `secrets/<ref>` 에 **0600** 으로 저장 |
| dlc-meta 클론 경로를 `--dlc-meta` 로 지정 | 자동 탐색 → 못 찾을 때만 묻는다 |
| `discover` 출력을 눈으로 읽고 옮겨 적음 | 상태·전이·커스텀필드를 **목록에서 고르게** 한다 |
| `validate`→`render`→`doctor`→`compose up` 순서를 스스로 앎 | 그 순서로 이끈다 |

- **중단·재개**: 답은 매 질문마다 `setup-answers.json`(gitignore)에 저장된다. Ctrl-C /
  Ctrl-D 로 그만둬도 같은 명령을 다시 실행하면 이어서 진행하며, 이미 답한 항목은
  `[기본값]` 으로 제시된다. 그 파일은 §2.5 의 답변 JSON 과 **같은 형식**이라
  `python -m app.setup validate setup-answers.json` 으로 언제든 수동 경로로 갈아탈 수 있다.
- **시크릿**: 입력은 화면에 보이지 않고(`getpass`), 값은 `config.yaml`·답변 파일·표준
  출력 어디에도 남지 않는다. 이미 파일이 있으면 **다시 묻지 않는다.** 웹훅 수신 토큰처럼
  사람이 정할 이유가 없는 값은 아예 묻지 않고 무작위로 만든다.
- **자동 선택은 확정이 아니다**: `discover` 가 확신한 값도 "이렇게 채웠습니다 — 맞습니까?"로
  확인을 받고, 아니라고 하면 후보에서 다시 고를 수 있다.
- 유용한 플래그: `--secrets-dir <경로>`(시크릿 파일을 쓸 호스트 디렉토리, 기본
  `<project-dir>/secrets`) · `--no-discover`(오프라인) · `--no-doctor` · `--all`(프로파일
  파생 항목까지 전부 질문) · `--answers <경로>`.

> **이 마법사(=CLI)가 1차 진입점이다 — `claude` 없이 설치가 끝나야 한다.** `claude` 를
> 쓴다면 §2.7 의 스킬로도 같은 절차를 시작할 수 있지만, 그것은 선택지일 뿐이고 게이트는
> 어느 쪽이든 같은 CLI 하나다.

---

### 2.7 (선택) 프로젝트 스킬 — `python -m app.setup skill`

`claude` 로 이 리포를 여는 사람을 위한 **편의**다. 설치에 필요하지 않다 — 없어도 §2.5·
§2.6 으로 설치가 끝난다.

```bash
python -m app.setup skill          # skill-templates/ → 내 .claude/skills/ (멱등)
python -m app.setup skill --list   # 설치하지 않고 목록만
```

그러면 `claude` 세션에서 `/install-jira-auto-dispatcher` 로 부를 수 있다. 스킬은 값을
캐내는 인터페이스일 뿐이고, 검증·산출·판정은 그대로 §2.5 의 CLI 가 한다.

- **`.claude/` 는 추적하지 않는다.** 그 아래는 그 머신의 **개인 영역**이다 — 세션 인증·
  로컬 설정·머신별 절대경로가 섞이므로 팀이 공유하면 서로의 환경을 덮어쓴다. 리포가
  추적하는 것은 `skill-templates/` 의 **템플릿**뿐이고, 실제 파일은 클론한 사람이 위
  명령으로 자기 머신에 만든다.
- **템플릿 하나 = 스킬 하나.** 생성기는 `skill-templates/` 를 스캔할 뿐 목록을 코드에
  들고 있지 않다. 새 스킬은 템플릿 파일(`<이름>/SKILL.template.md` 또는
  `<이름>.template.md`)을 추가하면 그것으로 끝이고, 스킬 이름·설명은 그 파일의
  frontmatter 에서 읽는다. 일부만 깔려면 `--only <이름>[,<이름>…]`.
- **멱등**: 이미 같은 내용이면 아무것도 쓰지 않는다. 내용이 다르면(=직접 고쳤다면)
  **조용히 덮어쓰지 않고** 그대로 두고 멈춘다 — 덮어쓰려면 `--force` 를 명시해야 하고,
  그때도 먼저 `.bak-<타임스탬프>` 로 백업한다.
- **실패해도 설치를 막지 않는다**: 권한이 없거나 파일시스템이 읽기 전용이면 마법사는
  경고 한 줄만 남기고 그대로 진행한다(종료코드에 영향 없음). 스킬은 필수 경로가 아니다.

---

## 3. 갈래 A — 로컬 (개인/평가용)

가장 단순하다. **`docker-compose.yml` 은 손대지 않는다** — 배포되는 그대로
Docker Desktop 에서도 socket-proxy 경유로 뜬다(설치 리허설로 확인).

```bash
# (1) 설정
cp config/config.example.yaml config/config.yaml
#     → deploy.profile: local  (docker_host 는 프로파일에서 파생 — 손댈 것 없다)
#     → compose 는 편집하지 않는다.

# (2) 시크릿 — 값이 아니라 파일로
mkdir -p secrets/service
printf '%s' '<JIRA_API_TOKEN>'  > secrets/service/jira-token
printf '%s' '<FORGE_PAT>'       > secrets/service/forge-token
chmod -R go-rwx secrets && find secrets -type f -exec chmod 600 {} \;

# (3) central 자기 claude 토큰 — .env 가 담는 값은 이것 하나뿐이다.
#     ⚠️ HOST_DEPLOY_DIR 은 더 이상 필요 없다 — 워커 마운트가 전부 named 볼륨이다(§2.2).
#     ⚠️ WORKER_SHARED_SECRET 도 더 이상 필요 없다(은퇴 — §2.5). 옛 .env 에 남아 있어도 무시된다.
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' '<claude setup-token 값>' >> .env
chmod 600 .env

# (4) 기동
docker compose up -d
docker compose logs -f central
```

- 관리 UI: `http://localhost:8787`. **포트를 다른 기기에 노출하지 말 것.**
- 로컬은 리소스가 곧 병목이다 — 사용자 1명(worker 1개, 4g)으로 시작해 본다.

> **(고급) docker.sock 직결** — 편하지만 central 에 **호스트 root 동치** 권한을 준다
> (SECURITY.md §4). 그 특권 확대를 감수할 때만, **두 곳을 함께** 바꾼다:
>
> 1. `config/config.yaml` 에 `deploy.docker_host: unix:///var/run/docker.sock` 을 **명시**
>    (명시 값이 프로파일 파생을 이긴다)
> 2. `docker-compose.yml` 에서 `socket-proxy` 서비스와 central 의 `depends_on` 을 지우고,
>    central `volumes` 에 `- /var/run/docker.sock:/var/run/docker.sock` 을 추가
>    (compose 파일 안 "(대안) docker.sock 직결" 주석 참조)
>
> ⚠️ 둘 중 하나만 바꾸면 **조용히 어긋난다** — 기동은 되고 워커 spawn 만 실패한다.
> 바꿨으면 `docker compose exec central python -m app.setup doctor --only docker` 로
> 실측한다.

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
5. 웹훅(`webhook.enabled: true`)을 꼭 써야 하면, 리버스 프록시(TLS)로 경로
   `POST /webhook/jira` **하나만** 노출하고 출처를 Jira IP 로 제한한다. **8787 은 절대
   함께 열지 않는다.** 폴링만 써도 기능은 동일하므로, 확신이 없으면 웹훅을 켜지 말고
   폴링으로 간다.
   - 인증은 헤더 `X-Jira-Webhook-Token` **전용**이다(`webhook.secret_ref` 파일값과 비교).
     쿼리 `?token=` 은 access 로그 유출 표면이라 **401 로 거부한다** — Jira 자동화에서
     헤더를 못 붙이면 앞단 프록시가 붙이게 한다. 시크릿 미설정이면 엔드포인트가 503 이다.

### 4.2 기동

socket-proxy 경유가 **기본**이다(docker.sock 직결 금지 — compose 를 손대지 않는다).

```bash
# VM 에서
git clone <이 저장소> ~/jira-auto-dispatcher && cd ~/jira-auto-dispatcher
cp config/config.example.yaml config/config.yaml   # → §2 대로 채운다

# ⚠️ 호스트 절대경로를 적을 항목은 없다(§2.2).
# ⚠️ WORKER_SHARED_SECRET 은 은퇴했다(§2.5) — .env 가 담는 값은 아래 하나뿐이다.
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

### 6.0 기동 후 진단은 **central 이 스스로 돌린다**

`/run/secrets`·`tcp://socket-proxy:2375` 같은 값은 **컨테이너 관점**이라 호스트에서 돌린
`doctor` 는 그것들을 `SKIP` 한다. 예전에는 그래서 기동 뒤에 사람이 진단을 한 번 더 돌려야
완전한 판정이 나왔다. 이제 central 이 **부팅 때 컨테이너 안에서 스스로** 돌린다
(백그라운드 데몬 스레드 — 기동을 막지도 늦추지도 않고, 실패해도 부팅은 된다). 보는 곳은 셋:

```bash
docker compose logs central | grep 자가진단                  # (1) 로그 — FAIL 은 고치는 법까지
curl -fsS http://localhost:8787/api/doctor                   # (2) 캐시된 결과(재실행 안 함)
curl -fsS -XPOST http://localhost:8787/api/doctor/refresh    # (3) 수동 재실행(비동기, 202)
```

(3) 은 관리 UI **최상단 배너**의 `다시 진단` 버튼과 같은 것이다. 배너에 FAIL 이 뜨면
무엇이 실패했고 어떻게 고치는지가 함께 보인다.

> ⚠️ **워커 실행에 치명적인 FAIL 이면 사용자 온보딩(`POST /onboard`)이 409 로 막힌다** —
> `config` · `secrets` · `jira_auth` · `jira_search` · `docker`. 그 상태로
> 워커를 띄우면 에러 없이 아무 일도 안 하거나 **조용히 실패하는 잡**만 쌓이기 때문이다.
> `forge_token` · `dlc_meta` · `notifier` · `worker_secret` 은 **막지 않는다**(central 자신의
> git·알림 경로가 degrade 될 뿐, 잡은 돌고 MR/PR 도 나온다). 막히는 항목은 전부 관리 UI
> **밖**(`config.yaml`·시크릿 파일·호스트 env)에서 고치는 것이라, UI 로만 고칠 수 있는 것을
> UI 로 잠그는 자충수가 아니다. 고친 뒤 `다시 진단` 을 누르면 즉시 풀린다.

컨테이너 안에서 손으로 돌리는 경로도 그대로 남아 있다:

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

> ⚠️ **웹 등록만으로는 절반이다.** 합류자는 로컬 프레임워크 합류도 거쳐야 한다 —
> 바로 아래 §8 이 그 2단 절차다. 팀원에게 링크를 줄 때는 §8 을 준다.

### 6.2 실제 티켓으로 끝까지

배포가 살아 있는지가 아니라 **플로우가 도는지**를 검증하려면 [E2E.md](E2E.md) 를 따른다
(티켓 생성 → 감지 → dispatch → 실행 → 브랜치/MR → 완료 회신, 각 단계 기대결과·실패 로그 포함).

---

## 7. 자주 틀리는 것

> 아래 대부분은 `python -m app.setup doctor`(§2.5)가 **증상이 나기 전에** 잡는다 —
> 로그를 뒤지기 전에 먼저 돌려 본다.

| 증상 | 원인 | 잡는 검사 |
|---|---|---|
| worker 가 뜨는데 설정/토큰을 못 읽음 | 워커 이미지가 **낡았다**(스폰 시 주입 materialize 를 모르는 옛 이미지). central 과 워커는 같은 이미지를 쓴다 — `docker compose build && docker compose up -d` 로 다시 올리면 central 이 부팅 시 낡은 워커를 재생성한다 | central 로그의 `주입 config materialize` 라인 |
| dlc-meta pull/push 가 안 됨 | 사설 GitLab 을 이 호스트에서 열 수 없거나 토큰 권한 부족 | `--only dlc_meta` |
| 시크릿 파일을 못 읽음 | 참조 경로 오타 · 파일 부재 · 값을 config 에 직접 적음 | `--only secrets` (+ `validate`) |
| 온보딩이 `Read-only file system` 500 | central 의 `./secrets` 마운트를 `:ro` 로 바꿨다 — central 은 **rw** 여야 한다 | — |
| 티켓이 감지되지 않음 | `jira.project`/`jira.projects`·사용자 `scope.projects`(그 사람 범위 밖 티켓은 버려진다)·`jira.trigger_statuses`(상태 **이름**)·assignee accountId 매핑 중 하나. 폴링 주기(기본 60s)도 기다렸는지 | `--only jira_search` (프로젝트 키까지) |
| 폴러가 아무 JQL 도 안 던짐(로그에 "감시할 프로젝트가 없어") | 인스턴스 기본값(`jira.project`)도 없고 등록 사용자 `scope.projects` 도 전부 비었다 — 전 프로젝트를 긁지 않으려고 **의도적으로** 멈춘 것이다 | `--only jira_search` |
| "티켓 없음"처럼 조용히 넘어감 대신 에러 | Jira **Server/DC** 를 가리켰다 — Cloud 전용이다 | `--only jira_auth,jira_search` |
| 잡이 running 으로 안 넘어감 | 자원 어드미션이 큐잉 중일 수 있다(`admission.min_free_mem_mb`·`max_load_per_core`) | — |
| MR 작성자가 엉뚱한 사용자 | worker 가 앰비언트 자격증명으로 push 했다 — per-user forge 토큰 경로를 확인 | — |
| 알림이 안 감 | `notifier.provider` 가 `none` 이거나 목록 밖 값(경고만 남기고 미발송) | `--only notifier` |
| worker 를 못 띄움 | docker 엔드포인트 접속 실패(소켓 권한 · socket-proxy 미기동) | `--only docker` |

---

## 8. 팀원 합류 (이미 도는 인스턴스에 참여하는 사람)

> §1~§7 은 **인스턴스를 세우는 사람**을 위한 것이다. 이 장은 그 인스턴스에 **나중에
> 합류하는 사람**을 위한 것이다 — 설치자는 이 장의 링크만 팀원에게 주면 된다.

### 8.0 합류는 **2단**이다 (하나만 하면 절반만 된다)

| 단계 | 어디서 | 무엇을 | 안 하면 |
|---|---|---|---|
| **1. 로컬 — 프레임워크 합류(SETTER)** | 본인 머신 | `ai-dlc-orchestrator` 를 clone 하고 `claude` 를 띄운다. 부트스트랩되지 않은 환경이면 SETTER 가 **합류 모드**로 분기해 공유 원격(`dlc-meta`)을 clone 하고 세션 자가점검 훅을 설치한다 | 당신의 **로컬 오케스트레이터가 정체성도 `dlc-meta` 도 없는 상태**로 남는다. 워커는 돌지만 당신 쪽 로컬 루프(리뷰·완성·완료 전이)가 성립하지 않는다 |
| **2. 웹 — 대시보드에 자격증명 등록** | 관리 UI(`http://<central>:8787`) | 아래 준비물을 폼에 넣고 제출 → 운영자가 enable | central 이 **당신 워커를 띄우지 않는다**. 당신에게 배정된 티켓은 아무 일도 일어나지 않는다(에러도 안 난다) |

프레임워크 레포: `https://github.com/yunhyuk-choi/ai-dlc-orchestrator`
(이 배포가 다른 URL 을 쓰면 관리 UI 의 **STEP 1** 카드에 그 주소가 표시된다 —
`run.orchestrator_repo_url` 에서 렌더된다.)

```bash
# 1단 — 로컬
git clone https://github.com/yunhyuk-choi/ai-dlc-orchestrator
cd ai-dlc-orchestrator
claude          # 미부트스트랩이면 SETTER 가 합류 모드로 분기한다
```

⚠️ 1단의 **정본은 프레임워크 레포의 안내**다. 여기서는 진입점만 적는다 — 남의 레포 절차를
복제하면 갈라진다. 그 레포의 `CLAUDE.md` / `agents/SETTER.md` 를 따른다.

### 8.1 2단(웹)의 준비물

⚠️ **아래 표는 참고용이다.** 화면의 실제 안내는 관리 UI 가 **이 인스턴스 설정에서 렌더**한다
(`GET /api/onboarding/guide` → `app/user_schema.py`). 그래서 forge 가 GitHub 인 배포에서는
GitHub PAT 안내만 보이고, 사내 GitLab 이면 토큰 발급 링크도 그 호스트를 가리킨다.
**문서와 화면이 어긋나면 화면이 맞다.**

| 값 | 무엇 | 어디서 |
|---|---|---|
| `username` | 내부 식별자(공백 없이). 워커 컨테이너·시크릿 폴더 이름이 된다 | 본인이 정한다(등록 후 변경 불가) |
| `jira_account_id` **(필수)** | 폴러가 티켓 담당자를 당신에게 매핑하는 키. **틀리면 당신 티켓은 영원히 감지되지 않는다(에러도 없다)** | 폼의 **`내 accountId 조회`** 버튼(이메일+토큰으로 서버가 `GET /rest/api/3/myself` 를 대신 호출) · 또는 Jira 프로필 URL 의 `/people/<accountId>` 뒷부분 |
| `jira_email` **(필수)** | Atlassian 계정 이메일. Jira Cloud 인증은 (이메일, 토큰) **쌍**이다 | 본인 Atlassian 계정 |
| `jira_token` **(필수)** | 본인 Jira API 토큰 | Atlassian 계정 → Security → API tokens → Create (`https://id.atlassian.com/manage-profile/security/api-tokens`) |
| `forge_token` **(필수)** | 브랜치 push + 변경요청(MR/PR) 생성을 **당신 이름으로** 하는 개인 토큰 | GitLab: 아바타 → Edit profile → Access Tokens, 스코프 `api` / GitHub: Settings → Developer settings → Personal access tokens, 스코프 `repo` |
| `claude_setup_token` **(필수)** | 워커 안 에이전트가 쓸 **당신의** Claude 자격 | 본인 머신 터미널에서 `claude setup-token`(Max 구독). ⚠️ 재발급하면 기존 토큰이 무효화된다 |
| `git_name` · `git_email` | 커밋 author. ⚠️ `git_email` 은 forge 계정에 **인증된 이메일**이어야 커밋이 당신 계정에 연결된다 | 본인 |
| `autonomy_mode` | A=완전자율(변경요청 초안까지) / B=경량 1차. 처음엔 B 권장 | 본인이 고른다 |
| 풀 퍼미션 동의 **(필수)** | 아래 §8.2 | 본인이 체크 |

> **`forge_token` 은 왜 필수인가.** 예전에는 없어도 등록이 됐다. 그러면 워커는 커밋까지는
> 하고 **MR/PR 을 만들지 못한다** — 에러 없이 반쪽만 도는, 이 시스템에서 가장 비싼 실패
> 모드다. 두 자율 모드(A·B) 모두 브랜치를 원격에 push 하도록 지시하므로 forge 를 쓰지
> 않는 경우가 없다. 그래서 **조건부가 아니라 무조건 필수**로 올렸다.

### 8.2 풀 퍼미션 동의는 **본인이** 한다

등록하면 이 시스템은 워커 안에서 `--dangerously-skip-permissions` 로 코딩 에이전트를
실행하고, 그 에이전트는 **당신의** Jira·forge·Claude 자격증명으로 동작한다. 사람의 매 단계
승인 없이 셸 실행·파일 쓰기·`git push`·티켓 전이가 일어나며 그 흔적은 **당신 계정에** 남는다.

- 설치자가 `config.yaml` 에 켠 `consent.full_permissions` 는 *설치자 자신*의 동의일 뿐이다.
- 그래서 온보딩 폼에 **본인 동의 체크박스**가 있고, 체크하지 않으면 서버가 등록을 거부한다
  (UI 비활성화가 아니라 `POST /onboard` 가 400 — 게이트는 서버에 있다).
- 동의 시각은 **서버 수신 시각**으로 레지스트리(`state/registry.json` 의
  `consent.accepted_at`)에 남는다. 클라이언트가 보낸 시각은 쓰지 않는다.

무엇에 동의하는지의 정본은 [SECURITY.md](SECURITY.md).

### 8.3 등록 후

등록은 안전 기본 `enabled=false` 로 시작한다. 운영자가 목록에서 **enable** 하면 그때
`jad-worker-<username>` 컨테이너가 뜬다. 그전까지는 티켓이 배정돼도 아무 일도 일어나지
않는다(정상이다).

> ⚠️ 관리 UI 상단 배너가 **온보딩 차단**을 표시하면 등록이 409 로 막힌다. 그건 당신 입력의
> 문제가 아니라 인스턴스 설정 문제다(§6.0) — 운영자가 고쳐야 한다.
