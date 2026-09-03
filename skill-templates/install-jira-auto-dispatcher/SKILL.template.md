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
| `python -m app.setup render <답변.json>` | `config/config.yaml` 생성(`.env` 는 만들지 않는다 — 6)에서 손으로 쓴다) |
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

값이 이 대화에 들어오지 않게, 아래를 그대로 주고 **설치자가 자기 터미널에서** 실행하게
하라. **1순위는 파이썬 한 줄**이다 — 파이썬은 이미 전제조건이라 리눅스·macOS·윈도우가
같은 명령 하나로 덮인다(입력은 화면에 찍히지 않고, 값은 파일로만 간다):

```bash
python -c "import getpass,os,pathlib,secrets; d=pathlib.Path('secrets/service'); d.mkdir(parents=True,exist_ok=True); [os.chmod(p,0o700) for p in (d.parent,d)]; w=lambda n,v:(d.joinpath(n).write_text(v,encoding='utf-8'), os.chmod(d/n,0o600)); w('jira-token',getpass.getpass('Jira API 토큰: ')); w('forge-token',getpass.getpass('forge PAT: ')); w('jira-webhook',secrets.token_hex(32))"
```

웹훅 시크릿(`jira-webhook`)은 사람이 정할 값이 아니라 난수다 — 묻지 마라.
(대안 · 리눅스/macOS 셸만) 같은 일을 셸로:

```bash
mkdir -p secrets/service && chmod 700 secrets secrets/service
umask 077
read -rs -p 'Jira API 토큰: ' T && printf '%s' "$T" > secrets/service/jira-token && unset T
read -rs -p 'forge PAT: '     T && printf '%s' "$T" > secrets/service/forge-token && unset T
openssl rand -hex 32 > secrets/service/jira-webhook
chmod 600 secrets/service/*
```

- **이미 토큰 파일을 갖고 있으면 그대로 써도 된다** — 재발급시킬 이유가 없다. 그 파일을
  `secrets/service/<참조 이름>` 으로 **복사**하게 하라. 값이 대화에 들어오지 않으므로
  규율 취지에 그대로 부합한다(파일 이름을 굳이 맞출 필요도 없다 — 답변의
  `*_ref`·`watcher_token_file` 을 그 참조 경로로 적으면 된다).
- ⚠️ **윈도우에서는 `0600` 이 실제로 적용되지 않는다**(NTFS 는 POSIX 모드 비트를 그렇게
  쓰지 않는다). 그래서 컨테이너 doctor 가 `[WARN] secrets … 권한이 헐겁습니다` 를
  **영구적으로** 낸다(리허설 실측 — 바인드 마운트된 파일이 리눅스 컨테이너 안에서 헐겁게
  보이고, 컨테이너 안에서 `chmod` 를 해도 사라지지 않는다).
  **판단: 이 WARN 은 그대로 두고 넘어가도 된다** — WARN 은 온보딩 게이트를 막지 않고
  (`FAIL` 만 막는다) 기능이 degrade 되지도 않는다. 다만 파일이 실제로 헐거운 것은
  사실이므로, 윈도우 호스트는 개인 평가용으로만 쓰고(`INSTALL.md` §2.3) 공유 머신이면
  NTFS ACL(`icacls`)로 따로 조여라. ⚠️ **리눅스·macOS 에서 같은 WARN 이 나오면 그건 진짜
  문제다** — 위 명령대로 했다면 나오지 않는다.

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
python -m app.setup render   setup-answers.json --dlc-meta ../dlc-meta   # → config/config.yaml
python -m app.setup doctor
```

`validate` 가 non-zero 면 **`render` 로 넘어가지 마라.** 출력의 `key` 와 `hint` 를 그대로
읽고 그 항목만 다시 물어라. 종료코드: `0` 통과 / `1` 게이트 실패 / `2` 사용 오류.

**`render` 의 경고 두 줄은 성격이 다르다 — 뭉뚱그리지 마라.**

- `⚠️ 아직 자리표시자가 남은 줄` → **반드시 고친다.** `<...>` 가 그대로 남은 것이고
  `doctor --only config` 가 FAIL 로 잡는다(게이트).
- `⚠️ 답하지도 파생되지도 않아 예시 값이 남은 항목` → **오류가 아니다.** 그 키에
  `config.example.yaml` 의 예시 값이 그대로 있다는 뜻이니, 생성된 `config/config.yaml`
  에서 그 키의 **실제 값을 보고** 아래로 가른다:
  - **무해 — 그대로 둔다**: 값이 `""`·`[]`·`false`·`none` 이거나, 그 기능이 꺼져 있어
    아무도 읽지 않는 참조. 리허설 사례가 전부 여기였다 — `jira.projects: []`(추가 감시
    프로젝트 없음) · `run.docs_repo_url: ""`(설계 문서 레포 미사용 → 프로비저닝 skip +
    경고) · `notifier.webhook_ref`(`provider: none` 이면 읽히지 않는다).
  - **반드시 고친다**: 값이 **남의 조직의 구체적인 id·이름**인 항목 —
    `jira.trigger_statuses` · `cancel_statuses` · `optout_labels` · `custom_fields` ·
    `done_transition_id` · `done_transition_names`. 이것들은 검증도 진단도 통과한 뒤
    착수 시점에 터지거나(없는 커스텀필드 → Jira 400) **에러 없이 아무 티켓도 못 찾는다**
    (상태 이름 불일치). 4)의 `discover` 결과로 다시 물어라.
  - **기능을 켰으면 고친다**: `notifier.provider` 를 `none` 이 아닌 값으로 바꿔 놓고
    `webhook_ref` 가 예시 그대로면 알림이 조용히 안 나간다.

### 6) 기동과 확인

**기동 전에 `.env` 부터.** `docker ps` 로 이 호스트에 이미 다른 인스턴스가 떠 있는지
확인하라(`*-central` 이 보이면 있는 것이다). 있으면 `.env` 에 아래 **세 줄**을 넣어라 —
빠뜨리면 이름이 겹쳐 뜨지 못하거나, 더 나쁘게는 상태를 공유한다:

```
JAD_INSTANCE=jad-stg          # 컨테이너·네트워크·워크스페이스 볼륨 이름 접두어
JAD_PORT=8788                 # 관리 UI 호스트 포트 — 인스턴스마다 달라야 한다
COMPOSE_PROJECT_NAME=jad-stg  # ⚠️ 상태 볼륨은 이 값으로만 갈린다(JAD_INSTANCE 로는 안 갈린다)
```

세 번째 줄이 특히 함정이다 — compose 프로젝트명은 **기본이 디렉토리 이름**이라, 같은
레포를 한 번 더 클론해 두 번째 인스턴스를 만들면 프로젝트명이 같아져 `jad-state`
볼륨(jobs·watermark·dedup·registry)을 **공유**한다. 두 인스턴스가 같은 티켓을 중복
처리하고 등록 사용자가 섞인다. 상세는 `INSTALL.md` §2.2.
첫 인스턴스면 세 줄 다 필요 없다(기본값 `jad` · 8787).
`.env` 에는 central 자기 claude 토큰(`CLAUDE_CODE_OAUTH_TOKEN`)도 들어간다 — `INSTALL.md` §3.

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
