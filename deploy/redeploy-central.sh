#!/usr/bin/env bash
# redeploy-central.sh — 개발서버에서 central 컨테이너만 무중단 교체한다.
#
# 실행 위치: 개발서버(<DEV_SERVER_HOST>). CI 의 deploy 잡이 소스를 rsync 한 뒤
#            ssh 로 "인자 전달" 방식으로 이 스크립트를 호출한다(heredoc 금지).
# 사용법:    bash deploy/redeploy-central.sh [APP_DIR]
#            APP_DIR 기본값 = /opt/jira-auto-dispatcher
#
# ────────────────────────────────────────────────────────────────────────────
# 왜 "무중단"이 성립하는가 (핵심 — 반드시 이해하고 유지):
#
#   worker 컨테이너(jad-worker-<user>)는 compose 가 아니라 central 이 Docker SDK 로
#   동적 spawn 한 "독립 컨테이너"다. 각 worker 는 자체 per-user 볼륨(jad-<username>,
#   ~/.claude)에 상태를 영속하고, central 로는 HTTP 로 "폴링/회신"만 한다.
#
#   따라서 이 스크립트가 central 만 잠깐 recreate 하는 동안:
#     - worker 는 절대 재생성/중지되지 않는다(이 스크립트는 --no-deps central 만 만짐).
#     - worker 안에서 돌던 in-flight 작업(claude -p 실행)은 그대로 계속된다.
#     - central 이 잠깐 내려간 사이의 worker 폴링은 실패→재시도(재연결 폴링)로 흡수된다.
#     - central 영속 상태(jobs/watermark/dedup/registry)는 "명명 볼륨"
#       (jad-state/jad-workspace)에 있어 컨테이너 recreate 로 유실되지 않는다.
#
#   ⚠️ 그러므로 이 스크립트는 worker 를 절대 건드리면 안 된다. `docker compose down`,
#      `--remove-orphans`, worker 대상 stop/rm 은 금지. 오직 central 만 recreate.
# ────────────────────────────────────────────────────────────────────────────
#
# POLICY-ENCODING: 이 파일 UTF-8(BOM 없음)·LF.

set -euo pipefail

# --- 파라미터(전부 env 로 오버라이드 가능) ---
APP_DIR="${1:-${JAD_APP_DIR:-/opt/jira-auto-dispatcher}}"
# 이미지·롤백 태그도 **인스턴스 축**이다 — compose 와 같은 파생 규칙을 쓴다
# (JAD_INSTANCE 미설정이면 예전과 같은 latest/rollback. app/naming.py default_image).
# 태그를 두 인스턴스가 공유하면 여기서 빌드·롤백하는 순간 **다른 인스턴스의 워커가
# 쓰는 실행 코드**가 바뀐다.
IMAGE="${JAD_IMAGE:-jira-auto-dispatcher:${JAD_INSTANCE:-latest}}"
ROLLBACK_TAG="${JAD_ROLLBACK_TAG:-jira-auto-dispatcher:rollback${JAD_INSTANCE:+-${JAD_INSTANCE}}}"
SERVICE="${JAD_SERVICE:-central}"                       # compose 서비스 키(= worker 도달 DNS)
HEALTH_URL="${JAD_HEALTH_URL:-http://localhost:8787/healthz}"
HEALTH_RETRIES="${JAD_HEALTH_RETRIES:-30}"              # 총 대기 = retries * interval
HEALTH_INTERVAL="${JAD_HEALTH_INTERVAL:-2}"             # 초
# JAD_LOAD_TAR 를 주면 서버 빌드 대신 tar 를 load 한다(DEPLOY.md 방식 B).

log() { printf '[redeploy] %s\n' "$*"; }
die() { printf '[redeploy][ERROR] %s\n' "$*" >&2; exit 1; }

command -v docker >/dev/null 2>&1 || die "docker 미설치/미접근"
docker compose version >/dev/null 2>&1 || die "docker compose v2 미설치"
[ -d "$APP_DIR" ] || die "APP_DIR 없음: $APP_DIR"
cd "$APP_DIR"
[ -f docker-compose.yml ] || die "docker-compose.yml 없음(APP_DIR 확인): $APP_DIR"

# /healthz 가 up 될 때까지 폴링. 성공 0, 실패 1.
wait_healthy() {
  local i
  for ((i = 1; i <= HEALTH_RETRIES; i++)); do
    if curl -fsS --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
      log "health OK ($HEALTH_URL) — ${i}회째"
      return 0
    fi
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

# --- 1) 롤백 포인트 확보: 현재 latest 를 rollback 태그로 보존 ---
HAS_ROLLBACK=0
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  docker tag "$IMAGE" "$ROLLBACK_TAG"
  HAS_ROLLBACK=1
  log "현재 $IMAGE 를 $ROLLBACK_TAG 로 태깅(롤백 포인트 확보)"
else
  log "기존 $IMAGE 없음 — 최초 배포(롤백 포인트 없음)"
fi

# --- 2) 새 이미지 확보: 기본은 서버에서 빌드(DEPLOY.md 방식 A) ---
if [ -n "${JAD_LOAD_TAR:-}" ]; then
  log "이미지 load: $JAD_LOAD_TAR"
  [ -f "$JAD_LOAD_TAR" ] || die "JAD_LOAD_TAR 파일 없음: $JAD_LOAD_TAR"
  gunzip -c "$JAD_LOAD_TAR" | docker load
else
  log "$IMAGE 빌드(서버, $APP_DIR)"
  docker build -t "$IMAGE" .
fi

# --- 3) central 만 무중단 recreate ---
#   --no-deps       : socket-proxy 등 의존 서비스 미변경.
#   --force-recreate: 같은 :latest 태그라도 새 이미지로 확실히 교체.
#   명명 볼륨(jad-state/jad-workspace)은 recreate 로 유지 → 상태 보존.
#   ⚠️ worker(jad-worker-<user>)는 compose 서비스가 아니므로 여기서 절대 안 건드려짐.
log "central 만 recreate (--no-deps --force-recreate $SERVICE)"
docker compose up -d --no-deps --force-recreate "$SERVICE"

# --- 4) 헬스 폴링 → 실패 시 롤백 ---
if wait_healthy; then
  log "배포 성공 — central up. worker 는 무변경(폴링 재연결)."
  # 성공 시 롤백 태그 정리는 선택 — 다음 배포에서 덮어씀. 이미지 누적 방지용 prune 는
  # 운영 정책에 맡긴다(여기선 명시적 삭제 안 함).
  exit 0
fi

log "헬스 실패 — 롤백 시도"
if [ "$HAS_ROLLBACK" -eq 1 ]; then
  docker tag "$ROLLBACK_TAG" "$IMAGE"
  docker compose up -d --no-deps --force-recreate "$SERVICE"
  if wait_healthy; then
    die "새 이미지 배포 실패 → 이전 이미지로 롤백 성공. (원인 조사 필요; 배포는 실패 처리)"
  fi
  die "새 이미지 배포 실패 + 롤백 후에도 헬스 실패 — 수동 개입 필요"
else
  die "새 이미지 배포 실패 + 롤백 포인트 없음(최초 배포) — 수동 개입 필요"
fi
