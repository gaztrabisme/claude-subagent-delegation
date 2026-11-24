import { spawn } from 'node:child_process';

const TOOL_NAMES = Object.freeze({
  bash: 'Bash',
  read: 'Read',
  write: 'Write',
  edit: 'Edit',
  grep: 'Grep',
  glob: 'Glob',
});

const DEFAULT_TIMEOUT_SECONDS = 150;
const MAX_TIMEOUT_SECONDS = 300;
const MAX_OUTPUT_BYTES = 64 * 1024;

function deny(reason) {
  return { allow: false, reason: String(reason || 'blocked: subagent guard denied the call') };
}

function commandArguments(command) {
  const args = [];
  let value = '';
  let quote = null;
  let escaped = false;
  let started = false;

  for (const char of command) {
    if (escaped) {
      value += char;
      escaped = false;
      started = true;
      continue;
    }
    if (quote === "'") {
      if (char === "'") quote = null;
      else value += char;
      started = true;
      continue;
    }
    if (quote === '"') {
      if (char === '"') quote = null;
      else if (char === '\\') escaped = true;
      else value += char;
      started = true;
      continue;
    }
    if (char === "'" || char === '"') {
      quote = char;
      started = true;
    } else if (char === '\\') {
      escaped = true;
      started = true;
    } else if (/\s/.test(char)) {
      if (started) args.push(value);
      value = '';
      started = false;
    } else {
      value += char;
      started = true;
    }
  }

  if (escaped || quote !== null) throw new Error('guard hook command has invalid quoting');
  if (started) args.push(value);
  return args;
}

function timeoutMilliseconds(env) {
  const seconds = Number(env.SUBAGENT_HOOK_TIMEOUT || DEFAULT_TIMEOUT_SECONDS);
  if (!Number.isFinite(seconds) || seconds <= 0) return DEFAULT_TIMEOUT_SECONDS * 1000;
  return Math.min(seconds, MAX_TIMEOUT_SECONDS) * 1000;
}

function hookPayload(call, toolName) {
  const input = call.arguments === undefined ? {} : call.arguments;
  if (input === null || typeof input !== 'object') {
    throw new Error('DSH tool arguments are not an object');
  }
  return {
    hook_event_name: 'PreToolUse',
    tool_name: toolName,
    tool_input: input,
    cwd: call.cwd || call.agent?.session?.header?.cwd || process.cwd(),
    session_id: call.sessionId || call.agent?.session?.id || call.agent?.id || '',
  };
}

async function runHook(command, payload, env) {
  const args = commandArguments(command);
  if (!args.length || !args[0]) throw new Error('guard hook command is empty');
  const hookEnv = { ...process.env, ...env };
  for (const name of Object.keys(hookEnv)) {
    if (/(KEY|TOKEN|SECRET|PASSWORD)/i.test(name)) delete hookEnv[name];
  }

  return await new Promise((resolve, reject) => {
    let child;
    try {
      child = spawn(args[0], args.slice(1), {
        env: hookEnv,
        stdio: ['pipe', 'pipe', 'ignore'],
        windowsHide: true,
      });
    } catch (error) {
      reject(error);
      return;
    }

    let stdout = '';
    let outputTooLarge = false;
    let settled = false;
    const finish = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(result);
    };
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish({ error: 'blocked: subagent guard hook timed out' });
    }, timeoutMilliseconds(env));

    child.on('error', (error) => finish({ error: `blocked: could not start subagent guard hook (${error.message})` }));
    child.stdout.on('data', (chunk) => {
      if (Buffer.byteLength(stdout) + chunk.length > MAX_OUTPUT_BYTES) {
        outputTooLarge = true;
        child.kill('SIGKILL');
        return;
      }
      stdout += chunk.toString('utf8');
    });
    child.on('close', (code) => finish({ code, stdout, outputTooLarge }));
    child.stdin.on('error', () => {});
    child.stdin.end(`${JSON.stringify(payload)}\n`);
  });
}

export async function decide(call, env = {}) {
  const tool = typeof call?.name === 'string' ? call.name.toLowerCase() : '';
  const toolName = TOOL_NAMES[tool];
  if (!toolName) return deny(`blocked: unsupported DSH tool ${tool || '(unnamed)'}`);

  const command = env.SUBAGENT_GUARD_HOOK;
  if (typeof command !== 'string' || !command.trim()) {
    return deny('blocked: SUBAGENT_GUARD_HOOK is not set');
  }

  let payload;
  try {
    payload = hookPayload(call, toolName);
  } catch (error) {
    return deny(`blocked: ${error instanceof Error ? error.message : String(error)}`);
  }

  let result;
  try {
    result = await runHook(command, payload, env);
  } catch (error) {
    return deny(`blocked: could not run subagent guard hook (${error instanceof Error ? error.message : String(error)})`);
  }
  if (result.error) return deny(result.error);
  if (result.outputTooLarge) return deny('blocked: subagent guard hook output exceeded its limit');

  let response;
  try {
    response = JSON.parse(result.stdout.trim());
  } catch {
    return deny('blocked: subagent guard hook returned invalid JSON');
  }
  const output = response?.hookSpecificOutput;
  const decision = output?.permissionDecision;
  const reason = typeof output?.permissionDecisionReason === 'string'
    ? output.permissionDecisionReason.trim()
    : '';
  if (decision === 'allow' && result.code === 0) {
    return { allow: true, reason: reason || 'allowed by subagent guard' };
  }
  if (decision === 'deny') return deny(reason || 'blocked by subagent guard');
  if (decision === 'allow') return deny('blocked: subagent guard hook exited unsuccessfully');
  return deny('blocked: subagent guard hook did not return an allow or deny decision');
}

export function apply(ctx) {
  const handler = async (exec, next) => {
    const decision = await decide({
      name: exec?.name,
      arguments: exec?.arguments,
      callId: exec?.callId,
      cwd: exec?.cwd || exec?.agent?.session?.header?.cwd,
      sessionId: exec?.sessionId || exec?.agent?.session?.id || exec?.agent?.id,
      agent: exec?.agent,
    }, process.env);
    if (!decision.allow) return { kind: 'deny', reason: decision.reason };
    return await next();
  };

  const register = () => ctx.on('tools/pre-execute', handler, { prepend: true });
  if (typeof ctx.effect === 'function') {
    ctx.effect(function* () {
      yield register();
    });
  } else {
    register();
  }
}
