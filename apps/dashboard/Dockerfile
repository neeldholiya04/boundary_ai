FROM node:20-alpine

WORKDIR /app

COPY apps/dashboard/package.json apps/dashboard/package-lock.json /app/
RUN npm ci

COPY apps/dashboard /app

ARG NEXT_PUBLIC_API_BASE_URL
ENV NEXT_PUBLIC_API_BASE_URL=${NEXT_PUBLIC_API_BASE_URL}
ENV NODE_ENV=production

RUN npm run build

EXPOSE 3000

CMD ["npx", "next", "start", "-H", "0.0.0.0", "-p", "3000"]
