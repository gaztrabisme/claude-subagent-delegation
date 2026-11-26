name=$(python3 -c "import os,re;print(re.search(r'You are worker \"(\w+)\"', os.environ['FAKE_PROMPT']).group(1))")
if [ -L node_modules ]; then echo "node_modules linked in $name"; else echo "node_modules not linked in $name"; fi
mkdir -p src .subagent
if [ "$name" = a ]; then
  echo 'export const a = () => "A";' > src/a.js
  echo "tweaked by a" >> README.md          # not owned by a: must be discarded
else
  sleep 1; echo 'export const b = () => "B";' > src/b.js
fi
echo "{\"status\":\"done\",\"summary\":\"$name done\"}" > .subagent/result.json
