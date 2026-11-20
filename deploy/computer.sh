#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
if ! command -v uv >/dev/null 2>&1; then
    echo "Install uv from https://docs.astral.sh/uv/getting-started/installation/ and run this command again." >&2
    exit 1
fi
if [ ! -x "${ALEPH_HERMES_PYTHON:-$HOME/.local/share/aleph/hermes/.venv/bin/python}" ]; then
    sh deploy/install-hermes.sh
fi
exec uv run --no-project --python 3.12 --with 'aiohttp>=3.12.15,<4' python -m aleph.computer "$@"
