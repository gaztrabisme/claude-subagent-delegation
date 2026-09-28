/** Bridge omp's tool_call hook to the server's Claude-shaped approval hook. */
import { spawnSync } from "node:child_process";
import type { HookAPI } from "@oh-my-pi/pi-coding-agent";

type ToolCall = { toolName?: string; input?: Record<string, unknown> };

function field(input: Record<string, unknown>, ...names: string[]): unknown {
	for (const name of names) {
		if (input[name] !== undefined) return input[name];
	}
	return undefined;
}

function claudeCall(event: ToolCall): { tool_name: string; tool_input: Record<string, unknown> } {
	const name = event.toolName ?? "";
	const input = event.input && typeof event.input === "object" ? event.input : {};
	if (name === "bash") return { tool_name: "Bash", tool_input: { command: field(input, "command") } };
	if (name === "write") {
		return {
			tool_name: "Write",
			tool_input: { file_path: field(input, "path", "file_path", "filePath"), content: field(input, "content") },
		};
	}
	if (name === "edit") {
		return {
			tool_name: "Edit",
			tool_input: {
				file_path: field(input, "path", "file_path", "filePath"),
				old_string: field(input, "oldText", "old_string"),
				new_string: field(input, "newText", "new_string"),
			},
		};
	}
	if (name === "read") {
		return { tool_name: "Read", tool_input: { file_path: field(input, "path", "file_path", "filePath") } };
	}
	return { tool_name: name, tool_input: input };
}

function blocked(reason: string) {
	return { block: true, reason };
}

export default function (pi: HookAPI) {
	pi.on("tool_call", async (event) => {
		const python = process.env.SUBAGENT_GUARD_PYTHON;
		const hook = process.env.SUBAGENT_GUARD_HOOK;
		const agent = process.env.SUBAGENT_GUARD_AGENT_ID;
		if (!python || !hook || !agent) return blocked("guard configuration is missing");

		const call = claudeCall(event);
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
