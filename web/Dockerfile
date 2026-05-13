FROM node:22-alpine AS deps
WORKDIR /app
COPY package.json ./
RUN npm install --no-audit --no-fund

FROM node:22-alpine AS builder
WORKDIR /app
ARG NEXT_PUBLIC_API_BASE
ENV NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE
# Build label baked into the static bundle so the PebbleHeader chip
# reports the exact build a user is running. Computed on the host
# (Makefile reads version.txt + ``git rev-parse``) and passed in as
# a build arg because:
#   * the repo-root ``version.txt`` lives OUTSIDE the web build
#     context (./web), so the in-container build can't read it;
#   * ``git`` isn't installed in node:22-alpine and ``.git`` isn't
#     in the build context anyway.
# next.config.mjs honors APP_BUILD_LABEL when present and skips its
# (now-unreachable) host-side resolution.
ARG APP_BUILD_LABEL
ENV APP_BUILD_LABEL=$APP_BUILD_LABEL
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
COPY --from=builder /app/public ./public
EXPOSE 3000
CMD ["node", "server.js"]
