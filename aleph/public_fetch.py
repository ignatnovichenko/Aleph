"""Bounded HTTP reads of public URLs submitted as project references."""
import asyncio
import ipaddress
import re
import socket
from urllib.parse import urljoin
import zlib

import aiohttp
from yarl import URL

# Managed HTTPS proxies can resolve the TLS hostname themselves. Restrict their
# use to provider-controlled DNS zones and use the pinned direct path for all
# other hosts. Recheck the destination on every redirect.
PROVIDER_HOSTS = {"docs.google.com", "drive.google.com", "notion.so", "www.notion.so",
                  "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
                  "youtube-nocookie.com", "www.youtube-nocookie.com"}
PROVIDER_ZONES = (".googleusercontent.com", ".notion.site", ".wikipedia.org", ".wikimedia.org")

class PublicURL(ValueError):
    pass


class FetchError(ValueError):
    def __init__(self, status=0):
        super().__init__("Источник временно недоступен.")
        self.status = status


def is_public(address):
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        elif ip.is_site_local or ip.sixtofour or ip.teredo or str(ip).startswith(("64:ff9b:", "64:ff9b::")):
            return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


def public_url(value):
    if not isinstance(value, str) or len(value) > 3000 or re.search(r"[\s\\\x00-\x1f\x7f]", value):
        raise PublicURL("Введите ссылку на публичный HTTP(S)-адрес.")
    try:
        url = URL(value)
        host = url.raw_host
        if (url.scheme not in {"http", "https"} or not host or url.user is not None
                or url.password is not None or url.port != (443 if url.scheme == "https" else 80)
                or "." not in host and ":" not in host
                or host.endswith((".localhost", ".local", ".internal")) or "%" in host):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            # Reject alternate literal forms that system resolvers can treat as numeric addresses.
            if not re.fullmatch(r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z][a-zA-Z0-9-]*\.?", host):
                raise ValueError from None
        else:
            if not is_public(address):
                raise ValueError
    except (ValueError, UnicodeError):
        raise PublicURL("Введите ссылку на публичный HTTP(S)-адрес.") from None
    return url.with_fragment(None)


async def _pinned(url):
    try:
        ipaddress.ip_address(url.raw_host)
    except ValueError:
        pass
    else:
        return url
    resolver = asyncio.get_running_loop().getaddrinfo
    async with asyncio.timeout(4):
        try:
            results = await resolver(url.raw_host, url.port, type=socket.SOCK_STREAM, family=socket.AF_INET)
        except socket.gaierror as error:
            if error.errno != socket.EAI_NONAME:
                raise
            results = await resolver(url.raw_host, url.port, type=socket.SOCK_STREAM, family=socket.AF_INET6)
    if not results or any(not is_public(row[4][0]) for row in results):
        raise PublicURL("Этот адрес ведёт в закрытую сеть.")
    return url.with_host(results[0][4][0])


def session(*, proxy=False):
    return aiohttp.ClientSession(
        connector=aiohttp.TCPConnector(force_close=True),
        timeout=aiohttp.ClientTimeout(total=7, connect=4), trust_env=proxy, auto_decompress=False,
        cookie_jar=aiohttp.DummyCookieJar(),
        headers={"User-Agent": "AlephAgent/1.0", "Accept-Encoding": "identity"},
    )


async def bounded(stream, limit, encoding=""):
    if encoding not in ("", "identity", "gzip", "deflate"):
        raise FetchError()
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding in {"gzip", "deflate"} else None
    data = bytearray()
    async for chunk in stream.iter_chunked(16384):
        try:
            data.extend(decoder.decompress(chunk, limit - len(data) + 1) if decoder else chunk)
        except zlib.error:
            raise FetchError() from None
        if len(data) > limit:
            raise FetchError(413)
    if decoder and (not decoder.eof or decoder.unused_data):
        raise FetchError()
    return bytes(data)


async def get(client, url, *, proxy=None, limit=2_000_000, domains=None, with_filename=False):
    for _ in range(5):
        target = public_url(url)
        if domains and target.host not in domains:
            raise FetchError(403)
        try:
            pinned = await _pinned(target)
            # Direct requests validate TLS against the hostname at the pinned IP.
            host = target.raw_host.rstrip(".")
            use_proxy = proxy is not None and target.scheme == "https" and (
                host in PROVIDER_HOSTS or host.endswith(PROVIDER_ZONES))
            transport = proxy if use_proxy else client
            async with transport.get(pinned, allow_redirects=False, headers={"Host": target.raw_authority},
                                     server_hostname=target.raw_host) as response:
                if response.status in (301, 302, 303, 307, 308) and response.headers.get("Location"):
                    url = urljoin(str(target), response.headers["Location"])
                    continue
                if response.status != 200:
                    raise FetchError(response.status)
                data = await bounded(response.content, limit, response.headers.get("Content-Encoding", "").lower())
                if with_filename:
                    disposition = response.content_disposition
                    return data, response.content_type, response.charset, disposition.filename if disposition else ""
                return data, response.content_type, response.charset
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise FetchError() from None
    raise FetchError(310)
