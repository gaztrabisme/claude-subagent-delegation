#!/bin/sh
# Install the delegate skill for all Claude Code projects (symlinks, so edits here apply live).
# POSIX sh: runs the same whether your interactive shell is bash, zsh, fish or anything else.
set -eu
src="$(cd "$(dirname "$0")" && pwd)/skills/delegate"
dst="$HOME/.claude/skills/delegate"
mkdir -p "$(dirname "$dst")"
if [ -e "$dst" ] && [ ! -L "$dst" ]; then
  echo "error: $dst exists and is not a symlink; remove it first" >&2
  exit 1
fi
ln -sfn "$src" "$dst"
chmod +x "$src/delegate.py"
echo "installed skill: $dst -> $src"

# Optional `delegate` command, so the runner is one word in any shell.
bin="$HOME/.local/bin"
mkdir -p "$bin"
ln -sfn "$src/delegate.py" "$bin/delegate"
echo "installed command: $bin/delegate"
case ":$PATH:" in
  *":$bin:"*) ;;
  *) echo "note: $bin is not on your PATH; add it, or run: python3 $dst/delegate.py" ;;
esac
