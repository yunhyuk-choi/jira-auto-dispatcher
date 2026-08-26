"""jira-auto-dispatcher 애플리케이션 패키지 (2-역할: central | worker).

Jira(PROJ) 티켓이 등록 사용자에게 새로 할당되면 이를 감지해, 그 사용자 정체성
으로 오케스트레이터(`claude` CLI = ai-dlc-orchestrator)를 자율 실행하는 시스템.
단일 도커 이미지가 env `ROLE`로 두 역할로 분기한다:

    central  상시 컨테이너 — Jira 감시 → dedup → 담당자를 등록 사용자에 매핑 →
             사용자 worker에 잡 배포. + 사용자 레지스트리/온보딩/관리 UI +
             사용자 worker 컨테이너 동적 spawn(Docker SDK). GitHub은 central만.
    worker   사용자별 동적 컨테이너 — Jira를 직접 안 봄. 중앙을 HTTP 폴링 →
             잡 수신 → 그 사용자 정체성(Claude/Jira/GitLab 토큰·git author)으로
             `claude -p` 실행 → 상태/로그 회신. 토큰 한도 감지·재개.

이 앱은 *디스패처*가 본령이다 — 티켓 팔로우·브랜치/MR 생성·완료(done) 전이는 앱이
흉내내지 않고 오케스트레이터/사용자에게 위임한다. **예외: 진행중(in-progress) 전이는
디스패처가 워커 실제 착수 시 수행한다**(해야할일→진행중, 디스패치 유저 토큰·런타임 발견·
멱등·best-effort — worker_dispatch.run_dispatch). 착수 시점을 아는 결정적 주체가 디스패처
뿐이라 그렇다. 자세한 설계 불변식은 CLAUDE.md 참고.

구성 요소(모듈):
    [공통]
    config       설정 로드/검증(central 중심, run.* 공유)
    main         ROLE 분기 진입점(central app factory / worker 루프)
    auth_login   브라우저 로그인 2-스텝(claude-hacker 이식, 폴백 인증)
    [설치 관문] — 운영 경로가 아니라 **설치 시점**에만 쓴다(`python -m app.setup`)
    setup_schema   "무엇을 물어야 하는가"의 기계가 읽는 선언(정본)
    setup_validate 그 선언을 강제하는 검증기(CLI·웹 온보딩 **공용** 라이브러리)
    setup_render   예시 파일을 템플릿으로 config.yaml 생성(주석=사용자 안내 보존)
    setup_doctor   설정이 실제로 동작하는지 실측 진단(네트워크·도커·마운트 함정)
    setup          위 셋을 부르는 얇은 CLI 껍데기(종료코드가 곧 게이트)
    [central]
    state        state/*.json 영속 계층(jobs/watermark/dedup/registry)
    jira_client  Jira REST v2/v3 클라이언트(감시 토큰)
    gate         원자적 dedup claim(폴러·웹훅 수렴점)
    queue        잡 스토어 + 상태머신(dispatch의 하부 저장)
    registry     등록 사용자 레지스트리 CRUD + 영속
    dispatch     잡 등록(enqueue) + 완료 상태머신(report_status) + 관측/Tier-2 pending(인프로세스)
    central_dispatch 모든 디스패치 진입점이 수렴하는 프랙탈 센트럴 세션 방출 seam(주입+관측성)
    spawner      Docker SDK로 사용자 worker 컨테이너 spawn/stop/status
    poller       high-watermark JQL 폴링 → 사용자 매핑 → 프랙탈 방출
    scheduler    레포락/자원 어드미션 + 취소/재오픈/재배정 상태머신
    [worker]
    agent_runner `claude -p` 실행 계약 구성 + per-user 정체성 주입
"""

__version__ = "0.0.0"  # Phase 0: 스캐폴딩
