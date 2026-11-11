"""Scheduled prompts per agent: an owner gives an agent a schedule and a prompt.

Modules: ``store`` (control-database tables, limits, at-most-once claims), ``worker``
(due-time pass and delivery through the board's wake path), ``routes`` (``/api/agent-schedules``).
Due times come from the shared ``commonplace.schedule_time`` module.

As a gateway extension, ``install(gateway)`` creates the tables, starts the worker (set
``COMMONPLACE_SCHEDULE_WORKER=off`` to run it by hand with ``run_once``) and adds the routes.
"""

import os


def install(gateway) -> None:
    from . import routes, store, worker

    if getattr(gateway, "agent_schedules", None) is not None:
        return
    store.ensure_schema(gateway.db)
    gateway.agent_schedules = worker.ScheduleWorker(
        gateway, start=os.environ.get("COMMONPLACE_SCHEDULE_WORKER", "on") != "off"
    )
    gateway.db.add_pre_close_callback(gateway.agent_schedules.stop)
    routes.install(gateway)
