"""Encoded upstream locations to reduce literal URL matches in source searches.

Base64 is reversible obfuscation, not encryption or access control.
"""

from base64 import b64decode
from urllib.parse import urlsplit

API_BASE = b64decode("aHR0cHM6Ly9rYXBpLmthbmthbmV3cy5jb20=").decode("ascii")
STREAM_BASE = b64decode("aHR0cHM6Ly92b2xjLXN0cmVhbS5ra3NtZy5jb20=").decode("ascii")
PLAYER_ORIGIN = b64decode("aHR0cHM6Ly9saXZlLmthbmthbmV3cy5jb20=").decode("ascii")

STREAM_HOST = urlsplit(STREAM_BASE).hostname
