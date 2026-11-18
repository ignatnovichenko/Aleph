"""Telegram voice notes and photos. Speech is decoded and recognized locally."""

from __future__ import annotations

import asyncio
import base64
import io
import os
from pathlib import Path
import sys

MAX_VOICE_BYTES = 12 * 1024 * 1024
MAX_VOICE_SECONDS = 300
MAX_PHOTO_BYTES = 10 * 1024 * 1024
MAX_PHOTO_PIXELS = 16_000_000
_speech_slot = asyncio.Semaphore(1)
_photo_slot = asyncio.Semaphore(2)


class MediaError(ValueError):
    """A short message suitable for a Telegram reply."""


async def transcribe(audio: bytes) -> str:
    if not audio:
        raise MediaError("Голосовое пришло пустым. Запиши его ещё раз.")
    if len(audio) > MAX_VOICE_BYTES:
        raise MediaError("Голосовое слишком большое. Раздели его на части до пяти минут.")
    try:
        await asyncio.wait_for(_speech_slot.acquire(), timeout=90)
    except TimeoutError:
        raise MediaError("Сейчас расшифровываю другие голосовые. Попробуй чуть позже.") from None

    try:
        interpreter = os.environ.get("WHISPER_PYTHON") or sys.executable
        try:
            worker = await asyncio.create_subprocess_exec(
                interpreter, str(Path(__file__).resolve()), "--speech-worker",
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            raise MediaError("Распознавание речи сейчас недоступно. Можешь написать текстом.") from None
        try:
            output, _ = await asyncio.wait_for(worker.communicate(audio), timeout=300)
        except TimeoutError:
            raise MediaError("Не успел расшифровать запись. Попробуй прислать более короткий фрагмент.") from None
        finally:
            if worker.returncode is None:
                try:
                    worker.kill()
                except ProcessLookupError:
                    pass
                await worker.wait()

        if worker.returncode == 4:
            raise MediaError("Не удалось прочитать голосовое. Попробуй записать его снова.")
        if worker.returncode == 5:
            raise MediaError("Не разобрал речь. Попробуй записать ещё раз ближе к микрофону.")
        if worker.returncode == 6:
            raise MediaError("Пока принимаю записи до пяти минут. Раздели голосовое на части.")
        if worker.returncode != 0:
            raise MediaError("Распознавание речи сейчас недоступно. Можешь написать текстом.")
        return output.decode("utf-8").strip()
    finally:
        _speech_slot.release()


def _speech_worker() -> int:
    try:
        import av
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return 3

    data = sys.stdin.buffer.read(MAX_VOICE_BYTES + 1)
    if len(data) > MAX_VOICE_BYTES:
        return 6
    # Telegram voice notes use Ogg/Opus. Bound decoded samples even if duration metadata is missing.
    sample_rate = 16000
    limit = sample_rate * MAX_VOICE_SECONDS
    chunks = []
    count = 0
    try:
        with av.open(io.BytesIO(data), format="ogg") as container:
            stream = container.streams.audio[0]
            resampler = av.AudioResampler(format="flt", layout="mono", rate=sample_rate)
            for frame in container.decode(stream):
                for mono in resampler.resample(frame):
                    count += mono.samples
                    if count > limit:
                        return 6
                    chunks.append(mono.to_ndarray().ravel())
            for mono in resampler.resample(None):
                count += mono.samples
                if count > limit:
                    return 6
                chunks.append(mono.to_ndarray().ravel())
    except (av.FFmpegError, ValueError, OSError, IndexError, RuntimeError):
        return 4
    if not count:
        return 5

    model = WhisperModel(os.getenv("WHISPER_MODEL") or "small", device="cpu",
                         compute_type="int8", cpu_threads=min(4, os.cpu_count() or 1))
    segments, _ = model.transcribe(np.concatenate(chunks), beam_size=3, vad_filter=True,
                                   condition_on_previous_text=False)
    text = " ".join(segment.text.strip() for segment in segments if segment.text.strip())
    if not text:
        return 5
    sys.stdout.buffer.write(text.encode("utf-8"))
    return 0


def _normalize_photo(photo: bytes) -> str:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        raise MediaError("Обработка фото сейчас недоступна.") from None
    try:
        with Image.open(io.BytesIO(photo), formats=["JPEG", "PNG", "WEBP"]) as source:
            if source.width * source.height > MAX_PHOTO_PIXELS:
                raise MediaError("Изображение слишком большое. Отправь уменьшенную копию.")
            image = ImageOps.exif_transpose(source)
            image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
            if image.mode != "RGB":
                rgba = image.convert("RGBA")
                image = Image.new("RGB", rgba.size, "white")
                image.paste(rgba, mask=rgba.getchannel("A"))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=88, optimize=True)
    except MediaError:
        raise
    except (ValueError, OSError, Image.DecompressionBombError):
        raise MediaError("Не удалось открыть фото. Отправь его как изображение в Telegram.") from None
    return "data:image/jpeg;base64," + base64.b64encode(output.getvalue()).decode("ascii")


async def normalize_photo(photo: bytes) -> str:
    if len(photo) > MAX_PHOTO_BYTES:
        raise MediaError("Фото слишком большое. Отправь его с включённым сжатием.")
    async with _photo_slot:
        return await asyncio.to_thread(_normalize_photo, photo)


if __name__ == "__main__" and sys.argv[1:] == ["--speech-worker"]:
    raise SystemExit(_speech_worker())
