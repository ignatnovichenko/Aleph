import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from aleph import chatgpt, chatgpt_auth
from aleph.api import install_api, signed_user
from aleph.chatgpt_pair import encrypt_for_vm
from aleph.feeds import CATALOG, parse_feed
from aleph.scheduler import deliver, queue_messages, quiet
from aleph.store import UTC, Store, parse_due, timestamp

TEST_TOKEN = "synthetic-bot-secret"


def auth(uid=1, at=None):
    fields = {"user": json.dumps({"id": uid, "first_name": "Аня"}), "auth_date": str(at or int(time.time()))}
    key = hmac.digest(b"WebAppData", TEST_TOKEN.encode(), hashlib.sha256)
    payload = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    return urlencode({**fields, "hash": hmac.digest(key, payload.encode(), hashlib.sha256).hex()})


class BackendAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        self.store = Store(":memory:")
        install_api(app, self.store, TEST_TOKEN)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.addCleanup(self.store.close)

    def headers(self, uid=1):
        return {"Authorization": "tma " + auth(uid)}

    async def test_signed_scope_mutations_export(self):
        self.assertEqual((await self.client.get("/api/state")).status, 401)
        headers = self.headers()
        response = await self.client.get("/api/state", headers=headers)
        self.assertEqual((await response.json())["user"]["first_name"], "Аня")
        await self.client.patch("/api/settings", headers=headers, json={"timezone": "Europe/Moscow"})
        body = {"title": "Встреча, Москва", "due_at": "2027-02-03T17:30"}
        h = {**headers, "Idempotency-Key": "test-task"}
        created = await self.client.post("/api/tasks", headers=h, json=body)
        self.assertEqual(created.status, 201)
        task = await created.json()
        self.assertEqual(task["due_at"], "2027-02-03T14:30:00Z")
        same = await (await self.client.post("/api/tasks", headers=h, json=body)).json()
        self.assertEqual(same["id"], task["id"])
        self.assertEqual((await self.client.patch(f"/api/tasks/{task['id']}", headers=self.headers(2),
                                                 json={"status":"done"})).status, 404)
        ics = await (await self.client.get("/api/calendar.ics", headers=headers)).text()
        self.assertIn(r"Встреча\, Москва", ics)
        self.assertIn("DTSTART:20270203T143000Z", ics)
        self.assertNotIn("Встреча", await (await self.client.get("/api/calendar.ics", headers=self.headers(2))).text())
        self.assertEqual((await self.client.post("/api/feeds", headers=headers, json={"url":"https://localhost/x"})).status, 400)
        added = await (await self.client.post("/api/feeds", headers=headers, json={"url":next(iter(CATALOG))})).json()
        self.assertEqual((await self.client.delete(f"/api/feeds/{added['id']}", headers=self.headers(2))).status, 404)

    async def test_project_routes_and_ownership(self):
        owner, stranger = self.headers(1), self.headers(2)
        created = await self.client.post("/api/projects", headers=owner,
                                         json={"name": "Пилот", "context": "Редактируем отчёт."})
        self.assertEqual(created.status, 201)
        project_id = (await created.json())["id"]
        state = await (await self.client.get("/api/state", headers=owner)).json()
        self.assertEqual(state["active_project_id"], project_id)
        self.assertEqual(state["projects"][0]["context"], "Редактируем отчёт.")
        self.assertEqual((await self.client.patch("/api/projects/active", headers=stranger,
                                                 json={"project_id": project_id})).status, 404)
        self.assertEqual((await self.client.patch(f"/api/projects/{project_id}", headers=stranger,
                                                 json={"context":"Перезапись"})).status, 404)
        self.assertEqual((await self.client.post("/api/tasks", headers=stranger,
                                                json={"title": "Чужая задача", "project_id": project_id})).status, 404)
        self.assertEqual((await self.client.post("/api/tasks", headers=owner,
                                                json={"title": "Сверить отчёт", "project_id": project_id})).status, 201)
        state = await (await self.client.get("/api/state", headers=stranger)).json()
        self.assertEqual(state["projects"], [])
        self.assertEqual(state["tasks"], [])

    async def test_reject_invalid_signed_requests(self):
        for value in [auth(at=int(time.time())-90000), auth(at=int(time.time())+900),
                      auth() + "&user=another", auth().replace("user=", "fake=")]:
            with self.assertRaises(ValueError):
                signed_user(value, TEST_TOKEN)

    async def test_malformed_fields_and_xml(self):
        for body in [{"timezone":"../../else"}, {"proactive":"true"}, {"quiet_start":"80:10"}, {"extra":1}]:
            self.assertEqual((await self.client.patch("/api/settings", headers=self.headers(), json=body)).status, 400)
        with self.assertRaises(ValueError):
            parse_feed(b'<!DOCTYPE a [<!ENTITY x "x">]><a>&x;</a>')
        self.assertEqual(parse_feed(b'<rss><channel><item><title>Hi</title><link>https://example.org/item</link></item></channel></rss>'),
                         [{"title":"Hi", "url":"https://example.org/item"}])

    async def test_chatgpt_pairing_requires_signed_owner_and_is_single_use(self):
        route = "/api/chatgpt"
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
                "ALEPH_CHATGPT_OWNER_ID": "1", "ALEPH_CHATGPT_DIR": str(Path(tmp) / "chatgpt"),
                "MINI_APP_URL": "https://aleph.example.org",
        }), patch("aleph.api.chatgpt", chatgpt.ChatGPTPlan()):
            self.assertEqual((await self.client.get(route)).status, 401)
            stranger = await (await self.client.get(route, headers=self.headers(2))).json()
            self.assertFalse(stranger["available"])
            self.assertEqual((await self.client.post(route + "/pair", headers=self.headers(2))).status, 404)
            response = await self.client.post(route + "/pair", headers=self.headers())
            self.assertEqual(response.status, 200)
            setup = await response.json()
            _, packet = encrypt_for_vm(setup["code"], {
                "client_id": "oaiapp_example", "subject": "example", "id_token": "signed-id-token",
                "access_token": "short-lived-access-token", "refresh_token": "private-refresh-token",
                "scopes": chatgpt_auth.SCOPES.split(), "expires_at": time.time() + 3600,
            })
            models = [{"slug": "gpt-6.2-sol", "display_name": "GPT-6.2 Sol"}]
            with patch.object(chatgpt, "validate_id_token", AsyncMock(return_value={"sub": "example"})), \
                 patch.object(chatgpt_auth, "models_for", AsyncMock(return_value=models)):
                imported = await self.client.post(route + "/import", json=packet)
            self.assertEqual(imported.status, 200)
            self.assertTrue((await imported.json())["connected"])
            self.assertNotIn("token", await (await self.client.get(route, headers=self.headers())).text())
            self.assertEqual((await self.client.post(route + "/import", json=packet)).status, 400)
            self.assertEqual((await self.client.delete(route, headers=self.headers(2))).status, 404)


class OkSession:
    status = 200
    def __init__(self):
        self.messages = []
    def post(self, url, *, json, **kwargs):
        self.messages.append(json)
        return self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def json(self, **kwargs):
        return {"ok":True}


class Persistence(unittest.IsolatedAsyncioTestCase):
    async def test_quiet_hours_restart_and_explicit_reminders(self):
        now = datetime(2027, 10, 10, 23, tzinfo=UTC)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.sqlite"
            s = Store(path)
            s.ensure_user(1, "Ana")
            s.patch_settings(1, {"proactive":False})
            s.create_task(1,"Выпить воды",timestamp(now),request_key="water")
            s.create_task(1,"Выпить воды",timestamp(now),request_key="water")
            queue_messages(s, now)
            session = OkSession()
            await deliver(s,session,TEST_TOKEN,now=now)
            self.assertEqual(len(session.messages),1)
            s.close()
            s = Store(path)
            self.addCleanup(s.close)
            queue_messages(s, now)
            await deliver(s,session,TEST_TOKEN,now=now)
            self.assertEqual(len(session.messages),1)
            s.patch_settings(1, {"proactive":True})
            queue_messages(s,now)
            self.assertEqual(s.db.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0],1)
            morning = now + timedelta(hours=10)
            queue_messages(s,morning)
            await deliver(s,session,TEST_TOKEN,now=morning)
            queue_messages(s,morning)
            await deliver(s,session,TEST_TOKEN,now=morning)
            self.assertEqual(len(session.messages),2)
            self.assertIn("обзор", session.messages[-1]["text"])
            self.assertTrue(quiet("01:00","22:00","08:00"))

    def test_dst_transition_and_cyrillic_ics_folding(self):
        from aleph.api import fold_ical
        with self.assertRaises(ValueError):
            parse_due("2027-03-14T02:30", "America/New_York")
        self.assertEqual(parse_due("2027-03-14T03:30", "America/New_York"), "2027-03-14T07:30:00Z")
        text = "SUMMARY:" + "Мои дела 🧡 " * 30
        folded = fold_ical(text).split("\r\n")
        self.assertTrue(all(len(line.encode()) <= 75 for line in folded))
        self.assertEqual(folded[0] + "".join(line[1:] for line in folded[1:]),text)

    async def test_rate_limit_is_retried_once_eligible(self):
        class Limited(OkSession):
            status = 429
            async def json(self, **kwargs):
                return {"ok":False, "parameters":{"retry_after":45}}
        now = datetime.now(UTC)
        s = Store(":memory:")
        self.addCleanup(s.close)
        s.ensure_user(1)
        s.patch_settings(1, {"proactive":False})
        s.create_task(1,"test",timestamp(now))
        queue_messages(s,now)
        await deliver(s,Limited(),TEST_TOKEN,now=now)
        ok = OkSession()
        await deliver(s,ok,TEST_TOKEN,now=now+timedelta(seconds=44))
        self.assertEqual(ok.messages,[])
        await deliver(s,ok,TEST_TOKEN,now=now+timedelta(seconds=45))
        self.assertEqual(len(ok.messages),1)

    async def test_change_due_or_complete_before_delivery(self):
        now = datetime.now(UTC)
        s = Store(":memory:")
        self.addCleanup(s.close)
        s.ensure_user(1)
        s.patch_settings(1, {"proactive":False})
        task = s.create_task(1,"test",timestamp(now))
        queue_messages(s, now)
        s.update_task(1,task["id"],{"status":"done"})
        session = OkSession()
        await deliver(s,session,TEST_TOKEN,now=now)
        self.assertEqual(session.messages, [])
        s.update_task(1,task["id"],{"status":"open"})
        queue_messages(s, now)
        await deliver(s,session,TEST_TOKEN,now=now)
        self.assertEqual(len(session.messages),1)

class Routines(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "routines.sqlite"
        self.store = Store(self.path)
        self.store.ensure_user(301, "Alina", "ru")
        self.store.ensure_user(302, "Bob", "en")
        self.store.patch_settings(301, {"timezone":"Europe/Moscow"})
        self.addCleanup(lambda: self.store.close())

    async def test_weather_weekday_local_time_fresh_and_restart(self):
        s = self.store
        routine = s.create_routine(301, "Погода", "weather", "08:30", "weekdays", "Москва", "RU", "update-1")
        duplicate = s.create_routine(301, "Погода", "weather", "08:30", "weekdays", "Москва", "RU", "update-1")
        self.assertEqual(routine["id"], duplicate["id"])
        self.assertEqual(s.list_routines(302), [])
        with self.assertRaises(KeyError):
            s.set_routine_active(302,routine["id"],False)
        monday = datetime(2030,1,7,5,30,tzinfo=UTC)
        self.assertEqual(monday.weekday(),0)
        s.db.execute("UPDATE routines SET active_since=?", (timestamp(monday-timedelta(minutes=15)),))
        forecast_calls=[]
        async def weather(city,country):
            forecast_calls.append((city,country))
            return {"forecast":{"current":{"temperature_2m":-3.4},"daily":{"temperature_2m_min":[-6],
                       "temperature_2m_max":[-1],"precipitation_probability_max":[50]}}}
        out = OkSession()
        queue_messages(s,monday-timedelta(minutes=1))
        await deliver(s,out,TEST_TOKEN,now=monday-timedelta(minutes=1),weather=weather)
        self.assertEqual(out.messages, [])
        queue_messages(s,monday)
        await deliver(s,out,TEST_TOKEN,now=monday,weather=weather)
        self.assertEqual(forecast_calls,[("Москва","RU")])
        self.assertIn("-3 °C",out.messages[0]["text"])
        s.close()
        s=self.store=Store(self.path)
        queue_messages(s,monday+timedelta(minutes=1))
        await deliver(s,out,TEST_TOKEN,now=monday+timedelta(minutes=1),weather=weather)
        self.assertEqual(len(out.messages),1)
        saturday=monday+timedelta(days=5,minutes=-10)
        queue_messages(s,saturday)
        await deliver(s,out,TEST_TOKEN,now=saturday,weather=weather)
        self.assertEqual(len(out.messages),1)
        sunday=monday+timedelta(days=6)
        queue_messages(s,sunday)
        await deliver(s,out,TEST_TOKEN,now=sunday,weather=weather)
        self.assertEqual(len(out.messages),1)
        queue_messages(s,monday+timedelta(days=7))
        await deliver(s,out,TEST_TOKEN,now=monday+timedelta(days=7),weather=weather)
        self.assertEqual(len(forecast_calls),2)
        self.assertEqual(len(out.messages),2)

    async def test_chat_weather_geocodes_local_script(self):
        from unittest.mock import AsyncMock
        from aleph.llm import Aleph
        agent=Aleph(None,"")
        agent._get_public_json=AsyncMock(side_effect=[
            {"results":[{"latitude":55,"longitude":37,"name":"Москва","country":"Россия"}]}, {}])
        result=await agent.weather("Москва","RU")
        self.assertEqual(result["place"],"Москва")
        self.assertEqual(agent._get_public_json.await_args_list[0].args[1]["language"],"ru")

    async def test_weekly_quiet_overnight_pause_and_remove(self):
        s=self.store
        r=s.create_routine(301,"Пришло время растяжки","reminder","23:45","mon,thu")
        monday=datetime(2030,1,7,20,45,tzinfo=UTC)
        queue_messages(s,monday)
        out=OkSession()
        await deliver(s,out,TEST_TOKEN,now=monday)
        self.assertEqual(out.messages, [])
        morning=monday+timedelta(hours=8,minutes=15)
        queue_messages(s,morning)
        await deliver(s,out,TEST_TOKEN,now=morning)
        self.assertEqual(len(out.messages),1)
        self.assertIn("растяжки",out.messages[0]["text"])
        thursday=monday+timedelta(days=3)
        queue_messages(s,thursday)
        s.set_routine_active(301,r["id"],False)
        await deliver(s,out,TEST_TOKEN,now=thursday+timedelta(hours=10))
        self.assertEqual(len(out.messages),1)
        s.set_routine_active(301,r["id"],True)
        with self.assertRaises(KeyError):
            s.delete_routine(302,r["id"])
        s.delete_routine(301,r["id"])
        self.assertEqual(s.list_routines(301),[])

    async def test_scoped_agenda_and_news(self):
        s=self.store
        s.patch_settings(301, {"digest_time":"20:00"})
        s.create_task(301,"Подготовить слайды")
        s.create_task(302,"Private other user task")
        s.create_routine(301,"Мой план","agenda","09:00")
        s.create_routine(301,"Мои новости","news","09:01")
        source=next(iter(CATALOG))
        s.add_feed(301,source)
        now=datetime(2030,1,7,6,0,tzinfo=UTC)
        s.db.execute("UPDATE routines SET active_since=?", (timestamp(now-timedelta(minutes=1)),))
        s.db.execute("INSERT INTO feed_cache(url,items,refreshed_at) VALUES(?,?,?)",
                     (source,json.dumps([{"title":"Launch","url":"https://example.org/article"}]),timestamp(now)))
        out=OkSession()
        for minute in range(2):
            instant=now+timedelta(minutes=minute)
            queue_messages(s,instant)
            await deliver(s,out,TEST_TOKEN,now=instant)
        texts=[m["text"] for m in out.messages if m["chat_id"]==301]
        self.assertEqual(len(texts),2)
        self.assertIn("Подготовить слайды",texts[0])
        self.assertNotIn("Private other user",texts[0])
        self.assertIn("https://example.org/article",texts[1])
