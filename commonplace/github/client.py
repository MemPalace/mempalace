"""Bounded direct GitHub transport; never logs remote bodies or credentials."""

from __future__ import annotations

import base64
import http.client
import json
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import ProxyHandler, Request, build_opener

from ..social.providers import SignInError, _NoRedirect

MAX_RESPONSE = 1024 * 1024


def request(method, url, *, form=None, json_body=None, token=None, credentials=None):
    headers = {
        "User-Agent": "Commonplace",
        "Accept": "application/json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    data = None
    if form is not None:
        data = urlencode(form, quote_via=quote).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    if credentials:
        headers["Authorization"] = (
            "Basic " + base64.b64encode(":".join(credentials).encode()).decode()
        )
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(
                Request(url, data=data, headers=headers, method=method), timeout=10
            )
        except HTTPError as exc:
            response = exc
        with response:
            status = response.code if isinstance(response, HTTPError) else response.status
            if status not in (200, 204):
                return status, {}, {}
            raw = response.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise SignInError("github_response")
            body = json.loads(raw) if raw else {}
            return status, response.headers, body
    except (OSError, http.client.HTTPException, ValueError, UnicodeError, RecursionError):
        raise SignInError("github_transport") from None
