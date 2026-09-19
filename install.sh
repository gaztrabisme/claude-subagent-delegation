#!/usr/bin/env bash
# Install the delegate skill for all Claude Code projects (symlink, so edits here apply live).
set -euo pipefail
src="$(cd "$(dirname "$0")" && pwd)/skills/delegate"
dst="$HOME/.claude/skills/delegate"
mkdir -p "$(dirname "$dst")"
if [ -e "$dst" ] && [ ! -L "$dst" ]; then
  echo "error: $dst exists and is not a symlink; remove it first" >&2
  exit 1
fi
ln -sfn "$src" "$dst"
echo "installed: $dst -> $src"
