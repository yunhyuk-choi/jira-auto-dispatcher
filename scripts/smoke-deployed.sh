#!/usr/bin/env bash
# smoke-deployed.sh — 배포된 central 대상 스모크 점검(관측/읽기 전용).
#
# 무엇을 하나:
#   - central 관리/헬스 엔드포인트의 HTTP 코드 + 최소 JSON 형태 확인
#       GET /healthz     → 200, {"status":"ok","role":"central"}
#       GET /api/users   → 200, JSON 배열
#       GET /api/jobs    → 200, JSON 배열
#   - (docker 접근 가능 시) **이 인스턴스의** 컨테이너 상태 출력:
#       <instance>-central / <instance>-socket-proxy / <instance>-worker-*
#
# 무엇을 안 하나:
#   - 배포/기동/온보딩 등 쓰기·라이브 조작 없음(전부 GET + docker ps).
#   - 시크릿(토큰)을 인자로 받지도, 출력하지도 않는다.
#
# ⚠️ **인스턴스를 무시하면 이 스크립트가 거짓 통과를 낸다.** 예전에는 컨테이너 이름을
#    `jad-central`·`jad-socket-proxy`·`jad-worker-` 로 하드코딩해서, 한 호스트에 여러
#    인스턴스가 뜬 환경에서 **남의 인스턴스 컨테이너를 자기 것인 양** 출력했다(리허설
#    실측: 21시간 된 다른 인스턴스의 컨테이너가 찍혔다). 검증 단계로 제시된 것이 거짓
#    통과를 내면 검증이 아니라 위험이다 — 그래서 이름을 인스턴스에서 파생한다.
#
# 인스턴스 결정 순서(먼저 정해지는 쪽이 이긴다):
#   1. env JAD_INSTANCE   2. 두 번째 인자   3. 리포 루트 `.env` 의 JAD_INSTANCE   4. `jad`
# 관리 UI 포트도 같은 방식으로 `.env` 의 JAD_PORT 를 본다(BASE_URL 을 안 줬을 때만).
#
# 사용법:
#   bash scripts/smoke-deployed.sh [BASE_URL] [INSTANCE]
#     BASE_URL 기본값: http://localhost:<JAD_PORT|8787>  (SSH 터널 권장: ssh -L 8787:localhost:8787 ...)
#     예) JAD_INSTANCE=jad-stg bash scripts/smoke-deployed.sh http://localhost:8788
#
# 종료코드: 모든 HTTP 점검 통과=0, 하나라도 실패=1.

set -eu

# --- 리포 루트의 .env 에서 값 하나 읽기(소싱하지 않는다 — 임의 셸 코드 실행 금지) ---
SCRIPT_DIR="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
ENV_FILE="${JAD_ENV_FILE:-$SCRIPT_DIR/../.env}"

env_file_value() {   # env_file_value <KEY> — 없으면 빈 문자열
  [ -f "$ENV_FILE" ] || return 0
  sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" \
    | tail -n 1 | sed -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/"
}

INSTANCE="${JAD_INSTANCE:-${2:-}}"
[ -n "$INSTANCE" ] || INSTANCE="$(env_file_value JAD_INSTANCE)"
[ -n "$INSTANCE" ] || INSTANCE="jad"          # app/naming.py DEFAULT_INSTANCE

PORT="${JAD_PORT:-}"
[ -n "$PORT" ] || PORT="$(env_file_value JAD_PORT)"
PORT_KNOWN=1
[ -n "$PORT" ] || { PORT="8787"; PORT_KNOWN=0; }

BASE_URL="${1:-http://localhost:$PORT}"
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
echo "target:   $BASE_URL"
echo "instance: $INSTANCE (컨테이너 이름 접두어 — 다른 인스턴스는 보지 않는다)"
# 인스턴스는 기본이 아닌데 포트를 못 알아냈고 BASE_URL 도 안 준 경우 — 그대로 두면
# **다른 인스턴스의 관리 UI** 를 재고 통과했다고 말하게 된다. 조용히 넘기지 않는다.
if [ "$INSTANCE" != "jad" ] && [ "$PORT_KNOWN" = 0 ] && [ -z "${1:-}" ]; then
  echo "  ⚠️ 이 인스턴스의 관리 UI 포트를 알아내지 못해 기본 8787 을 씁니다 —"
  echo "     8787 은 보통 **첫 인스턴스**의 것입니다. BASE_URL 인자나 JAD_PORT 를 주세요."
fi
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
  # central / socket-proxy / 동적 worker — 전부 **이 인스턴스** 이름으로만 찾는다.
  # `--filter name=` 은 정규식(부분 일치)이라 `^`·`$` 로 고정한다 — 고정하지 않으면
  # 이름이 겹치는 다른 인스턴스가 섞여 들어와 "떠 있다"는 거짓 통과가 된다.
  for pat in "^${INSTANCE}-central\$" "^${INSTANCE}-socket-proxy\$" "^${INSTANCE}-worker-"; do
    out="$(docker ps --filter "name=${pat}" --format '  {{.Names}}\t{{.Status}}' 2>/dev/null || true)"
    if [ -n "$out" ]; then
      printf '%s\n' "$out"
    else
      printf '  (none matched: %s)\n' "$pat"
    fi
  done
else
  echo "  (docker CLI 없음 — 서버에서 실행하거나 SSH로 확인:"
  echo "     ssh <deploy-user>@<DEV_SERVER_HOST> 'docker ps --filter name=^${INSTANCE}-')"
fi

echo
echo "== 결과: PASS=$PASS FAIL=$FAIL =="
[ "$FAIL" -eq 0 ]
