"""Visible, one-step-at-a-time Chrome automation on a paired desktop."""

import asyncio
import hashlib
import json
import re
import secrets
import time
from contextlib import suppress
from pathlib import Path
from urllib.parse import unquote, urlsplit

import aiohttp

from . import chrome_local
from .public_fetch import public_url

SENSITIVE = re.compile(
    r"pay|purchas|buy|checkout|order|transfer|bill|donat|delet|eras|trash|remove|send|share|publish|post\b|submit|confirm|book|invite|sign.?out|logout|оплат|плат[её]ж|купить|заказ|перев[ео]|удал|стереть|отправ|опублик|поделиться|подтверд|приглас",
    re.IGNORECASE,
)
ROUTINE = re.compile(
    r"^(next|back|previous|continue|more|search|filter|show|expand|open|view|download|далее|назад|продолжить|найти|поиск|открыть|показать|скачать)(\b|$)",
    re.IGNORECASE,
)
SECRET = re.compile(
    r"password|passwd|passcode|token|cookie|secret|otp|one.time|two.factor|credit.card|card.number|cvc|cvv|парол|код подтверждения|номер карты",
    re.IGNORECASE,
)
SELECTOR = "a[href],button,input,textarea,select,[role=button],[contenteditable=true]"
# This is a fixed observer, never JavaScript supplied by the model or the site.
META = """e => {
 const txt=s=>(s||'').replace(/\\s+/g,' ').trim().slice(0,140);
 const label=n=>txt(n.getAttribute('aria-label')||n.labels?.[0]?.innerText||(['BUTTON','A'].includes(n.tagName)?n.innerText:'')||n.placeholder||n.name||n.title||(n.type==='submit'?n.value:''));
 const privateField=n=>/password|hidden|file|cookie|token|secret|otp|one-time|cc-|passcode|cvv|cvc|card.?number|парол/i.test([n.type,n.name,n.autocomplete,n.placeholder,n.id].join(' '));
 const form=e.form||e.closest('form');
 let submit=null;
 if(form && e.type==='submit' && !Array.from(form.elements).some(n=>privateField(n) && n.name)
     && (e.getAttribute('formmethod')||form.method).toLowerCase()==='post'
     && form.enctype==='application/x-www-form-urlencoded'){
   const dest=e.hasAttribute('formaction')?e.formAction:form.action;
   const params=new URLSearchParams();
   for(const n of form.elements){
     if(!n.name || n.matches(':disabled') || (['radio','checkbox'].includes(n.type)&&!n.checked)
        || (['submit','button','reset','image'].includes(n.type)&&n!==e))continue;
     if(n.tagName==='SELECT')for(const o of n.selectedOptions)params.append(n.name,o.value);
     else params.append(n.name,n.value);
   }
   const fields=params.toString();
   if(fields.length<9000)submit={url:dest, body:fields};
 }
 const scope=e.closest('dialog,[role=dialog],tr,li,form,article');
 const context=scope?txt(Array.from(scope.querySelectorAll('legend,h1,h2,h3,th,td,output,[role=heading]'))
   .map(n=>n.innerText).join(' ')):'';
 const privateForm=form && Array.from(form.elements).some(n=>n.type!=='hidden' && n.type!=='file' && privateField(n));
 return {context,privateForm:!!privateForm,filled:(e.tagName==='INPUT'||e.tagName==='TEXTAREA') && !!e.value,tag:e.tagName.toLowerCase(),type:e.type||'',label:label(e), private:privateField(e),
 href:e.tagName==='A'?e.href:'', submit, target:form?.getAttribute('action')||'',
 heading:txt(form?.querySelector('legend,h1,h2,h3')?.innerText),
 values:form?Array.from(form.elements).filter(n=>!privateField(n)&&['INPUT','SELECT','TEXTAREA'].includes(n.tagName)&&n.value)
 .slice(0,12).map(n=>[label(n),txt(n.value)]):[],
 options:e.tagName==='SELECT'?Array.from(e.options).slice(0,32).map(o=>txt(o.label)):[]}
}"""


class Halt(Exception):
    pass


def display_url(url):
    parts = urlsplit(url)
    return (parts.scheme + "://" + parts.netloc + parts.path)[:280] if parts.hostname else "Новая вкладка"


def sensitive_url(url):
    parts = urlsplit(url)
    return bool(SENSITIVE.search(unquote(parts.path + "?" + parts.query)))


class Browser:
    def __init__(self, context, root, check, gate, authorize, test_origin=None):
        self.ctx, self.root, self.check, self.gate, self.authorize = context, root, check, gate, authorize
        self.test_origin = test_origin
        self.tabs, self.elements = [], {}
        self.page = None
        self.network_lock = asyncio.Lock()
        self.submission = None
        self.last_action = ""
        self.downloads = []
        self.allowed_urls = set()

    def url_ok(self, url):
        parts = urlsplit(url)
        if self.test_origin and (parts.scheme, parts.netloc) == self.test_origin:
            return True
        try:
            return public_url(url).scheme == "https"
        except ValueError:
            return False

    async def route(self, route):
        try:
            self.check()
            req = route.request
            if not self.url_ok(req.url):
                return await route.abort()
            # Scripts cannot silently submit a form, including background requests.
            writing = req.method.upper() not in {"GET", "HEAD", "OPTIONS"}
            suspicious_get = (
                req.method == "GET"
                and req.resource_type in {"document", "fetch", "xhr"}
                and sensitive_url(req.url)
            )
            if writing or suspicious_get:
                async with self.network_lock:
                    self.check()
                    host = req.frame.page.url
                    payload = req.post_data_buffer or b""
                    if len(payload) > 2_000_000:
                        return await route.abort()
                    source = req.frame.page
                    exact = (source, req.method, req.url, payload)
                    if (
                        self.submission
                        and self.submission[0] == exact
                        and time.monotonic() < self.submission[1]
                        and req.is_navigation_request()
                        and req.frame == source.main_frame
                    ):
                        self.submission = None
                        await self.gate()
                        return await route.continue_()
                    if (
                        writing
                        and not req.is_navigation_request()
                        and urlsplit(host).netloc != urlsplit(req.url).netloc
                    ):
                        return await route.abort()
                    fingerprint = hashlib.sha256(req.url.encode() + b"\0" + payload).hexdigest()[:16]
                    detail = (
                        f"{req.method} {display_url(req.url)}\nСо страницы {display_url(host)}"
                        f"\n{self.last_action}\nОтпечаток запроса {fingerprint}"
                    )
                    await self.authorize(detail)
                    self.check()
            await route.continue_()
        except (Exception, asyncio.CancelledError):  # noqa: BLE001 - a failed interceptor must abort the request.
            with suppress(Exception):
                await route.abort()

    async def start(self):
        await self.ctx.route("**/*", self.route)
        await self.ctx.route_web_socket("**/*", lambda ws: ws.close())
        self.check()
        self.page = self.ctx.pages[0] if self.ctx.pages else await self.ctx.new_page()

    async def snapshot(self):
        self.check()
        self.tabs.extend(p for p in self.ctx.pages if p not in self.tabs)
        alive = [p for p in self.tabs if not p.is_closed()]
        if self.page not in alive:
            if not alive:
                raise Halt("Все вкладки закрыты.")
            self.page = alive[-1]
        page = self.page
        self.elements = {}
        controls = []
        for handle in (await page.query_selector_all(SELECTOR))[:160]:
            if not await handle.is_visible():
                await handle.dispose()
                continue
            meta = await handle.evaluate(META)
            if meta["private"] or SECRET.search(meta["label"]):
                await handle.dispose()
                continue
            idx = len(controls)
            self.elements[idx] = (handle, meta)
            item = {"id": idx, "kind": meta["tag"], "label": meta["label"]}
            if meta["tag"] in ("input", "textarea"):
                item["filled"] = meta["filled"]
            if meta["href"] and self.url_ok(meta["href"]):
                item["link"] = display_url(meta["href"])
                self.allowed_urls.add(meta["href"])
            if meta["options"]:
                item["options"] = meta["options"]
            controls.append(item)
        # Omit form values and editable areas even when the site mirrors them as DOM text.
        words = await page.evaluate("""() => {
          const walk=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT);
          const out=[];let n,size=0;while((n=walk.nextNode())&&size<4500){
            const e=n.parentElement;if(!e||e.closest('script,style,form,input,textarea,[contenteditable],noscript,[hidden]')||!e.checkVisibility())continue;
            const t=n.textContent.trim();if(t&&!/password|passwd|cookie|passcode|bearer |secret|token[=:]|пароль|код подтверждения/i.test(t)){out.push(t);size+=t.length}}
          return out.join(' ').slice(0,4500)}""")
        return {
            "tab": self.tabs.index(page),
            "tabs": [{"id": self.tabs.index(p), "page": display_url(p.url)} for p in alive],
            "text": words,
            "controls": controls,
            "downloads": self.downloads[-6:],
        }

    async def step(self, a):
        self.check()
        self.submission = None
        op = a.get("op")
        if op == "observe":
            await asyncio.sleep(0.6)
            return "Страница обновлена."
        if op in ("open", "go"):
            url = a.get("url", "")
            if not self.url_ok(url) or (url not in self.allowed_urls and url + "/" not in self.allowed_urls):
                raise Halt("Пришли точную ссылку на нужный сайт в поручении.")
            if sensitive_url(url):
                exact = (" · " + hashlib.sha256(url.encode()).hexdigest()[:16]) if urlsplit(url).query else ""
                await self.authorize("Открыть " + display_url(url) + exact)
            await self.gate()
            if op == "open":
                self.page = await self.ctx.new_page()
                await self.gate()
            self.check()
            if sensitive_url(url):
                self.submission = ((self.page, "GET", url, b""), time.monotonic() + 3)
            await self.page.goto(url, wait_until="domcontentloaded", timeout=18000)
            return "Открыто " + display_url(self.page.url)
        if op == "switch":
            idx = a.get("tab")
            if type(idx) != int or not 0 <= idx < len(self.tabs) or self.tabs[idx].is_closed():
                raise Halt("Вкладка уже закрыта.")
            await self.gate()
            self.page = self.tabs[idx]
            await self.page.bring_to_front()
            return "Выбрана вкладка " + str(idx)
        if (
            op not in {"click", "fill", "select", "download"}
            or type(a.get("id")) != int
            or a["id"] not in self.elements
        ):
            raise Halt("Элемент не найден. Страница могла измениться.")
        h, original = self.elements[a["id"]]
        url = self.page.url
        value = a.get("value")
        if op in ("fill", "select") and (not isinstance(value, str) or len(value) > 4000):
            raise Halt("Недопустимое значение поля.")
        if (
            op == "fill"
            and original["tag"] not in ("input", "textarea")
            and not await h.evaluate("e=>e.isContentEditable")
        ):
            raise Halt("Элемент не является текстовым полем.")
        if op == "select" and value not in original["options"]:
            raise Halt("Пункт списка изменился.")
        if original["href"] and not self.url_ok(original["href"]):
            raise Halt("Ссылка не ведёт на веб-страницу.")
        if original["privateForm"] and op in ("click", "download"):
            raise Halt("Эта форма содержит данные для входа или оплаты. Заверши её вручную в профиле Chrome.")
        label = original["label"]
        action_context = " ".join(
            [
                label,
                original["href"],
                original["heading"],
                original["context"],
                original["target"],
                urlsplit(url).path,
            ]
        )
        committed = op in ("click", "download") and (
            SENSITIVE.search(action_context) or (original["tag"] != "a" and not ROUTINE.search(label))
        )
        if committed:
            fields = ", ".join(f"{k} = {v}" for k, v in original["values"] if not SECRET.search(k))
            detail = f"Нажать «{label}» на {display_url(url)}"
            if original["href"]:
                detail += " → " + display_url(original["href"])
            if original["submit"] and original["submit"]["url"] != url:
                detail += " → " + display_url(original["submit"]["url"])
            if original["context"] and not SECRET.search(original["context"]):
                detail += " · " + original["context"]
            if fields:
                detail += " · " + fields[:900]
            # A stripped query, hidden form state or truncated preview must not turn an old
            # authorization into permission for a different recipient, item or amount.
            if (
                urlsplit(url).query
                or not original["submit"]
                or len(fields) > 900
                or (
                    original["submit"]
                    and (urlsplit(original["submit"]["url"]).query or len(original["submit"]["body"]) > 100)
                )
            ):
                signature = json.dumps([url, original], ensure_ascii=False).encode()
                detail += " · " + hashlib.sha256(signature).hexdigest()[:16]
            await self.authorize(detail)
        if op in ("click", "download"):
            await h.click(trial=True, timeout=3500)
        if url != self.page.url or original != await h.evaluate(META) or not await h.is_visible():
            raise Halt("Страница изменилась до действия. Действие отменено.")
        await self.gate()
        self.check()
        self.last_action = detail if committed else f"Действие на странице: {label}"
        if committed and original["submit"]:
            dest = original["submit"]
            self.submission = ((self.page, "POST", dest["url"], dest["body"].encode()), time.monotonic() + 3)
        elif committed and original["href"]:
            self.submission = ((self.page, "GET", original["href"], b""), time.monotonic() + 3)
        if op == "fill":
            await h.fill(value, timeout=2500)
        elif op == "select":
            await h.select_option(label=value, timeout=2500)
        elif op == "click":
            before = set(self.ctx.pages)
            await h.click(timeout=3500, no_wait_after=True)
            # A site may hold navigation while the user approves an exact outbound request.
            await asyncio.sleep(0.12)
            async with self.network_lock:
                self.check()
            if not self.page.is_closed():
                await self.page.wait_for_load_state("domcontentloaded", timeout=18000)
            for opened in self.ctx.pages:
                if opened not in before:
                    if opened not in self.tabs:
                        self.tabs.append(opened)
                    return f"Открыта вкладка {self.tabs.index(opened)}. Переключись на неё (switch)."

        else:
            async with self.page.expect_download(timeout=15000) as pending:
                self.check()
                await h.click(timeout=3500)
            dl = await pending.value
            self.check()
            name = (
                re.sub(r"[^\w. ()-]", "_", Path(dl.suggested_filename).name).strip(" .")[:120] or "download"
            )
            folder = self.root / "downloads"
            folder.mkdir(mode=0o700, exist_ok=True)
            dest = folder / (secrets.token_hex(8) + "-" + name)
            try:
                await dl.save_as(dest)
                self.check()
            except BaseException:
                dest.unlink(missing_ok=True)
                raise
            self.downloads.append(dest.name)
            return "Скачан файл " + dest.name
        return "Выполнено: " + op + " · " + label

    async def dispose(self):
        for h, _ in self.elements.values():
            with suppress(Exception):
                await h.dispose()
        self.elements.clear()


async def work(runner, root, epoch, job, *, pairing, headless=False, test_origin=None):
    from playwright.async_api import async_playwright

    turn = job["turn"]
    active = True

    def check():
        if not active or chrome_local.state(root) != (epoch, 0):
            raise Halt("Chrome остановлен на компьютере.")

    async def request(op, **fields):
        return await runner.request("/browser/" + op, {"turn": turn, **fields})

    async def gate():
        check()
        await asyncio.wait_for(request("gate"), 3)
        check()

    approval_lock = asyncio.Lock()

    async def authorize(detail):
        async with approval_lock:
            await wait_approval(detail)

    async def wait_approval(detail):
        check()
        granted = await request("authorize", detail=detail)
        ticket = granted.get("ticket")
        deadline = time.monotonic() + 180
        while not granted.get("approved"):
            if time.monotonic() > deadline:
                raise Halt("Время подтверждения истекло.")
            await asyncio.sleep(0.35)
            check()
            granted = await request("approval", ticket=ticket)
        await gate()

    async def local_watch():
        while True:
            check()
            await asyncio.sleep(0.04)

    async def remote_watch():
        while True:
            if (await asyncio.wait_for(request("watch"), 3))["stopped"]:
                raise Halt("Chrome остановлен в Telegram.")

    async def drive():
        await gate()
        pw = await async_playwright().start()
        ctx = None
        try:
            await gate()
            ctx = await pw.chromium.launch_persistent_context(
                str(chrome_local.profile(root, pairing)),
                executable_path=chrome_local.chrome_binary(),
                headless=headless,
                accept_downloads=True,
                service_workers="block",
                chromium_sandbox=not headless,
                args=[
                    "--disable-background-networking",
                    "--disable-sync",
                    "--disable-extensions",
                    "--disable-features=PasswordManagerOnboarding,AutofillServerCommunication",
                    "--disable-session-crashed-bubble",
                    "--no-first-run",
                ],
            )
            browser = Browser(ctx, root, check, gate, authorize, test_origin)
            for url in job.get("urls", []):
                browser.allowed_urls.add(url)
                if not urlsplit(url).path:
                    browser.allowed_urls.add(url + "/")
            await browser.start()
            while True:
                view = await browser.snapshot()
                a = (await request("plan", view=view))["action"]
                if a.get("op") in ("done", "fail"):
                    result = str(a.get("text") or "Chrome завершил работу.")[:2000]
                    if browser.downloads:
                        result += "\nСкачано на компьютер (~/.config/aleph/browser/downloads): " + ", ".join(
                            browser.downloads
                        )
                    return a["op"] == "done", result
                result = await browser.step(a)
                await browser.dispose()
                await request("record", result=result)
        finally:
            try:
                if ctx:
                    await ctx.close()
            finally:
                await pw.stop()

    chrome_local.state(root)
    lock = chrome_local.activity_lock(root)
    if lock is None:
        await request("finish", success=False, result="Профиль Chrome сейчас занят.")
        return
    tasks = [asyncio.create_task(f()) for f in (drive, local_watch, remote_watch)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for watcher in tasks[1:]:
            if watcher in done:
                watcher.result()
        success, result = tasks[0].result()
    except Halt as e:
        success, result = False, str(e)
    except Exception:  # noqa: BLE001 - close the whole browser on any driver or transport failure.
        success, result = (
            False,
            "Работа Chrome прервана. Проверь связь, установку Chrome и закрой окно ручного входа. Поручение не возобновится автоматически.",
        )
    finally:
        active = False
        for t in tasks:
            if not t.done():
                t.cancel()
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            lock.close()
    if runner.session.closed:
        return
    with suppress(aiohttp.ClientError, TimeoutError):
        await request("closed")
    with suppress(aiohttp.ClientError, TimeoutError):
        await request("finish", success=success, result=result)


async def listen(runner, root, pairing):
    while True:
        try:
            epoch, paused = chrome_local.state(root)
            if not paused and chrome_local.chrome_binary():
                job = await runner.request("/browser/claim", {"epoch": epoch})
                if job.get("turn"):
                    await work(runner, root, epoch, job, pairing=pairing)
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
            pass
        await asyncio.sleep(2)
