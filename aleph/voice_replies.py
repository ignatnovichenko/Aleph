"""Per-message opt-in, local synthesis and durable Telegram voice delivery."""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory

import aiohttp

SLOT = asyncio.Semaphore(1)
LOG = logging.getLogger(__name__)
VOICE = r"(?:голосовым(?:\s+сообщением)?|голосовое(?:\s+сообщение)?|голосом|аудиосообщени(?:е|ем))"
REQUEST = re.compile(
    rf"\b(?:(?:ответь|отвечай|ответьте|скажи|пришли|отправь|запиши|можешь\s+ответить|можно)\s*,?\s*"
    rf"(?:(?:мне|пожалуйста|это|ответ|свой\s+ответ)\s*[, ]\s*){{0,2}}{VOICE}"
    r"|(?:дай|запиши)\s+голосовой\s+ответ|озвучь\s+(?:(?:твой|свой|последний|предыдущий|этот)\s+)?ответ"
    r"|(?:reply|respond|answer|send (?:me )?(?:your|the) answer)\s+(?:with|as|by|in)\s+(?:a\s+)?(?:voice(?:\s+(?:note|message))?|audio))\b",
    re.IGNORECASE,
)
STANDALONE = re.compile(rf"^\s*{VOICE}(?:[, ]+пожалуйста)?[.!? ]*$", re.IGNORECASE)
QUOTE = re.compile(r"""«[^»]*»|“[^”]*”|"[^"]*"|(?<!\w)'[^']*'(?!\w)|`[^`]*`""", re.DOTALL)
NEGATION = re.compile(r"(?:\bне|\bне\s+надо|\bне\s+нужно|\bdon't|\bdo not)\s*$", re.IGNORECASE)
TEXT_INSTEAD = re.compile(
    r"\b(?:ответь|отвечай|лучше|только|давай)\s+текстом\b|\bбез голосов(?:ого|ых)\b", re.IGNORECASE
)
CONTEXT = re.compile(
    r"\n\n\[(?:Цитата из сообщения, на которое дан ответ|Files for this request|Attached document content)\]"
)


def preference(query):
    """Return (opt-in, request without delivery instruction), ignoring quoted sources."""
    boundary = CONTEXT.search(query)
    head = query[: boundary.start()] if boundary else query
    visible = QUOTE.sub(lambda m: " " * len(m[0]), head)
    if TEXT_INSTEAD.search(visible):
        return False, query
    match = REQUEST.search(visible) or STANDALONE.fullmatch(visible)
    if (
        not match
        or NEGATION.search(visible[: match.start()])
        or re.search(
            r"\b(?:если (?:я |кто-то )?(?:скажу|скажет|напишу)|(?:переведи|объясни) (?:фразу|слова)|if I (?:say|type))\s*[:—,]?\s*$",
            visible[: match.start()],
            re.IGNORECASE,
        )
    ):
        return False, query
    content = (head[: match.start()].rstrip(" ,:;—") + " " + head[match.end() :].lstrip(" ,:;—")).strip(
        " .,!?:;—\n"
    )
    if content.casefold() in {"пожалуйста", "please"}:
        content = ""
    return True, content + query[len(head) :]


def segments(text):
    # Keep link labels and normal punctuation. Limit the duration of a single synthesis process.
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)]+\)", r"\1", text)
    text = re.sub(r"(?m)^#{1,6}\s+|\*\*|__|`", "", text).strip() or text.strip()
    while len(text) > 1050:
        stop = max(text.rfind(p, 0, 1050) for p in (". ", "! ", "? ", "\n"))
        if stop < 420:
            stop = text.rfind(" ", 0, 1050)
        split = stop + 1 if stop > 0 else 1050
        yield text[:split].strip()
        text = text[split:].strip()
    if text:
        yield text


async def run(command, data=None, *, timeout, env=None):
    proc = await asyncio.create_subprocess_exec(
        *command,
        env=env,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.communicate(data), timeout)
        if proc.returncode:
            raise ValueError("Voice renderer failed")
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


async def synthesize(text):
    root = Path(os.getenv("ALEPH_BLUETTS_HOME") or Path.home() / ".local/share/aleph/bluetts")
    lang = "ru" if re.search("[А-Яа-яЁё]", text) else "en"
    async with SLOT:
        with TemporaryDirectory(prefix="aleph-voice-") as tmp:
            wav = Path(tmp) / "speech.wav"
            opus = Path(tmp) / "voice.ogg"
            await run(
                [root / "venv/bin/python", Path(__file__).with_name("tts_worker.py"), root, wav, lang],
                text.encode(),
                timeout=180,
                env={
                    "PATH": os.defpath,
                    "LANG": "C.UTF-8",
                    "ORT_NUM_THREADS": "2",
                    "OPENBLAS_NUM_THREADS": "1",
                },
            )
            await run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    wav,
                    "-ac",
                    "1",
                    "-ar",
                    "48000",
                    "-c:a",
                    "libopus",
                    "-application",
                    "voip",
                    "-b:a",
                    "32k",
                    opus,
                ],
                timeout=30,
            )
            return opus.read_bytes()


async def upload(tg, audio, address):
    from .bot import TelegramError

    form = aiohttp.FormData()
    for key, value in address.items():
        form.add_field(key, json.dumps(value) if isinstance(value, dict) else str(value))
    form.add_field("voice", audio, filename="aleph.ogg", content_type="audio/ogg")
    try:
        async with tg.session.post(
            f"https://api.telegram.org/bot{tg.token}/sendVoice",
            data=form,
            allow_redirects=False,
            timeout=aiohttp.ClientTimeout(total=35),
        ) as response:
            payload = await response.json(content_type=None)
            if not payload.get("ok"):
                raise TelegramError(
                    int(payload.get("error_code", response.status)),
                    int((payload.get("parameters") or {}).get("retry_after", 0)),
                )
            return payload["result"]
    except (aiohttp.ClientError, TimeoutError, KeyError, ValueError) as exc:
        raise TelegramError() from exc


async def deliver(inbox, tg, ident, answer, address, ru, valid=lambda: True):
    """Resume at the first unsent segment. Keep encoded audio across Telegram retries."""
    from .bot import TelegramError

    last = None
    for index, part in enumerate(segments(answer)):
        row = inbox.db.execute("SELECT voice_part,voice_audio FROM bot_inbox WHERE id=?", (ident,)).fetchone()
        if not row or not valid():
            return None
        if index < row["voice_part"]:
            continue
        try:
            audio = row["voice_audio"]
            if not audio:
                audio = await synthesize(part)
                if (
                    not inbox.db.execute(
                        "UPDATE bot_inbox SET voice_audio=? WHERE id=?", (audio, ident)
                    ).rowcount
                    or not valid()
                ):
                    return None
            last = await upload(tg, audio, address)
        except TelegramError as error:
            if error.code not in (400, 413):
                raise
            last = await tg.call("sendMessage", **address, text=part)
        except (OSError, TimeoutError, ValueError) as error:
            LOG.warning("Voice synthesis unavailable: %s", type(error).__name__)
            note = "Не удалось озвучить ответ." if ru else "Voice synthesis is unavailable."
            last = await tg.call("sendMessage", **address, text=note + "\n\n" + part)
        inbox.db.execute("UPDATE bot_inbox SET voice_audio=NULL,voice_part=? WHERE id=?", (index + 1, ident))
    return last
