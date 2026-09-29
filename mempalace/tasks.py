"""High-level task envelopes shared by CLI and remote MCP clients."""

import re
import secrets


_IMMUTABLE_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _required_text(value, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"task {field_name} must not be empty")
    return value.strip()


def validate_task_base_commit(value: str) -> str:
    """Require an immutable abbreviated or full Git object id."""
    value = _required_text(value, "base commit")
    if not _IMMUTABLE_COMMIT_RE.fullmatch(value):
        raise ValueError(
            "task base commit must be a hexadecimal Git object id "
            "(at least 7 characters), not a branch or tag"
        )
    return value.lower()


def validate_task_request(task, *, source: str = "task request") -> dict:
    """Validate every field the controlled launcher relies on."""
    if not isinstance(task, dict) or task.get("type") != "task.request":
        raise ValueError(f"{source} must contain one task.request event object")
    required = ("correlation_id", "to_agent", "branch", "base_commit")
    missing = [field for field in required if field not in task]
    if missing:
        raise ValueError(f"{source} is missing required field(s): {', '.join(missing)}")

    _required_text(task["correlation_id"], "correlation id")
    _required_text(task["branch"], "branch")
    validate_task_base_commit(task["base_commit"])
    addressed_agent = task["to_agent"]
    if addressed_agent is not None:
        _required_text(addressed_agent, "destination agent")
    return task


def task_slug(value: str, fallback: str = "work") -> str:
    """Return a short routing-safe label for task ids and project streams."""
    if not isinstance(value, str):
        raise ValueError("task labels must be strings")
    slug = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    return slug[:40].rstrip("_") or fallback


def task_handoff(correlation_id: str, agent: str) -> str:
    """Render the portable one-line wake-up prompt for a stored task."""
    return (
        f"Open MemPalace task {correlation_id} as {agent}. "
        "Claim it, follow its exact definition of done, and deliver through the logstream."
    )


def create_task(
    logstream,
    *,
    project: str,
    from_agent: str,
    to_agent: str,
    goal: str,
    branch: str,
    base_commit: str,
    done: str,
) -> dict:
    """Append one canonical task request and return it with its handoff line."""
    project = _required_text(project, "project")
    from_agent = _required_text(from_agent, "requesting agent")
    to_agent = _required_text(to_agent, "destination agent")
    branch = _required_text(branch, "branch")
    base_commit = validate_task_base_commit(base_commit)
    if not isinstance(goal, str) or not goal.strip():
        raise ValueError("task goal must not be empty")
    if not isinstance(done, str) or not done.strip():
        raise ValueError("task definition of done must not be empty")
    correlation_id = f"task_{task_slug(goal)}_{secrets.token_hex(4)}"
    body = (
        f"Goal:\n{goal}\n\n"
        f"Definition of done:\n{done}\n\n"
        "Delivery:\n"
        "Close the loop through MemPalace: claim the request, then submit a patch "
        "with mempalace_patch_submit or reply with blocked/failed evidence."
    )
    event = logstream.append_event(
        type="task.request",
        stream=f"project/{task_slug(project, fallback='project')}",
        room="delegation",
        from_agent=from_agent,
        to_agent=to_agent,
        correlation_id=correlation_id,
        branch=branch,
        base_commit=base_commit,
        status="open",
        body=body,
    )
    return {"task": event, "handoff": task_handoff(correlation_id, to_agent)}
