"""Stdio transport — reads JSON-RPC lines from stdin, writes responses to stdout."""
import json
import logging
import sys

from mempalace.mcp_server import handle_request

logger = logging.getLogger("mempalace.transport.stdio")


def serve() -> None:
    """Run the stdio JSON-RPC loop. Returns when stdin is closed."""
    logger.info("MemPalace stdio transport starting...")
    while True:
        try:
            line = sys.stdin.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except KeyboardInterrupt:
            break
        except Exception as e:  # noqa: BLE001
            logger.error(f"stdio transport error: {e}")
