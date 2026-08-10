# CLAUDE.md — jira-auto-dispatcher (이 레포에서 작업할 Claude 온보딩)

> 이 파일은 Claude Code 세션이 **이 리포지토리**에서 작업할 때 읽는 온보딩 문서다.
> 사람용 개요는 [README.md](README.md). 여기서는 *다른 Claude가 맥락을 빠르게 잡고
> 설계 불변식을 깨지 않도록* 의도·함정·경계를 기록한다.

## 이 앱의 역할 = **디스패처** (경계를 지켜라)

이 앱은 Jira(HAN) 티켓 신규 할당을 **감지**해서 오케스트레이터(`claude` CLI =
ai-dlc-orchestrator)를 **기동**하고, 토큰 한도로 끊기면 **재개**한다. 그게 전부다.

- **상태 전이·티켓 팔로우·브랜치/MR 생성은 앱이 흉내내지 말 것.** 그것은
  **오케스트레이터가 담당**한다. 앱은 티켓을 감지해 워커로 넘길 뿐이다.
- 즉 "감지 → dedup → 큐 → 워커(claude 기동) → 재개"의 파이프라인만 앱의 책임이다.

## 핵심 설계 불변식 (깨지 말 것)

1. **단일 원자 dedup 게이트** — 폴러·웹훅 둘 다 반드시 `gate.claim()`으로 수렴한다.
   어느 경로도 큐에 직접 넣지 않는다. 중복 트리거는 게이트가 흡수한다.
2. **루프 차단** — 무한 자동화 루프를 다음으로 막는다:
   - 트리거 티켓 = 작업 티켓 (오케스트레이터는 새 티켓을 만들지 않고 기존 티켓으로 처리)
   - dedup(같은 티켓 재트리거 차단)
   - 동시성 1 (자율 에이전트 폭주 방지)
   - 브랜치/MR 가역성 (사고 시 `auto/<TICKET>` 브랜치 삭제로 리셋)
3. **학습은 로컬(사람 교정)만** — 개발서버의 자율 실행은 **학습층 read-only**다.
   자기 출력을 다시 학습하지 않는다(에코 챔버 방지).
4. **재개(resume)** — `claude -p --resume <session-id>`(또는 `--from-pr <PR#>`)로
   이어간다. 폴백은 런 저널 + 결정적 브랜치. `~/.claude` 볼륨 영속이 전제다.
5. **토큰 한도(Max 롤링)** — 워커가 `--output-format stream-json`으로 한도를
   감지하면 잡을 `interrupted`로 두고 `reset_at`을 파싱한다. 재개는
   리셋시각 스케줄러 / 야간 드레인 / UI "지금 재개" 버튼 중 하나로 트리거된다.

## dlc-meta 3층 구조

| 층 | 경로 | 내용 |
|---|---|---|
| 공통 | `common/` | PR 거버넌스(프론트=컨벤션 성숙, 백엔드=WIP) |
| 사용자 오버레이 | `agents/<user>/` | 사용자별 에이전트 오버레이(config `triggers[].agent` 키) |
| 런 저널 | `runs/<ticket>/` | 티켓별 실행 저널(재개 폴백 컨텍스트) |

## 자율 모드 A/B

`config` 의 `triggers[].autonomy_mode`로 분기한다(둘 다 구현 대상):

- **A = 완전자율 MR 초안** — 컨벤션이 성숙한 레포(예: portal-frontend)에 적합.
- **B = 경량 1차 + 로컬 완성** — 컨벤션 WIP 레포(예: portal-backend)에 적합.

레포별 오버라이드는 `triggers[].per_repo`로 지정한다.

## 구현 로드맵 (Phase 마커)

각 모듈 docstring에 담당 Phase가 명시돼 있다. 스캐폴딩(Phase 0)은 완료:

| Phase | 모듈 | 내용 |
|---|---|---|
| 0 (완료) | `auth_login.py`, `main.py`(부분), `templates/index.html` | 로그인 이식 + app factory 골격 + 관리 UI |
| 1 | `config.py`, `state.py` | 설정 로드/검증 + state/*.json 영속 |
| 2 | `jira_client.py` | Jira REST(issue/transition/comment/JQL) |
| 3 | `gate.py`, `queue.py` | 원자적 dedup 게이트 + 잡 상태머신 |
| 4 | `poller.py`, `webhook.py` | high-watermark 폴링 + 웹훅 수렴 |
| 5 | `worker.py`, `scheduler.py`, `main.py`(배선) | claude 실행/한도/재개 + 스케줄러 |
| 6 | `Dockerfile`, `docker-compose.yml` | 컨테이너/배포 |

## ⚠️ 보안

- 워커의 `--dangerously-skip-permissions`는 도구 권한 자율 에이전트 = **RCE 표면**이다.
  승인할 사람이 없는 자율 실행 전제라 권한을 죽일 수 없다 → **사내망·신뢰 환경 한정**,
  외부 노출 금지. 폭주 방지는 동시성 1 + dedup + 브랜치 가역성으로 한다.

## 함정 (claude-hacker 계승)

- **인코딩(Popen)**: Windows(cp949)에서 `claude` UTF-8 출력 디코딩 실패를 막으려면
  모든 `subprocess.Popen`은 `text=True, encoding='utf-8', errors='replace'`.
- **커맨드명**: 로그인은 `claude auth login`(‘claude login’은 없음), 비대화형 실행은
  `claude -p <prompt>`.
- **로그인 코드 주입**: `claude auth login`은 `code#state`를 CLI **stdin에 붙여넣어**
  완료한다. CLI가 TTY 전용으로 코드를 읽으면 pty(pywinpty) 우회가 필요할 수 있다.
- **파일명**: 프론트 템플릿은 `templates/index.html`이다(claude-hacker의 오타
  `intex.html`을 이 레포에서 정정함). `render_template('index.html')`과 일치.

## POLICY-ENCODING

이 레포에서 생성하는 모든 파일은 **UTF-8(BOM 없음)·LF**. 로케일 의존 셸 출력 금지,
생성 후 손상 검증.
