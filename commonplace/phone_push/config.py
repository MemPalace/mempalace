"""Phone notification configuration."""

import ipaddress
import logging
import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from commonplace import urls, webhooks
from commonplace.extensions import ToolError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    url: str
    token: str | None = field(default=None, repr=False)


def load() -> Config | None:
    value = os.environ.get("COMMONPLACE_NTFY_URL")
    if not value:
        return None
    try:
        webhooks.validate_url(value)
        parts = urlsplit(value)
        if parts.path not in ("", "/") or "?" in value or ";" in value or "@" in value:
            raise ValueError
    except (ToolError, ValueError):
        logger.warning("invalid COMMONPLACE_NTFY_URL")
        return None

    token = os.environ.get("COMMONPLACE_NTFY_TOKEN")
    if token is not None and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token) is None:
        logger.warning("invalid COMMONPLACE_NTFY_TOKEN")
        return None
    for name, public_url in (("AGENT_URL", urls.AGENT_URL), ("APP_URL", urls.APP_URL)):
        try:
            host = urlsplit(public_url).hostname
            if not host or host.lower().rstrip(".") == "localhost":
                raise ValueError
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                address = None
            if address is not None:
                address = getattr(address, "ipv4_mapped", None) or address
                if address.is_loopback or address.is_unspecified:
                    raise ValueError
        except ValueError:
            logger.warning("invalid %s", name)
            return None
    return Config(url=value.rstrip("/"), token=token)
