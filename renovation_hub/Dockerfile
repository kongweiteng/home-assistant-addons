ARG BUILD_FROM
ARG NODE_BUILD_FROM=node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32
FROM ${NODE_BUILD_FROM} AS frontend-build

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./frontend/
WORKDIR /build/frontend
RUN --mount=type=cache,target=/root/.npm npm ci

WORKDIR /build
COPY frontend ./frontend
COPY renovation_hub/web ./renovation_hub/web
WORKDIR /build/frontend
RUN npm run build

FROM ${BUILD_FROM}

ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        ffmpeg \
        jq \
        python3 \
        python3-aiohttp \
        python3-pil

COPY run.sh /
COPY renovation_hub /opt/renovation-hub/renovation_hub
COPY --from=frontend-build /build/frontend/dist /opt/renovation-hub/web

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/renovation-hub \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

CMD ["/run.sh"]
