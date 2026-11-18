"""ChatGPT plan integration for the owner of a self-hosted Aleph instance."""
import asyncio
import json
import os
import time

import aiohttp

from .chatgpt_auth import (AUTH_TIMEOUT, INVALID_REFRESH, PLAN_SCOPE, RESOURCE, REVOCATION, TOKEN, ChatGPTError,
                           apply_tokens, choose_model, clear_tokens, directory, error_code, grant_client,
                           host_id, read_record, request_json, validate_id_token, write_record)
from .chatgpt_pair import PairingStore, owner

SESSION_FILE = "session.json"
NAMESPACE = "aleph"


class ChatGPTPlan:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.pairings = PairingStore()

    def record(self, uid):
        if not owner(uid):
            return None
        try:
            result = read_record(SESSION_FILE)
            return result if result and result.get("telegram_owner") == uid else None
        except (ValueError, OSError):
            return None

    def selected(self, uid):
        record = self.record(uid)
        if record and record.get("state") in {"connected", "reconnect"}:
            return record.get("client_id")
        return None

    def status(self, uid):
        record = self.record(uid) or {}
        status = {"available": owner(uid), "connected": record.get("state") == "connected", "mode": "self_hosted"}
        if status["connected"]:
            if record.get("email"):
                status["email"] = record["email"]
            if record.get("model_name"):
                status["model"] = record["model_name"]
        elif record.get("state") == "reconnect":
            status["needs_reauth"] = True
        return status

    def prepare(self, uid, url):
        if not owner(uid) or not url:
            raise ValueError("ChatGPT connection is unavailable.")
        return self.pairings.create(uid, url, host_id())

    async def install(self, session, packet):
        async with self.lock:
            uid, record = self.pairings.consume(packet)
            client = record.get("client_id")
            if not grant_client(client) or not isinstance(record.get("id_token"), str):
                raise ValueError("Invalid ChatGPT session file.")
            claims = await validate_id_token(session, record["id_token"], client)
            if claims["sub"] != record.get("subject"):
                raise ValueError("Invalid ChatGPT account.")
            scopes = record.get("scopes")
            if not isinstance(scopes, list) or PLAN_SCOPE not in scopes or not isinstance(record.get("refresh_token"), str):
                raise ValueError("The ChatGPT plan grant is missing.")
            if not isinstance(record.get("access_token"), str) or not isinstance(record.get("expires_at"), (int, float)):
                raise ValueError("Invalid ChatGPT session file.")
            if not record["expires_at"] > time.time():
                raise ValueError("The ChatGPT grant expired.")
            current = {name: record[name] for name in ("access_token", "refresh_token", "expires_at", "scopes", "id_token")}
            current.update(client_id=client, subject=claims["sub"], email=claims.get("email"), telegram_owner=uid,
                           state="connected", earliest_refresh_at=record.get("earliest_refresh_at"))
            current = await choose_model(session, current, os.getenv("ALEPH_CHATGPT_MODEL", record.get("model", "")))
            prior = self.record(uid)
            write_record(SESSION_FILE, current)
            if prior and prior.get("client_id") != client:
                await self._revoke(session, prior)
            return self.status(uid)

    async def _revoke(self, session, record):
        if not record.get("refresh_token"):
            return True
        fields = {"token": record["refresh_token"], "token_type_hint": "refresh_token", "client_id": record["client_id"]}
        for attempt in range(2):
            try:
                async with session.post(REVOCATION, data=fields, allow_redirects=False, timeout=AUTH_TIMEOUT) as reply:
                    if reply.status == 200:
                        return True
                    if reply.status < 500:
                        return False
            except (TimeoutError, aiohttp.ClientError):
                pass
            if not attempt:
                await asyncio.sleep(.3)
        return False

    async def disconnect(self, session, uid, *, erase=False):
        async with self.lock:
            self.pairings.cancel(uid)
            current = self.record(uid)
            revoked = await self._revoke(session, current) if current else True
            if current:
                if erase:
                    (directory() / SESSION_FILE).unlink(missing_ok=True)
                else:
                    write_record(SESSION_FILE, {**clear_tokens(current), "telegram_owner": uid})
            return {**self.status(uid), "revoked": revoked}

    async def active(self, session, uid, client):
        async with self.lock:
            record = self.record(uid)
            if not record or record.get("client_id") != client or record.get("state") != "connected":
                raise ChatGPTError(401, "reconnect")
            now, expires = time.time(), record["expires_at"]
            earliest = record.get("earliest_refresh_at")
            if expires > now + 90 or (expires > now and isinstance(earliest, (int, float)) and now < earliest):
                return record
            try:
                fresh = await request_json(session, "POST", TOKEN, data={"grant_type": "refresh_token",
                    "client_id": client, "refresh_token": record["refresh_token"], "resource": RESOURCE})
                record = apply_tokens(record, fresh)
            except ChatGPTError as error:
                if error.code in INVALID_REFRESH | {"plan_not_granted", "invalid_client"}:
                    write_record(SESSION_FILE, {**clear_tokens(record, "reconnect"), "telegram_owner": uid})
                    raise ChatGPTError(401, "reconnect") from None
                raise
            write_record(SESSION_FILE, record)
            return record

    async def complete(self, session, uid, client, messages, tools, previous=None, *, model=None, reasoning=""):
        record = await self.active(session, uid, client)
        payload = responses_payload(messages, model or record["model"], tools, previous, reasoning=reasoning)
        headers = {"Authorization": "Bearer " + record["access_token"], "Accept": "text/event-stream"}
        try:
            async with session.post(RESOURCE + "/responses", headers=headers, json=payload, allow_redirects=False,
                                    read_bufsize=256 * 1024, timeout=aiohttp.ClientTimeout(total=95)) as reply:
                if reply.status != 200:
                    try:
                        problem = await reply.json(content_type=None)
                    except ValueError:
                        problem = None
                    raise ChatGPTError(reply.status, error_code(problem))
                return await completed_reply(reply.content)
        except (TimeoutError, aiohttp.ClientError, ValueError, TypeError, KeyError) as error:
            raise ChatGPTError() from error


def responses_payload(messages, model, tools, previous=None, *, reasoning=""):
    previous = previous or {}
    instructions, items = [], []
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "system":
            if isinstance(content, str):
                instructions.append(content)
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content})
        elif role == "assistant":
            calls = message.get("tool_calls") or []
            if calls and calls[0]["id"] in previous:
                items.extend(previous[calls[0]["id"]])
                continue
            if content:
                items.append({"role": role, "content": content})
            for call in calls:
                items.append({"type": "function_call", "call_id": call["id"], "namespace": NAMESPACE,
                              "name": call["function"]["name"], "arguments": call["function"]["arguments"]})
        elif role in {"user", "developer"}:
            if isinstance(content, list):
                content = [{"type": "input_image", "image_url": part["image_url"]["url"], "detail": "auto"}
                           if part.get("type") == "image_url" else {"type": "input_text", "text": part["text"]}
                           for part in content]
            items.append({"role": role, "content": content})
    payload = {"model": model, "input": items, "store": False, "stream": True}
    if reasoning:
        payload["reasoning"] = {"effort": reasoning}
    if instructions:
        payload["instructions"] = "\n\n".join(instructions)
    if tools:
        payload["include"] = ["reasoning.encrypted_content"]
        payload["tools"] = [{"type": "namespace", "name": NAMESPACE, "description": "Aleph workspace tools.",
                             "tools": [{"type": "function", "strict": False, **tool["function"]} for tool in tools]}]
    return payload


async def completed_reply(lines):
    async for raw in lines:
        if not raw.startswith(b"data:") or raw[5:].strip() == b"[DONE]":
            continue
        event = json.loads(raw[5:])
        kind = event.get("type")
        response = event.get("response") or {}
        if kind == "response.completed":
            reply = chat_message(response["output"])
            reply["_model_id"] = response.get("model")
            return reply
        if kind in {"response.failed", "response.incomplete", "error"}:
            code = error_code(response or event)
            status = {"subscription_sharing_usage_limit_exceeded": 429, "subscription_sharing_usage_unavailable": 503,
                      "subscription_sharing_user_unavailable": 503, "subscription_sharing_user_not_eligible": 403}.get(code, 400)
            raise ChatGPTError(status, code)
    raise ChatGPTError(503, "incomplete_stream")


def chat_message(output):
    text, calls = [], []
    for item in output:
        if item["type"] == "message":
            text.extend(part.get("text") or part.get("refusal") for part in item.get("content", [])
                        if part.get("type") in {"output_text", "refusal"})
        elif item["type"] == "function_call":
            name = item["name"]
            if name.startswith(NAMESPACE + "."):
                name = name.split(".", 1)[1]
            calls.append({"id": item["call_id"], "type": "function",
                          "function": {"name": name, "arguments": item["arguments"]}})
    result = "\n".join(part for part in text if isinstance(part, str))
    if not (result or calls):
        raise ChatGPTError(503, "empty_response")
    reply = {"role": "assistant", "content": result or None}
    if calls:
        reply.update(tool_calls=calls, _chatgpt_output=output)
    return reply


plan = ChatGPTPlan()
