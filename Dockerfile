# jira-auto-dispatcher — 단일 컨테이너 이미지 (ROLE로 central|worker 분기)
#
# 하나의 이미지를 두 역할로 재사용한다. ENTRYPOINT는 그대로 `python -m app.main`
# 이며, 앱이 env ROLE(central|worker)을 읽어 분기한다(app/main.py).
#   central: 관리 UI + Jira 감시/디스패치 + worker 컨테이너 spawn(Docker SDK)
#   worker : 중앙 HTTP 폴링 → `claude -p` 자율 실행 → 상태 회신
#
# 이번 페이즈: 동작 가능한 최소 골격까지. 완성은 Phase 6.
# POLICY-ENCODING: 생성 파일 UTF-8(BOM 없음)·LF.

FROM python:3.12-slim

# --- 시스템 의존성: git (worker의 per-user 커밋/브랜치/MR에 필수) ---
# TODO(Phase 6): ca-certificates, curl, (docker CLI는 SDK로 대체하므로 불필요) 확정.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# --- claude CLI 설치 ---
# TODO(Phase 6): 설치 방식 확정. claude CLI는 node 런타임 기반이므로 아래 중 택1:
#   (a) node 베이스 이미지로 전환 후 `npm i -g @anthropic-ai/claude-code`
#   (b) 공식 설치 스크립트(curl -fsSL https://claude.ai/install.sh | sh) — 네트워크 전제
# 지금은 자리표시자(런타임에 PATH에 claude가 있다고 가정).
# RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

# --- 파이썬 의존성 ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 앱 소스 ---
COPY . .

# --- 런타임 전제(볼륨/네트워크는 docker-compose 및 spawner가 배선) ---
#   ~/.claude          Claude 세션/인증 영속(사용자별 볼륨; worker)
#   state/             central 영속(jobs/watermark/dedup/registry) — central만
#   workspace/         오케스트레이터 작업 공간
#   orchestrator/ dlc-meta/ dataspace_docs/  런타임 clone(앱이 소유하지 않음)
#   env ROLE           central(기본) | worker
#   env(worker)        DISPATCH_USER, CENTRAL_URL, CLAUDE_CODE_OAUTH_TOKEN
EXPOSE 8787

# central·worker 공통 진입점(내부에서 ROLE 분기).
# TODO(Phase 6): 프로덕션 central은 gunicorn 등 WSGI로 대체 검토.
ENTRYPOINT ["python", "-m", "app.main"]
