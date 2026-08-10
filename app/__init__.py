"""jira-auto-dispatcher 애플리케이션 패키지 (2-역할: central | worker).

Jira(HAN) 티켓이 등록 사용자에게 새로 할당되면 이를 감지해, 그 사용자 정체성
으로 오케스트레이터(`claude` CLI = ai-dlc-orchestrator)를 자율 실행하는 시스템.
단일 도커 이미지가 env `ROLE`로 두 역할로 분기한다:

    central  상시 컨테이너 — Jira 감시 → dedup → 담당자를 등록 사용자에 매핑 →
             사용자 worker에 잡 배포. + 사용자 레지스트리/온보딩/관리 UI +
             사용자 worker 컨테이너 동적 spawn(Docker SDK). GitHub은 central만.
    worker   사용자별 동적 컨테이너 — Jira를 직접 안 봄. 중앙을 HTTP 폴링 →
             잡 수신 → 그 사용자 정체성(Claude/Jira/GitLab 토큰·git author)으로
             `claude -p` 실행 → 상태/로그 회신. 토큰 한도 감지·재개.

이 앱은 *디스패처*일 뿐이다 — 상태 전이·티켓 팔로우·브랜치/MR 생성은 앱이
흉내내지 않고 오케스트레이터에게 위임한다. 자세한 설계 불변식은 CLAUDE.md 참고.

구성 요소(모듈):
    [공통]
    config       설정 로드/검증(central 중심, run.* 공유)
    main         ROLE 분기 진입점(central app factory / worker 루프)
    auth_login   브라우저 로그인 2-스텝(claude-hacker 이식, 폴백 인증)
    [central]
    state        state/*.json 영속 계층(jobs/watermark/dedup/registry)
    jira_client  Jira REST v2/v3 클라이언트(감시 토큰)
    gate         원자적 dedup claim(폴러·웹훅 수렴점)
    queue        잡 스토어 + 상태머신(dispatch의 하부 저장)
    registry     등록 사용자 레지스트리 CRUD + 영속
    dispatch     사용자별 잡 큐 + central↔worker HTTP 프로토콜
    spawner      Docker SDK로 사용자 worker 컨테이너 spawn/stop/status
    poller       high-watermark JQL 폴링 → 사용자 매핑 → 디스패치
    webhook      얇은 웹훅 엔드포인트(기본 비활성)
    scheduler    리셋시각 재개 + 야간 드레인(사용자 큐 재-enqueue)
    [worker]
    worker       중앙 폴링 → 실행 → 상태 회신 + 한도 감지/재개
    agent_runner `claude -p` 실행 계약 구성 + per-user 정체성 주입
"""

__version__ = "0.0.0"  # Phase 0: 스캐폴딩
