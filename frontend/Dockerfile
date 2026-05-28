# Frontend image — build the Vite bundle, then serve it as static files. The
# runtime stage carries no node_modules and no toolchain, just nginx and the
# built assets (~30MB vs ~500MB for a node runtime).
FROM node:22-alpine AS build

WORKDIR /app

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./

# Vite inlines env vars at BUILD time, so this can't be set when the container
# starts — the value is baked into the bundle here. Compose passes it as a build
# arg; without it api.js falls back to the deployed Render URL, which is the wrong
# target for local containers.
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
RUN npm run build


FROM nginx:1.27-alpine AS runtime

COPY --from=build /app/dist /usr/share/nginx/html
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD wget --quiet --tries=1 --spider http://localhost/ || exit 1
