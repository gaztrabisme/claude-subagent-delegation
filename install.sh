#!/bin/sh
# Install the delegate skill for all Claude Code projects (symlink, so edits here apply live).
# POSIX sh: runs the same whether your interactive shell is bash, zsh, fish or anything else.
#
# The runner itself is a Python module now: install the package (`uv sync`, `pip install -e .`)
# and call it as `subagent`, or as `python3 -m subagent.cli` without installing the console script.
set -eu
src="$(cd "$(dirname "$0")" && pwd)/skills/delegate"
dst="$HOME/.claude/skills/delegate"
mkdir -p "$(dirname "$dst")"
if [ -e "$dst" ] && [ ! -L "$dst" ]; then
  echo "error: $dst exists and is not a symlink; remove it first" >&2
  exit 1
fi
ln -sfn "$src" "$dst"
echo "installed skill: $dst -> $src"
