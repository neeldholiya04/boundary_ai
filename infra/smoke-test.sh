#!/usr/bin/env bash
# Checks a deployment's access rules end to end (docs/DEPLOY.md, step 9).
#   infra/smoke-test.sh https://boundary.3-91-20-7.sslip.io https://admin.3-91-20-7.sslip.io
# Asks for the admin account's password (or reads ADMIN_PASSWORD; the username is ADMIN_USER, default
# "admin", as listed in AUTH_USERS); extra arguments go to curl (e.g. -k --resolve ... for a local
# rehearsal with self-signed certificates).
set -uo pipefail
P=${1:?public URL}; A=${2:?admin URL}; shift 2; EXTRA=("$@")
ADMIN_USER=${ADMIN_USER:-admin}
if [[ -z "${ADMIN_PASSWORD:-}" ]]; then read -rs -p "Admin password: " ADMIN_PASSWORD; echo; fi
code() { curl -s "${EXTRA[@]}" -o /dev/null -w "%{http_code}" "$@"; }
login() {
  curl -s "${EXTRA[@]}" -X POST -H 'content-type: application/json' \
    -d "{\"username\":\"$1\",\"password\":\"$2\"}" "$3/api/auth/login" |
    python3 -c 'import json,sys; print(json.load(sys.stdin).get("token",""))' 2>/dev/null
}
fails=0
check() {
  local got=$1 want=$2 label=$3 r=ok
  [[ " $want " == *" $got "* ]] || { r=FAIL; fails=$((fails + 1)); }
  printf "%-4s %-44s got %s (want %s)\n" "$r" "$label" "$got" "$want"
}
for H in "$P" "$A"; do
  check "$(code "$H/health")" 200 "$H /health"
  check "$(code "$H/login")" 200 "$H sign-in page"
  check "$(code "$H/api/conversations")" 401 "$H chat API without signing in"
  check "$(code "$H/api/guard/status")" 401 "$H admin API without signing in"
  check "$(code -X POST -H 'content-type: application/json' -d '{"stage":"user_input","text":"hello"}' "$H/api/guard/scan")" 401 "$H playground API without signing in"
  check "$(code "$H/metrics")" 404 "$H /metrics hidden"
done
check "$(code -X POST -H 'content-type: application/json' -d '{"username":"'"$ADMIN_USER"'","password":"wrong-password"}' "$A/api/auth/login")" 401 "sign-in with a wrong password"
TOKEN=$(login "$ADMIN_USER" "$ADMIN_PASSWORD" "$A")
[[ -n "$TOKEN" ]] && got=yes || got=no
check "$got" yes "admin can sign in"
check "$(code -H "Authorization: Bearer $TOKEN" "$A/api/guard/status")" 200 "admin API with the admin's token"
check "$(code -H "Authorization: Bearer $TOKEN" "$A/api/playground/scenarios")" 200 "playground API for the admin"
hsts=$(curl -sI "${EXTRA[@]}" "$P/login" | grep -ci 'strict-transport-security')
check "$hsts" 1 "HSTS header on the public host"
echo; [[ $fails -eq 0 ]] && echo "all checks passed" || { echo "$fails check(s) failed"; exit 1; }
