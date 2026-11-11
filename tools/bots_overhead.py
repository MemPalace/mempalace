"""Hub overhead of one bot turn: request in -> first ``delta`` out, minus the provider's delay.

Informational only; it gates nothing. It starts a test hub and a loopback fake streaming
provider that waits a fixed time before its first token, drives a few turns over real HTTP
and prints the overhead per turn and the median.

    uv run python tools/bots_overhead.py [turns] [provider_delay_ms]
"""

import statistics
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tests._bots_helpers import bots_hub_fixture  # noqa: E402, F401
from tests.test_chat_model_stream import fake_server, o_text, openai_frame  # noqa: E402

TURNS = int(sys.argv[1]) if len(sys.argv) > 1 else 10
DELAY_S = (int(sys.argv[2]) if len(sys.argv) > 2 else 200) / 1000


def measure_overhead(bots_hub):
    from tests.test_bots_turn import Stream, bot_of, configure

    hub = bots_hub
    sent_first = []

    class Delay(threading.Event):
        """Holds the first token for DELAY_S, as a provider thinking would."""

        def wait(self, timeout=None):
            time.sleep(DELAY_S)
            sent_first.append(time.monotonic())
            return True

    results = []
    for n in range(TURNS + 1):
        script = [Delay(), o_text("Hello "), o_text("there."), openai_frame(finish="stop")]
        with fake_server(script) as (origin, _state):
            configure(hub, base_url=origin, user_daily_limit=1000, daily_limit=1000)
            bot = bot_of(hub)
            began = time.monotonic()
            stream = Stream(hub, hub.owner, bot["id"], "Hello there", f"overhead-{n:04d}")
            ack = time.monotonic()
            event = None
            while event != "delta":
                event, _ = stream.next()
            first = time.monotonic()
            stream.until_end()
        if n:  # the first turn warms imports and connections
            results.append(
                {
                    "ack_ms": (ack - began) * 1000,
                    "first_delta_ms": (first - began) * 1000,
                    "overhead_ms": (first - began - DELAY_S) * 1000,
                }
            )
    print(f"\nprovider first-token delay: {DELAY_S * 1000:.0f} ms, turns: {len(results)}")
    for row in results:
        print("  " + ", ".join(f"{k} {v:.1f}" for k, v in row.items()))
    for key in ("ack_ms", "first_delta_ms", "overhead_ms"):
        values = [row[key] for row in results]
        print(f"{key}: median {statistics.median(values):.1f}, max {max(values):.1f}")


if __name__ == "__main__":
    sys.exit(
        pytest.main(
            [
                __file__,
                "-q",
                "-s",
                "-p",
                "no:cacheprovider",
                "-p",
                "tests.conftest",
                "-o",
                "python_files=bots_overhead.py",
                "-o",
                "python_functions=measure_*",
                "--rootdir",
                str(ROOT),
            ]
        )
    )
