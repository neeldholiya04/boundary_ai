#!/usr/bin/env bash
# Checks a deployment's access rules end to end (docs/DEPLOY.md, step 9).
#   infra/smoke-test.sh https://boundary.3-91-20-7.sslip.io https://admin.3-91-20-7.sslip.io
# Asks for the admin password (or reads ADMIN_PASSWORD from the environment); extra arguments go to
# curl (e.g. -k --resolve ... for a local rehearsal with self-signed certificates).
set -uo pipefail
P=${1:?public URL}; A=${2:?admin URL}; shift 2; EXTRA=("$@")
if [[ -z "${ADMIN_PASSWORD:-}" ]]; then read -rs -p "Admin password: " ADMIN_PASSWORD; echo; fi
code() { curl -s "${EXTRA[@]}" -o /dev/null -w "%{http_code}" "$@"; }
fails=0
check() {
  local got=$1 want=$2 label=$3 r=ok
  [[ " $want " == *" $got "* ]] || { r=FAIL; fails=$((fails + 1)); }
  printf "%-4s %-40s got %s (want %s)\n" "$r" "$label" "$got" "$want"
}
check "$(code "$P/health")" 200 "public /health"
check "$(code "$P/")" 302 "public / redirects to the playground"
check "$(code "$P/playground")" 200 "public /playground"
check "$(code "$P/chat")" 302 "public admin page redirected"
check "$(code "$P/api/playground/scenarios")" 200 "public playground API"
check "$(code -X POST -H 'content-type: application/json' -d '{"stage":"user_input","text":"hello"}' "$P/api/guard/scan")" 200 "public scan API"
check "$(code -X POST "$P/api/chat")" 404 "public: admin API (chat) hidden"
check "$(code "$P/api/guard/status")" 404 "public: admin API (guard) hidden"
check "$(code "$P/metrics")" 404 "public: /metrics hidden"
check "$(code "$A/")" 401 "admin without password"
check "$(code -u admin:wrong-password "$A/")" 401 "admin with a wrong password"
check "$(code -u "admin:$ADMIN_PASSWORD" "$A/")" "200 307" "admin with the password"
check "$(code -u "admin:$ADMIN_PASSWORD" "$A/api/guard/status")" 200 "admin API"
check "$(code -u "admin:$ADMIN_PASSWORD" "$A/metrics")" 404 "admin: /metrics hidden"
hsts=$(curl -sI "${EXTRA[@]}" "$P/playground" | grep -ci 'strict-transport-security')
check "$hsts" 1 "HSTS header on the public host"
echo; [[ $fails -eq 0 ]] && echo "all checks passed" || { echo "$fails check(s) failed"; exit 1; }
