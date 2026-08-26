---
name: install-jira-auto-dispatcher
description: jira-auto-dispatcher 를 처음 설치·설정할 때 쓴다. 설치자가 "설치해 줘", "세팅 도와줘", "config.yaml 만들어 줘", "install", "onboarding setup", "왜 doctor 가 실패하지" 같은 요청을 하거나, 이 리포를 갓 클론해 무엇부터 할지 물을 때 트리거된다. 대화로 값을 모아 기존 설치 관문 CLI(python -m app.setup)에 태운다.
---

# jira-auto-dispatcher 설치

이 스킬은 **값을 캐내는 인터페이스**다. 검증·생성·판정은 **전부** 기존 CLI 가 한다:

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

**A. 지금 이 대화로 진행**(아래 절차). 값을 물어보고 `setup-answers.json` 에 모은 뒤 CLI 를 태운다.

**B. claude 없이**: 설치자가 직접 `python -m app.setup wizard` 를 실행한다. 같은 질문을
터미널에서 하고 같은 답변 파일·같은 CLI 를 쓴다. 설치자가 대화를 원하지 않거나, 서버에
SSH 로만 붙어 있으면 이쪽을 권해라. (수동 절차 전체는 `INSTALL.md`.)

---

## 절차

### 0) 자리 확인

```bash
python --version && docker compose version && ls config/config.example.yaml
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
- `WORKER_SHARED_SECRET` — `render` 가 `.env` 에 만든다(멱등). 사람이 정할 값이 아니다.

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
