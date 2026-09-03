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
| `python -m app.setup consent --request` | **사람의 동의**를 요청하는 요청서를 만든다(부작용 없음). 서브 에이전트가 동의와 관련해 할 수 있는 **유일한** 동작 |
| `python -m app.setup discover --answers <답변.json>` | 이 Jira 인스턴스에 **실제로 있는** 커스텀필드·상태·전이 id 조회. **config.yaml 이 없어도 된다**(답변의 base_url·이메일·토큰 참조만 있으면 돈다) |
| `python -m app.setup validate <답변.json>` | 스키마 검증. **통과 못 하면 non-zero** |
| `python -m app.setup render <답변.json>` | `config/config.yaml` 생성(`.env` 는 만들지 않는다 — 6)에서 손으로 쓴다). 파일이 이미 있으면 **`--force`**(자동 백업) |
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

## 진입 방법 — **호출자 종류로 갈린다**(게이트는 어느 쪽이든 같다)

| 누가 진행하나 | 무엇을 쓰나 | 왜 |
|---|---|---|
| **사람**(터미널 앞의 설치자) | `python -m app.setup wizard` | 같은 질문을 터미널에서 하고, 답을 `setup-answers.json` 에 모아 아래 CLI 를 그대로 태운다. `claude` 가 없어도 설치가 끝난다 — **사람에게는 이쪽이 1차 진입점**이다 |
| **에이전트**(이 룰북을 실은 서브·헤드리스 세션) | **먼저 `JAD_SETUP_ACTOR=subagent` 를 내보내고**, 아래 절차의 **비대화형 CLI** (`discover --answers` → `validate` → `render` → `doctor`) | ⚠️ `wizard` 를 **쓰지 마라** — 대화형 stdin 을 읽으므로 헤드리스에서는 멈추거나 EOF 로 죽는다(이제 `JAD_SETUP_ACTOR` 를 선언하면 wizard 가 스스로 거부한다). 에이전트는 자기 대화 채널에서 답을 모아 `setup-answers.json` 에 쓰고 위 명령만 돌린다 |

**서브로 실렸으면 첫 명령 전에 주체를 선언해라** — 이건 예의가 아니라 **안전장치**다.
설치 관문은 이 값을 보고 *동의를 만들 수 있는 주체인가*를 가른다.

```bash
export JAD_SETUP_ACTOR=subagent        # 리눅스·macOS
$env:JAD_SETUP_ACTOR = "subagent"      # 윈도우 PowerShell
```


두 경로는 **같은 답변 파일·같은 검증기·같은 종료코드**를 쓴다. 그래서 중간에 갈아탈 수 있다
(마법사를 하다 멈추고 CLI 로 잇거나 그 반대도 된다). 수동 절차 전체는 `INSTALL.md`.

### 에이전트로 실렸을 때 — 확인을 받을 수 없으면 멈추나, 진행하나

서브로 실렸을 때는 **사용자와의 직통 채널이 없다.** 질문·확인·시크릿 생성 요청은 전부 상위를
통해 올리고, 상위가 받아 온 답으로 이어간다. 끝나면 맨 아래 「상위에 돌려줄 것」의 4-튜플을
반환한다. 그런데 자율 실행이라 답이 오지 않을 수 있다 — **그때의 규칙은 아래가 전부다**:

| 상황 | 규칙 |
|---|---|
| `discover` 의 **`suggested_answers`**(확정된 값) | **진행한다.** 그대로 답변에 넣고, "확인 없이 채웠음"을 근거(`discover` 출력)와 함께 4-튜플의 *인터뷰 응답 원본* 에 **반드시 보고**한다. 확정값은 인스턴스가 실제로 준 사실이지 네 추측이 아니다 |
| `discover` 의 **`candidates`**(후보 여럿·부분일치뿐) | **멈춘다.** 네가 고르지 마라. 여기까지의 답변 파일을 저장하고, 후보 목록을 그대로 *권고 다음 단계* 에 실어 반환한다. 잘못 고른 커스텀필드 id 는 검증도 진단도 통과한 뒤 **운영에서** 터진다 — 되돌리는 비용이 기다리는 비용보다 훨씬 크다 |
| **동의**(`consent.full_permissions`) | **멈추되 빈손으로 멈추지 않는다.** `python -m app.setup consent --request --json` 을 돌려 그 출력을 4-튜플의 *권고 다음 단계* 에 그대로 실어 상위에 반환한다. 대신 눌러 줄 **수단 자체가 없다**(아래 1 항) |
| **시크릿 값** | **멈춘다.** 토큰은 설치자가 자기 터미널에서 파일로 만든다(3 항). 값을 대화로 받아 오지 마라 |

즉 **"확정된 사실은 진행 후 보고 · 판단이 필요한 선택과 사람의 권한은 멈춤"** 이다. 멈출 때도
빈손으로 돌아가지 않는다 — 여기까지의 답변 파일 · 무엇이 왜 막혔는지 · 다음 한 걸음을 실어라.

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

### 1) 동의부터 (여기서 막히면 뒤는 무의미) — **동의는 사람에게서만 온다**

이 시스템은 사람 승인 없이 파일 쓰기·셸·`git push` 권한을 가진 에이전트를 헤드리스로
돌린다. 관리 UI(8787)와 worker 는 RCE 표면이다(`SECURITY.md`). 그래서 이 설치는 "사람이
그 위험을 알고 풀 퍼미션을 준다"를 **전제**로 하고, 그 전제는 반드시 사람에게서 와야 한다.

⚠️ **답변 파일에 `consent.full_permissions: true` 를 적는 것은 동의가 아니다.** 예전
룰북은 "대신 눌러 주지 마라"라고 하면서 같은 문서에서 "서브에겐 사용자 채널이 없다"고도
했다 — 양립 불가한 두 지시였고, 리허설에서 서브 에이전트가 **스스로 true 를 적어** 통과했다.
지금은 그 구멍이 닫혀 있다: 동의는 **별도 증서**(`setup-consent.json`)로만 성립하고,
증서는 아래 두 채널로만 만들어진다. 증서 없이 답변에 true 만 있으면 `validate` 가
`consent_unattested` 로 **그 자리에서** 막는다.

| 누가 | 명령 | 무엇이 기록되나 |
|---|---|---|
| **터미널 앞의 사람** | `python -m app.setup consent` | 고지를 읽고 확인 문구를 직접 입력한다. **stdin 이 TTY 여야** 하므로 헤드리스 세션에서는 애초에 돌지 않는다 |
| **사용자 채널을 가진 상위 오케스트레이터** | `python -m app.setup consent --relay --granted-by "<사람>" --statement "<그 사람이 한 말 원문>" --relayed-by "<중계자>"` | 누가 동의했는지·그 사람의 원문·중계자가 함께 남는다. 이 경로로 만든 동의는 검증·진단·`config.yaml` 어디서나 "중계된 동의"로 표시된다 |

**서브 에이전트인 너는 둘 다 할 수 없다**(`JAD_SETUP_ACTOR=subagent` 를 선언했으면 관문이
거부하고, 선언하지 않았어도 TTY 가 없어 사람 채널은 열리지 않는다). 네가 할 일은 하나다:

```bash
python -m app.setup consent --request --json
```

이 출력(고지 원문 + 상위가 실행할 `--relay` 명령)을 **4-튜플의 *권고 다음 단계* 에 그대로
실어 반환**해라. 상위가 사용자에게 묻고, 동의를 받으면 `--relay` 로 전달한다. 그 뒤에
너를 다시 띄우면 증서가 이미 있으므로 그대로 이어서 진행된다.

사용자가 동의하지 않으면 **그것으로 끝이다** — 설치를 진행하지 마라. 동의 거부는 오류가
아니라 정상적인 결과다.

### 2) 값 모으기 — **묻는 횟수를 줄이는 것이 핵심**

물어야 하는 것(전부 `app/setup_schema.py` 선언에서 온다):

| 키 | 비고 |
|---|---|
| ~~`consent.*`~~ | **묻지 마라.** 1 항의 증서(`setup-consent.json`)가 정본이고, 관문이 답변에 자동으로 합친다. 답변 파일에 적어도 무의미하다(증서가 이긴다) |
| `jira.base_url` · `jira.project` · `jira.watcher_email` | Jira Cloud 전용 |
| `jira.watcher_token_file` | 참조. 기본 `service/jira-token` |
| `forge.kind` | dlc-meta URL 에서 자동 판정되면 **확인만** 받아라 |
| `forge.token_ref` | 참조. 기본 `service/forge-token` |
| `webhook.enabled` / `webhook.secret_ref` | 참조. 기본 `service/jira-webhook` |
| `notifier.provider` | 기본 `none` — 알림 없이도 완전히 동작한다 |
| `deploy.profile` | `local` / `cloud_vm` / `onprem_server` |
| `deploy.secrets_base_dir` | **자주 빠뜨린다.** 보통 `/run/secrets`(컨테이너 관점) |
| `jira.trigger_statuses` 등 | ⚠️ 손으로 적지 말고 **4) 조회 결과에서 고르게** 하라(그때 답변 파일에 채워 넣는다 — 지금 비어 있어도 된다) |

묻지 **않는** 것:

- `run.dlc_meta_repo_url` — 로컬 dlc-meta 클론의 `origin` 에서 읽는다.
  `--dlc-meta <경로>` 로 주거나 생략하면 흔한 위치를 탐색한다.
- `deploy.docker_host` · `deploy.workspace_volume` — `deploy.profile` 에서 파생된다.

독립적이고 무관한 질문을 한꺼번에 던지지 마라. 2~3개씩 맥락으로 묶어라.

모은 답은 **중첩 JSON** 으로 `setup-answers.json` 에 저장한다(gitignore 된다). 값 하나를
못 구해 멈춰도 이 파일이 남아 이어서 할 수 있다.

```json
{
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

**`config/config.yaml` 은 아직 없어도 된다.** 조회는 답변 파일을 직접 읽는다 — 붙는 데
필요한 것은 `jira.base_url`·`jira.watcher_email`·`jira.watcher_token_file` 셋뿐이고,
`jira.project` 까지 있으면 상태·전이도 함께 조회한다(없으면 그 둘만 SKIP + 사유 출력).
전부 2 항에서 이미 모은 값이다:

```bash
python -m app.setup discover --answers setup-answers.json --json > jira-discover.json
python -m app.setup discover --answers setup-answers.json          # 사람이 읽는 출력
```

> ⚠️ **여기서 `render` 를 먼저 돌리지 마라.** 예전 절차가 그랬는데, `render` 는 전체 검증을
> 돌려 `jira.trigger_statuses` 가 없으면 exit 1 로 죽는다 — **조회가 채워 주려던 바로 그
> 값**이라 빈 상태에서는 영원히 못 지나간다(리허설 실측). 순서는 **조회 → 검증 → 생성**이다.
> (`--answers` 를 생략해도 작업 디렉토리의 `setup-answers.json` 을 자동으로 쓴다. 그 경우
> 무엇을 읽었는지 표준 에러로 말해 준다.)

- `suggested_answers` 에 담긴 것은 **확정된 값**이다. 그대로 답변에 넣되 설치자에게
  "이렇게 채웠습니다 — 맞습니까?"로 **확인**을 받아라(확인을 받을 수 없는 자율 실행이면
  위 「확인을 받을 수 없으면」 표의 규칙을 따른다).
- 확정되지 않은 항목은 `candidates` 만 있다. **네가 고르지 마라** — 후보를 보여주고
  설치자가 고르게 하라. 부분일치로 잘못 고른 커스텀필드 id 는 검증도 진단도 통과한 뒤
  착수 시점에 엉뚱한 필드로 터진다.
- 상태·전이는 `{"id": ..., "name": ...}` 형태로 적어라.
- **`cancel_statuses` 는 "그 개념이 없는 프로젝트"가 흔하다.** 조회에 취소류 상태가 하나도
  없으면 그게 정상이며 **`[]` 로 두는 것이 정답**이다(`"cancel_statuses": []`). 비워 두면
  취소 감시가 꺼질 뿐 다른 기능은 그대로다 — 억지로 비슷한 상태를 골라 넣지 마라. 그건
  "티켓을 옮겼는데 잡이 조용히 취소되는" 오작동이 된다.

### 5) 검증 → 생성 → 진단 (순서 고정)

```bash
python -m app.setup validate setup-answers.json --dlc-meta ../dlc-meta   # 0 아니면 여기서 멈춘다
python -m app.setup render   setup-answers.json --dlc-meta ../dlc-meta   # → config/config.yaml
python -m app.setup doctor
```

`validate` 가 non-zero 면 **`render` 로 넘어가지 마라.** 출력의 `key` 와 `hint` 를 그대로
읽고 그 항목만 다시 물어라. 종료코드: `0` 통과 / `1` 게이트 실패 / `2` 사용 오류.

⚠️ **`config/config.yaml` 이 이미 있으면 `render` 는 `이미 파일이 있습니다` 로 exit 2 다** —
덮어쓰려면 `--force` 를 붙인다(먼저 `.bak-<타임스탬프>` 로 백업한다):

```bash
python -m app.setup render setup-answers.json --dlc-meta ../dlc-meta --force
```

답을 고쳐 다시 렌더하는 일은 설치 중에 흔하다(4 항 조회로 값을 채운 뒤가 특히 그렇다).
`--force` 없이 실패했다고 손으로 `config.yaml` 을 고치지 마라 — 다음 `render --force` 가
그 편집을 되돌린다. **정본은 언제나 `setup-answers.json` 이다.**

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
    `jira.trigger_statuses` · `optout_labels` · `custom_fields` · `done_transition_names`.
    이것들은 검증도 진단도 통과한 뒤 착수 시점에 터지거나(없는 커스텀필드 → Jira 400)
    **에러 없이 아무 티켓도 못 찾는다**(상태 이름 불일치). 4)의 `discover` 결과로 다시 물어라.
  - **기능을 켰으면 고친다**: `notifier.provider` 를 `none` 이 아닌 값으로 바꿔 놓고
    `webhook_ref` 가 예시 그대로면 알림이 조용히 안 나간다.

  **두 버킷에 걸쳐 보이는 두 항목 — 값을 보고 가른다(뭉뚱그리지 마라):**

  | 항목 | 렌더된 값 | 판정 |
  |---|---|---|
  | `jira.done_transition_id` | `""`(렌더 기본) | **무해 — 그대로 둔다.** 비어 있으면 이름(`done_transition_names`)으로 찾고, 그것도 안 맞으면 done 카테고리 전이가 **정확히 하나**일 때만 그걸 쓴다. 아무것도 못 정하면 조용히 넘어가지 않고 **가능한 전이 목록을 담은 에러**로 실패한다. 즉 빈 값은 "이름으로 찾겠다"는 정상 상태다 |
  | `jira.done_transition_id` | 숫자(예 `"41"`) | **반드시 고친다.** 남의 인스턴스 전이 id 이며 **엉뚱한 전이를 실행할 수 있다.** `discover --only transitions` 로 실측해 바꾸거나, 확신이 없으면 `""` 로 비워 이름 경로에 맡겨라 |
  | `jira.cancel_statuses` | `[]` | **무해 — 그대로 둔다.** 이 프로젝트에 "취소" 개념이 없다는 뜻이고, 취소 감시만 꺼진다(4 항 참조). 비슷한 상태를 억지로 넣지 마라 |
  | `jira.cancel_statuses` | 남의 상태 이름·id | **반드시 고친다.** 그 이름이 이 인스턴스에 없으면 취소가 감지되지 않고, 우연히 다른 뜻의 상태와 겹치면 **멀쩡한 잡이 취소된다** |

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

**워커 이미지는 이 세 줄에서 자동으로 갈린다 — 더 적을 것은 없다.** compose 가
`jira-auto-dispatcher:${JAD_INSTANCE:-latest}` 로 태그를 파생하므로, `JAD_INSTANCE` 를 준
순간 이미지도 그 인스턴스 것이 된다(미설정이면 예전 그대로 `:latest`). 이게 없던 시절에는
두 번째 인스턴스를 빌드하는 순간 **돌고 있는 첫 인스턴스의 워커 이미지가 갈아치워졌다**.
`config/config.yaml` 의 `spawn.image`·`spawn.network` 도 `render` 가 인스턴스에 맞춰 써
준다 — **손으로 고치지 마라**(다음 `render --force` 가 되돌린다. 정본은 답변 파일이다).
사내 레지스트리 이미지를 써야 할 때만 `.env` 에 `JAD_IMAGE=<repo>:<tag>` 를 더한다.

**`CLAUDE_CODE_OAUTH_TOKEN` 은 기동 필수가 아니다 — 다만 없으면 기능 하나가 조용히
degrade 된다.** 이 값은 central *자신*의 claude 토큰으로, 신규 티켓의 대상 레포를 LLM 으로
판단할 때만 쓴다(`run.repo_resolution: llm`, 기본값). 비어 있어도 스택은 뜨고 관리 UI 도
정상이며, 레포 판단은 `repo_map` 기반 **정적 폴백**으로 내려간다(에러 없이 조용히). 워커가
쓰는 사용자별 claude 토큰은 이것과 **별개**이며 관리 UI 온보딩 폼에서 받는다. 그러므로:
LLM 레포 판단을 쓸 거면 지금 넣고(`INSTALL.md` §3), 아니면 비워 둔 채 진행하되 **4-튜플의
*권고 다음 단계* 에 "정적 폴백으로 동작 중"이라고 적어라.**

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8787/healthz
docker compose exec central python -m app.setup doctor      # 컨테이너 관점으로 한 번 더
bash scripts/smoke-deployed.sh http://127.0.0.1:8787        # (선택) 엔드포인트 + 이 인스턴스 컨테이너
```

관리 UI 포트는 `.env` 의 `JAD_PORT`(기본 8787)다 — 바꿨다면 위 명령의 8787 도 그 값으로
읽어라. 그 주소가 **대시보드 좌표**이고, 상위에 반드시 돌려줘야 하는 값이다(맨 아래 절).
스모크 스크립트는 `.env` 의 `JAD_INSTANCE`·`JAD_PORT` 를 읽어 **자기 인스턴스 컨테이너만**
본다 — 다른 인스턴스가 뜬 호스트에서 남의 컨테이너를 보고 통과했다고 착각하지 않게.

호스트에서 SKIP 이던 검사(`/run/secrets`·`tcp://socket-proxy:2375`)가 컨테이너 안에서는
실측된다. 마지막으로 관리 UI 에서 사용자 온보딩을 안내하라 — per-user 자격증명은 설치
단계가 아니라 그 폼에서 받는다.

---

## 막혔을 때

| 증상 | 먼저 볼 것 |
|---|---|
| `validate` 가 `consent_unattested` 로 막힘 | 동의 증서가 없다. 답변 파일의 true 는 동의가 아니다 — 1 항대로 `consent --request` 를 상위에 반환하고 기다려라. **답변 파일을 고쳐서 뚫으려 하지 마라**(뚫리지 않는다) |
| `consent` 가 `이 채널에는 사람이 없습니다` 로 exit 1 | 정상이다. 헤드리스 세션에는 사람이 없다 — 사람 채널은 터미널에서만 열린다 |
| `validate` 가 `run.dlc_meta_repo_url` 누락으로 막힘 | dlc-meta 클론 경로를 `--dlc-meta` 로 줬는가 |
| `render` 가 `이미 파일이 있습니다` 로 exit 2 | 재렌더는 `--force`(먼저 `.bak-*` 백업). `config.yaml` 을 손으로 고치지 말고 답변 파일을 고쳐 다시 렌더한다 |
| `discover` 가 `조회에 쓸 입력이 없습니다` 로 exit 2 | 아직 답변 파일이 없다. 2 항의 세 값(`jira.base_url`·`watcher_email`·`watcher_token_file`)만 담아 `setup-answers.json` 을 먼저 만든다 — **`render` 를 먼저 돌리는 게 아니다** |
| 두 번째 인스턴스를 띄웠더니 첫 인스턴스가 이상해짐 | `.env` 의 세 줄(특히 `COMPOSE_PROJECT_NAME`)을 넣었는가. 이미지·네트워크·볼륨은 `JAD_INSTANCE` 에서 파생되므로 그 한 줄이 빠지면 전부 공유된다 |
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
| **생성 파일 경로[]** | `config/config.yaml` · `.env` · `setup-answers.json` · `setup-consent.json`(있다면 — **경로만**, 내용에 사람 이름·원문이 있다) · `secrets/service/*`(**경로만**) · 생성했다면 `.claude/skills/install-jira-auto-dispatcher/SKILL.md` |
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
