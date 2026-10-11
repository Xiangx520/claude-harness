FROM python:3.13-slim-bookworm

ARG DEBIAN_MIRROR=deb.debian.org
ARG DEBIAN_MIRROR_SCHEME=https
RUN sed -i "s|http://deb.debian.org|${DEBIAN_MIRROR_SCHEME}://${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=2 -o Acquire::https::Timeout=30 install -y --no-install-recommends git ripgrep coreutils \
    && sed -i "s|${DEBIAN_MIRROR_SCHEME}://${DEBIAN_MIRROR}|https://deb.debian.org|g" /etc/apt/sources.list.d/debian.sources \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 sandbox \
    && useradd --uid 1000 --gid 1000 --no-create-home sandbox

USER 1000:1000
WORKDIR /workspace
ENV HOME=/tmp PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["/bin/sleep", "infinity"]
