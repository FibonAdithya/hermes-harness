FROM node:22-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates curl jq ripgrep python3 python3-venv make \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/githubcli.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code

# Commits are attributable: distinct from the owner's own work in git log.
RUN git config --system user.name "Hermes Agent" \
    && git config --system user.email "hermes-agent@users.noreply.github.com"

# The credentials file is bind-mounted here at run time; Docker cannot mount a
# file into a directory that does not exist.
RUN useradd -m -u 1000 runner \
    && mkdir -p /home/runner/.claude \
    && chown -R runner:runner /home/runner

USER runner
WORKDIR /work
