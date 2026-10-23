# Linux smoke environment for sampan (see scripts/linux-smoke/run.sh). Build context = `git archive` of the repo.
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends tmux git curl ca-certificates locales lsof procps util-linux \
 && locale-gen en_US.UTF-8 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.11.3 /uv /uvx /usr/local/bin/
COPY scripts/linux-smoke/fake-bin/ /usr/local/fake-bin/
RUN useradd -m -s /bin/bash tester
USER tester
ENV HOME=/home/tester LANG=en_US.UTF-8 LC_ALL=en_US.UTF-8 TERM=xterm-256color UV_LINK_MODE=copy
RUN uv python install 3.11 3.12 3.13
COPY --chown=tester:tester . /home/tester/src
WORKDIR /home/tester/src
