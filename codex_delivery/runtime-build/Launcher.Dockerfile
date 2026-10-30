ARG BUILD_FROM
FROM ${BUILD_FROM} AS build
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g; s|https://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends gcc libc6-dev libseccomp-dev python3 >/dev/null
COPY isolate.c /isolate.c
ARG ISOLATION_PROFILE=landlock-v1
ARG RESOURCE_PROFILE=cgroup-v1
RUN case "$ISOLATION_PROFILE" in landlock-v1) profile_flags='' ;; readonly-jail-v1) profile_flags='-DREADONLY_JAIL=1' ;; *) exit 1 ;; esac \
    && case "$RESOURCE_PROFILE" in cgroup-v1) resource_flags='' ;; native-resource-v1) resource_flags='-DNATIVE_RESOURCE=1' ;; *) exit 1 ;; esac \
    && gcc -static -Os -Wall -Wextra -Werror $profile_flags $resource_flags -pthread -o /isolate /isolate.c -lseccomp
FROM ${BUILD_FROM}
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR}|g; s|https://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 >/dev/null \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /isolate /isolate
RUN printf 'outside-chroot-fixture\n' > /outside-sentinel
COPY qualify-launcher.py /qualify-launcher.py
ENTRYPOINT ["/usr/bin/python3.11", "-I", "/qualify-launcher.py"]
