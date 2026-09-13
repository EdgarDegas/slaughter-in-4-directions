import os
import secrets
from dataclasses import dataclass, field

from . import constants


def browser_id():
    # Match the web client's 21-character Nano ID, independently of any login.
    return constants.BROWSER_ID or secrets.token_urlsafe(16)[:21]


@dataclass(frozen=True)
class Config:
    channel_id: str = constants.CHANNEL_ID
    api_version: str = constants.API_VERSION
    api_secret: str = constants.API_SECRET
    donor_ids: tuple[int, ...] = constants.DONOR_IDS
    scan_days: int = constants.SCAN_DAYS
    max_donors: int = constants.MAX_DONORS
    request_interval: float = constants.REQUEST_INTERVAL
    refresh_margin: int = constants.REFRESH_MARGIN
    lookup_timeout: int = constants.LOOKUP_TIMEOUT
    access_token: str = constants.ACCESS_TOKEN
    allowed_hosts: tuple[str, ...] = constants.ALLOWED_HOSTS
    expected_stream: str = constants.EXPECTED_STREAM
    uuid: str = field(default_factory=browser_id)
    user_agent: str = constants.USER_AGENT
    port: int = constants.PORT

    @classmethod
    def from_env(cls):
        defaults = cls()
        channel = os.getenv("SMG_CHANNEL_ID", defaults.channel_id)
        donors = os.getenv(
            "SMG_DONOR_IDS",
            ",".join(map(str, defaults.donor_ids)) if channel == defaults.channel_id else "",
        )
        hosts = os.getenv("UPSTREAM_HOSTS", "").strip() or ",".join(defaults.allowed_hosts)
        return cls(
            channel_id=channel,
            api_version=os.getenv("SMG_API_VERSION", defaults.api_version),
            api_secret=os.getenv("SMG_API_SECRET", defaults.api_secret),
            donor_ids=tuple(int(x.strip()) for x in donors.split(",") if x.strip()),
            scan_days=max(1, min(7, int(os.getenv("SMG_SCAN_DAYS", defaults.scan_days)))),
            request_interval=max(0.25, float(os.getenv("SMG_REQUEST_INTERVAL", defaults.request_interval))),
            lookup_timeout=max(30, min(300, int(os.getenv("SMG_LOOKUP_TIMEOUT", defaults.lookup_timeout)))),
            access_token=os.getenv("ACCESS_TOKEN", defaults.access_token),
            allowed_hosts=tuple(x.strip().lower() for x in hosts.split(",") if x.strip()),
            expected_stream=os.getenv("SMG_EXPECTED_STREAM", defaults.expected_stream),
            uuid=os.getenv("SMG_UUID", "").strip() or defaults.uuid,
            user_agent=os.getenv("SMG_USER_AGENT", "").strip() or defaults.user_agent,
            port=int(os.getenv("PORT", defaults.port)),
        )
