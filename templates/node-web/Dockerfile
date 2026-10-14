FROM node:24.21.0-alpine3.24

ENV NODE_ENV=production
WORKDIR /app
COPY package*.json ./
RUN npm install --omit=dev --no-audit --no-fund && npm cache clean --force
COPY . .

# Annex runs every app as uid 10001 on a read-only filesystem
RUN adduser -D -H -u 10001 app
USER 10001

HEALTHCHECK --interval=30s --timeout=3s --start-period=30s --start-interval=2s \
  CMD node -e "fetch('http://127.0.0.1:' + (process.env.PORT || 8080) + '/healthz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"

CMD ["node", "server.js"]
