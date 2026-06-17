"""Shared Atlassian (Jira / Confluence) auth + URL helpers.

Two deployment shapes, switched explicitly by ``method`` (never guessed from
the URL, so on-prem instances on custom domains work):

* **cloud** (``*.atlassian.net`` and Atlassian-hosted) — HTTP Basic with
  ``email:api_token``. REST v3 for Jira, ``/wiki/rest/api`` for Confluence.
* **server** — Jira/Confluence Server or Data Center, authenticated with a
  Bearer Personal Access Token. REST v2 for Jira, ``/rest/api`` for Confluence.

This module is product-agnostic: it knows how to authenticate and where the
host lives, not which product's endpoints to call. The Jira/Confluence
connectors build their own API paths on top.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass
class AtlassianAuth:
    """Credentials + base URL for one Atlassian site.

    ``method`` is ``"cloud"`` or ``"server"``. ``email`` is required only for
    cloud (it's the username half of the Basic credential); server uses the
    token alone as a Bearer PAT.
    """

    base_url: str = ""
    method: str = "cloud"
    email: str = ""
    token: str = ""

    @property
    def is_cloud(self) -> bool:
        # Anything that isn't explicitly "server" is treated as cloud — cloud
        # is the safe default for a blank/legacy value.
        return (self.method or "cloud").lower() != "server"

    @property
    def configured(self) -> bool:
        if not self.base_url or not self.token:
            return False
        if self.is_cloud and not self.email:
            return False
        return True

    def headers(self) -> dict[str, str]:
        """Authorization + JSON headers for this site."""
        if self.is_cloud:
            if not self.email:
                raise RuntimeError(
                    "Jira/Confluence Cloud requires an email address. Re-save "
                    "the sync config with your Atlassian account email."
                )
            raw = f"{self.email}:{self.token}".encode()
            token = base64.b64encode(raw).decode()
            authz = f"Basic {token}"
        else:
            authz = f"Bearer {self.token}"
        return {
            "Authorization": authz,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }


def normalize_base_url(url: str) -> str:
    """Reduce any Atlassian URL to its ``scheme://host[:port]`` origin.

    Accepts bare hostnames (assumes https), full deep links
    (``https://x.atlassian.net/browse/PROJ-1``), trailing slashes, etc., and
    returns just the origin so connectors can append their own REST paths.
    Returns ``""`` for blank input.
    """
    url = (url or "").strip()
    if not url:
        return ""
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"
