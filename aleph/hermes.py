"""Hermes owns the agent loop; Aleph owns provider access, tool authorization and persistence."""
import asyncio
import hmac
import json
import logging
import os
import secrets
import time

from aiohttp import web

from . import projects
from .hermes_process import TURN_TIMEOUT, hermes_python, run_worker
from .llm import Aleph, ModelUnavailable, REPORT_TOOLS, TOOLS

log = logging.getLogger(__name__)
MAX_STEPS = 12
MAX_PARALLEL_CALLS = 12


def create_agent(session, model_key):
    engine = os.environ.get("ALEPH_ENGINE", "hermes")
    if engine == "hermes":
        return HermesAleph(session, model_key, hermes_python())
    if engine == "direct":
        return Aleph(session, model_key)
    raise ValueError("ALEPH_ENGINE must be hermes or direct")


class TurnBroker:
    """A private, authenticated loopback endpoint for exactly one inbound update."""

    def __init__(self, agent, store, uid, ledger, update_id, model, opencode_model, project_id, *, toolset=TOOLS):
        self.agent, self.store, self.uid = agent, store, uid
        self.ledger, self.update_id, self.model = ledger, update_id, model
        self.opencode_model = opencode_model
        self.project_id = project_id
        self.toolset = toolset
        self.tool_names = {tool["function"]["name"] for tool in toolset}
        self.token = secrets.token_urlsafe(32)
        self.step = 0
        self.failure = None
        self.model_lock = asyncio.Lock()
        self.tool_lock = asyncio.Lock()
        self.calls = {}

    def check(self, request):
        if not hmac.compare_digest(request.headers.get("Authorization", ""), "Bearer " + self.token):
            raise web.HTTPUnauthorized()

    async def complete(self, request):
        self.check(request)
        return await self.complete_payload(await request.json())

    async def complete_payload(self, data):
        if data.get("model") != self.model or data.get("stream") or not isinstance(data.get("messages"), list):
            raise web.HTTPBadRequest()
        async with self.model_lock:
            step = self.step
            if step >= MAX_STEPS:
                return web.json_response({"error": {"message": "Turn limit reached"}}, status=400)
            self.failure = None
            try:
                reply = self.ledger.get_step(self.update_id, step)
                if reply is None:
                    reply = await self.agent.complete_turn(
                        data["messages"], self.opencode_model, tools=self.toolset if data.get("tools") and step < MAX_STEPS - 1 else False,
                        uid=self.uid, project_id=self.project_id, ledger=self.ledger, update_id=self.update_id, step=step,
                    )
                    self.ledger.save_step(self.update_id, step, reply)
            except ModelUnavailable as exc:
                self.failure = exc
                # Aleph's provider client already owns HTTP retries and regional fallback.
                return web.json_response({"error": {"message": "Model temporarily unavailable"}}, status=400)
            calls = reply.get("tool_calls") or []
            if len(calls) > MAX_PARALLEL_CALLS:
                return web.json_response({"error": {"message": "Tool limit reached"}}, status=400)
            self.step += 1
            for n, call in enumerate(calls):
                self.calls[call["id"]] = (step, n, call)
            return web.json_response({
                "id": f"chatcmpl-aleph-{step}", "object": "chat.completion", "created": int(time.time()),
                "model": reply.get("_aleph_model", {}).get("model") or self.model, "choices": [{"index": 0,
                    "message": {"role": "assistant", **{k: v for k, v in reply.items() if not k.startswith("_")}},
                    "finish_reason": "tool_calls" if calls else "stop"}],
            })

    async def tool(self, request):
        self.check(request)
        return await self.tool_payload(await request.json())

    async def tool_payload(self, payload):
        name, args = payload.get("name"), payload.get("arguments")
        match = self.calls.get(payload.get("id"))
        if not match or name not in self.tool_names:
            return web.json_response({"error": "Tool not authorized for this conversation."}, status=400)
        step, n, call = match
        function = call["function"]
        if name != function["name"] or args != json.loads(function["arguments"]):
            return web.json_response({"error": "Tool arguments changed in transit."}, status=400)
        if self.update_id < 0:
            stage = {"create_document":"writing","edit_document":"writing","inspect_document":"reading",
                     "read_project_source":"reading","reference_search":"research"}.get(name)
            if stage:
                self.store.db.execute("UPDATE scheduled_reports SET stage=? WHERE task_id=?",(stage,-self.update_id))
        async with self.tool_lock:
            result = await self.agent.call_tool(self.store, self.uid, self.ledger, self.update_id, step, n, call, project_id=self.project_id)
        response = json.dumps(result, ensure_ascii=False, default=str)
        if len(response) > 26000:
            response = json.dumps({"partial_result": response[:25000]}, ensure_ascii=False)
        return web.Response(text=response, content_type="application/json")


class HermesAleph(Aleph):
    def __init__(self, session, model_key, python):
        super().__init__(session, model_key)
        self.python = python

    async def respond(self, store, uid, text, photo, ledger, update_id, *, scheduled=False):
        project_id = ledger.bind_project(update_id, uid)
        project = projects.get(store, uid, project_id)
        if scheduled and store.get_task(uid,-update_id)["report_on_server"]:
            project = {**project,"computer_id":None}
        messages, opencode_model = self.turn_context(store, uid, text, photo, project_id, scheduled=scheduled)
        choice, _ = self.bind_turn_model(uid, ledger, update_id, opencode_model)
        model = choice["model"] or choice["provider"]
        toolset = REPORT_TOOLS if scheduled else TOOLS
        if project.get("computer_id"):
            from .llm import _tool, _string
            toolset = [*toolset, _tool("folder_files", "List available documents in this project's locally granted folder.", {}),
                       _tool("open_folder_file", "Import the named document from the project folder for inspect_document or edit_document. Use only relevant files.", {"filename":_string("Exact name returned by folder_files")},["filename"])]
            messages[0]["content"] += "\nFor a request about the project folder use folder_files and open_folder_file. Inspect the returned file ID. Cite original file and page or cell when comparing. Use create_document or edit_document for the final attachment."
        broker = TurnBroker(self, store, uid, ledger, update_id, model, opencode_model, project_id, toolset=toolset)
        turn = {"project_id":project_id, "model": model, "messages": messages, "tools": toolset, "max_steps": MAX_STEPS - 1}
        if computer_id := project.get("computer_id"):
            try:
                packet = await store.computer_hub.run(computer_id, project["computer_name"], turn, broker, TURN_TIMEOUT)
            except TimeoutError:
                raise ModelUnavailable() from None
            return self.worker_answer(packet, broker)
        app = web.Application(client_max_size=24 * 1024 * 1024)
        app.router.add_post("/v1/chat/completions", broker.complete)
        app.router.add_post("/tool", broker.tool)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            turn.update(endpoint=f"http://127.0.0.1:{port}", key=broker.token)
            answer = await self.run_hermes(turn, broker)
            return answer[:14000]
        finally:
            await runner.cleanup()

    async def run_hermes(self, turn, broker):
        try:
            packet = await run_worker(self.python, turn)
        except TimeoutError:
            raise ModelUnavailable() from None
        return self.worker_answer(packet, broker)

    @staticmethod
    def worker_answer(packet, broker):
        if broker.failure:
            raise broker.failure
        if "answer" not in packet:
            log.warning("Hermes did not finish the turn (%s)", packet.get("error_type", "worker_failure"))
            raise ModelUnavailable()
        return packet["answer"]
