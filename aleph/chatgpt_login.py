"""Connect a local ChatGPT sign-in to your own Aleph deployment."""
import argparse
import asyncio
from getpass import getpass
import secrets
import webbrowser

import aiohttp
from aiohttp import web

from .chatgpt_auth import (ChatGPTError, RESOURCE, TOKEN, apply_tokens, authorize_url, choose_model,
                           clear_tokens, grant_client, read_record, request_json, validate_id_token, write_record)
from .chatgpt_pair import LIFETIME, encrypt_for_vm, pairing_info


async def local_grant(session, registration, agent_host=None):
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    received = asyncio.get_running_loop().create_future()

    async def callback(request):
        query = request.rel_url.query
        supplied = query.getall("state", [])
        if len(supplied) != 1 or not supplied[0].isascii() or not secrets.compare_digest(state, supplied[0]):
            raise web.HTTPBadRequest(text="This is not the current Aleph sign-in attempt.")
        if received.done():
            raise web.HTTPConflict(text="Aleph already received this sign-in attempt.")
        received.set_result(query)
        return web.Response(text="Return to the Aleph terminal to finish connecting.", headers={"Cache-Control": "no-store"})

    app = web.Application()
    app.router.add_get("/auth/callback", callback)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, "127.0.0.1", 0).start()
        redirect = f"http://127.0.0.1:{runner.addresses[0][1]}/auth/callback"
        url = authorize_url(redirect, state, nonce, verifier, registration, agent_host)
        print("Sign in to ChatGPT in your browser and allow use of your plan.", flush=True)
        if not await asyncio.to_thread(webbrowser.open, url, new=2):
            print("Open this sign-in link on this computer:\n" + url, flush=True)
        query = await asyncio.wait_for(received, LIFETIME)
    finally:
        await runner.cleanup()

    if query.get("error"):
        raise ChatGPTError(403, "sign_in_declined")
    codes, clients = query.getall("code", []), query.getall("client_id", [])
    issued = clients[0] if len(clients) == 1 else None
    saved = registration["client_id"] if registration else None
    if len(codes) != 1 or not codes[0] or len(clients) > 1 or (issued is not None and saved and issued != saved):
        raise ChatGPTError(400, "invalid_callback")
    client = saved or issued
    if not grant_client(client):
        raise ChatGPTError(400, "invalid_client")
    data = await request_json(session, "POST", TOKEN, data={"grant_type": "authorization_code", "client_id": client,
                              "code": codes[0], "code_verifier": verifier, "redirect_uri": redirect, "resource": RESOURCE})
    raw_id = data.get("id_token")
    if not isinstance(raw_id, str):
        raise ChatGPTError(401, "missing_identity")
    claims = await validate_id_token(session, raw_id, client, nonce)
    if registration and registration["subject"] != claims["sub"]:
        raise ChatGPTError(401, "different_identity")
    selected = {"client_id": client, "subject": claims["sub"], "email": claims.get("email"), "id_token": raw_id}
    # Retain the verified client mapping even if the plan grant was declined.
    write_record("registration.json", clear_tokens({**selected, "scopes": str(data.get("scope", "")).split()}))
    return await choose_model(session, apply_tokens(selected, data))


async def connect(code, new_account):
    config = pairing_info(code)
    endpoint = config["endpoint"]
    print(f"Connecting to Aleph at {endpoint.split('/api/')[0]}", flush=True)
    registration = None if new_account else read_record("registration.json")
    async with aiohttp.ClientSession(trust_env=True) as session:
        record = await local_grant(session, registration, config["vm_host"] if registration else None)
        endpoint, packet = encrypt_for_vm(code, record)
        await request_json(session, "POST", endpoint, json=packet)
    print("ChatGPT is connected. Reopen Aleph settings to see the active model.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    command = commands.add_parser("connect", help="Use the pairing code from Aleph settings")
    command.add_argument("--new-account", action="store_true", help="Register a different ChatGPT account")
    args = parser.parse_args()
    try:
        code = getpass("Paste the one-time code from Aleph settings: ").strip()
        asyncio.run(connect(code, args.new_account))
    except ChatGPTError as error:
        messages = {"sign_in_declined": "Sign-in was declined.", "plan_not_granted": "Allow use of your ChatGPT plan and try again.",
                    "different_identity": "Use the registered account or add --new-account.",
                    "missing_models": "This account returned no eligible ChatGPT models."}
        hint = messages.get(error.code)
        if not hint:
            hint = f"Sign-in or Aleph connection failed (HTTP {error.status or 'unavailable'}). Use a fresh code and try again."
        parser.exit(1, hint + "\n")
    except (ValueError, OSError, TimeoutError, KeyboardInterrupt):
        hint = "The pairing code or local sign-in could not be completed. Use a fresh code and try again."
        parser.exit(1, hint + "\n")


if __name__ == "__main__":
    main()
