FROM node:20

ENTRYPOINT ["/bin/bash"]

# Install system tools, and gh CLI
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        less git procps sudo fzf man-db unzip gnupg2 \
        aggregate jq nano vim \
        openssh-client curl python3 python3-pip && \
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /usr/share/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install --no-cache-dir --break-system-packages pre-commit

# Install Claude Code CLI
ENV CLAUDE_CODE_VERSION=2.1.97
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

# Add GitHub SSH host keys
RUN ssh-keyscan github.com >> /etc/ssh/ssh_known_hosts

# Create wowkmang user
RUN useradd -m -s /bin/bash wowkmang

RUN mkdir -p /workspace/repo
WORKDIR /workspace/repo
RUN chown -R wowkmang:wowkmang /workspace
