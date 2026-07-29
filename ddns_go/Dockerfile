ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG BUILD_ARCH
ARG DDNS_GO_VERSION
ARG DDNS_GO_SHA256_AMD64
ARG DDNS_GO_SHA256_AARCH64

RUN apk add --no-cache ca-certificates curl tzdata \
    && case "${BUILD_ARCH}" in \
         amd64) \
           asset_arch="x86_64"; \
           asset_sha256="${DDNS_GO_SHA256_AMD64}" \
           ;; \
         aarch64) \
           asset_arch="arm64"; \
           asset_sha256="${DDNS_GO_SHA256_AARCH64}" \
           ;; \
         *) \
           echo "Unsupported architecture: ${BUILD_ARCH}" >&2; \
           exit 1 \
           ;; \
       esac \
    && asset="ddns-go_${DDNS_GO_VERSION}_linux_${asset_arch}.tar.gz" \
    && curl --fail --location --retry 5 --retry-all-errors \
         --output "/tmp/${asset}" \
         "https://github.com/jeessy2/ddns-go/releases/download/v${DDNS_GO_VERSION}/${asset}" \
    && echo "${asset_sha256}  /tmp/${asset}" | sha256sum -c - \
    && tar --extract --gzip --file "/tmp/${asset}" --directory /usr/local/bin ddns-go \
    && chmod 0755 /usr/local/bin/ddns-go \
    && rm -f "/tmp/${asset}" \
    && apk del curl

COPY rootfs /
