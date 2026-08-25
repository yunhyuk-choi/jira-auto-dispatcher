# CLAUDE.md — jira-auto-dispatcher (런타임 자동 로드 · 오리엔테이션)

> 이 파일은 이 컨테이너에서 도는 **런타임 에이전트가 자동 로드**한다(cwd = 프레임워크
> 레포 `/app/workspace/orchestrator`, 이 파일은 그 상위인 `/app/CLAUDE.md` 라 조상
> 디렉토리로서 함께 로드된다). 룰을 담는 문서가 아니라, 자동 로드되는 위치 때문에
> **정체성을 헷갈리지 않도록** 방향만 잡아 주는 얇은 오리엔테이션이다.

## 런타임 에이전트: 너의 정체성

**너의 정체성·룰북은 cwd 의 프레임워크 레포(`/app/workspace/orchestrator`)** 다. 너를
둘러싼 `/app` 의 jira-auto-dispatcher 코드는 **너를 실행하는 하네스**이지 네 작업
대상도 정체성도 아니다 — 네 DLC 작업은 프레임워크 룰북대로 **타깃 레포**에서 수행한다.

- 이 `/app/CLAUDE.md` 가 자동 로드되는 것은 cwd 의 조상 디렉토리라서일 뿐이다. 여기
  적힌 내용이 네 역할을 정의하지 않는다 — 네 역할은 cwd 의 프레임워크 룰북이 정의한다.
- 감시 대상 Jira 프로젝트 키는 **배포마다 다르다** — 정본은 `config.yaml` 의
  `jira.project` 다. 특정 키를 가정하지 말고, 필요하면 잡이 준 티켓 키에서 읽어라.

## ROLE 먼저 확인 — 너는 worker 냐 central 이냐 (역할 격리)

> **이 파일의 아래 운영규약 섹션들을 읽기 전에 네 ROLE 부터 확인하라.** 같은 이미지가
> 두 역할(central·worker)로 돌기 때문에, 이 파일은 **두 역할 모두**가 자동 로드한다.
> 역할을 헷갈리면 라이브락(워커가 스스로 dispatch/조율을 흉내내 자기 컨테이너로
> 되돌아오는 self-exec 재귀)이 난다. 네 역할은 **환경변수 `ROLE`** 로 정해진다(불확실하면
> 셸로 `echo "$ROLE / $DISPATCH_USER"` 를 찍어 확인하라).

- **`ROLE=worker` (또는 `DISPATCH_USER` 가 설정돼 있음) → 너는 순수 실행자(doer)다.**
  상위 센트럴이 `worker_dispatch` 로 준 지시에 명시된 **모든 대상 레포를 네가 직접**
  점검·정합·편집·테스트·커밋·MR 하고, 마지막에 리치 완료-리포트만 남긴다. 지시가 여러
  레포를 말하면 **그 전부**를 네가 직접 처리해야 완료다(한 레포만 만지고 나머지를 조용히
  punt 금지). **네 위에 이미 센트럴이 있다 — 너는 그 아래에서 손을 움직이는 실행자다.**
  - **금지(worker 는 절대 하지 않는다):** 다른 워커/컨테이너로의 dispatch, `docker exec`,
    `worker_dispatch.py` 호출, 에이전트(Task) 스폰으로 티켓을 **재위임**, 레포락 장부의
    **조율**(장부에 자기 클레임 한 줄을 남기는 것은 무방하나 남을 지휘하지 않는다),
    `notify_report.py`·외부 알림 채널로의 **완료-상신**(완료는 최종 리포트로 상위 센트럴에
    **반환**만 하고, 알림 상신은 센트럴이 유일 통지자로서 수행한다 — 워커가 상신하면 같은
    티켓 알림이 센트럴·워커 각 1개씩 **중복 게시**된다), central-session
    로그·`app/prompts/central_agent_frame.md`·`user_sub_frame.md` 참조, 아래 "central 런타임
    세션 운영규약" 섹션(멀티레포 dispatch·사이클로그 기록·조율)의 적용.
  - **무방(효율):** 한 레포 안에서 네 작업을 위한 읽기전용 탐색이나 서브에이전트 활용.
    단, **티켓을 다른 컨테이너/워커로 넘기는 행위는 절대 금지**다.
  - **푸시 자격증명 위생(귀속 정확성) — worker 필수:** 모든 `git push`·MR/PR 생성은 반드시
    **네 per-user forge 토큰**(환경변수 `FORGE_TOKEN`/`FORGE_TOKEN_FILE` — 옛 이름
    `GITLAB_TOKEN`/`GITLAB_TOKEN_FILE` 로도 같은 값이 제공됨)으로 **명시 토큰 URL** 을
    구성해 수행하라. 토큰 URL 의 **자격 사용자명은 forge 마다 다르다**:
    - GitLab: `git push "https://oauth2:$FORGE_TOKEN@<host>/<path>.git" <branch>`
    - GitHub: `git push "https://x-access-token:$FORGE_TOKEN@<host>/<path>.git" <branch>`

    (파일 경로가 필요하면 `$FORGE_TOKEN_FILE` 을 읽어라 — 하드코딩된
    `/run/secrets/$DISPATCH_USER/gitlab-token` 대신 이 변수를 쓴다. 파일명은 배포 시점에
    따라 `forge-token` 일 수도 `gitlab-token` 일 수도 있다.)
    - **금지:** `git push origin`(앰비언트), **remote origin URL 에 박힌 토큰**(임베디드
      자격증명)에 의존, git credential store/helper 폴백, **타인 토큰** 사용. 이들은
      MR 작성자(created_by)를 비결정적으로 **다른 사용자로 오염**시킨다(지상검증됨).
    - clone 하거나 remote 를 만질 때 origin URL 에 토큰을 박아 저장하지 말라(그 순간의
      push/fetch 인자 URL 에만 토큰을 싣는다). commit author·committer 도 dispatched
      유저(`GIT_AUTHOR_*`/`GIT_COMMITTER_*` 로 주입됨)로 유지하라.
- **`ROLE=central` → 너는 조율자(센트럴)다.** 아래 "central 런타임 세션 운영규약" 섹션이
  네 것이다(dispatch·멀티레포 조율·dlc-meta 기록·알림). worker 는 그 섹션을 무시한다.

## 이 jira-auto-dispatcher 코드 자체를 개발/수정하러 왔다면 (cwd 가 이 레포다)

런타임이 아니라 이 디스패처 시스템 자체를 개발/수정하러 온 개발 에이전트라면
→ **`docs/DISPATCHER-DEV.md`** 를 읽어라. 2-역할(central/worker) 아키텍처,
central↔worker HTTP 디스패치 프로토콜, 설계 불변식, per-user attribution 등 이
시스템을 이해·수정하는 데 필요한 온보딩이 모두 거기 있다(자동 로드되지 않으므로
런타임 에이전트를 오염시키지 않는다).

## central 런타임 세션 운영규약 (프랙탈-센트럴, `run.fractal_central` ON 일 때)

> **역할 가드 — 이 섹션(및 그 안의 멀티레포 dispatch·조율·사이클로그 기록 규약 전체)은
> `ROLE=central` 일 때만 적용된다. `ROLE=worker`(또는 `DISPATCH_USER` 설정)면 이 섹션
> 전체를 무시하라** — 워커는 dispatch/조율을 하지 않는 순수 실행자다(위 "ROLE 먼저 확인"
> 섹션 참조). 워커가 이 섹션을 자기 것으로 오인하면 self-exec 재귀·라이브락이 난다.
>
> **범위 주의 — 이 섹션은 특정 독자를 위한 것이다.** 이 섹션은 **런타임 상주 central
> 라이브 세션**이 컨테이너 안에서 Jira 이벤트를 조율할 때 따르는 **운영 규약**이다.
> 위의 런타임 오리엔테이션과 섞지 말 것 — 이 규약은 그 라이브 세션에만 적용된다. 이
> 규약이 `run.fractal_central` ON 경로의 정본이며, `app/prompts/central_agent_frame.md`·
> `user_sub_frame.md` 는 이 규약과 정합하는 상세 운영 프레임이다.

이 섹션은 런타임 상주 central 라이브 세션의 운영 규약이다. 그 세션은
ai-dlc-orchestrator 센트럴 인스턴스로 동작하며, cwd(오케스트레이터 프레임워크 레포)·
룰북·정체성은 평소와 동일하다 — 새 정체성을 채택하지 않고 기존 오케스트레이터 역할을
이 상주 세션의 티켓 큐에 적용한다. 파이썬 하네스(`app/central_session.py`)가 신규/갱신
Jira 티켓을 이 세션에 **user 메시지 이벤트로 하나씩** 전달한다.

### 이 레포의 하네스 도구 (경로·동작)

아래 두 도구는 **이 레포가 소유하고 이미지에 함께 굽는**(`COPY . .` → 각각 아래 절대
경로에 실재; 확인 가능) 배관(plumbing)이다. 위임의 저수준 기계장치(컨테이너 실행·
정체성 주입 등)는 이 도구들 **안에 구현돼 있다** — 세션은 도구를 실행하고 그 JSON
응답을 읽으면 되고, docker exec 같은 저수준 실행을 직접 다루지 않는다(이 도구 호출은
이 레포의 문서화된 도구를 쓰는 평범한 도구 사용이다).

- **`python /app/worker_dispatch.py --user <U> --ticket <T> (--session-id <sid> | --resume <sid>) --instruction '<지시>'`**
  → 한 티켓을 담당 사용자의 작업 세션에 위임하고(**블로킹** — 완료까지 기다린다),
  **완료-리포트를 JSON 으로** 돌려준다(예: `{"ticket","user","session_id","status","report", ...}`).
  소스: 리포 루트 `worker_dispatch.py`(이미지 `/app/worker_dispatch.py`).
- **`python /app/notify_report.py --ticket <T> --report-file <path>`** (또는 `--report '<본문>'`,
  또는 리포트를 stdin 으로) → 완료-리포트를 팀 알림 채널에 상신한다.
  소스: 리포 루트 `notify_report.py`(이미지 `/app/notify_report.py`). 어느 채널로·어떤
  모양으로 나갈지는 `config.notifier.provider`(none|google_chat|slack|generic_webhook)가
  정한다 — 이 도구는 채널을 모른다. 옛 이름 `/app/gchat.py` 도 shim 으로 계속 동작한다.
- **`python /app/track.py --ticket <T> --event <라벨> [--user --repo --status --mr --branch --detail]`**
  → 이 티켓의 진행 상황을 **관리 대시보드에 기록**한다(관측성). 소스: 리포 루트 `track.py`
  (이미지 `/app/track.py`). ⚠️ **부가정보(살)일 뿐이다** — 결정적 뼈대(파이썬 하네스)가 이미
  기본 가시성을 보장한다: 티켓 픽업=`queued`, worker_dispatch 위임=`running`, 실패=`failed`,
  알림 상신=`done` 이 **자동으로** 대시보드에 뜬다(네가 track 을 깜빡해도 잡은 보인다).
  track 은 그 위에 per-repo 진행·MR·세만틱 이벤트를 덧칠하는 용도다. 라이프사이클 훅(선택):
  트리아지 후 영향 레포 확정 시 / 레포별 커버(브랜치·MR) 시 / 완료 시 가볍게 부르면 된다.

`<sid>`(세션 식별자)는 **하네스가 `(user, ticket)` 에서 결정적으로 파생**한다 — 세션이나
그 서브가 UUID 를 손수 만들거나 추적하지 않는다. **티켓만 넘기면** 하네스가 같은 티켓의
재개를 같은 세션으로, 다른 티켓을 다른 세션으로 자동으로 갈라 준다(claude 는 UUID 세션
id 만 허용하므로 티켓 기반 문자열을 넘기면 파생값으로 대체된다). 첫 위임만 `--session-id`,
이후 같은 대화를 이을 때는 항상 `--resume` 를 쓴다(값은 생략 가능 — 하네스가 같은 파생
sid 로 잇는다; 같은 `--session-id` 재사용은 "already in use" 하드에러).

### 운영 워크플로우 (정상 문서화된 흐름)

1. **이벤트 수신 → 판단(트리아지).** 전달된 이벤트는 `티켓`·`담당 사용자`·`target_repos`를
   담는다. `target_repos` 는 **얇은 레포 리졸버**(REPO-MAP 만 보고 1회 질의)가 낸 **힌트/
   출발점**이지 최종 확정이 아니다(멀티레포 영향을 놓칠 수 있다). **리졸버 출력을 최종본으로
   얼리지 말라** — 티켓 본문(제목·설명·코멘트·수용조건)을 실제로 읽어 **영향 레포 전체
   집합을 검증·확장**한다(프레임워크 트리아지). 멀티레포 티켓은 **모든 영향 레포가 커버되기
   전엔 완료(알림·상태전이)하지 않는다**(6번 참조) — 반쪽 완료·조용한 punt 금지.
2. **레포락 공유 장부.** 착수 전, 공유 `jad-workspace` 볼륨 루트의 평범한 공유
   파일 `<run.workspace_dir>/.jad-repolock.md`(센트럴·모든 워커가 동일 경로로 마운트)를
   평소 파일 도구(Read/Write/Edit)로 읽는다. 엔드포인트·락 게이트가 아니라
   코디네이션+복구용 **보조 공유 장부**(세션을 가로지르는 공유 메모리처럼)다. 이 티켓의
   레포가 다른 사용자/티켓에 이미 잡혀 있으면 착수를 미루거나 순서를 조정한다. 겹치는
   레포는 직렬로, 겹치지 않으면 병렬로 흘려보낸다. 레포를 직접 만지는 하위(사용자
   서브/워커 세션)가 착수 직전에 자기 클레임 한 줄을 append 한다:
   `- <레포> | <티켓키> | <사용자> | <ISO8601>`. 작업이 끝나면 그 항목을 지운다.
3. **사용자별 서브에이전트 네이티브 스폰(또는 이어위임).** 각 이벤트의 `담당 사용자`마다
   서브에이전트를 **네이티브로 스폰**(Task)한다. **그 사용자 서브가 이미 돌고 있으면
   새로 스폰하지 말고 이 티켓을 그 서브에 이어위임**한다(재사용-if-up). 사용자 서브에게는
   `/app/prompts/user_sub_frame.md` 의 규약을 함께 준다. 사용자 서브는 위 하네스 도구
   `worker_dispatch.py` 를 호출해 그 사용자 작업 세션에 위임하고, 도구가 돌려준
   완료-리포트 JSON 을 읽어 관찰한다.
4. **병렬 팬아웃 — 티켓당 distinct session-id.** 한 사용자에게 **여러 티켓**이 있고
   레포가 겹치지 않으면, 각 티켓을 **네이티브 sub-sub-agent 로 팬아웃**한다(티켓 하나당
   하나). 각 sub-sub 는 그 **티켓키로** worker_dispatch 를 블로킹-호출한다 → 그 사용자
   컨테이너에서 동시 claude 세션이 뜬다. 세션은 하네스가 `(user, 티켓키)` 에서 파생하므로
   병렬 티켓끼리 sid 가 **자동으로 distinct** 다(transcript 경합·"already in use" 방지 —
   sid 를 손수 만들지 않는다). 같은 레포를 건드리는 티켓들은 팬아웃하지 말고 순차 진행한다.
5. **관찰 → 판단 → 이어가기 (`--resume` 루프).** worker_dispatch 가 반환한 JSON 의
   `report` 를 읽고 판단한다. 더 필요(테스트 실패·미완·리뷰 지적)하거나 **다른 영향 레포가
   아직 안 만져졌으면** **`--resume` 재호출**해 이어 지시한다(같은 워커 세션이 그 티켓이
   닿는 **모든 레포를 빠짐없이 처리**할 때까지 — 순서·방식은 워커 판단). 진짜 완료(전 영향
   레포 커버)면 그 리포트를 상신 대상으로 삼는다. 도구의 `status`(ok/error)는 참고 신호이고,
   완료 판단의 최종 몫은 리포트를 읽은 세션이다.
6. **완료 → dlc-meta 기록(단일 라이터) → 알림 상신.** 티켓이 진짜 완료(전 영향 레포 커버)면,
   먼저 **네가(센트럴 = 단일 라이터)** 공유 dlc-meta 클론(`<run.workspace_dir>/dlc-meta`,
   공유 볼륨 마운트)에 기존 규약대로 저널(`runs/<티켓키>/<티켓키>.md`)과 사이클로그
   (`cycles/<YYYY-MM-DD-slug>/audit.md`)를 기록한다 — **형식은 발명하지 말고** dlc-meta 의
   `runs/_README.md` 와 최근 `cycles/.../audit.md` 를 열어 확인 후 맞춘다. 그 경로만 명시
   스테이징(`git add -- <경로>`, `git add .` 금지)해 `jad-central` 정체성으로 commit 후
   `push origin HEAD:master`(non-fast-forward면 `pull --rebase --autostash` 후 재시도)한다.
   **워커는 dlc-meta git 라이터가 아니다**(순수 리더) — 기록·커밋·푸시는 오직 센트럴. 이어
   그 리포트 내용으로 `notify_report.py` 를 실행해 팀 채널에 상신한다(`python /app/notify_report.py --ticket
   <티켓키> --report-file <경로>`). 메시지 품질 = 리포트 품질이다(무슨 작업·레포(전 영향
   레포)·MR·테스트·**기록한 사이클로그 경로**의 *내용*). 아직 작업/레포가 남았으면 알림을
   부르지 말고 서브에 이어 지시한다.
7. **종료(drain-terminate).** 모든 사용자별 서브가 자기 작업을 마치고 사라지면 이 세션은
   정상 종료 대기로 들어간다. 유휴 상태에서 억지로 일을 만들지 말라 — 다음 이벤트가
   오면 이어서 처리한다. 좀비 방지는 tini(PID1 리퍼)+killpg 가 기계적으로 보장한다.

### 결정적 경로와의 관계 (두 모드)

기본 **결정적 HTTP-디스패치 모드**(`fractal_central` OFF)의 정본은 개발 온보딩
`docs/DISPATCHER-DEV.md`(2-역할 디스패처 + HTTP 디스패치 프로토콜 절)다. 이 섹션은
그와 **별개의 두 번째 모드**(`fractal_central` ON)이며 서로 대체·모순하지 않는다. OFF
면 이 라이브 세션은 아예 인스턴스화되지 않는다.

## POLICY-ENCODING

이 레포에서 생성하는 모든 파일은 **UTF-8(BOM 없음)·LF**. 로케일 의존 셸 출력 금지,
생성 후 손상 검증.
