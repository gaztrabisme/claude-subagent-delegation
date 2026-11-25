# r7a toolerr tests

Finding: a RegistryError raised in a tool body that is not re-raised as ToolError reaches the client as the bare "Error executing tool <name>" (UnexpectedToolError).

| Test | State | Reason |
|---|---|---|
| m_delegate_at_the_lane_cap_raises_tool_error_with_cap_text | red | delegate re-raises bare RegistryError (no caller_key, no admission barrier) |
| m_delegate_with_an_unknown_workspace_raises_tool_error_with_message | red | bare RegistryError raised from workspace resolution |
| m_continue_on_a_closed_agent_raises_tool_error_with_message | red | bare RegistryError ("agent ... is closed") |
| m_unknown_id_in_other_tools[cancel] | red | registry.agent raises RegistryError; wrapper gives UnexpectedToolError |
| m_unknown_id_in_other_tools[await] | red | registry.find_run raises RegistryError; same |
| m_unknown_id_in_other_tools[transcript] | red | same |
| m_end_to_end_through_the_mcp_wrapper_the_cap_text_reaches_the_client | red | app.call_tool("delegate") yields UnexpectedToolError "Error executing tool delegate" |

Tests treat UnexpectedToolError (a ToolError subclass) as a failure; only a plain ToolError carrying the message passes.
No green tests. tests/r7a_lifecycle still passes (11).
