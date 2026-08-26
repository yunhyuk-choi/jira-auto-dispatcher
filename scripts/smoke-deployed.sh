#!/usr/bin/env bash
# smoke-deployed.sh — 배포된 central 대상 스모크 점검(관측/읽기 전용).
#
# 무엇을 하나:
#   - central 관리/헬스 엔드포인트의 HTTP 코드 + 최소 JSON 형태 확인
#       GET /healthz     → 200, {"status":"ok","role":"central"}
#       GET /api/users   → 200, JSON 배열
#       GET /api/jobs    → 200, JSON 배열
#   - (docker 접근 가능 시) 컨테이너 상태 출력: jad-central / jad-socket-proxy / jad-worker-*
#
# 무엇을 안 하나:
#   - 배포/기동/온보딩 등 쓰기·라이브 조작 없음(전부 GET).
#   - 시크릿(토큰·WORKER_SHARED_SECRET)을 인자로 받지도, 출력하지도 않는다.
#
# 사용법:
#   bash scripts/smoke-deployed.sh [BASE_URL]
#     BASE_URL 기본값: http://localhost:8787  (SSH 터널 권장: ssh -L 8787:localhost:8787 ...)
#
# 종료코드: 모든 HTTP 점검 통과=0, 하나라도 실패=1.

set -eu

BASE_URL="${1:-http://localhost:8787}"
BASE_URL="${BASE_URL%/}"   # 끝 슬래시 제거

PASS=0
FAIL=0

c_green() { printf '\033[32m%s\033[0m' "$1"; }
c_red()   { printf '\033[31m%s\033[0m' "$1"; }

# check_http <name> <path> <expect_code> <body_grep_regex|"">
#   응답 코드와 (선택) 본문 정규식을 확인. 본문은 임시파일로 받아 코드와 분리.
check_http() {
  name="$1"; path="$2"; expect="$3"; body_re="${4:-}"
  url="${BASE_URL}${path}"
  body_file="$(mktemp 2>/dev/null || echo "/tmp/jad-smoke.$$.$RANDOM")"
  # -s 조용히, -o 본문 파일, -w 코드만. 연결 실패 시 code=000.
  code="$(curl -s -o "$body_file" -w '%{http_code}' --max-time 15 "$url" 2>/dev/null || echo 000)"

  ok=1
  if [ "$code" != "$expect" ]; then
    ok=0
  fi
  if [ -n "$body_re" ] && ! grep -Eq "$body_re" "$body_file" 2>/dev/null; then
    ok=0
  fi

  if [ "$ok" = 1 ]; then
    printf '  [%s] %-24s %s (%s)\n' "$(c_green PASS)" "$name" "$path" "$code"
    PASS=$((PASS + 1))
  else
    printf '  [%s] %-24s %s (code=%s, expected=%s%s)\n' "$(c_red FAIL)" "$name" "$path" \
      "$code" "$expect" "$([ -n "$body_re" ] && echo ", body!~/$body_re/" || true)"
    FAIL=$((FAIL + 1))
  fi
  rm -f "$body_file" 2>/dev/null || true
}

echo "== jira-auto-dispatcher smoke =="
echo "target: $BASE_URL"
echo

echo "-- HTTP 엔드포인트 --"
# healthz: role=central 문자열까지 확인.
check_http "healthz"   "/healthz"   200 '"role"[[:space:]]*:[[:space:]]*"central"'
# 관리 API: JSON 배열(빈 배열 [] 포함)로 시작하는지만 최소 확인.
check_http "api_users" "/api/users" 200 '^[[:space:]]*\['
check_http "api_jobs"  "/api/jobs"  200 '^[[:space:]]*\['

echo
echo "-- docker 컨테이너 상태 --"
if command -v docker >/dev/null 2>&1; then
  # central / socket-proxy / 동적 worker. 없으면 (none) 표기.
  for pat in jad-central jad-socket-proxy jad-worker-; do
    out="$(docker ps --filter "name=${pat}" --format '  {{.Names}}\t{{.Status}}' 2>/dev/null || true)"
    if [ -n "$out" ]; then
      printf '%s\n' "$out"
    else
      printf '  (none matched: %s)\n' "$pat"
    fi
  done
else
  echo "  (docker CLI 없음 — 서버에서 실행하거나 SSH로 확인:"
  echo "     ssh <deploy-user>@<DEV_SERVER_HOST> 'docker ps --filter name=jad-')"
fi

echo
echo "== 결과: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ]
