FROM wiznote/wizserver@sha256:6f68b50e683230b9f6b756e9283043d57231468f4cc4d97550da9d03d9ffeb0f

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

COPY run.sh /usr/local/bin/wiznote-ha-entrypoint
RUN chmod 0755 /usr/local/bin/wiznote-ha-entrypoint

CMD ["/usr/local/bin/wiznote-ha-entrypoint"]
