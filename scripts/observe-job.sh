#!/usr/bin/env bash
# observe-job.sh — 특정 ticket/user 잡을 /api/jobs로 추적(관측 전용).
#
# 무엇을 하나:
#   GET {BASE_URL}/api/jobs 를 주기 폴링해, 지정 ticket(+user)의 잡에서
#   status / reset_at / mr_url / branch / attempts 를 뽑아 변화가 있을 때만 한 줄 출력.
#   상태 전이(queued→running→done, interrupted+reset_at, cancelling→cancelled 등)를
#   눈으로 따라가기 위한 보조 도구다(§10 취소/재오픈 포함).
#
# 무엇을 안 하나:
#   - 읽기(GET)만. 잡을 만들거나 상태를 바꾸지 않는다.
#   - 시크릿을 받지도 출력하지도 않는다(/api/jobs는 인증 불요·토큰 미포함).
#
# 사용법:
#   bash scripts/observe-job.sh <TICKET> [USER] [BASE_URL] [--once] [--interval N]
#     TICKET    필수. 예: <PROJECT_KEY>-142
#     USER      선택. 같은 티켓이 여러 사용자에 없다면 생략 가능(생략 시 티켓만으로 매칭).
#     BASE_URL  선택. 기본 http://localhost:8787
#     --once        1회 출력 후 종료(폴링 안 함).
#     --interval N  폴링 주기(초, 기본 10).
#
# 종료: Ctrl-C. 잡이 종결(done/failed/cancelled) 상태로 관측되면 마지막 줄 출력 후 종료.

set -eu

TICKET=""
USER_FILTER=""
BASE_URL="http://localhost:8787"
ONCE=0
INTERVAL=10

# --- 인자 파싱(위치 인자 + 플래그 혼용) ---
POS=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --once) ONCE=1; shift ;;
    --interval) INTERVAL="${2:-10}"; shift 2 ;;
    --interval=*) INTERVAL="${1#*=}"; shift ;;
    -h|--help)
      grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *)
      POS=$((POS + 1))
      case "$POS" in
        1) TICKET="$1" ;;
        2) USER_FILTER="$1" ;;
        3) BASE_URL="$1" ;;
        *) echo "예상치 못한 인자: $1" >&2; exit 2 ;;
      esac
      shift ;;
  esac
done

if [ -z "$TICKET" ]; then
  echo "사용법: bash scripts/observe-job.sh <TICKET> [USER] [BASE_URL] [--once] [--interval N]" >&2
  exit 2
fi
BASE_URL="${BASE_URL%/}"

# --- JSON 추출 백엔드 선택: jq > python3 > (실패 시 안내) ---
JSON_MODE=""
if command -v jq >/dev/null 2>&1; then
  JSON_MODE="jq"
elif command -v python3 >/dev/null 2>&1; then
  JSON_MODE="py"
elif command -v python >/dev/null 2>&1; then
  JSON_MODE="py2"
else
  echo "jq 또는 python 이 필요합니다(둘 중 하나). 원본 확인: curl -s $BASE_URL/api/jobs" >&2
  exit 3
fi

# extract_line <json> — 매칭 잡 1건을 "status|reset_at|mr_url|branch|attempts"로 출력(없으면 빈 줄).
extract_line() {
  json="$1"
  case "$JSON_MODE" in
    jq)
      printf '%s' "$json" | jq -r --arg t "$TICKET" --arg u "$USER_FILTER" '
        (if type=="array" then . else [] end)
        | map(select(.ticket==$t and ($u=="" or .user==$u)))
        | (.[0] // {})
        | [ (.status // "-"), (.reset_at // "-"), (.mr_url // "-"),
            (.branch // "-"), (.attempts // 0 | tostring) ]
        | join("|")
      ' 2>/dev/null
      ;;
    py|py2)
      PYBIN="python3"; [ "$JSON_MODE" = "py2" ] && PYBIN="python"
      printf '%s' "$json" | "$PYBIN" - "$TICKET" "$USER_FILTER" <<'PYEOF' 2>/dev/null
import sys, json
ticket = sys.argv[1]
user = sys.argv[2]
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
    sys.exit(0)
if not isinstance(data, list):
    print("")
    sys.exit(0)
match = None
for j in data:
    if not isinstance(j, dict):
        continue
    if j.get("ticket") == ticket and (user == "" or j.get("user") == user):
        match = j
        break
if match is None:
    print("")
    sys.exit(0)
def g(k, d="-"):
    v = match.get(k)
    return d if v in (None, "") else str(v)
print("|".join([g("status"), g("reset_at"), g("mr_url"), g("branch"), g("attempts", "0")]))
PYEOF
      ;;
  esac
}

fetch_jobs() {
  curl -s --max-time 15 "${BASE_URL}/api/jobs" 2>/dev/null || true
}

echo "== observe-job =="
echo "ticket=$TICKET user=${USER_FILTER:-<any>} target=$BASE_URL interval=${INTERVAL}s (backend=$JSON_MODE)"
echo "time                 status       reset_at              mr_url / branch / attempts"
echo "-------------------- ------------ --------------------- ----------------------------------"

LAST=""
TERMINAL_SEEN=0

print_row() {
  line="$1"
  status="$(printf '%s' "$line" | cut -d'|' -f1)"
  reset_at="$(printf '%s' "$line" | cut -d'|' -f2)"
  mr_url="$(printf '%s' "$line" | cut -d'|' -f3)"
  branch="$(printf '%s' "$line" | cut -d'|' -f4)"
  attempts="$(printf '%s' "$line" | cut -d'|' -f5)"
  ts="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '-')"
  printf '%-20s %-12s %-21s mr=%s br=%s try=%s\n' \
    "$ts" "$status" "$reset_at" "$mr_url" "$branch" "$attempts"
  case "$status" in
    done|failed|cancelled) TERMINAL_SEEN=1 ;;
  esac
}

while :; do
  json="$(fetch_jobs)"
  if [ -z "$json" ]; then
    ts="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '-')"
    printf '%-20s (central 응답 없음 — 연결/터널 확인)\n' "$ts"
  else
    line="$(extract_line "$json")"
    if [ -z "$line" ]; then
      if [ "$LAST" != "__missing__" ]; then
        ts="$(date '+%Y-%m-%d %H:%M:%S' 2>/dev/null || echo '-')"
        printf '%-20s (잡 없음 — 아직 생성 전이거나 티켓/사용자 불일치)\n' "$ts"
        LAST="__missing__"
      fi
    elif [ "$line" != "$LAST" ]; then
      print_row "$line"
      LAST="$line"
    fi
  fi

  if [ "$ONCE" = 1 ]; then
    break
  fi
  if [ "$TERMINAL_SEEN" = 1 ]; then
    echo "(종결 상태 관측 — 종료)"
    break
  fi
  sleep "$INTERVAL"
done
