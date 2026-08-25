# jira-auto-dispatcher — 단일 컨테이너 이미지 (ROLE로 central|worker 분기)
#
# 하나의 이미지를 두 역할로 재사용한다. ENTRYPOINT는 `python -m app.main` 이며,
# 앱이 env ROLE(central|worker)을 읽어 분기한다(app/main.py).
#   central: 관리 UI + Jira 감시/디스패치 + worker 컨테이너 spawn(Docker SDK)
#   worker : 중앙 HTTP 폴링 → `claude -p` 자율 실행 → 상태 회신
#
# 계약 정합(반드시 유지):
#   image   = jira-auto-dispatcher:latest  (config.spawn.image / compose image)
#   network = jad-net                       (config.spawn.network / compose networks)
#   central = http://central:8787           (config.spawn.central_url / compose 서비스명)
#   claude  = /home/app/.claude             (spawner CLAUDE_CONFIG_DIR = $HOME/.claude)
#   claude bin은 비-root 유저(uid 1000) PATH에 있어야 한다(run.claude_bin=claude).
#
# POLICY-ENCODING: 생성 파일 UTF-8(BOM 없음)·LF.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# --- 시스템 의존성 ---
#   git            worker의 per-user 커밋/브랜치/MR·런타임 레포 clone에 필수
#   ca-certificates HTTPS(Jira/GitLab/claude 설치·인증)
#   curl           claude 독립 실행 설치 스크립트 + HEALTHCHECK 프로브 + docker CLI 정적 바이너리 취득
# (spawner의 라이프사이클 관리는 Docker SDK로 하지만, 프랙탈 PUSH 경로의 central→worker
#  `docker exec`는 아래 docker CLI(클라이언트 전용)를 쓴다 — 데몬은 설치하지 않는다.)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates curl bash \
    && rm -rf /var/lib/apt/lists/*

# --- docker CLI(클라이언트 전용, 데몬 미설치) ---
# 프랙탈 PUSH 경로: central 역할의 per-user sub가 `docker exec jad-worker-<user> claude -p --resume ...`를
# 실행한다. central은 원격 socket-proxy(DOCKER_HOST=tcp://socket-proxy:2375)를 타깃하므로 로컬 데몬은
# 불필요 — 공식 정적 배포에서 클라이언트 바이너리 1개(docker/docker)만 뽑아 /usr/local/bin에 둔다.
# /usr/local/bin은 비-root(uid 1000) PATH에도 있으므로 claude가 호출할 수 있다. worker 역할은 이
# 바이너리를 쓰지 않는다(공유 이미지 — 무해). 버전은 재현성을 위해 핀한다.
ARG DOCKER_CLI_VERSION=27.5.1
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_CLI_VERSION}.tgz" \
        -o /tmp/docker.tgz \
    && tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker \
    && rm /tmp/docker.tgz \
    && docker --version
# ↑ `docker --version` 은 빌드 중 검증 라인(클라이언트 단독으로 동작 — 데몬 불요). 실패 시 빌드 실패.

# --- 비-root 유저(uid 1000) + 런타임 디렉토리 소유/권한 ---
# HOME은 Docker가 USER로 자동 설정하지 않으므로 명시한다(claude·설치 스크립트가 $HOME 사용).
#   ~/.claude   : 사용자 인증/세션 영속(worker; 런타임에 per-user 볼륨이 마운트)
#   /app/state  : central 영속(jobs/watermark/dedup/registry)
#   /app/workspace : 오케스트레이터 작업 공간
ENV HOME=/home/app
RUN useradd --create-home --uid 1000 --shell /bin/bash app \
    && mkdir -p /home/app/.claude /app/state /app/workspace \
    && chown -R 1000:1000 /home/app /app

WORKDIR /app

# --- 파이썬 의존성(시스템 전역 site-packages = 전 유저 공유) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 앱 소스 ---
COPY . .
RUN chown -R 1000:1000 /app

# --- claude CLI: 이미지에 "한 번만" 설치(레이어 1벌 = 전 컨테이너 공유) ---
# 독립 실행 바이너리 우선: 공식 설치 스크립트가 비-root 유저 홈(/home/app/.local/bin)에
# 네이티브 바이너리를 깐다 → 그 유저 PATH에 노출(아래 ENV PATH). node 런타임 불필요.
#
# 폴백(설치 스크립트 실패/불확실 시): npm 경로 — node 런타임 필요.
#   USER root
#   RUN apt-get update && apt-get install -y --no-install-recommends nodejs npm \
#       && rm -rf /var/lib/apt/lists/* \
#       && npm i -g @anthropic-ai/claude-code
#   USER app   # (이 경우 PATH는 전역이라 별도 ENV PATH 불필요)
#
# ⚠️ 반드시 비-root(app) 유저로 설치해야 uid 1000 worker의 PATH에서 실행된다.
USER app
ENV PATH=/home/app/.local/bin:$PATH
# 설치 스크립트는 bash 문법을 쓴다(dash/sh 아님) → 반드시 bash로 파이프.
RUN curl -fsSL https://claude.ai/install.sh | bash \
    && claude --version
# ↑ `claude --version` 은 빌드 중 검증 라인이다 — 설치 실패 시 이미지 빌드가 실패한다.

# --- 런타임 기본 ---
ENV ROLE=central
EXPOSE 8787

# central·worker 모두 8787/healthz 를 서빙한다(app/main.py). curl 프로브.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8787/healthz || exit 1

# central·worker 공통 진입점(내부에서 ROLE 분기). 비-root(app)로 실행.
ENTRYPOINT ["python", "-m", "app.main"]
