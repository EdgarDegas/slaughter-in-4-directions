"""Editable application defaults. Environment variables can override these values."""

from .endpoints import STREAM_HOST


CHANNEL_ID = "10"
API_VERSION = "2.42.23"
# Public web-client signing constant, not an account credential.
API_SECRET = "28c8edde3d61a0411511d3b1866f0636"
DONOR_IDS = (2218590, 2215494, 2215102, 2213967)
SCAN_DAYS = 7
MAX_DONORS = 24
REQUEST_INTERVAL = 0.5
REFRESH_MARGIN = 120
LOOKUP_TIMEOUT = 180

ALLOWED_HOSTS = (STREAM_HOST,)
EXPECTED_STREAM = ""
# Empty creates a fresh anonymous 21-character browser ID per process.
BROWSER_ID = ""
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# Keep a personal access key in the optional, ignored .env instead of source code.
ACCESS_TOKEN = ""
PORT = 8080
