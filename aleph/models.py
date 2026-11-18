"""Provider catalogs and the owner's model preferences."""
import asyncio
import json
import os
import time
from urllib.parse import urlsplit

from .chatgpt import plan
from .chatgpt_auth import ChatGPTError, models_for, request_json

DEFAULT_OPENCODE = "claude-haiku-5-5"
PROVIDERS = ("chatgpt", "opencode")
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
CATALOG_TTL = 300


def model_option(provider, model, name, reasoning="", images=None):
    return {"provider": provider, "model": model, "name": name, "reasoning": reasoning, "images": images}


def legacy_option(uid, provider, opencode_model):
    if provider == "chatgpt":
        record = plan.record(uid) or {}
        return model_option(provider, record.get("model", ""), record.get("model_name", "ChatGPT"))
    name = "Claude Haiku 5.5" if opencode_model == DEFAULT_OPENCODE else opencode_model
    return model_option(provider, opencode_model, name, "high")


def saved(store, uid):
    row = store.db.execute("SELECT provider,selections,last_reply FROM model_settings WHERE user_id=?", (uid,)).fetchone()
    return (row[0], json.loads(row[1]), json.loads(row[2]) if row[2] else None) if row else ("", {}, None)


def current(uid, provider, selections, opencode_model):
    if not provider:
        provider = "chatgpt" if plan.selected(uid) else "opencode"
    return selections.get(provider) or legacy_option(uid, provider, opencode_model)


def describe(store, uid, opencode_model):
    provider, selections, last = saved(store, uid)
    return {"active": current(uid, provider, selections, opencode_model), "last": last}


def remember_reply(store, uid, model):
    store.db.execute("""INSERT INTO model_settings(user_id,last_reply) VALUES(?,?)
        ON CONFLICT(user_id) DO UPDATE SET last_reply=excluded.last_reply""", (uid, json.dumps(model)))


def disconnect_chatgpt(store, uid):
    store.db.execute("UPDATE model_settings SET provider='opencode' WHERE user_id=? AND provider='chatgpt'", (uid,))


def opencode_models_url():
    base = os.getenv("OPENCODE_BASE_URL", "").rstrip("/").removesuffix("/chat/completions")
    url = urlsplit(base)
    if not base or (url.hostname == "opencode.ai" and (url.path.startswith("/zen/go/") or url.path.startswith("/inference/go/"))):
        return "https://opencode.ai/inference/go/v1/models"
    return base + "/models"


def levels(raw):
    return list(dict.fromkeys(value for value in raw if value in EFFORTS)) if isinstance(raw, list) else []


def opencode_models(items):
    result = []
    for item in items:
        capabilities = item.get("capabilities") or {}
        inputs = (item.get("modalities") or {}).get("input") or []
        if item.get("type") != "chat" or not (capabilities.get("tools") or {}).get("supported") or "text" not in inputs:
            continue
        reasoning = (capabilities.get("reasoning") or {}).get("effort")
        result.append({"id": item["id"], "name": item.get("name") or item["id"], "levels": levels(reasoning),
                       "images": any(kind == "image" or kind.startswith("image/") for kind in inputs),
                       "training": (item.get("go") or {}).get("training") is True})
    return result


def chatgpt_models(items):
    result = []
    for item in items:
        inputs = item.get("input_modalities") or []
        if item.get("supported_in_api") is False or item.get("allow_only_codex_cloud_agent_orchestrator") or item.get("allow_only_chatgpt_workspace_executor"):
            continue
        if inputs and "text" not in inputs:
            continue
        reasoning = item.get("supported_reasoning_levels") or []
        result.append({"id": item["slug"], "name": item["display_name"],
                       "levels": levels([entry.get("effort") for entry in reasoning]),
                       "default": item.get("default_reasoning_level") or "", "images": "image" in inputs or not inputs})
    return result


class ModelCatalogs:
    def __init__(self, model_key):
        self.key = model_key
        self.opencode_model = os.getenv("OPENCODE_MODEL") or DEFAULT_OPENCODE
        self.opencode_url = opencode_models_url()
        self._cache = {}
        self._locks = {provider: asyncio.Lock() for provider in PROVIDERS}

    async def get(self, session, uid, provider):
        client = plan.selected(uid) if provider == "chatgpt" else None
        if ((provider == "chatgpt" and not plan.status(uid)["connected"])
                or (provider == "opencode" and not self.key)):
            return {"status": "disconnected", "models": []}
        key = (provider, client)
        async with self._locks[provider]:
            cached = self._cache.get(key)
            if cached and cached[0] > time.monotonic():
                return {"status": "ready", "models": cached[1]}
            try:
                if provider == "chatgpt":
                    record = await plan.active(session, uid, client)
                    entries = chatgpt_models(await models_for(session, record["access_token"]))
                else:
                    data = await request_json(session, "GET", self.opencode_url,
                                              headers={"Authorization": "Bearer " + self.key})
                    entries = opencode_models(data["data"])
            except (ChatGPTError, KeyError, TypeError, AttributeError):
                return {"status": "unavailable", "models": []}
            self._cache = {key: value for key, value in self._cache.items() if value[0] > time.monotonic()}
            self._cache[key] = (time.monotonic() + CATALOG_TTL, entries)
            return {"status": "ready", "models": entries}

    async def all(self, session, uid):
        catalogs = await asyncio.gather(*(self.get(session, uid, provider) for provider in PROVIDERS))
        return dict(zip(PROVIDERS, catalogs))

    async def choose(self, session, store, uid, fields):
        if set(fields) != {"provider", "model", "reasoning"} or not all(isinstance(value, str) for value in fields.values()):
            raise ValueError("Проверь настройки модели.")
        provider, model, reasoning = fields["provider"], fields["model"], fields["reasoning"]
        if provider not in PROVIDERS:
            raise ValueError("Выбери провайдера модели.")
        catalog = await self.get(session, uid, provider)
        entry = next((item for item in catalog["models"] if item["id"] == model), None)
        if entry is None or reasoning and reasoning not in entry["levels"]:
            raise ValueError("Выбранная модель или уровень рассуждений сейчас недоступны.")
        _, selections, _ = saved(store, uid)
        selections[provider] = model_option(provider, model, entry["name"], reasoning, entry["images"])
        store.db.execute("""INSERT INTO model_settings(user_id,provider,selections) VALUES(?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET provider=excluded.provider,selections=excluded.selections""",
                         (uid, provider, json.dumps(selections)))
        return describe(store, uid, self.opencode_model)
