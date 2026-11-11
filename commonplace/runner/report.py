"""``commonplace runner report``: send one run's status, log pointer and usage to the hub.

Whatever starts or watches agents on the machine calls this with the numbers the agent
or its provider gave. Nothing is estimated here: a value that is not passed is not sent.
"""

import os
import sys

from ..client import ClientError
from . import agent, ledger

_KEYS = ("status", "log_ref", "input_tokens", "output_tokens", "cost_micros", "currency")


def run(args) -> int:
    try:
        config = agent._load(args.config)
        token = os.environ.get(config["token_env"], "")
        agent._require(
            token and "\n" not in token and "\r" not in token,
            "credential environment variables required",
        )
        body = {"job_id": args.job}
        for key in _KEYS:
            if getattr(args, key) is not None:
                body[key] = getattr(args, key)
        try:
            body = ledger.clean(body)
        except ValueError as exc:
            raise agent._ConfigError(str(exc)) from None
    except (OSError, ValueError, ClientError, TypeError, KeyError) as exc:
        return agent._config_failure(exc)
    try:
        agent._Hub(config, token).post("usage", body)
    except agent._HubError as exc:
        if exc.status in (400, 409):
            # 409: the run has not started, or already ended with another status.
            print(f"runner: report refused ({exc.status})", file=sys.stderr)
            return 5
        exc.report()
        return 3 if exc.kind == "credential" else 5
    print("runner: usage recorded")
    return 0
