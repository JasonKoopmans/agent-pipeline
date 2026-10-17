# syntax=docker/dockerfile:1
# Core runner image: everything every role and stage needs, nothing more.
#   prepare / publish (the GitHub-token stages) always run this image.
#   Roles that do not build code (planner, merger) run it for every stage.
# Stack images (images/stacks/*) and toolsets (images/toolsets/*) are
# devcontainers layered on top of it.
#
#   docker build -f images/core/Dockerfile -t agent-runner:dev-core .

# ---------------------------------------------------------------- builder
FROM docker.io/jdxcode/mise:2026.10.7-debian AS builder
ENV MISE_DATA_DIR=/opt/mise MISE_CACHE_DIR=/tmp/mise-cache MISE_YES=1
# libatomic1: node's arm64 build links it (builder only; Node never ships).
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 && rm -rf /var/lib/apt/lists/*
COPY images/core/mise.toml /etc/mise/config.toml
RUN MISE_GLOBAL_CONFIG_FILE=/etc/mise/config.toml mise install
# Collect the runtime payload in /out: a relocatable Python and the native
# Claude Code binary (the npm package's platform build; no Node needed to run it).
# No gh: agent-tick talks to the GitHub REST API directly.
RUN set -eux; \
    mkdir -p /out/opt /out/usr/local/bin; \
    # Python lives at its final path in both stages, so script shebangs stay valid.
    cp -a "$(MISE_GLOBAL_CONFIG_FILE=/etc/mise/config.toml mise where python)" /opt/python; \
    rm -rf /opt/python/lib/python3.14/test /opt/python/lib/python3.14/idlelib /opt/python/lib/python3.14/tkinter; \
    claude_bin="$(find /opt/mise/installs/npm-anthropic-ai-claude-code -type f -path '*claude-code-linux-*/claude' | head -1)"; \
    test -n "$claude_bin"; cp "$claude_bin" /out/usr/local/bin/claude; \
    chmod 0755 /out/usr/local/bin/claude
COPY pyproject.toml /src/pyproject.toml
COPY agent_tick /src/agent_tick
RUN /opt/python/bin/python3 -m pip install --no-cache-dir --no-compile /src \
 && rm -rf /opt/python/lib/python3.14/site-packages/pip* /opt/python/bin/pip* \
 && cp -a /opt/python /out/opt/python

# ---------------------------------------------------------------- runtime
FROM docker.io/library/debian:trixie-slim
# git: workspace operations · openssl: signs GitHub App JWTs · bubblewrap +
# socat: Claude Code's Bash sandbox · tini: reaps session child processes.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates git openssl bubblewrap socat tini \
 && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/ /
RUN groupadd --gid 1000 agent && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent
ENV PATH=/opt/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
COPY prompts /opt/agent/prompts
COPY mcp /opt/agent/mcp
RUN claude --version && python3 --version && agent-tick check-config --config /dev/null; test $? -eq 2

USER 1000:1000
ENV HOME=/home/agent AGENT_HOME=/home/agent
WORKDIR /work
ENTRYPOINT ["/usr/bin/tini", "--", "agent-tick"]
CMD ["check-config"]
