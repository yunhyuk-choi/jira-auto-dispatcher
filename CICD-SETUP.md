# CICD-SETUP.md — jira-auto-dispatcher CI/CD 운영자 셋업 가이드

> 이 문서는 **운영자(사용자)가 GitLab UI/서버에서 수작업**으로 해야 하는 것들의 체크리스트다.
> CI 코드 자체(`.gitlab-ci.yml`, `deploy/redeploy-central.sh`)는 리포에 이미 있다.
> 시크릿 **값**은 이 문서/코드에 절대 넣지 않는다 — 변수 이름·참조만 다룬다.
>
> - 팀 GitLab: `https://gitlab.example.com`
> - 개발서버: **<DEV_SERVER_HOST>** (SSH `<deploy-user>`, sudo + docker)
> - 배포 경로: `/opt/jira-auto-dispatcher`
> - 파이프라인: `test → build → deploy` (deploy 는 main 한정, central 무중단 교체)

---

## 1. GitLab 러너 등록 (프로젝트별)

jira-auto-dispatcher 전용 러너를 등록한다. 잡별 요구 능력:

| 잡 | 필요 능력 | executor 권장 |
|---|---|---|
| `test` | python 컨테이너 실행 | **docker executor** (`image: python:3.12` 사용) |
| `build` | **Kaniko** 로 이미지 빌드 검증(아웃바운드 egress 필요 — Dockerfile 이 claude CLI 설치) | **일반 docker executor** — ⭐**privileged 불필요, DinD 불필요** |
| `deploy` | ssh + rsync 로 개발서버 접속 | docker executor(alpine 이미지에 apk 설치) 또는 shell executor |

⭐ **build 는 Kaniko 를 쓴다** — docker 데몬·DinD·privileged 전부 불필요. 공유 러너(다른 프로젝트와
같이 쓰는)를 privileged 로 못 바꾸는 환경을 위한 선택. 러너 config 를 건드릴 필요가 없다.

등록 절차(예):

1. 프로젝트 → Settings → CI/CD → Runners → "New project runner".
2. 서버/VM 에서 `gitlab-runner register` → URL `https://gitlab.example.com`, 발급된 토큰 입력.
3. **executor = docker** (일반 설정으로 충분). `test`(python:3.12)·`build`(kaniko)·`deploy`(alpine) 모두
   일반 docker executor 에서 돈다. **`privileged = true` 불필요.** 러너에 gcr.io·claude.ai 등 아웃바운드
   egress 만 있으면 된다(kaniko 이미지 pull + Dockerfile claude 설치).
4. 러너에 **태그**를 붙였다면 `.gitlab-ci.yml` 잡에 동일 `tags:` 를 추가해 라우팅한다
   (현재 파일은 태그 미지정 = 프로젝트의 아무 가용 러너나 사용).

> `build`(kaniko `--no-push`)는 "빌드 성사 게이트"일 뿐이고, 실제 배포 이미지는 `deploy` 가 서버에서
> 빌드한다(사내 레지스트리 미사용 — DEPLOY.md). MR 파이프라인(`merge_request_event`)에서도 돌아 **MR 빌드 검증**을 한다.

---

## 2. CI/CD 변수 (Settings → CI/CD → Variables)

모두 **Protected**(보호 브랜치에서만 노출), 키/토큰류는 **Masked** 권장.
`deploy` 는 main(보호 브랜치)에서만 도므로 Protected 로 충분하다.

| 변수 | 타입 | 값(설명) | 비고 |
|---|---|---|---|
| `SSH_PRIVATE_KEY` | Variable(값) | 배포용 SSH 개인키 **내용** (개행 포함 PEM) | `<deploy-user>@<DEV_SERVER_HOST>` 로 무암호 접속 가능한 키. Masked 는 개행 포함 키에서 깨질 수 있음 → Masked 대신 Protected 만 켜거나, 아래 File 타입 대안 사용. |
| `SSH_KNOWN_HOSTS` | **File** | 개발서버 호스트키 라인 | `ssh-keyscan -H <DEV_SERVER_HOST>` 결과를 그대로 넣는다(중간자 방지). |
| `DEPLOY_HOST` | Variable(값) | `<deploy-user>@<DEV_SERVER_HOST>` | ssh/rsync 대상. |

SSH 키 준비(로컬/운영 PC):

```bash
# 1) 배포 전용 키페어 생성(암호 없이 — CI 무인 실행)
ssh-keygen -t ed25519 -f jad_deploy -N "" -C "gitlab-ci jira-auto-dispatcher deploy"

# 2) 공개키를 개발서버 <deploy-user> 계정에 등록
ssh-copy-id -i jad_deploy.pub <deploy-user>@<DEV_SERVER_HOST>
#   또는 서버의 ~<deploy-user>/.ssh/authorized_keys 에 jad_deploy.pub 내용 추가

# 3) 개인키 내용을 SSH_PRIVATE_KEY 변수에 붙여넣기(cat jad_deploy 전체)
# 4) 호스트키를 SSH_KNOWN_HOSTS(File) 변수에 넣기
ssh-keyscan -H <DEV_SERVER_HOST>
```

> **SSH_PRIVATE_KEY 를 File 타입으로 쓰고 싶다면**: 변수 타입을 File 로 바꾸면 변수값이
> "임시파일 경로"로 주입된다. 그 경우 `.gitlab-ci.yml` 의 deploy `before_script` 에서
> `printf ... > ~/.ssh/id_deploy` 대신 `cp "$SSH_PRIVATE_KEY" ~/.ssh/id_deploy` 로 바꾼다
> (개행 손상 걱정이 없어 더 안전).

### central 시크릿 관련 (대개 CI 변수 불필요)

central/worker 런타임 시크릿(Jira watcher 토큰, `WORKER_SHARED_SECRET`, per-user 토큰 등)은
**CI 가 다루지 않는다** — 서버의 `/opt/jira-auto-dispatcher/secrets/` 와 `.env` 에 이미
배치돼 있고(DEPLOY.md 2~3단계), `deploy` 잡의 rsync 는 `secrets/`·`.env`·`config/config.yaml`
을 **제외**하므로 덮어쓰지 않는다. 즉 **이 값들을 CI 변수로 넣을 필요가 없다**.
(서버에 아직 안 깔았다면, CI 배포 전에 DEPLOY.md 2~5단계를 한 번 수행해 두어야 한다.)

---

## 3. 프로젝트 정책 (Settings → Merge requests / Repository)

- **Merge requests**: "Pipelines must succeed" 체크 →
  `only_allow_merge_if_pipeline_succeeds = true`. (MR 은 `test` 게이트를 통과해야 머지)
- **Protected branches**: `main` 을 Protected 로. push 는 Maintainer 만/직접 push 금지,
  머지는 MR 경유. (deploy 가 main 에서만 돌게 하는 안전장치 + Protected 변수 노출 조건)
- (선택) `main` 에 "Code owner approval" / 최소 승인 수 설정.

---

## 4. dlc-meta 레포 CI (배포 아님 — 검증 게이트만)

dlc-meta 는 **서비스가 아니라 런타임 pull 데이터**다(central/worker 가 매 실행 git pull).
배포 개념이 없고 **커밋 = 즉시 반영**이므로, 나쁜 커밋이 곧장 런타임에 물린다.
따라서 CI 는 배포가 아니라 **MR 게이트(검증)** 만 둔다: 인코딩(UTF-8·LF)·markdown 구조·
3층 디렉토리(`common/agents/runs`) 존재.

아래 내용을 **dlc-meta 레포 루트의 `.gitlab-ci.yml`** 로 배치한다(이 리포가 아님 —
운영자가 dlc-meta 에 나중에 넣는다):

```yaml
# .gitlab-ci.yml — dlc-meta (런타임 pull 데이터, 배포 없음).
# validate 게이트만: 인코딩(UTF-8·LF)·markdown 구조·3층 디렉토리 존재. MR 게이트.
# POLICY-ENCODING: UTF-8(BOM 없음)·LF.

stages:
  - validate

validate:
  stage: validate
  image: python:3.12-slim
  rules:
    - if: '$CI_PIPELINE_SOURCE == "merge_request_event"'
    - if: '$CI_COMMIT_BRANCH'
  script:
    # (1) 3층 디렉토리 골격 존재 확인
    - for d in common agents runs; do test -d "$d" || { echo "누락 디렉토리: $d"; exit 1; }; done
    # (2) BOM 금지 — 어떤 파일도 UTF-8 BOM(EF BB BF)로 시작하면 실패
    - |
      bom=$(git ls-files -z | xargs -0 -I{} sh -c 'head -c3 "{}" | od -An -tx1 | grep -qi "ef bb bf" && echo "{}"' 2>/dev/null || true)
      if [ -n "$bom" ]; then echo "BOM 발견:"; echo "$bom"; exit 1; fi
    # (3) CRLF 금지 — 추적 파일에 CR(0x0d)이 있으면 실패(LF 강제)
    - |
      crlf=$(git ls-files -z | xargs -0 -I{} sh -c 'grep -Il $(printf "\r") "{}" && echo "{}"' 2>/dev/null | sort -u || true)
      if [ -n "$crlf" ]; then echo "CRLF 발견:"; echo "$crlf"; exit 1; fi
    # (4) UTF-8 유효성 — 모든 .md 가 UTF-8 로 디코드되는지
    - git ls-files '*.md' -z | xargs -0 -I{} python -c "import sys; open('{}',encoding='utf-8').read()"
    # (5) markdown 구조 — 모든 .md 는 최소 한 개의 최상위 제목(# )을 가진다
    - |
      bad=""
      for f in $(git ls-files '*.md'); do
        grep -qE '^# ' "$f" || bad="$bad $f"
      done
      if [ -n "$bad" ]; then echo "최상위 제목(# ) 없는 md:$bad"; exit 1; fi
    - echo "dlc-meta validate 통과"
```

> 위 검사는 `git ls-files` 기준(추적 파일만) 이라 런타임 clone 산출물에 영향받지 않는다.
> 3층 디렉토리 이름(`common/agents/runs`)이 실제 구조와 다르면 (1)의 목록만 맞춰 수정한다.

---

## 5. GitHub (<your-github-owner>/jira-auto-dispatcher) — 개인용, 선택

개인 미러에는 배포 없이 pytest 게이트만 두는 워크플로우를 이미 넣어 두었다:
`.github/workflows/ci.yml` (push/PR 시 python 3.12 + `pytest -q`). 배포/이미지 빌드는
전적으로 사내 GitLab 파이프라인 몫이다. 원치 않으면 이 파일을 삭제하면 된다.

---

## 6. 배포 흐름 요약 (참고 — 자동화된 부분)

main 에 머지되면 GitLab 파이프라인이:

1. `test` — `pytest -q` (126 통과 게이트).
2. `build` — `docker build` 성사 게이트(러너에서, 레지스트리 전송 없음).
3. `deploy` — 소스를 `<DEV_SERVER_HOST>:/opt/jira-auto-dispatcher` 로 rsync(시크릿/상태 제외)
   → ssh 로 `deploy/redeploy-central.sh` 를 인자 전달 실행(heredoc 아님)
   → 서버에서 이미지 빌드 → **central 만** `--no-deps --force-recreate` → `/healthz` 폴링
   → 실패 시 이전 이미지로 롤백.

**worker(jad-worker-<user>)는 배포 중 절대 안 건드려진다** — 독립 컨테이너 + per-user
상태 볼륨이라 central 이 잠깐 교체돼도 in-flight 작업이 안 끊기고, worker 는 폴링을
재연결한다. central 영속 상태는 명명 볼륨(jad-state/jad-workspace)에 있어 recreate 로
유실되지 않는다.

> 서버에 최초 1회 셋업(config.yaml·secrets·.env·`docker compose up -d`)은 DEPLOY.md 를
> 따라 수동으로 끝내 둔 뒤부터 이 파이프라인이 이어받는다.
