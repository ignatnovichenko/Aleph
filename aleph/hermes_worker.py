"""Run one upstream AIAgent in a process without Aleph's credentials."""
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
from urllib.request import build_opener, ProxyHandler, Request


def run(turn):
    # This config is private to this worker and contains no user data.
    root = Path(os.environ["HERMES_HOME"])
    (root / "config.yaml").write_text("model:\n  streaming: false\ncompression:\n  enabled: false\ntools:\n  tool_search:\n    enabled: 'off'\n")
    from run_agent import AIAgent
    from tools.registry import registry
    from tools.approval_context import _approval_tool_call_id

    local_http = build_opener(ProxyHandler({}))

    def handler(name):
        def invoke(arguments, **_):
            tool_call_id = _approval_tool_call_id.get()
            request = Request(turn["endpoint"] + "/tool", method="POST", headers={
                "Authorization": "Bearer " + turn["key"], "Content-Type": "application/json",
            }, data=json.dumps({"name": name, "arguments": arguments, "id": tool_call_id}).encode())
            timeout = 105 if name in {"inspect_document", "edit_document", "create_document"} else 22
            with local_http.open(request, timeout=timeout) as response:
                return response.read(180000).decode()
        return invoke

    for tool in turn["tools"]:
        schema = tool["function"]
        registry.register(schema["name"], "aleph_workspace", schema, handler(schema["name"]))

    messages = turn["messages"]
    agent = AIAgent(
        base_url=turn["endpoint"] + "/v1", api_key=turn["key"], provider="custom",
        api_mode="chat_completions", model=turn["model"], max_iterations=turn["max_steps"],
        max_tokens=4096, enabled_toolsets=["aleph_workspace"], quiet_mode=True,
        skip_context_files=True, skip_memory=True, skip_background_review=True, load_soul_identity=False,
        capabilities={"vision": True}, run_budget_seconds=240,
    )
    try:
        # Verify Hermes' assembled tool surface before giving it any user input.
        expected = {t["function"]["name"] for t in turn["tools"]}
        if agent.valid_tool_names != expected:
            raise RuntimeError("Unexpected Hermes tool set")
        result = agent.run_conversation(
            messages[-1]["content"], system_message=messages[0]["content"],
            conversation_history=messages[1:-1],
        )
        if result.get("failed") or result.get("error") or not result.get("final_response"):
            return {"error_type": "agent_turn_failed"}
        return {"answer": result["final_response"]}
    finally:
        agent.close()


if __name__ == "__main__":
    try:
        payload = json.load(sys.stdin)
        # Hermes can emit status messages even in quiet mode. Keep stdout as a
        # one-object protocol and do not relay its diagnostic output to users.
        with redirect_stdout(sys.stderr):
            response = run(payload)
    except Exception as error:
        response = {"error_type": type(error).__name__}
    print(json.dumps(response, ensure_ascii=False))
