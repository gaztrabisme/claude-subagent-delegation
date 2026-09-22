#!/bin/sh
# Install the delegate skill and the `subagent` package.
# POSIX sh: runs the same whether your interactive shell is bash, zsh, fish or anything else.
#
# The skill is a symlink into this checkout, so edits here apply live. The
# runner itself is a Python package installed as `subagent` (console script);
# `uv tool install --editable .` puts it on PATH, or `pipx install -e .` when
# uv is not installed.
set -eu
repo="$(cd "$(dirname "$0")" && pwd)"
src="$repo/skills/delegate"
dst="$HOME/.claude/skills/delegate"
mkdir -p "$(dirname "$dst")"
if [ -e "$dst" ] && [ ! -L "$dst" ]; then
  echo "error: $dst exists and is not a symlink; remove it first" >&2
  exit 1
fi
ln -sfn "$src" "$dst"
echo "installed skill: $dst -> $src"

# Tests exercise the CLI straight from the checkout (PYTHONPATH); skip the
# slow, network-touching tool install there.
if [ "${SUBAGENT_SKIP_INSTALL:-}" = "1" ]; then
  echo "skipping package install (SUBAGENT_SKIP_INSTALL=1)"
elif command -v uv >/dev/null 2>&1; then
  uv tool install --editable "$repo"
else
  echo "install the package yourself: pipx install -e '$repo'"
fi
echo "next: run 'subagent init' to write ~/.config/subagent/config.toml"
