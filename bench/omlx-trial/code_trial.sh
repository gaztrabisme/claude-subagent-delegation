#!/bin/bash
# L3b code trial: three packages, one at a time, 21-minute cap each. Usage: code_trial.sh <provider> <tag>
N=/private/tmp/claude-501/-Users-owner-Documents-Work-tools-mcp-subagent-mcp/8e2468da-e76c-4e99-9739-c14fc8c57bad/scratchpad/omlx-run
P=$1; TAG=$2
export SUBAGENT_CONFIG=$N/../omlx-swift-overlay.toml
set -a; . ~/.config/agent-capabilities/subagent.env; set +a
for id in p1 p2 p3; do
  W=$N/ws-$TAG-$id
  mkdir -p "$W" && cp -R "$N/../omlx-trial/code/$id/." "$W/" && (cd "$W" && git init -q && git add -A && git -c user.name=trial -c user.email=trial@local commit -qm "trial $id")
  START=$(date +%s)
  perl -e 'alarm shift; exec @ARGV' 1260 subagent --root "$W" run --plan "$N/plan-$id.md" --provider "$P" > "$N/out-$TAG-$id.txt" 2>&1
  RC=$?
  SECS=$(( $(date +%s) - START ))
  TESTS=$(cd "$W" && python3 -m pytest -q 2>&1 | tail -1)
  LOCKED=$(git -C "$W" diff --quiet HEAD -- tests && echo unchanged || echo CHANGED)
  echo "$TAG $id rc=$RC seconds=$SECS tests='$TESTS' tests_dir=$LOCKED" | tee -a "$N/code-trial-$TAG.log"
  memory_pressure | tail -1 >> "$N/code-trial-$TAG.log"
done
