"""The model a bot may use: the owner's chat settings, never a silent fallback."""

from commonplace.chat import settings as chat_settings


def resolve(db, workspace_id, board=None):
    """``(settings, key, code)``; ``code`` is "" when usable.

    ``needs_configuration``: no model is set up or it is off. ``needs_turn_on``: the board inherits a
    hosted setting it has not been turned on for. ``key_missing``: the configured key is not usable.
    """
    config = chat_settings.front(db, workspace_id, board)
    if not config.enabled or config.provider == "none":
        return None, None, "needs_configuration"
    if not config.applies:
        return None, None, "needs_turn_on"
    key = None
    if config.key_ref is not None:
        key = chat_settings.resolve_key(config)
        if key is None:
            return None, None, "key_missing"
    return config, key, ""
