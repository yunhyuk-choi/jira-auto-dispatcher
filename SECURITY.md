# SECURITY.md — jira-auto-dispatcher 보안 태세 & 자율 실행 인가 기록

> 이 문서는 **운영자가 자율 full-permission 실행을 명시적으로 인가했다는 기록**이며,
> 그 근거와 보완 통제를 함께 남긴다. worker 컨테이너는 사람 승인 없이 도구 권한을
> 가진 Claude 에이전트를 헤드리스로 실행하므로, 이 인가는 **의식적 결정**이다.

---

## 1. 무엇을 인가했는가 (명시적 결정)

worker 컨테이너의 `claude`는 `--dangerously-skip-permissions`를 **헤드리스**로 실행한다.
대화형 승인 다이얼로그를 사람이 눌러줄 수 없으므로, 스포너(`app/spawner.py`)가
컨테이너 생성 시 그 사용자 `~/.claude/settings.json`(`CLAUDE_CONFIG_DIR`)에
**사전 인가 설정**을 써 넣는다:

```json
{
  "permissions": { "defaultMode": "bypassPermissions" },
  "skipDangerousModePermissionPrompt": true,
  "skipAutoPermissionPrompt": true,
  "skipWorkflowUsageWarning": true
}
```

- 이는 **시스템 레벨 인가**다 — 모든 worker 컨테이너에 공통 적용되며, **사용자 입력이
  필요 없다**. 온보딩은 자격증명(Jira/GitLab/Claude 토큰)만 받고 **권한 승인 단계는 없다**.
- 이 파일은 read-only 바인드로 주입되어 컨테이너 런타임이 되돌릴 수 없다.

## 2. 왜 인가가 불가피한가 (근거)

- **헤드리스 자율 전제.** 이 시스템의 목적은 신규 Jira 티켓에서 사람 개입 없이
  worker 오케스트레이터를 기동해 레포 작업을 수행하는 것이다(RECURSIVE-DISPATCH.md §2·§5).
  대화형 승인 루프는 자율 실행과 원천적으로 양립하지 않는다 — 눌러줄 사람이 없다.
- 따라서 권한을 "죽여" 대화형으로 되돌리는 것은 선택지가 아니며, 대신 **실행 표면을
  격리·가역·조임으로 감싸는 보완 통제**로 위험을 관리한다(아래 §3).

## 3. 보완 통제 (위험 관리의 실체)

| 통제 | 내용 | 위치 |
|---|---|---|
| **사내망 한정** | 외부 노출 금지. central/worker는 신뢰 사설망(`spawn.network`)에서만 통신. | README·CLAUDE.md |
| **MR 게이트 · 자동머지 없음** | 산출은 브랜치/MR **초안**까지. 병합은 사람이 한다. | RECURSIVE-DISPATCH.md §5 |
| **가역성** | 산출은 항상 가역 단위(브랜치 `auto/<TICKET>`·MR). 사고 시 브랜치 삭제로 리셋. | §6 |
| **dedup 게이트** | 폴러·웹훅을 수렴해 중복 트리거 제거(무한루프 방지). | `app/gate.py` |
| **트리거=작업 티켓** | worker는 새 사이클 티켓을 만들지 않는다(자기되먹임 루프 차단). | §6 |
| **동시성/레포락** | worker당 동시성 1 + 레포 단위 배타락(같은 레포 병렬 금지 → 교차오염 차단). | `app/scheduler.py`·§4 |
| **컨테이너 격리** | 사용자마다 별도 컨테이너. `mem_limit`(기본 4g)는 하드 백스톱. | `app/spawner.py`·§4 |
| **비-root 실행** | worker 컨테이너는 `user=1000:1000`(`spawn.run_as`)로 실행 — 특권 축소. | `app/spawner.py` |
| **시크릿 볼륨(ro)** | 토큰은 값이 아니라 per-user 시크릿 디렉토리를 **read-only** 마운트로 노출. 다른 사용자 시크릿은 안 보인다. Jira/GitLab은 값 대신 **파일경로**로 주입. | `app/spawner.py` |
| **시크릿 미노출** | 시크릿 값은 `config.read_secret`로만 읽고, env dict·예외·로그에 절대 싣지 않는다. 온보딩 응답에도 토큰 값 없음. | `app/onboarding.py`·`app/spawner.py` |
| **GitHub 격리** | GitHub 토큰은 central 전용 — worker env에서 상속분까지 제거. | `app/agent_runner.py` |
| **안전 기본 enabled=false** | 온보딩 사용자는 비활성으로 시작. 운영자가 검토 후 명시적으로 활성화. | `app/onboarding.py` |
| **향후 조임** | `permission_level`로 사용자별 권한을 조일 수 있다(현재 `bypass`만; `sandbox`·`allowlist`는 TODO 분기). | `app/spawner.py` |

## 4. docker.sock 특권 (별도 위험)

central의 스포너가 `docker.sock`에 접근해 컨테이너를 띄우는 것은 사실상 **호스트 root
권한과 동치**다(특권 상승 표면). 완화책:

- **docker-socket-proxy**(tecnativa 등)를 앞단에 두어 `CONTAINERS`/`POST`만 최소 허용하고,
  central은 프록시 TCP 엔드포인트로만 접근한다(`spawn.docker_host=tcp://socket-proxy:2375`).
  sock 직결은 개발/사내망 한정.
- **central 자체를 비-root**로 실행, 사내망 한정.

## 5. permission_level로 조이기

레지스트리 사용자 레코드의 `permission_level`(기본 `bypass`)로 사전 인가 강도를
사용자별로 조정할 수 있다. 현재는 `bypass`만 구현돼 있고, `sandbox`(격리 FS/네트워크)·
`allowlist`(도구/명령 화이트리스트)는 `app/spawner.py`의 `render_settings()`에 **TODO 분기
자리**로 예약돼 있다. 권한을 더 조이려면 이 분기를 채우고 사용자 `permission_level`을
변경하면 된다.

---

**요약:** 자율 헤드리스 실행에는 대화형 승인이 성립하지 않으므로 운영자가 bypass 사전
인가를 **명시적으로 채택**했고, 그 위험은 사내망 한정·MR 게이트(자동머지 없음)·가역성·
dedup·동시성/레포락·컨테이너 격리·비-root·시크릿 ro 볼륨·시크릿 미노출·`permission_level`
조임 여지의 다층 통제로 관리한다.
