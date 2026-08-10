"""jira-auto-dispatcher 애플리케이션 패키지.

Jira(HAN) 티켓이 지정 사용자에게 새로 할당되면 이를 감지하여 오케스트레이터
(`claude` CLI = ai-dlc-orchestrator)를 자율 실행하는 상시 디스패처.

이 앱은 *디스패처*일 뿐이다 — 상태 전이·티켓 팔로우·브랜치/MR 생성은 앱이
흉내내지 않고 오케스트레이터에게 위임한다. 자세한 설계 불변식은 CLAUDE.md 참고.

구성 요소(모듈):
    config      설정 로드/검증
    state       state/*.json 영속 계층
    jira_client Jira REST v2/v3 클라이언트
    gate        원자적 dedup claim(폴러·웹훅 수렴점)
    queue       잡 스토어 + 상태머신
    poller      high-watermark JQL 폴링
    webhook     얇은 웹훅 엔드포인트(기본 비활성)
    worker      claude -p 실행 + 한도 감지 + 재개
    scheduler   리셋시각 재개 + 야간 드레인 배치
    auth_login  브라우저 로그인 2-스텝(claude-hacker 이식)
    main        Flask app factory + 라우트 배선 + 백그라운드 스레드 기동
"""

__version__ = "0.0.0"  # Phase 0: 스캐폴딩
