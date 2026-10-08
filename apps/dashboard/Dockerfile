# syntax=docker/dockerfile:1.7
# The dashboard (Next.js). Built from the repo root:
#   docker build -f apps/dashboard/Dockerfile -t boundary-dashboard .
# NEXT_PUBLIC_API_BASE_URL defaults to "" so the browser calls the API on the same host (/api/...):
# behind Caddy, both hosts route /api to the agent.

FROM node:22-alpine AS build
WORKDIR /app
COPY apps/dashboard/package.json apps/dashboard/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY apps/dashboard ./
ARG NEXT_PUBLIC_API_BASE_URL=""
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL} \
    NEXT_TELEMETRY_DISABLED=1
RUN npm run build && npm prune --omit=dev

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=build --chown=node:node /app ./
USER node
EXPOSE 3000
CMD ["node_modules/.bin/next", "start", "-H", "0.0.0.0", "-p", "3000"]
