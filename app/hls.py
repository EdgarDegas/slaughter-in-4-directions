"""Rewrite every HLS resource into an opaque, same-server URL."""

import hashlib
import hmac
import re
import secrets
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .upstream import AUTH_PARAMS, UpstreamError

URI_ATTRIBUTE = re.compile(r'(?P<prefix>\bURI=")(?P<uri>[^"\r\n]*)(?P<suffix>")')
PLAYLIST_TAGS = ("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-RENDITION-REPORT:")


def validate_url(url, hosts):
    try:
        parts = urlsplit(url)
        if (parts.scheme != "https" or parts.hostname not in hosts or parts.port not in (None, 443)
                or parts.username or parts.password or parts.fragment
                or any(ord(c) < 32 for c in url)):
            raise ValueError("URL not allowed")
    except ValueError as exc:
        raise UpstreamError("The stream uses an unapproved URL. Check UPSTREAM_HOSTS against the broadcaster's CDN hosts.") from exc
    return url


@dataclass
class Resource:
    url: str
    playlist: bool
    touched: float


class Resources:
    def __init__(self, hosts, *, limit=20000, ttl=21600):
        self.hosts = hosts
        self.limit = limit
        self.ttl = ttl
        self.secret = secrets.token_bytes(32)
        self.items = OrderedDict()

    def register(self, url, playlist=False):
        validate_url(url, self.hosts)
        parts = urlsplit(url)
        canonical = url
        if playlist:
            # A selected variant must keep the same local URL across token refreshes.
            query = [(k, "<credential>" if k in AUTH_PARAMS else v)
                     for k, v in parse_qsl(parts.query, keep_blank_values=True)]
            canonical = urlunsplit(parts._replace(query=urlencode(query)))
        key = hmac.new(self.secret, (str(playlist) + canonical).encode(), hashlib.sha256).hexdigest()[:40]
        self.items[key] = Resource(url, playlist, time.monotonic())
        self.items.move_to_end(key)
        while len(self.items) > self.limit:
            self.items.popitem(last=False)
        return key

    def get(self, key):
        resource = self.items.get(key)
        if resource is None:
            return None
        if time.monotonic() - resource.touched > self.ttl:
            del self.items[key]
            return None
        resource.touched = time.monotonic()
        self.items.move_to_end(key)
        return resource


def rewrite_playlist(text, base_url, resources, access_key=""):
    if not text.lstrip("\ufeff \r\n").startswith("#EXTM3U"):
        raise UpstreamError("The upstream server returned a non-HLS response.")
    # These need variable expansion/steering support and must not silently leak URLs.
    if "#EXT-X-DEFINE:" in text or "#EXT-X-CONTENT-STEERING:" in text:
        raise UpstreamError("This stream requires HLS variables or content steering, which this version does not support.")

    def local(uri, playlist=False):
        if "{$" in uri:
            raise UpstreamError("Unresolved HLS variable in playlist.")
        url = urljoin(base_url, uri)
        playlist = playlist or urlsplit(url).path.lower().endswith(".m3u8")
        key = resources.register(url, playlist)
        suffix = "index.m3u8" if playlist else "media"
        path = f"/hls/{key}/{suffix}"
        return path + ("?" + urlencode({"key": access_key}) if access_key else "")

    output = []
    next_is_playlist = False
    for line in text.lstrip("\ufeff").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            is_playlist = stripped.startswith(PLAYLIST_TAGS)
            line = URI_ATTRIBUTE.sub(lambda m: m["prefix"] + local(m["uri"], is_playlist) + m["suffix"], line)
            if stripped.startswith("#EXT-X-STREAM-INF:"):
                next_is_playlist = True
            output.append(line)
        elif stripped:
            output.append(local(stripped, next_is_playlist))
            next_is_playlist = False
        else:
            output.append(line)
    return "\n".join(output) + "\n"
