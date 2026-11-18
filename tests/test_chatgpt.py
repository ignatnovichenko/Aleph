import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from aleph import chatgpt, chatgpt_auth as auth
from aleph.bot import Inbox
from aleph.chatgpt_pair import encrypt_for_vm
from aleph.store import Store


OWNER = 51
MODEL = {"slug": "gpt-6.2-sol", "display_name": "GPT-6.2 Sol", "visibility": "list"}


def record():
    return {"client_id": "oaiapp_test", "subject": "test-sub", "id_token": "id-token", "access_token": "access-token",
            "refresh_token": "refresh-token", "scopes": auth.SCOPES.split(), "expires_at": time.time() + 300,
            "state": "connected", "telegram_owner": OWNER, "model": MODEL["slug"], "model_name": MODEL["display_name"]}


async def events(*packets):
    for packet in packets:
        yield b"data: " + json.dumps(packet).encode() + b"\n"


class ChatGPTPlanTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        location = TemporaryDirectory()
        self.addCleanup(location.cleanup)
        self.directory = Path(location.name) / "chatgpt"
        env = patch.dict(os.environ, {"ALEPH_CHATGPT_OWNER_ID": str(OWNER), "ALEPH_CHATGPT_DIR": str(self.directory)})
        env.start()
        self.addCleanup(env.stop)
        self.plan = chatgpt.ChatGPTPlan()

    async def test_one_use_encrypted_pair_and_owner_status(self):
        with self.assertRaises(ValueError):
            self.plan.prepare(12, "https://aleph.example.org")
        pair = self.plan.prepare(OWNER, "https://aleph.example.org")
        endpoint, packet = encrypt_for_vm(pair["code"], record())
        self.assertEqual(endpoint, "https://aleph.example.org/api/chatgpt/import")
        self.assertNotIn("refresh-token", json.dumps(packet))
        with self.assertRaises(ValueError):
            await self.plan.install(None, {**packet, "ciphertext": "AA"})
        with patch.object(chatgpt, "validate_id_token", AsyncMock(return_value={"sub": "test-sub", "email": "user@example.org"})), \
             patch.object(auth, "models_for", AsyncMock(return_value=[MODEL])):
            status = await self.plan.install(None, packet)
        self.assertTrue(status["connected"])
        self.assertEqual(status["model"], MODEL["display_name"])
        self.assertFalse(self.plan.status(12)["available"])
        self.assertIsNone(self.plan.selected(12))
        self.assertEqual(self.plan.selected(OWNER), "oaiapp_test")
        self.assertEqual((self.directory / chatgpt.SESSION_FILE).stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            await self.plan.install(None, packet)
        with patch.object(self.plan, "_revoke", AsyncMock(return_value=False)):
            disconnected = await self.plan.disconnect(None, OWNER)
        self.assertFalse(disconnected["connected"])
        self.assertFalse(disconnected["revoked"])
        self.assertNotIn("access_token", auth.read_record(chatgpt.SESSION_FILE))
        self.assertIsNone(self.plan.selected(OWNER))

    async def test_refresh_rotates_and_reconnect_does_not_switch_billing(self):
        original = {**record(), "expires_at": 0}
        auth.write_record(chatgpt.SESSION_FILE, original)
        token_reply = {"token_type": "Bearer", "access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}
        with patch.object(chatgpt, "request_json", AsyncMock(return_value=token_reply)) as request:
            result = await self.plan.active(None, OWNER, original["client_id"])
        self.assertEqual(request.call_args.kwargs["data"]["refresh_token"], "refresh-token")
        self.assertEqual(result["access_token"], "new-access")
        auth.write_record(chatgpt.SESSION_FILE, {**result, "expires_at": 0})
        with patch.object(chatgpt, "request_json", AsyncMock(side_effect=auth.ChatGPTError(400, "invalid_grant"))):
            with self.assertRaises(auth.ChatGPTError):
                await self.plan.active(None, OWNER, original["client_id"])
        self.assertTrue(self.plan.status(OWNER)["needs_reauth"])
        self.assertEqual(self.plan.selected(OWNER), original["client_id"])

    async def test_signed_identity_checks_nonce_issuer_and_audience(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
        public.update(kid="test", alg="RS256")
        token = jwt.encode({"sub": "abc", "aud": "oaiapp_test", "iss": auth.ISSUER, "nonce": "mine", "exp": int(time.time()) + 90},
                           key, algorithm="RS256", headers={"kid": "test"})
        with patch.object(auth, "request_json", AsyncMock(return_value={"keys": [public]})):
            self.assertEqual((await auth.validate_id_token(None, token, "oaiapp_test", "mine"))["sub"], "abc")
            with self.assertRaises(auth.ChatGPTError):
                await auth.validate_id_token(None, token, "oaiapp_test", "different")
            with self.assertRaises(auth.ChatGPTError):
                await auth.validate_id_token(None, token, "oaiapp_other", "mine")
        redirect = "http://127.0.0.1:1455/auth/callback"
        auth_url = parse_qs(urlsplit(auth.authorize_url(redirect, "state", "mine", "verifier")).query)
        self.assertEqual(auth_url["redirect_uri"], [redirect])
        self.assertEqual(auth_url["code_challenge_method"], ["S256"])
        self.assertEqual(auth_url["client_id"], ["dynamic_agent_client"])

    async def test_responses_format_replays_encrypted_reasoning_and_tool_calls(self):
        output = [{"id": "rs_short", "type": "reasoning", "encrypted_content": "encoded", "summary": []},
                  {"type": "function_call", "id": "fc_short", "call_id": "call_one", "name": "workspace",
                   "arguments": "{}", "namespace": "aleph"}]
        reply = await chatgpt.completed_reply(events({"type": "response.completed", "response": {"output": output}}))
        self.assertEqual(reply["tool_calls"][0]["function"]["name"], "workspace")
        messages = [{"role": "system", "content": "Instructions"}, {"role": "user", "content": "Show my plan"}, reply,
                    {"role": "tool", "tool_call_id": "call_one", "content": "No tasks"}]
        tools = [{"type": "function", "function": {"name": "workspace", "description": "List tasks", "parameters": {}}}]
        payload = chatgpt.responses_payload(messages, MODEL["slug"], tools, {"call_one": reply["_chatgpt_output"]})
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertEqual(payload["instructions"], "Instructions")
        self.assertEqual(payload["tools"][0]["type"], "namespace")
        self.assertEqual(payload["input"][1:3], output)
        self.assertEqual(payload["input"][3]["call_id"], "call_one")
        with self.assertRaises(auth.ChatGPTError) as failed:
            await chatgpt.completed_reply(events({"type": "response.failed", "response": {
                "error": {"code": "subscription_sharing_usage_limit_exceeded"}}}))
        self.assertEqual(failed.exception.status, 429)

    async def test_pending_turn_keeps_its_original_billing_source(self):
        store = Store(":memory:")
        self.addCleanup(store.close)
        inbox = Inbox(store)
        inbox.ingest([{"update_id": 1, "message": {"from": {"id": OWNER}, "chat": {"id": OWNER, "type": "private"}}}])
        self.assertEqual(inbox.bind_chatgpt(1, OWNER, None), "")
        self.assertEqual(inbox.bind_chatgpt(1, OWNER, "oaiapp_new"), "")
