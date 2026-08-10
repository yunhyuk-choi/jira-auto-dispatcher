# jira-auto-dispatcher

Jira(HAN) 티켓이 새로 생겨 지정 사용자에게 할당되면 이를 감지해, 개발서버 도커
컨테이너에서 **오케스트레이터(`claude` CLI = ai-dlc-orchestrator)를 자율 실행**해
브랜치·MR 초안을 만드는 상시 **디스패처**다.

베이스는 `claude-web-wrapper`(claude-hacker): Flask로 `claude` CLI를 감싼 웹 래퍼
(브라우저 로그인 + `claude -p` 스트리밍)에서 재사용 자산을 이식했다.

## 목적

- Jira에 새 할당 티켓이 뜨면 사람 개입 없이 1차 개발(브랜치/MR 초안)을 자동 착수한다.
- 상태 전이·티켓 팔로우·브랜치/MR 생성 자체는 **디스패처가 흉내내지 않고
  오케스트레이터에게 위임**한다. 이 앱은 "감지 → 기동 → 재개" 만 책임진다.

## 아키텍처

```
Jira(HAN)
  │  (1) 신규 할당 감지
  ├── poller       high-watermark JQL 폴링(상시)
  └── webhook      얇은 웹훅(기본 비활성, 포트 열릴 때)
         │
         ▼  (2) 수렴
      gate         단일 원자적 dedup claim (중복 흡수)
         │
         ▼  (3) 큐잉
      queue        잡 스토어 + 상태머신
                   queued → running → (interrupted) → done/failed
         │
         ▼  (4) 실행
      worker       claude -p (오케스트레이터) 자율 실행
                   stream-json 파싱 → 토큰 한도 감지 → interrupted
         │
         ▼  (5) 재개
      scheduler    리셋시각 재개 + 야간 드레인 배치
```

- **영속(state/)**: jobs·watermark·dedup을 JSON으로 영속 → 재시작에도 복원.
- **웹훅-레디**: 폴러가 기본. 웹훅은 켜면 동일한 게이트로 수렴한다(중복 안전).
- **auth_login**: 브라우저 2-스텝 로그인(`claude auth login`)을 웹에서 완료
  (컨테이너 `~/.claude` 볼륨에 영속).

## 실행

```bash
pip install -r requirements.txt

# 설정 준비
cp config/config.example.yaml config/config.yaml   # 값 채우기(계정ID/토큰 경로 등)

python -m app.main        # 관리 콘솔: http://127.0.0.1:5000 (기본 0.0.0.0:5000)
```

> 현재는 스캐폴딩 단계다. 동작하는 것은 관리 UI 렌더 + 브라우저 로그인
> (`/start-login`·`/complete-login`) + `/healthz` 뿐이며, 폴러/워커/스케줄러 등
> 핵심 로직은 이후 Phase에서 구현된다(각 모듈 docstring의 Phase 마커 참조).

## ⚠️ 보안 (설계상 중요)

- 워커는 `claude -p ... --dangerously-skip-permissions`로 **도구 권한을 가진 자율
  에이전트**를 실행한다 — 파일 편집·셸 실행 가능. 이는 사실상 **원격 코드 실행(RCE)
  표면**이다.
- 승인할 사람이 없는 자율 실행 전제이므로 권한을 죽일 수 없다. 대신
  **사내망·신뢰 환경 한정**으로만 구동하고 외부에 노출하지 않는다.
- 폭주 방지: `concurrency: 1`, 결정적 브랜치(`auto/<TICKET>`)로 멱등 재개,
  사고 시 브랜치 삭제로 리셋(가역성).

## 연동 레포 (런타임 clone, gitignore됨)

| 레포 | 역할 | 컨테이너 경로(config `run`) |
|---|---|---|
| ai-dlc-orchestrator | 오케스트레이터(`claude`가 이 정체성으로 동작) | `/app/orchestrator` |
| dlc-meta | PR 거버넌스·에이전트 오버레이·런 저널(3층) | `/app/dlc-meta` |
| dataspace_docs | 설계 문서 저장소 | `/app/dataspace_docs` |

## 설정

`config/config.example.yaml`이 스키마 정본이다. `config/config.yaml`(gitignore됨)로
복사해 채운다. 시크릿(Jira 토큰·웹훅 시크릿)은 값이 아니라 **파일 경로**로 참조한다
(`token_file`, `shared_secret_file`).
