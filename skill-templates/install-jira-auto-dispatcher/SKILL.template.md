---
name: install-jira-auto-dispatcher
description: jira-auto-dispatcher 를 처음 설치·설정할 때 쓴다. 설치자가 "설치해 줘", "세팅 도와줘", "config.yaml 만들어 줘", "install", "onboarding setup", "왜 doctor 가 실패하지" 같은 요청을 하거나, 이 리포를 갓 클론해 무엇부터 할지 물을 때 트리거된다. 대화로 값을 모아 기존 설치 관문 CLI(python -m app.setup)에 태운다.
---

# jira-auto-dispatcher 설치

> **이 룰북은 두 자리에서 읽힌다.** ① 설치자가 `.claude/skills/…/SKILL.md` 로 펼쳐 슬래시
> 커맨드(`/install-jira-auto-dispatcher`)로 부르는 **생성된 개인 산출물**(gitignore), ② 상위
> 오케스트레이터가 이 리포를 클론한 뒤 **서브 에이전트 룰북으로 그대로 싣는 원본**.
> 어느 쪽이든 **작업 디렉토리는 이 리포 클론**이고 절차는 같다.
>
> 추적되는 원본은 `skill-templates/install-jira-auto-dispatcher/SKILL.template.md` 이고,
> `python -m app.setup skill` 이 그 템플릿에서 ①의 파일을 만든다.
> 내용을 바꾸려면 **템플릿을 고치고** `python -m app.setup skill --force` 로 다시 생성해라
> (생성물을 직접 고치면 다음 재생성 때 갈린다 — 그래서 `--force` 없이는 덮어쓰지 않는다).

이 룰북은 **값을 캐내는 인터페이스**다. 검증·생성·판정은 **전부** 기존 CLI 가 한다:

| 명령 | 하는 일 |
|---|---|
| `python -m app.setup discover` | 이 Jira 인스턴스에 **실제로 있는** 커스텀필드·상태·전이 id 조회 |
| `python -m app.setup validate <답변.json>` | 스키마 검증. **통과 못 하면 non-zero** |
| `python -m app.setup render <답변.json>` | `config/config.yaml` 생성(+ `.env` 공유 시크릿) |
| `python -m app.setup doctor` | 그 설정으로 **실제로 붙는지** 실측 |

**절대 하지 말 것**

- 검증·진단 로직을 새로 짜지 마라. 위 명령의 출력과 종료코드가 판정이다.
- 시크릿 **값**을 `config/config.yaml` 이나 `setup-answers.json` 에 쓰지 마라. 이 리포는
  설정에 **참조(상대 경로)만** 둔다. 값은 `secrets/<ref>` 에 0600 파일로 간다.
- 토큰 값을 **대화·명령줄·로그에 넣지 마라.** 시크릿 파일은 아래처럼 **설치자가 자기
  터미널에서** 만들게 하라(그래야 값이 이 대화에 남지 않는다).
- `doctor` 가 FAIL 인데 "아마 괜찮을 것"이라고 넘어가지 마라. 원인을 고치거나, 왜 이
  환경에서는 그 검사가 의미 없는지(호스트 vs 컨테이너 관점) 근거를 대라.

---

## 두 갈래 — 어느 쪽이든 게이트는 같다

**A. 대화로 진행**(아래 절차). 값을 물어보고 `setup-answers.json` 에 모은 뒤 CLI 를 태운다.
설치자가 직접 부르든, 상위 오케스트레이터가 이 룰북을 서브에 실어 띄우든 절차는 **같다**.
다만 서브로 실렸을 때는 **사용자와의 직통 채널이 없다** — 질문·확인·시크릿 생성 요청은 전부
상위를 통해 올리고, 상위가 받아 온 답으로 이어간다. 끝나면 맨 아래
「상위에 돌려줄 것」의 4-튜플을 반환한다.

**B. claude 없이**(1차 진입점): 설치자가 직접 `python -m app.setup wizard` 를 실행한다. 같은
질문을 터미널에서 하고 같은 답변 파일·같은 CLI 를 쓴다. 설치자가 대화를 원하지 않거나,
서버에 SSH 로만 붙어 있으면 이쪽을 권해라. (수동 절차 전체는 `INSTALL.md`.)

---

## 전제조건 — 없으면 0) 첫 명령에서 막힌다

설치 자체는 **설치자가 자기 손으로** 한다(브라우저·관리자 권한이 필요하다). 여기서 할 일은
*무엇을 · 어디서 · 어떻게* 받는지 주고, **확인 명령의 출력으로** 갖춰졌는지 판정하는 것이다 —
"설치했다"는 말은 근거가 아니다. 하나라도 실패하면 **거기서 멈춰라.** 없는 도구를 있다고
가정하고 넘어가면 한참 뒤 엉뚱한 곳(5)의 `render`·6)의 기동)에서 터진다.

| 필요한 것 | 왜 | 공식 설치처 | 확인 명령 |
|---|---|---|---|
| **Docker Engine + Compose v2** | central·worker 컨테이너를 띄운다(6단계) | Windows·macOS: <https://docs.docker.com/desktop/> · Linux: <https://docs.docker.com/engine/install/> + <https://docs.docker.com/compose/install/linux/> | `docker --version` · `docker compose version` · `docker ps` |
| **git** | dlc-meta·런타임 레포 clone, worker 의 브랜치·MR | <https://git-scm.com/downloads> | `git --version` |
| **Python** | 설치 관문 CLI(`python -m app.setup`)를 **호스트에서** 돌린다 | <https://www.python.org/downloads/> | `python --version` |
| **이 리포의 파이썬 의존성** | 관문 CLI 가 `pyyaml`·`requests`(+진단의 `docker` SDK)를 쓴다 | 리포 클론 안에서 `pip install -r requirements.txt` | `python -c "import flask, yaml, requests, docker; print('deps ok')"` |

- **Compose 는 v2 다** — `docker compose`(공백)가 돌아야 한다. `docker-compose`(하이픈, v1)만
  있으면 이 리포의 명령이 전부 어긋난다.
- `docker --version` 은 클라이언트만 본다. **데몬까지** 확인하려면 `docker ps` 다(Docker
  Desktop 은 앱이 실행 중이어야 한다).
- **파이썬 버전** — 컨테이너 안은 이미지가 3.12 로 고정한다(`Dockerfile` = `python:3.12-slim`).
  호스트 파이썬이 그와 같을 필요는 없다(호스트는 관문 CLI 만 돌린다). 다만 이 리포가 **실제로
  검증하는 조합은 3.12** 이므로(CI 가 3.12 로만 돈다) 3.12 를 권장한다. 리눅스·macOS 는
  `python`·`pip` 이 `python3`·`pip3` 일 수 있다.
- `docker` SDK 가 없으면 `doctor` 의 docker 검사가 **FAIL 이 아니라 SKIP** 으로 빠진다 —
  갖춰졌다고 **오해하기 쉬운 자리**다. 위 import 확인을 건너뛰지 마라.

---

## 절차

### 0) 자리 확인

작업 디렉토리는 **이 리포 클론**이다(두 소비 방식 모두) — 아니면 멈추고 위치부터 잡아라.
위 전제조건의 확인 명령이 하나라도 실패하면 그것부터 해결한다.

```bash
git rev-parse --show-toplevel && python --version && docker compose version && ls config/config.example.yaml
```

`setup-answers.json` 이 이미 있으면 **이어서 하는 중**이다 — 먼저 읽고, 이미 답한 것은
다시 묻지 마라.

### 1) 동의부터 (여기서 막히면 뒤는 무의미)

이 시스템은 사람 승인 없이 파일 쓰기·셸·`git push` 권한을 가진 에이전트를 헤드리스로
돌린다. 관리 UI(8787)와 worker 는 RCE 표면이다(`SECURITY.md`). 설치자가 명시로 동의하지
않으면 `validate` 가 `consent_required` 로 막는다 — 대신 눌러 주지 마라.

### 2) 값 모으기 — **묻는 횟수를 줄이는 것이 핵심**

물어야 하는 것(전부 `app/setup_schema.py` 선언에서 온다):

| 키 | 비고 |
|---|---|
| `consent.full_permissions` / `consent.accepted_at` | 설치자 본인의 동의 + 지금 시각(ISO-8601) |
| `jira.base_url` · `jira.project` · `jira.watcher_email` | Jira Cloud 전용 |
| `jira.watcher_token_file` | 참조. 기본 `service/jira-token` |
| `forge.kind` | dlc-meta URL 에서 자동 판정되면 **확인만** 받아라 |
| `forge.token_ref` | 참조. 기본 `service/forge-token` |
| `webhook.enabled` / `webhook.secret_ref` | 참조. 기본 `service/jira-webhook` |
| `notifier.provider` | 기본 `none` — 알림 없이도 완전히 동작한다 |
| `deploy.profile` | `local` / `cloud_vm` / `onprem_server` |
| `deploy.secrets_base_dir` | **자주 빠뜨린다.** 보통 `/run/secrets`(컨테이너 관점) |
| `jira.trigger_statuses` 등 | ⚠️ 손으로 적지 말고 **3단계 조회 결과에서 고르게** 하라 |

묻지 **않는** 것:

- `run.dlc_meta_repo_url` — 로컬 dlc-meta 클론의 `origin` 에서 읽는다.
  `--dlc-meta <경로>` 로 주거나 생략하면 흔한 위치를 탐색한다.
- `deploy.docker_host` · `deploy.workspace_volume` — `deploy.profile` 에서 파생된다.

독립적이고 무관한 질문을 한꺼번에 던지지 마라. 2~3개씩 맥락으로 묶어라.

모은 답은 **중첩 JSON** 으로 `setup-answers.json` 에 저장한다(gitignore 된다). 값 하나를
못 구해 멈춰도 이 파일이 남아 이어서 할 수 있다.

```json
{
  "consent": {"full_permissions": true, "accepted_at": "2026-01-01T09:00:00+09:00"},
  "deploy":  {"profile": "local", "secrets_base_dir": "/run/secrets"},
  "forge":   {"kind": "gitlab", "token_ref": "service/forge-token"},
  "jira":    {"base_url": "https://your-org.atlassian.net", "project": "PROJ",
              "watcher_email": "bot@your-org.example",
              "watcher_token_file": "service/jira-token",
              "trigger_statuses": [{"id": "10000", "name": "해야 할 일"}]},
  "notifier": {"provider": "none"},
  "webhook": {"enabled": true, "secret_ref": "service/jira-webhook"}
}
```

### 3) 시크릿 파일 — **설치자가 직접** 만든다

값이 이 대화에 들어오지 않게, 아래 명령을 그대로 주고 **설치자가 자기 터미널에서**
실행하게 하라(리눅스/맥; 윈도우는 `INSTALL.md` §2.3):

```bash
mkdir -p secrets/service && chmod 700 secrets secrets/service
umask 077
read -rs -p 'Jira API 토큰: ' T && printf '%s' "$T" > secrets/service/jira-token && unset T
read -rs -p 'forge PAT: '     T && printf '%s' "$T" > secrets/service/forge-token && unset T
openssl rand -hex 32 > secrets/service/jira-webhook     # 웹훅 토큰은 사람이 정할 값이 아니다
chmod 600 secrets/service/*
```

`secrets/` 는 gitignore 되고, compose 가 이 디렉토리를 `deploy.secrets_base_dir` 자리에
마운트한다. 파일이 없거나 권한이 헐거우면 `doctor --only secrets` 가 잡는다 — 네가
확인했다고 선언하지 말고 **그 명령의 출력으로** 확인해라.

### 4) 조회 — 값을 눈으로 옮겨 적지 않기

`jira.base_url`·`watcher_email`·`watcher_token_file` 만 채워도 돈다:

```bash
python -m app.setup render setup-answers.json --dlc-meta ../dlc-meta   # 최소 config 먼저
python -m app.setup discover --json > /tmp/jira.json
```

- `suggested_answers` 에 담긴 것은 **확정된 값**이다. 그대로 답변에 넣되 설치자에게
  "이렇게 채웠습니다 — 맞습니까?"로 **확인**을 받아라.
- 확정되지 않은 항목은 `candidates` 만 있다. **네가 고르지 마라** — 후보를 보여주고
  설치자가 고르게 하라. 부분일치로 잘못 고른 커스텀필드 id 는 검증도 진단도 통과한 뒤
  착수 시점에 엉뚱한 필드로 터진다.
- 상태·전이는 `{"id": ..., "name": ...}` 형태로 적어라.

### 5) 검증 → 생성 → 진단 (순서 고정)

```bash
python -m app.setup validate setup-answers.json --dlc-meta ../dlc-meta   # 0 아니면 여기서 멈춘다
python -m app.setup render   setup-answers.json --dlc-meta ../dlc-meta   # → config/config.yaml + .env
python -m app.setup doctor
```

`validate` 가 non-zero 면 **`render` 로 넘어가지 마라.** 출력의 `key` 와 `hint` 를 그대로
읽고 그 항목만 다시 물어라. 종료코드: `0` 통과 / `1` 게이트 실패 / `2` 사용 오류.

### 6) 기동과 확인

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
docker compose exec central python -m app.setup doctor      # 컨테이너 관점으로 한 번 더
```

관리 UI 포트는 `.env` 의 `JAD_PORT`(기본 8787)다 — 바꿨다면 위 두 줄의 8787 도 그 값으로
읽어라. 그 주소가 **대시보드 좌표**이고, 상위에 반드시 돌려줘야 하는 값이다(맨 아래 절).

호스트에서 SKIP 이던 검사(`/run/secrets`·`tcp://socket-proxy:2375`)가 컨테이너 안에서는
실측된다. 마지막으로 관리 UI 에서 사용자 온보딩을 안내하라 — per-user 자격증명은 설치
단계가 아니라 그 폼에서 받는다.

---

## 막혔을 때

| 증상 | 먼저 볼 것 |
|---|---|
| `validate` 가 `run.dlc_meta_repo_url` 누락으로 막힘 | dlc-meta 클론 경로를 `--dlc-meta` 로 줬는가 |
| `doctor` 의 `forge_token` 이 SKIP | `forge.base_url` 이 비어 있다(사내 PAT 를 외부로 안 보내려는 의도적 SKIP) |
| 폴러가 에러 없이 아무 티켓도 못 찾음 | `jira.trigger_statuses` 이름이 인스턴스와 어긋난 것 — `discover` 로 확인 |
| `POST /onboard` 가 409 | 기동 시 doctor 게이트가 막은 것. `/api/doctor` 의 FAIL 을 먼저 고쳐라 |

정본 문서: `INSTALL.md`(설치) · `SECURITY.md`(위험 모델) · `DEPLOY.md`(상시 운영).

---

## 상위에 돌려줄 것 — 4-튜플

상위 오케스트레이터가 이 룰북을 서브로 띄웠을 때의 **반환 계약**이다(사람이 직접 부른
경우에도 같은 4개를 마지막 보고에 적으면 된다).

| 항목 | 무엇을 싣나 |
|---|---|
| **생성 파일 경로[]** | `config/config.yaml` · `.env` · `setup-answers.json` · `secrets/service/*`(**경로만**) · 생성했다면 `.claude/skills/install-jira-auto-dispatcher/SKILL.md` |
| **정합성 체크** | `validate`·`doctor`(**호스트·컨테이너 두 관점**)의 종료코드와 항목별 PASS/FAIL/SKIP. SKIP 은 그 이유까지. 네 판단이 아니라 그 명령의 출력이 근거다 |
| **인터뷰 응답 원본** | `setup-answers.json` 의 내용(참조·비-시크릿 값만 들어 있다) + 설치자가 고른 상태·전이·커스텀필드 id 와 그 근거(`discover` 결과) |
| **권고 다음 단계** | 남은 일 — 보통 ① 관리 UI 온보딩 폼으로 per-user 자격증명 등록 ② Jira 웹훅 등록 ③ FAIL·SKIP 중 사람이 결정해야 하는 항목 |

**반드시 실어라 — 관리 UI 대시보드 좌표.** 상위가 이 값을 `dlc-meta` 에 기록한다.

- 좌표: `http://<central 호스트>:<포트>` — 포트는 `.env` 의 `JAD_PORT`(기본 8787)
- 인스턴스 접두어: `.env` 의 `JAD_INSTANCE`(기본 `jad`)와 거기서 파생된 컨테이너 이름
  (`<접두어>-central`) — 한 호스트에 여러 인스턴스가 뜰 수 있어, 상위가 나중에 이 인스턴스를
  지목하려면 필요하다

⚠️ **시크릿 값은 반환에 싣지 마라.** 토큰·웹훅 시크릿은 **참조 경로**(`secrets/service/jira-token`
같은)로만 말한다. `setup-answers.json` 은 설계상 참조만 담으므로 그 내용은 그대로 실어도
되지만, 싣기 전에 값이 섞여 들어가지 않았는지 **한 번 훑어라**.
