#!/usr/bin/env bash
# Build and (re)start the production stack at the checked-out commit, then wait until the agent is
# healthy. Used by the deploy workflow (through AWS SSM) and by hand (docs/DEPLOY.md).
#
#   git fetch origin && git checkout --detach origin/main && infra/deploy.sh   # deploy latest main
#   git checkout --detach <older-sha> && infra/deploy.sh                       # roll back
#
# Images are tagged with the commit (RELEASE), so rolling back to an earlier deploy reuses its images.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f .env ]] || { echo "missing .env (see docs/DEPLOY.md, step 7)" >&2; exit 1; }
export RELEASE
RELEASE=$(git rev-parse --short HEAD)
compose=(docker compose --env-file .env -f infra/docker-compose.deploy.yml)

# Record the release in .env too, so a later `docker compose ... up` by hand (e.g. starting the
# observability profile) recreates containers from these images. Without it, compose used to fall
# back to an old `:dev` image and could silently swap the live agent for a stale build.
if grep -q '^RELEASE=' .env; then
  sed -i "s|^RELEASE=.*|RELEASE=$RELEASE|" .env
else
  printf '\nRELEASE=%s\n' "$RELEASE" >> .env
fi

echo "deploying $RELEASE ($(git log -1 --format=%s))"
"${compose[@]}" up -d --build --remove-orphans

# The agent loads its models on start (minutes on a first deploy); its healthcheck says when it's up.
status=starting
for _ in $(seq 1 90); do
  container=$("${compose[@]}" ps -q agent)
  status=$(docker inspect -f '{{.State.Health.Status}}' "$container" 2>/dev/null || echo starting)
  [[ "$status" == healthy ]] && break
  sleep 10
done
if [[ "$status" != healthy ]]; then
  echo "agent not healthy after 15 minutes (status: $status); last log lines:" >&2
  "${compose[@]}" logs --tail 80 agent >&2
  exit 1
fi

"${compose[@]}" ps
docker image prune -f >/dev/null # dangling layers only; tagged releases stay for rollbacks
echo "deployed $RELEASE"
