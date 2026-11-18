#!/bin/sh
set -eu

dest=${1:-"$HOME/.local/share/aleph/hermes"}
release=v0.23.2
commit=ae2a81ccc00e9b7c06301523e5bb09608397149c

if [ ! -d "$dest/.git" ]; then
    mkdir -p "$(dirname "$dest")"
    git -c advice.detachedHead=false clone --depth 1 --branch "$release" https://github.com/NousResearch/hermes-agent.git "$dest"
fi
if [ "$(git -C "$dest" rev-parse HEAD)" != "$commit" ]; then
    echo "Hermes checkout needs $commit. Use a fresh installation directory." >&2
    exit 1
fi

cd "$dest"
uv sync --python 3.14 --frozen --no-dev --no-default-groups
