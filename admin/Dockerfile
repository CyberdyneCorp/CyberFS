# CyberFS admin dashboard image -- multi-stage, non-root, pinned Node base.
#
# SvelteKit + @sveltejs/adapter-node. Runtime config is read via
# $env/dynamic/public, so one built image serves every environment: it needs
# ONLY the PUBLIC_* URLs at runtime, and no Postgres/Redis/MinIO credentials.
#
# Build context is admin/ (see compose files: dockerfile: admin/Dockerfile with
# context admin). admin/.dockerignore keeps node_modules, build output, and all
# test material out of the context.

# ---- builder: install all deps and build the adapter-node server ------------
FROM node:22.11-bookworm-slim AS builder

WORKDIR /app

# Lockfile first so the install layer caches across source-only changes.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# Drop dev dependencies; keep only what `node build` needs at runtime.
RUN npm prune --omit=dev

# ---- runtime ----------------------------------------------------------------
FROM node:22.11-bookworm-slim AS runtime

ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000

WORKDIR /app

# adapter-node output plus its pruned runtime dependencies. No source, no tests.
COPY --from=builder /app/build ./build
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./package.json

# The node image ships an unprivileged `node` user (uid 1000).
USER node

EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD node -e "require('http').get('http://localhost:'+(process.env.PORT||3000)+'/',r=>process.exit(r.statusCode<500?0:1)).on('error',()=>process.exit(1))"

CMD ["node", "build"]
