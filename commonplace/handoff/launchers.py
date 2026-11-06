"""Start the successor agent. Owner: part T08.

``client``: returns ``None``; the calling hook starts the successor and
registers it (``POST /api/handoffs/<id>/successor``). ``command``: run
``argv`` detached, no shell, placeholders filled per argument, env gets
``COMMONPLACE_HANDOFF_ID`` and ``COMMONPLACE_THREAD_ID``. ``webhook``: POST
JSON with an ``X-Commonplace-Signature: sha256=<hmac>`` header, 10 s
timeout, expects ``{"agent_ref", "url"?, "thread_id"?}``.
Gateway commands and webhooks need operator opt-in. Webhook destinations must
be public; redirects are never followed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import subprocess
import tempfile
import threading
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..admission import GATE, REGISTRY, RegistryError
from .launcher_policy import (
    LauncherPolicyError,
    require_enabled,
    require_public_address,
    validate_webhook_url,
)
from .models import Handoff, LauncherConfig, SuccessorRef


class LaunchError(RuntimeError):
    pass


class LaunchPolicyError(LaunchError):
    pass


class _PublicConnection:
    def connect(self):
        super().connect()
        try:
            require_public_address(self.sock.getpeername()[0])
        except LauncherPolicyError as error:
            self.close()
            raise LaunchPolicyError(str(error)) from None


class _PublicHTTPConnection(_PublicConnection, HTTPConnection):
    pass


class _PublicHTTPSConnection(_PublicConnection, HTTPSConnection):
    pass


class _PublicHTTPHandler(HTTPHandler):
    def http_open(self, request):
        return self.do_open(_PublicHTTPConnection, request)


class _PublicHTTPSHandler(HTTPSHandler):
    def https_open(self, request):
        return self.do_open(_PublicHTTPSConnection, request, context=self._context)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _write_prompt(directory: Path, handoff_id: str, prompt: str) -> Path:
    if not handoff_id or handoff_id in (".", "..") or any(char in handoff_id for char in "/\\\0"):
        raise LaunchError("Invalid handoff id.")
    with GATE.creating():
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        destination = directory / f"{handoff_id}.md"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{handoff_id}.", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(prompt.encode("utf-8"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return destination


def _command(
    config: LauncherConfig, handoff: Handoff, prompt: str, prompt_dir: str
) -> SuccessorRef:
    if not prompt_dir:
        raise LaunchError("A prompt directory is required.")
    if (
        not isinstance(config.argv, (tuple, list))
        or not config.argv
        or not all(isinstance(argument, str) for argument in config.argv)
        or "{" in config.argv[0]
        or "}" in config.argv[0]
    ):
        raise LaunchError("The handoff command must be a nonempty argument list.")
    try:
        directory = Path(prompt_dir).resolve()
        prompt_file = _write_prompt(directory, handoff.id, prompt)
        requested_cwd = handoff.cwd
        cwd = Path(requested_cwd).resolve() if requested_cwd else directory
        if not cwd.is_dir():
            cwd = directory
        replacements = {
            "prompt": prompt,
            "prompt_file": str(prompt_file),
            "handoff_id": handoff.id,
            "thread_id": handoff.stored_thread_id,
            "cwd": str(cwd),
        }
        argv = [
            re.sub(r"\{([^{}]+)\}", lambda match: replacements.get(match[1], match[0]), argument)
            for argument in config.argv
        ]
        environment = os.environ.copy()
        environment.update(
            COMMONPLACE_HANDOFF_ID=handoff.id,
            COMMONPLACE_THREAD_ID=handoff.stored_thread_id,
        )
        log_path = directory / f"{handoff.id}.log"
        # The successor is recorded (and the launch counted) before it exists, so
        # a backup either refuses while it runs or sees its launch mid-run.
        with GATE.creating():
            descriptor = os.open(
                log_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            with os.fdopen(descriptor, "ab") as log:
                log_path.chmod(0o600)
                with REGISTRY.launching(handoff.id) as started:
                    process = subprocess.Popen(
                        argv,
                        cwd=cwd,
                        env=environment,
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                    )
                    for _attempt in (1, 2):
                        try:
                            started(int(process.pid))
                            break
                        except (RegistryError, AttributeError, TypeError, ValueError):
                            # Retried once; then the starting entry stays and keeps
                            # backups refused until the gateway restarts (fail closed).
                            continue
    except RegistryError:
        raise LaunchError("Could not record the handoff command.") from None
    except (OSError, ValueError):
        raise LaunchError("Could not start the handoff command.") from None
    try:
        status = process.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        # Keep the process object alive and reap it when the detached agent ends.
        threading.Thread(target=process.wait, daemon=True).start()
    except OSError:
        raise LaunchError("Could not check the handoff command.") from None
    else:
        if status:
            raise LaunchError(f"Handoff command exited with status {status}.")
    return SuccessorRef(agent_ref=f"pid:{process.pid}")


def _webhook(config: LauncherConfig, handoff: Handoff, prompt: str, timeout: float) -> SuccessorRef:
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise LaunchError("The webhook timeout must be positive and finite.")
    try:
        body = json.dumps(
            {
                "handoff_id": handoff.id,
                "workspace": handoff.workspace_id,
                "stored_thread_id": handoff.stored_thread_id,
                "task_ref": handoff.task_ref,
                "branch": handoff.branch,
                "pr_url": handoff.pr_url,
                "prompt": prompt,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(config.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        request = Request(
            config.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Commonplace-Signature": f"sha256={signature}",
                "X-Commonplace-Event": "handoff.launch",
            },
            method="POST",
        )
        # Only contact the configured launcher; never an environment proxy or
        # a redirect target which could receive the prompt and its signature.
        # Check the connected peer too, before sending a request: DNS may have
        # changed since URL validation.
        opener = build_opener(
            ProxyHandler({}), _NoRedirect(), _PublicHTTPHandler(), _PublicHTTPSHandler()
        )
        with opener.open(request, timeout=timeout) as response:
            if not 200 <= response.status < 300:
                raise LaunchError(f"Handoff webhook returned status {response.status}.")
            result = json.load(response)
    except HTTPError as error:
        error.close()
        raise LaunchError(f"Handoff webhook returned status {error.code}.") from None
    except (OSError, URLError, HTTPException, ValueError, OverflowError, RecursionError):
        # Never reflect the URL, response body or transport error: any of them
        # could contain the configured secret.
        raise LaunchError("Handoff webhook request failed.") from None
    if (
        not isinstance(result, dict)
        or not isinstance(result.get("agent_ref"), str)
        or not result["agent_ref"].strip()
        or any(key in result and not isinstance(result[key], str) for key in ("url", "thread_id"))
    ):
        raise LaunchError("Handoff webhook returned an invalid successor.")
    return SuccessorRef(
        agent_ref=result["agent_ref"],
        url=result.get("url", ""),
        thread_id=result.get("thread_id", ""),
    )


def launch(
    config: LauncherConfig,
    handoff: Handoff,
    prompt: str,
    *,
    prompt_dir: str,
    timeout: float = 10.0,
) -> Optional[SuccessorRef]:
    """Raises ``LaunchError`` on failure. ``prompt_dir``: where
    ``{prompt_file}`` is written (0600). ``timeout`` applies to webhooks.

    Commands run in ``handoff.cwd`` when it is a directory, else in ``prompt_dir``.
    """
    try:
        require_enabled(config.kind)
        if config.kind == "webhook":
            validate_webhook_url(config.url)
    except LauncherPolicyError as error:
        raise LaunchPolicyError(str(error)) from None
    if config.kind == "client":
        return None
    if config.kind == "command":
        return _command(config, handoff, prompt, prompt_dir)
    if config.kind == "webhook":
        return _webhook(config, handoff, prompt, timeout)
    raise LaunchError("Unknown handoff launcher kind.")
