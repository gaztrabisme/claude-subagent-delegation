/** Bridge omp's tool_call hook to the server's Claude-shaped approval hook. */
import { statSync } from "node:fs";
import { resolve } from "node:path";
import { spawnSync } from "node:child_process";
import type { HookAPI } from "@oh-my-pi/pi-coding-agent";

type ToolCall = { toolName?: string; input?: Record<string, unknown> };
type GuardCall = { tool_name: string; tool_input: Record<string, unknown> };

const READONLY_LSP_ACTIONS = new Set([
	"diagnostics", "definition", "type_definition", "implementation", "references",
	"hover", "symbols", "status", "capabilities",
]);

function field(input: Record<string, unknown>, ...names: string[]): unknown {
	for (const name of names) {
		if (input[name] !== undefined) return input[name];
	}
	return undefined;
}

function isGuardableWritePath(value: unknown): value is string {
	if (typeof value !== "string" || value.trim() === "" || value.includes("://") ||
		/[*?{}[\]]/.test(value)) return false;
	try {
		if (statSync(resolve(process.cwd(), value)).isDirectory()) return false;
	} catch {
		// A new file is still a valid write target; the classifier checks its path.
	}
	return true;
}

function claudeCall(event: ToolCall): GuardCall | null {
	const name = event.toolName ?? "";
	const input = event.input && typeof event.input === "object" ? event.input : {};
	if (name === "bash") {
		return { tool_name: "Bash", tool_input: { command: field(input, "command") } };
	}
	if (name === "write" || name === "edit") {
		const path = field(input, "path", "file_path", "filePath");
		if (!isGuardableWritePath(path)) return null;
		return {
			tool_name: name === "write" ? "Write" : "Edit",
			tool_input: {
				file_path: path,
				content: field(input, "content"),
			},
		};
	}
	if (name === "ast_edit") {
		const paths = field(input, "paths");
		if (!Array.isArray(paths) || paths.length === 0 ||
			paths.some((path) => !isGuardableWritePath(path))) return null;
		return {
			tool_name: "MultiEdit",
			tool_input: { edits: paths.map((path) => ({ file_path: path })) },
		};
	}
	if (name === "eval") {
		const code = field(input, "code");
		if (typeof code !== "string") return null;
		const language = field(input, "language");
		if (language && !["javascript", "js", "python", "ruby"].includes(String(language))) return null;
		const interpreter = language === "python" ? "python3 -c" : language === "ruby" ? "ruby -e" : "node -e";
		const quoted = `'${code.replace(/'/g, "'\\''")}'`;
		return { tool_name: "Bash", tool_input: { command: `${interpreter} ${quoted}` } };
	}
	if (name === "lsp") {
		const action = field(input, "action");
		const file = field(input, "file", "path", "file_path");
		if (typeof action !== "string") return null;
		if (READONLY_LSP_ACTIONS.has(action)) {
			return { tool_name: "Read", tool_input: file ? { file_path: file } : {} };
		}
		if (action === "code_actions" && input.apply !== true) {
			return { tool_name: "Read", tool_input: file ? { file_path: file } : {} };
		}
		if (!isGuardableWritePath(file)) return null;
		if (action === "rename_file") {
			const destination = field(input, "new_name");
			if (!isGuardableWritePath(destination)) return null;
			return {
				tool_name: "MultiEdit",
				tool_input: { edits: [{ file_path: file }, { file_path: destination }] },
			};
		}
		if (action !== "rename" && action !== "code_actions") return null;
		return {
			tool_name: "Edit",
			tool_input: {
				file_path: file,
				old_string: field(input, "oldText", "old_string"),
				new_string: field(input, "newText", "new_string"),
			},
		};
	}
	if (name === "read") {
		return { tool_name: "Read", tool_input: { file_path: field(input, "path", "file_path", "filePath") } };
	}
	if (name === "task") return { tool_name: "Task", tool_input: input };
	return null;
}

function blocked(reason: string) {
	return { block: true, reason };
}

export default function (pi: HookAPI) {
	pi.on("tool_call", async (event) => {
		const call = claudeCall(event);
		if (!call) return blocked(`unmapped or unreadable tool '${event.toolName ?? ""}'`);

		const python = process.env.SUBAGENT_GUARD_PYTHON;
		const hook = process.env.SUBAGENT_GUARD_HOOK;
		const agent = process.env.SUBAGENT_GUARD_AGENT_ID;
		if (!python || !hook || !agent) return blocked("guard configuration is missing");

		const input = JSON.stringify({ ...call, cwd: process.cwd() });
		const timeout = Number(process.env.SUBAGENT_HOOK_TIMEOUT ?? 150_000);
		const result = spawnSync(python, [hook, "--agent", agent], {
			input,
			encoding: "utf8",
			timeout: Number.isFinite(timeout) && timeout > 0 ? timeout * 1000 : 150_000,
		});
		if (result.error) return blocked(`guard process failed: ${result.error.message}`);

		try {
			const response = JSON.parse(result.stdout || "{}");
			const output = response.hookSpecificOutput;
			if (result.status === 0 && output?.permissionDecision === "allow") return undefined;
			if (output?.permissionDecision === "deny") {
				return blocked(output.permissionDecisionReason || "denied by the supervisor");
			}
		} catch {
			// A missing or malformed response is a deny below.
		}
		return blocked("guard returned no valid allow decision");
	});
}
