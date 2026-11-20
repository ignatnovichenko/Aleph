"""Connect and run Aleph's Hermes worker on a personal computer."""
import argparse
import asyncio
from getpass import getpass
import hmac
import json
import os
from pathlib import Path
import re
import secrets
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit, urlunsplit

import aiohttp
from aiohttp import web

from .computers import JOB_REQUEST_LIMIT, RUNNER_PATH
from .hermes_process import hermes_python, run_worker

DEFAULT_CONFIG = Path.home() / ".config/aleph/computer.json"


def origin(value):
    parts = urlsplit(value)
    if (any(ch.isspace() for ch in value) or parts.scheme != "https" or not parts.hostname or parts.username is not None
            or parts.password is not None or parts.query or parts.fragment or parts.path not in ("", "/")):
        raise ValueError("Use the HTTPS Aleph server address shown in Settings.")
    return urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def save_config(path, config):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
        json.dump(config, file)
    try:
        os.replace(file.name, path)
    finally:
        Path(file.name).unlink(missing_ok=True)


class Revoked(Exception):
    pass


class Runner:
    def __init__(self, session, server, token):
        self.session, self.server = session, server + RUNNER_PATH
        self.headers = {"Authorization": f"Bearer {token}", "X-Aleph-Runner": secrets.token_urlsafe(24)} if token else {}

    async def request(self, path, fields=None):
        async with self.session.post(self.server + path, json=fields or {}, headers=self.headers,
                                     allow_redirects=False) as response:
            if response.status == 401:
                raise Revoked
            if response.status >= 300:
                raise aiohttp.ClientResponseError(response.request_info, response.history, status=response.status)
            return await response.json() if response.status != 204 else None

    async def execute(self, python, job):
        job_id = job.get("id") if isinstance(job, dict) else None
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32}", job_id) or not isinstance(job.get("turn"), dict):
            raise ValueError("Invalid turn ID")
        local_key = secrets.token_urlsafe(32)

        async def proxy(request):
            if not hmac.compare_digest(request.headers.get("Authorization", ""), f"Bearer {local_key}"):
                raise web.HTTPUnauthorized()
            command = "complete" if request.path == "/v1/chat/completions" else "tool"
            async with self.session.post(f"{self.server}/{job_id}/{command}", data=await request.read(),
                                         headers={**self.headers, "Content-Type": "application/json"},
                                         timeout=aiohttp.ClientTimeout(total=108 if command == "tool" else 35),
                                         allow_redirects=False) as response:
                return web.Response(body=await response.read(), status=response.status, content_type="application/json")

        async def keepalive():
            while True:
                await asyncio.sleep(10)
                try:
                    await self.request(f"/{job_id}/alive")
                except aiohttp.ClientResponseError as error:
                    if error.status == 404:
                        return
                except (aiohttp.ClientError, TimeoutError):
                    continue

        app = web.Application(client_max_size=JOB_REQUEST_LIMIT)
        app.router.add_post("/v1/chat/completions", proxy)
        app.router.add_post("/tool", proxy)
        site = web.AppRunner(app, access_log=None, shutdown_timeout=1)
        await site.setup()
        try:
            await web.TCPSite(site, "127.0.0.1", 0).start()
            port = site.addresses[0][1]
            turn = {**job["turn"], "key": local_key, "endpoint": f"http://127.0.0.1:{port}"}
            worker = asyncio.create_task(run_worker(python, turn))
            watcher = asyncio.create_task(keepalive())
            try:
                finished, _ = await asyncio.wait({worker, watcher}, return_when=asyncio.FIRST_COMPLETED)
                if watcher in finished:
                    watcher.result()
                    return
                try:
                    result = worker.result()
                except (OSError, TimeoutError):
                    result = {"error_type": "worker_unavailable"}
            finally:
                for task in (worker, watcher):
                    task.cancel()
                await asyncio.gather(worker, watcher, return_exceptions=True)
            for retry in range(3):
                try:
                    await self.request(f"/{job_id}/result", result)
                    return
                except aiohttp.ClientResponseError as error:
                    if error.status == 404:
                        return
                except (aiohttp.ClientError, TimeoutError):
                    pass
                await asyncio.sleep(2 ** retry)
        finally:
            await site.cleanup()

    async def run(self, python):
        delays = 1
        while True:
            try:
                job = await self.request("/claim")
                if job:
                    await self.execute(python, job)
                delays = 1
            except (aiohttp.ClientError, TimeoutError, ValueError):
                if delays == 1:
                    print("Reconnecting to Aleph…", flush=True)
                await asyncio.sleep(delays)
                delays = min(delays * 2, 15)


async def main(args):
    python = hermes_python()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35), trust_env=True) as session:
        if args.command == "connect":
            server = origin(args.server)
            code = getpass("One-time code from Aleph: ").strip()
            config = await Runner(session, server, None).request("/pair", {"code": code})
            config["server"] = server
            save_config(args.config, config)
        else:
            config = json.loads(args.config.read_text())
            server = origin(args.server or config["server"])
        print(f"{config['name']} · Aleph runner started. Keep this window open. Ctrl+C to stop.", flush=True)
        await Runner(session, server, config["token"]).run(python)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("connect").add_argument("--server", required=True)
    commands.add_parser("run").add_argument("--server")
    args = parser.parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass
    except Revoked:
        raise SystemExit("The pairing code expired or this computer was disconnected. Create a new code in Aleph.") from None
    except (KeyError, FileNotFoundError):
        raise SystemExit("Pair this computer from Settings → Computers first.") from None
    except (aiohttp.ClientError, TimeoutError):
        raise SystemExit("Could not reach Aleph. Check the server address and try again.") from None
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from None
