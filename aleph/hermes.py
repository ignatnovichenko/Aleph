"""Hermes owns the agent loop; Aleph owns provider access, tool authorization and persistence."""
import asyncio
import hmac
import json
import logging
import os
from pathlib import Path
import secrets
import tempfile
import time

from aiohttp import web

from .llm import Aleph, ModelUnavailable, TOOLS

log = logging.getLogger(__name__)
TOOL_NAMES = {tool["function"]["name"] for tool in TOOLS}
MAX_STEPS = 12
MAX_PARALLEL_CALLS = 12
TURN_TIMEOUT = 265
WORKER = Path(__file__).with_name("hermes_worker.py")


def hermes_python():
    path = Path(os.environ.get("ALEPH_HERMES_PYTHON") or
                Path.home() / ".local/share/aleph/hermes/.venv/bin/python")
    if not path.is_absolute() or not os.access(path, os.X_OK):
        raise RuntimeError("Hermes Python runtime missing. Run deploy/install-hermes.sh or set ALEPH_HERMES_PYTHON.")
    return path


def create_agent(session, model_key):
    engine = os.environ.get("ALEPH_ENGINE", "hermes")
    if engine == "hermes":
        return HermesAleph(session, model_key, hermes_python())
    if engine == "direct":
        return Aleph(session, model_key)
    raise ValueError("ALEPH_ENGINE must be hermes or direct")


class TurnBroker:
    """A private, authenticated loopback endpoint for exactly one inbound update."""

    def __init__(self, agent, store, uid, ledger, update_id, model, opencode_model, project_id):
        self.agent, self.store, self.uid = agent, store, uid
        self.ledger, self.update_id, self.model = ledger, update_id, model
        self.opencode_model = opencode_model
        self.project_id = project_id
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
        data = await request.json()
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
                        data["messages"], self.opencode_model, tools=bool(data.get("tools")) and step < MAX_STEPS - 1,
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
        payload = await request.json()
        name, args = payload.get("name"), payload.get("arguments")
        match = self.calls.get(payload.get("id"))
        if not match or name not in TOOL_NAMES:
            return web.json_response({"error": "Tool not authorized for this conversation."}, status=400)
        step, n, call = match
        function = call["function"]
        if name != function["name"] or args != json.loads(function["arguments"]):
            return web.json_response({"error": "Tool arguments changed in transit."}, status=400)
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

    async def respond(self, store, uid, text, photo, ledger, update_id):
        project_id = ledger.bind_project(update_id, uid)
        messages, opencode_model = self.turn_context(store, uid, text, photo, project_id)
        choice, _ = self.bind_turn_model(uid, ledger, update_id, opencode_model)
        model = choice["model"] or choice["provider"]
        broker = TurnBroker(self, store, uid, ledger, update_id, model, opencode_model, project_id)
        app = web.Application(client_max_size=24 * 1024 * 1024)
        app.router.add_post("/v1/chat/completions", broker.complete)
        app.router.add_post("/tool", broker.tool)
        runner = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await runner.setup()
        try:
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = runner.addresses[0][1]
            turn = {"endpoint": f"http://127.0.0.1:{port}", "key": broker.token,
                    "model": model, "messages": messages, "tools": TOOLS, "max_steps": MAX_STEPS - 1}
            answer = await self.run_hermes(turn, broker)
            return answer[:14000]
        finally:
            await runner.cleanup()

    async def run_hermes(self, turn, broker):
        # A new Hermes home for every turn keeps files, plugins, log output and
        # cached state outside both other users' turns and the server credential store.
        with tempfile.TemporaryDirectory(prefix="aleph-hermes-") as home:
            env = {"PATH": os.defpath, "LANG": "C.UTF-8", "HERMES_HOME": home,
                   "NO_PROXY": "127.0.0.1,localhost", "PYTHONUNBUFFERED": "1"}
            process = await asyncio.create_subprocess_exec(
                str(self.python), str(WORKER), cwd=home, env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(
                    process.communicate(json.dumps(turn, ensure_ascii=False).encode()), TURN_TIMEOUT,
                )
            except TimeoutError:
                raise ModelUnavailable() from None
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if broker.failure:
                raise broker.failure
            try:
                packet = json.loads(output)
            except (ValueError, UnicodeError):
                packet = {}
            answer = packet.get("answer")
            if process.returncode or not isinstance(answer, str) or not answer.strip():
                log.warning("Hermes did not finish the turn (%s)", packet.get("error_type", "worker_failure"))
                raise ModelUnavailable()
            return answer.strip()
