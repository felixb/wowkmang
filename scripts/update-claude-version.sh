#!/bin/bash
set -eo pipefail

DOCKERFILE="images/base/Dockerfile"

# Extract current version from Dockerfile
CURRENT_VERSION=$(grep 'ENV CLAUDE_CODE_VERSION=' "$DOCKERFILE" | cut -d'=' -f2)
echo "Current Claude Code version: $CURRENT_VERSION"

# Fetch latest version from npm registry
LATEST_VERSION=$(curl -sf https://registry.npmjs.org/@anthropic-ai/claude-code/latest | jq -r '.version')
echo "Latest Claude Code version: $LATEST_VERSION"

if [ "$CURRENT_VERSION" != "$LATEST_VERSION" ]; then
    echo "Update needed: $CURRENT_VERSION -> $LATEST_VERSION"
    sed -i "s/ENV CLAUDE_CODE_VERSION=.*/ENV CLAUDE_CODE_VERSION=$LATEST_VERSION/" "$DOCKERFILE"
    if [[ -n "$GITHUB_OUTPUT" ]] ; then
      echo "latest_version=$LATEST_VERSION" >> "$GITHUB_OUTPUT"
      echo "update_needed=true" >> "$GITHUB_OUTPUT"
    fi
else
    echo "No update needed."
    if [[ -n "$GITHUB_OUTPUT" ]] ; then
      echo "update_needed=false" >> "$GITHUB_OUTPUT"
    fi
fi
