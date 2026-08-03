ARG BUILD_FROM
ARG TARGETARCH
ARG CODEX_VERSION=0.146.0
ARG DEBIAN_MIRROR=https://deb.debian.org/debian
ARG DEBIAN_SECURITY_MIRROR=https://deb.debian.org/debian-security

FROM scratch AS codex-amd64
ARG CODEX_VERSION
ADD --checksum=sha256:91a32565acb7fef5d300294577092a121503406e34ce7e992af8bdf1998b65fb \
    https://registry.npmjs.org/@openai/codex/-/codex-${CODEX_VERSION}-linux-x64.tgz \
    /codex.tgz

FROM scratch AS codex-arm64
ARG CODEX_VERSION
ADD --checksum=sha256:dee08f72d728189d9ac672356c6704e74e8c82e8c725feb8a0fbd1b6e0516724 \
    https://registry.npmjs.org/@openai/codex/-/codex-${CODEX_VERSION}-linux-arm64.tgz \
    /codex.tgz

FROM codex-${TARGETARCH} AS codex-package

FROM ${BUILD_FROM}

ARG DEBIAN_MIRROR
ARG DEBIAN_SECURITY_MIRROR
ARG TARGETARCH

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    set -eu; \
    sed -i \
        -e "s|http://deb.debian.org/debian-security|${DEBIAN_SECURITY_MIRROR}|g" \
        -e "s|http://deb.debian.org/debian|${DEBIAN_MIRROR}|g" \
        /etc/apt/sources.list.d/debian.sources; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        jq \
        python3

RUN --mount=type=bind,from=codex-package,source=/codex.tgz,target=/tmp/codex.tgz \
    set -eu; \
    case "$TARGETARCH" in \
        amd64) \
            codex_target="x86_64-unknown-linux-musl"; \
            codex_sha512="7ecc2fc86a6b00f08c88e12e7bfecc28c93ba428e1f6464825f257e62f5ab667e798661b602714859b041fd2c43f4fb4b79c723ea0db7cd5d8ecd4b1215b97c4"; \
            ;; \
        arm64) \
            codex_target="aarch64-unknown-linux-musl"; \
            codex_sha512="aa2603c649041675c6ee3a1a7495ba43e5dc8320d70a919da5af6793f73e8b4804a26babd63edb1efc75d8d7d658217fcbc4eaae2e9acbe14bb82d8c8dd7a1b4"; \
            ;; \
        *) echo "不支持的 TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    echo "${codex_sha512}  /tmp/codex.tgz" | sha512sum --check --strict; \
    mkdir -p /tmp/codex-package; \
    tar -xzf /tmp/codex.tgz -C /tmp/codex-package; \
    install -D -m 0755 "/tmp/codex-package/package/vendor/${codex_target}/bin/codex" /opt/codex/codex; \
    /opt/codex/codex --version; \
    /opt/codex/codex app-server generate-json-schema --out /opt/codex-schema; \
    rm -rf /tmp/codex-package

COPY run.sh /
COPY codex_controller /opt/codex-controller/codex_controller

RUN chmod 0755 /run.sh

ENV PYTHONPATH=/opt/codex-controller \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODEX_BINARY=/opt/codex/codex \
    CODEX_SCHEMA_DIR=/opt/codex-schema

CMD ["/run.sh"]
