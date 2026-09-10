"""
query_parser.py — Parser for Palace Query Language (PQL) and Command DSL.

Converts concise single-line/multi-line DSL queries and structured JSON payloads
into normalized action dictionaries for palace_query, palace_exec, and palace_coordinate.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple


class QueryParseError(ValueError):
    """Raised when a PQL DSL string cannot be parsed."""


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ].*)?$")


class QuotedToken(str):
    """A DSL token that was written in quotes; never treated as a keyword flag."""


_BARE_FLAGS = frozenset({"APPLY", "COMMIT", "PREVIEW", "DRY_RUN"})
_KG_ADD_FLAGS = frozenset({"FROM", "TO", "VALID_FROM", "VALID_TO", "CLOSET", "DRAWER"})
_KG_INVALIDATE_FLAGS = frozenset({"ENDED", "AT", "DATE"})
_KG_SUPERSEDE_FLAGS = frozenset({"AT", "DATE"})


def tokenize_dsl(text: str) -> List[str]:
    """Tokenize a DSL string into logical tokens, preserving quoted blocks."""
    tokens: List[str] = []
    text = text.strip()
    if not text:
        return []

    # If the text is wrapped in JSON format {...}, treat it as JSON directly
    if text.startswith("{") and text.endswith("}"):
        return [text]

    pos = 0
    length = len(text)
    while pos < length:
        # Skip leading whitespace
        while pos < length and text[pos].isspace():
            pos += 1
        if pos >= length:
            break

        # Check for quoted string
        if text[pos] in ('"', "'"):
            quote_char = text[pos]
            start_pos = pos + 1
            pos += 1
            escaped = False
            content = []
            while pos < length:
                ch = text[pos]
                if escaped:
                    if ch == "n":
                        content.append("\n")
                    elif ch == "t":
                        content.append("\t")
                    elif ch == "r":
                        content.append("\r")
                    elif ch in ('"', "'", "\\"):
                        content.append(ch)
                    else:
                        content.append("\\" + ch)
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == quote_char:
                    pos += 1
                    break
                else:
                    content.append(ch)
                pos += 1
            tokens.append(QuotedToken("".join(content)))
            continue

        # Check for arrow symbols -> or =>
        if text[pos : pos + 2] in ("->", "=>"):
            tokens.append(text[pos : pos + 2])
            pos += 2
            continue

        # Check for standalone colon or colon followed by space
        if text[pos] == ":" and (pos + 1 >= length or text[pos + 1].isspace()):
            tokens.append(":")
            pos += 1
            continue

        # Read unquoted word up to whitespace, quote, arrow, or colon-followed-by-space
        start_pos = pos
        while pos < length and not text[pos].isspace() and text[pos] not in ('"', "'"):
            if text[pos : pos + 2] in ("->", "=>"):
                break
            if text[pos] == ":" and (pos + 1 >= length or text[pos + 1].isspace()):
                break
            pos += 1
        word = text[start_pos:pos]
        if word:
            tokens.append(word)

    return tokens


def _parse_key_value_tokens(tokens: List[str]) -> Dict[str, Any]:
    """
    Parse tokens that contain key:value pairs, key:"quoted" pairs, or KEY value pairs.
    Handles forms like:
      - `stream:project/app`
      - `goal:"fix bug"` (tokenized as `goal:`, `fix bug`)
      - `stream : project/app`
      - `STREAM project/app`
    """
    result: Dict[str, Any] = {}
    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        if not tok or tok == ":":
            i += 1
            continue

        # Case 1: tok ends with ":" (e.g. `goal:`, next token is `"fix bug"`)
        if tok.endswith(":") and len(tok) > 1 and i + 1 < n:
            k = tok[:-1].lower().strip()
            v = tokens[i + 1]
            if k:
                result[k] = _parse_val(v)
            i += 2
            continue

        # Case 2: tok is "key:value" (quoted tokens stay verbatim content)
        if (
            ":" in tok
            and not tok.startswith("http://")
            and not tok.startswith("https://")
            and not isinstance(tok, QuotedToken)
        ):
            k, v = tok.split(":", 1)
            k = k.lower().strip()
            if k:
                result[k] = _parse_val(v)
            i += 1
            continue

        # Case 3: tok is "key", next is ":", next is "value"
        if i + 2 < n and tokens[i + 1] == ":":
            k = tok.lower().strip()
            v = tokens[i + 2]
            if k:
                result[k] = _parse_val(v)
            i += 3
            continue

        # Case 4: tok is key keyword, next is value (including uppercase values
        # like CONTENT NASA). Bare flags never consume the following token.
        if i + 1 < n and tok.isupper() and tokens[i + 1] != ":":
            nxt = tokens[i + 1]
            if nxt.upper() not in _BARE_FLAGS:
                k = tok.lower().strip()
                if k:
                    result[k] = _parse_val(nxt)
                i += 2
                continue

        # Standalone flag (e.g. APPLY, COMMIT, PREVIEW, DRY_RUN)
        if tok.isupper() and tok not in ("->", "=>", ":") and not isinstance(tok, QuotedToken):
            result[tok.lower()] = True
            i += 1
            continue

        i += 1

    return result


def _parse_val(val: str) -> Any:
    """Parse string representation of boolean, integer, float, or JSON into native type."""
    if isinstance(val, QuotedToken):
        return str(val)
    v_lower = val.lower()
    if v_lower == "true":
        return True
    if v_lower == "false":
        return False
    if v_lower == "null" or v_lower == "none":
        return None
    try:
        if "." in val:
            return float(val)
        return int(val)
    except ValueError:
        pass

    if (val.startswith("{") and val.endswith("}")) or (val.startswith("[") and val.endswith("]")):
        try:
            return json.loads(val)
        except Exception:
            pass

    return val


def _is_known_flag_token(tok: str, flag_keys: frozenset) -> bool:
    """True when tok starts option flags, not when it merely contains a colon."""
    if isinstance(tok, QuotedToken):
        return False
    if tok.upper() in flag_keys:
        return True
    if ":" not in tok or tok.startswith("http://") or tok.startswith("https://"):
        return False
    return tok.split(":", 1)[0].upper() in flag_keys


def _split_object_and_flags(tokens: List[str], flag_keys: frozenset) -> Tuple[List[str], List[str]]:
    obj_tokens: List[str] = []
    flag_tokens: List[str] = []
    in_flags = False
    for tok in tokens:
        if not in_flags and _is_known_flag_token(tok, flag_keys):
            in_flags = True
        if in_flags:
            flag_tokens.append(tok)
        else:
            obj_tokens.append(tok)
    return obj_tokens, flag_tokens


# ==============================================================================
# 1. PALACE QUERY PARSER
# ==============================================================================


def parse_query_input(input_data: Any) -> Tuple[str, Dict[str, Any]]:  # noqa: C901
    """
    Parse input to `palace_query`.

    Returns:
        (target_operation, parsed_parameters_dict)
    """
    # 1. Structured JSON / Dict input
    if isinstance(input_data, dict):
        params = dict(input_data)
        # Check if there is an embedded command/dsl string in query, command, or input
        for k in ("query", "command", "input", "dsl", "pql"):
            if k in params and isinstance(params[k], str):
                v_strip = params[k].strip()
                first_w = v_strip.split(None, 1)[0].upper() if v_strip else ""
                if first_w in (
                    "FIND",
                    "SEARCH",
                    "TAXONOMY",
                    "WINGS",
                    "ROOMS",
                    "DRAWER",
                    "DRAWERS",
                    "CHECK",
                    "AAAK",
                    "KG",
                    "TRAVERSE",
                    "TUNNELS",
                    "TUNNEL",
                    "FOLLOW",
                    "HALLWAYS",
                    "HALLWAY",
                    "GRAPH",
                    "DIARY",
                    "STATUS",
                    "FILED",
                    "SETTINGS",
                ):
                    op, parsed_p = parse_query_input(v_strip)
                    for pk, pv in params.items():
                        if pk not in (k, "target", "action") and pk not in parsed_p:
                            parsed_p[pk] = pv
                    return op, parsed_p

        target = params.pop("target", None) or params.pop("action", None)
        if not target:
            if "query" in params or "content" in params or "text" in params:
                target = "search"
            elif "drawer_id" in params:
                target = "drawer"
            elif "entity" in params:
                target = "kg_query"
            elif "status" in params:
                target = "status"
            elif "taxonomy" in params:
                target = "taxonomy"
            elif "wings" in params:
                target = "wings"
            elif "rooms" in params:
                target = "rooms"
            elif "tunnels" in params:
                target = "list_tunnels"
            else:
                target = "search"

        t_lower = str(target).lower()
        if t_lower in ("search", "find"):
            target = "search"
            if "query" not in params:
                if "content" in params:
                    params["query"] = params.pop("content")
                elif "text" in params:
                    params["query"] = params.pop("text")
        elif t_lower in ("list_tunnels", "tunnels"):
            target = "list_tunnels"
        elif t_lower == "find_tunnels":
            target = "find_tunnels"
        elif t_lower in ("list_drawers", "drawers"):
            target = "drawers"
        elif t_lower in ("list_hallways", "hallways"):
            target = "list_hallways"
        elif t_lower in ("list_wings", "wings"):
            target = "wings"
        elif t_lower in ("list_rooms", "rooms"):
            target = "rooms"
        elif t_lower in ("taxonomy", "get_taxonomy"):
            target = "taxonomy"
        elif t_lower in ("kg_stats", "kg_statistics"):
            target = "kg_stats"
        elif t_lower in ("graph_stats", "stats"):
            target = "graph_stats"
        else:
            target = t_lower

        return target, params

    # If it's a JSON string
    if isinstance(input_data, str) and input_data.strip().startswith("{"):
        try:
            d = json.loads(input_data)
            if isinstance(d, dict):
                return parse_query_input(d)
        except Exception:
            pass

    if not isinstance(input_data, str):
        raise QueryParseError(
            f"Expected string or dict query input, got {type(input_data).__name__}"
        )

    raw_text = input_data.strip()
    if not raw_text:
        return "status", {}

    tokens = tokenize_dsl(raw_text)
    if not tokens:
        return "status", {}

    first_tok = tokens[0].upper()

    # --- System / Status ---
    if first_tok in ("STATUS", "INFO"):
        return "status", {}
    if first_tok in ("FILED", "MEMORIES_FILED", "MEMORIES_FILED_AWAY"):
        return "filed", {}
    if first_tok in ("SETTINGS", "HOOK_SETTINGS"):
        return "settings", {}
    if first_tok in ("AAAK", "AAAK_SPEC", "SPEC"):
        return "aaak_spec", {}

    # --- Taxonomy & Structure ---
    if first_tok == "TAXONOMY":
        wing = None
        if len(tokens) > 2 and tokens[1].upper() == "IN":
            wing = tokens[2]
        elif len(tokens) > 1 and not tokens[1].upper().startswith("IN"):
            wing = tokens[1]
        return "taxonomy", {"wing": wing} if wing else {}

    if first_tok == "WINGS":
        return "wings", {}

    if first_tok == "ROOMS":
        wing = None
        if len(tokens) > 2 and tokens[1].upper() == "IN":
            wing = tokens[2]
        elif len(tokens) > 1:
            wing = tokens[1]
        return "rooms", {"wing": wing} if wing else {}

    if first_tok == "DRAWER":
        if len(tokens) < 2:
            raise QueryParseError("DRAWER requires a drawer_id (e.g. 'DRAWER drw_123')")
        return "drawer", {"drawer_id": tokens[1]}

    if first_tok == "DRAWERS":
        # Parse DRAWERS [IN wing/room] [LIMIT n] [OFFSET n] [SINCE date] [BEFORE date]
        params: Dict[str, Any] = {}
        i = 1
        n = len(tokens)
        while i < n:
            tok = tokens[i].upper()
            if tok == "IN" and i + 1 < n:
                loc = tokens[i + 1]
                if "/" in loc:
                    w, r = loc.split("/", 1)
                    params["wing"] = w
                    params["room"] = r
                else:
                    params["wing"] = loc
                i += 2
            elif tok == "LIMIT" and i + 1 < n:
                params["limit"] = int(tokens[i + 1])
                i += 2
            elif tok == "OFFSET" and i + 1 < n:
                params["offset"] = int(tokens[i + 1])
                i += 2
            elif tok == "SINCE" and i + 1 < n:
                params["since"] = tokens[i + 1]
                i += 2
            elif tok == "BEFORE" and i + 1 < n:
                params["before"] = tokens[i + 1]
                i += 2
            elif ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                params[k.lower()] = _parse_val(v)
                i += 1
            else:
                i += 1
        return "drawers", params

    if first_tok in ("CHECK_DUP", "CHECK_DUPLICATE") or (
        first_tok == "CHECK" and len(tokens) > 1 and tokens[1].upper() in ("DUP", "DUPLICATE")
    ):
        start_idx = 2 if first_tok == "CHECK" else 1
        if len(tokens) <= start_idx:
            raise QueryParseError("CHECK DUP requires content string")
        content = tokens[start_idx]
        threshold = 0.9
        if len(tokens) > start_idx + 2 and tokens[start_idx + 1].upper() == "THRESHOLD":
            threshold = float(tokens[start_idx + 2])
        return "check_duplicate", {"content": content, "threshold": threshold}

    # --- Knowledge Graph ---
    if first_tok == "KG":
        if len(tokens) > 1 and tokens[1].upper() == "STATS":
            return "kg_stats", {}
        if len(tokens) > 1 and tokens[1].upper() == "TIMELINE":
            entity = " ".join(tokens[2:]) if len(tokens) > 2 else None
            return "kg_timeline", {"entity": entity} if entity else {}

        # KG <entity> [AS OF <date>] [DIRECTION <dir>]
        if len(tokens) < 2:
            raise QueryParseError("KG query requires an entity name (e.g. 'KG Max')")
        entity_tokens = []
        i = 1
        n = len(tokens)
        while i < n:
            tok_u = tokens[i].upper()
            if tok_u == "AS" and i + 1 < n and tokens[i + 1].upper() == "OF":
                break
            if tok_u in ("AS_OF", "DIRECTION"):
                break
            entity_tokens.append(tokens[i])
            i += 1
        if not entity_tokens:
            raise QueryParseError("KG query requires an entity name (e.g. 'KG Max')")
        params = {"entity": " ".join(entity_tokens)}
        while i < n:
            tok = tokens[i].upper()
            if tok == "AS" and i + 2 < n and tokens[i + 1].upper() == "OF":
                params["as_of"] = tokens[i + 2]
                i += 3
            elif tok == "AS_OF" and i + 1 < n:
                params["as_of"] = tokens[i + 1]
                i += 2
            elif tok == "DIRECTION" and i + 1 < n:
                params["direction"] = tokens[i + 1].lower()
                i += 2
            elif ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                params[k.lower()] = _parse_val(v)
                i += 1
            else:
                i += 1
        return "kg_query", params

    # --- Graph Navigation & Hallways ---
    if first_tok == "TRAVERSE":
        if len(tokens) < 2:
            raise QueryParseError("TRAVERSE requires start_room (e.g. 'TRAVERSE auth-setup')")
        room = tokens[1]
        hops = 2
        if len(tokens) > 3 and tokens[2].upper() == "HOPS":
            hops = int(tokens[3])
        return "traverse", {"start_room": room, "max_hops": hops}

    if first_tok == "TUNNELS":
        # TUNNELS [BETWEEN wing_a AND wing_b] or [IN wing]
        params = {}
        i = 1
        n = len(tokens)
        while i < n:
            tok = tokens[i].upper()
            if tok == "BETWEEN" and i + 3 < n and tokens[i + 2].upper() == "AND":
                params["wing_a"] = tokens[i + 1]
                params["wing_b"] = tokens[i + 3]
                i += 4
            elif tok == "IN" and i + 1 < n:
                params["wing"] = tokens[i + 1]
                i += 2
            elif ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                params[k.lower()] = _parse_val(v)
                i += 1
            else:
                params["wing"] = tokens[i]
                i += 1
        if "wing_a" in params and "wing_b" in params:
            return "find_tunnels", params
        return "list_tunnels", params

    if first_tok == "FOLLOW":
        if len(tokens) < 2:
            raise QueryParseError("FOLLOW requires wing/room or wing room")
        loc = tokens[1]
        if "/" in loc:
            w, r = loc.split("/", 1)
            return "follow_tunnels", {"wing": w, "room": r}
        elif len(tokens) >= 3:
            return "follow_tunnels", {"wing": tokens[1], "room": tokens[2]}
        raise QueryParseError("FOLLOW requires wing and room")

    if first_tok == "HALLWAYS":
        wing = None
        if len(tokens) > 2 and tokens[1].upper() == "IN":
            wing = tokens[2]
        elif len(tokens) > 1:
            wing = tokens[1]
        return "list_hallways", {"wing": wing} if wing else {}

    if first_tok == "GRAPH" and len(tokens) > 1 and tokens[1].upper() == "STATS":
        return "graph_stats", {}

    # --- Agent Diary ---
    if first_tok == "DIARY":
        if len(tokens) < 2:
            raise QueryParseError("DIARY requires agent_name (e.g. 'DIARY antigravity')")
        agent_name = tokens[1]
        params = {"agent_name": agent_name}
        i = 2
        n = len(tokens)
        while i < n:
            tok = tokens[i].upper()
            if tok in ("LAST", "LIMIT") and i + 1 < n:
                params["last_n"] = int(tokens[i + 1])
                i += 2
            elif tok == "IN" and i + 1 < n:
                params["wing"] = tokens[i + 1]
                i += 2
            elif ":" in tokens[i]:
                k, v = tokens[i].split(":", 1)
                params[k.lower()] = _parse_val(v)
                i += 1
            else:
                i += 1
        return "diary_read", params

    # --- Default: SEARCH / FIND ---
    query_str = ""
    start_i = 0
    if first_tok in ("FIND", "SEARCH"):
        start_i = 1

    search_tokens = []
    params: Dict[str, Any] = {}
    i = start_i
    n = len(tokens)
    while i < n:
        tok = tokens[i]
        tok_upper = tok.upper()
        if tok_upper == "IN" and i + 1 < n:
            loc = tokens[i + 1]
            if "/" in loc:
                w, r = loc.split("/", 1)
                params["wing"] = w
                params["room"] = r
            else:
                params["wing"] = loc
            i += 2
        elif tok_upper == "LIMIT" and i + 1 < n:
            params["limit"] = int(tokens[i + 1])
            i += 2
        elif tok_upper == "SINCE" and i + 1 < n:
            params["since"] = tokens[i + 1]
            i += 2
        elif tok_upper == "BEFORE" and i + 1 < n:
            params["before"] = tokens[i + 1]
            i += 2
        elif tok_upper in ("MAX_DIST", "MAX_DISTANCE") and i + 1 < n:
            params["max_distance"] = float(tokens[i + 1])
            i += 2
        elif tok_upper == "STRATEGY" and i + 1 < n:
            params["candidate_strategy"] = tokens[i + 1].lower()
            i += 2
        elif tok_upper == "SOURCE" and i + 1 < n:
            params["source_file"] = tokens[i + 1]
            i += 2
        elif tok_upper == "CONTEXT" and i + 1 < n:
            params["context"] = tokens[i + 1]
            i += 2
        elif ":" in tok and not tok.startswith("http") and not isinstance(tok, QuotedToken):
            k, v = tok.split(":", 1)
            params[k.lower()] = _parse_val(v)
            i += 1
        else:
            search_tokens.append(tok)
            i += 1

    query_str = " ".join(search_tokens).strip()
    if not query_str and "query" in params:
        query_str = str(params.pop("query"))

    params["query"] = query_str
    return "search", params


# ==============================================================================
# 2. PALACE EXEC PARSER
# ==============================================================================


def parse_exec_input(input_data: Any) -> Tuple[str, Dict[str, Any]]:  # noqa: C901
    """
    Parse input to `palace_exec`.

    Returns:
        (action_operation, parsed_parameters_dict)
    """
    # 1. Structured JSON / Dict input
    if isinstance(input_data, dict):
        params = dict(input_data)
        # Check if there is an embedded command/dsl string in command, input, dsl, pql
        for k in ("command", "input", "dsl", "pql"):
            if k in params and isinstance(params[k], str):
                v_strip = params[k].strip()
                first_w = v_strip.split(None, 1)[0].upper() if v_strip else ""
                if first_w in (
                    "ADD",
                    "UPDATE",
                    "DELETE",
                    "CREATE",
                    "TUNNEL",
                    "HALLWAY",
                    "KG",
                    "DIARY",
                    "MINE",
                    "SYNC",
                    "CHECKPOINT",
                    "RECONNECT",
                    "SETTINGS",
                ):
                    op, parsed_p = parse_exec_input(v_strip)
                    for pk, pv in params.items():
                        if pk not in (k, "action", "target") and pk not in parsed_p:
                            parsed_p[pk] = pv
                    return op, parsed_p

        action = params.pop("action", None) or params.pop("target", None)
        if not action:
            if "drawer_id" in params and (
                "content" in params
                or "document" in params
                or "text" in params
                or "wing" in params
                or "room" in params
            ):
                action = "update_drawer"
            elif "content" in params or "document" in params:
                action = "add_drawer"
            elif "drawer_id" in params:
                action = "delete_drawer"
            elif "subject" in params and "predicate" in params and "old_object" in params:
                action = "kg_supersede"
            elif (
                "subject" in params
                and "predicate" in params
                and ("object" in params or "obj" in params)
            ):
                action = "kg_add"
            elif "source_wing" in params and "target_wing" in params:
                action = "create_tunnel"
            elif "agent" in params and ("entry" in params or "content" in params):
                action = "diary_write"
            elif "items" in params:
                action = "checkpoint"
            else:
                action = "add_drawer"

        action = action.lower()
        if action in ("add", "add_drawer", "create_drawer"):
            action = "add_drawer"
            if "content" not in params:
                if "document" in params:
                    params["content"] = params.pop("document")
                elif "text" in params:
                    params["content"] = params.pop("text")
        elif action in ("kg_add", "add_fact", "add_triple"):
            action = "kg_add"
            if "object" not in params and "obj" in params:
                params["object"] = params.pop("obj")
        elif action in ("kg_invalidate", "invalidate"):
            action = "kg_invalidate"
            if "ended" not in params and "valid_to" in params:
                params["ended"] = params.pop("valid_to")
        elif action in ("diary_write", "diary"):
            action = "diary_write"
            if "diary" in params and isinstance(params["diary"], dict):
                d = params.pop("diary")
                params["entry"] = d.get("content") or d.get("entry") or d.get("text") or ""
                if "topic" in d:
                    params["topic"] = d["topic"]
                if "wing" in d:
                    params["wing"] = d["wing"]
            elif "entry" not in params and "content" in params:
                params["entry"] = params.pop("content")
        return action, params

    if isinstance(input_data, str) and input_data.strip().startswith("{"):
        try:
            d = json.loads(input_data)
            if isinstance(d, dict):
                return parse_exec_input(d)
        except Exception:
            pass

    if not isinstance(input_data, str):
        raise QueryParseError(
            f"Expected string or dict exec input, got {type(input_data).__name__}"
        )

    raw_text = input_data.strip()
    if not raw_text:
        raise QueryParseError("Empty exec command")

    tokens = tokenize_dsl(raw_text)
    if not tokens:
        raise QueryParseError("Empty exec command")

    first_tok = tokens[0].upper()

    # --- System / Maintenance ---
    if first_tok == "RECONNECT":
        return "reconnect", {}

    if first_tok == "SETTINGS":
        kv = _parse_key_value_tokens(tokens[1:])
        return "hook_settings", kv

    # --- Checkpoint ---
    if first_tok == "CHECKPOINT":
        payload_str = raw_text[len("CHECKPOINT") :].strip()
        if payload_str.startswith("{"):
            try:
                d = json.loads(payload_str)
                return "checkpoint", d
            except Exception as e:
                raise QueryParseError(f"Invalid JSON in CHECKPOINT: {e}")
        raise QueryParseError("CHECKPOINT requires JSON payload {items: [...], diary?: {...}}")

    # --- Mining & Sync ---
    if first_tok == "MINE":
        if len(tokens) < 2:
            raise QueryParseError("MINE requires a source path (e.g. 'MINE /path/to/repo')")
        source = tokens[1]
        kv = _parse_key_value_tokens(tokens[2:])
        kv["source"] = source
        return "mine", kv

    if first_tok == "SYNC":
        kv = _parse_key_value_tokens(tokens[1:])
        if "apply" in kv:
            kv["apply"] = bool(kv["apply"])
        return "sync", kv

    # --- Drawer CRUD ---
    if first_tok == "ADD":
        # ADD [IN wing/room] "verbatim content" [SOURCE file] [ADDED_BY agent]
        params: Dict[str, Any] = {}
        content_tokens = []
        i = 1
        n = len(tokens)
        while i < n:
            tok = tokens[i]
            tok_upper = tok.upper()
            if tok_upper == "IN" and i + 1 < n:
                loc = tokens[i + 1]
                if "/" in loc:
                    w, r = loc.split("/", 1)
                    params["wing"] = w
                    params["room"] = r
                else:
                    params["wing"] = loc
                i += 2
            elif tok_upper == "SOURCE" and i + 1 < n:
                params["source_file"] = tokens[i + 1]
                i += 2
            elif tok_upper == "ADDED_BY" and i + 1 < n:
                params["added_by"] = tokens[i + 1]
                i += 2
            elif ":" in tok and not isinstance(tok, QuotedToken):
                k, v = tok.split(":", 1)
                params[k.lower()] = _parse_val(v)
                i += 1
            else:
                content_tokens.append(tok)
                i += 1

        if content_tokens and "content" not in params:
            params["content"] = " ".join(content_tokens)

        if not params.get("content"):
            raise QueryParseError("ADD requires content to store")
        if not params.get("wing") or not params.get("room"):
            raise QueryParseError(
                "ADD requires wing and room (e.g. 'ADD IN backend/auth \"content\"')"
            )
        return "add_drawer", params

    if first_tok == "UPDATE":
        # UPDATE drawer_id [CONTENT "..."] [WING "..."] [ROOM "..."]
        if len(tokens) < 2:
            raise QueryParseError("UPDATE requires drawer_id")
        drawer_id = tokens[1]
        kv = _parse_key_value_tokens(tokens[2:])
        kv["drawer_id"] = drawer_id
        return "update_drawer", kv

    if first_tok == "DELETE":
        if len(tokens) < 2:
            raise QueryParseError("DELETE requires DRAWER, SOURCE, TUNNEL, or HALLWAY")
        sub_cmd = tokens[1].upper()
        if sub_cmd == "DRAWER":
            if len(tokens) < 3:
                raise QueryParseError("DELETE DRAWER requires drawer_id")
            return "delete_drawer", {"drawer_id": tokens[2]}
        if sub_cmd == "SOURCE":
            if len(tokens) < 3:
                raise QueryParseError("DELETE SOURCE requires source_file path")
            source_file = tokens[2]
            dry_run = True
            if len(tokens) > 3 and tokens[3].upper() in ("COMMIT", "APPLY"):
                dry_run = False
            return "delete_by_source", {"source_file": source_file, "dry_run": dry_run}
        if sub_cmd == "TUNNEL":
            if len(tokens) < 3:
                raise QueryParseError("DELETE TUNNEL requires tunnel_id")
            return "delete_tunnel", {"tunnel_id": tokens[2]}
        if sub_cmd == "HALLWAY":
            if len(tokens) < 3:
                raise QueryParseError("DELETE HALLWAY requires hallway_id")
            return "delete_hallway", {"hallway_id": tokens[2]}
        # Fallback: if token 1 is a drawer ID
        return "delete_drawer", {"drawer_id": tokens[1]}

    # --- Knowledge Graph Lifecycle ---
    if first_tok == "KG":
        if len(tokens) < 2:
            raise QueryParseError("KG requires ADD, INVALIDATE, or SUPERSEDE")
        kg_op = tokens[1].upper()

        if kg_op == "ADD":
            # KG ADD subject -> predicate -> object [FROM date] [TO date] [CLOSET id] [DRAWER id]
            remaining = tokens[2:]
            arrows = [idx for idx, t in enumerate(remaining) if t == "->"]
            if len(arrows) >= 2:
                subject = " ".join(remaining[: arrows[0]])
                predicate = " ".join(remaining[arrows[0] + 1 : arrows[1]])
                obj_and_flags = remaining[arrows[1] + 1 :]
            elif len(remaining) >= 3:
                subject = remaining[0]
                predicate = remaining[1]
                obj_and_flags = remaining[2:]
            else:
                raise QueryParseError(
                    "KG ADD requires 'subject -> predicate -> object' or 'subject predicate object'"
                )

            obj_tokens, flag_tokens = _split_object_and_flags(obj_and_flags, _KG_ADD_FLAGS)
            obj = " ".join(obj_tokens)
            kv = _parse_key_value_tokens(flag_tokens)
            params = {"subject": subject, "predicate": predicate, "object": obj}
            if "from" in kv:
                params["valid_from"] = kv["from"]
            if "valid_from" in kv:
                params["valid_from"] = kv["valid_from"]
            if "to" in kv:
                params["valid_to"] = kv["to"]
            if "valid_to" in kv:
                params["valid_to"] = kv["valid_to"]
            if "closet" in kv:
                params["source_closet"] = kv["closet"]
            if "drawer" in kv:
                params["source_drawer_id"] = kv["drawer"]
            return "kg_add", params

        if kg_op in ("INVALIDATE", "DELETE"):
            # KG INVALIDATE subject -> predicate -> object [ENDED date]
            remaining = tokens[2:]
            arrows = [idx for idx, t in enumerate(remaining) if t == "->"]
            if len(arrows) >= 2:
                subject = " ".join(remaining[: arrows[0]])
                predicate = " ".join(remaining[arrows[0] + 1 : arrows[1]])
                obj_and_flags = remaining[arrows[1] + 1 :]
            elif len(remaining) >= 3:
                subject = remaining[0]
                predicate = remaining[1]
                obj_and_flags = remaining[2:]
            else:
                raise QueryParseError(
                    "KG INVALIDATE requires 'subject -> predicate -> object' or 'subject predicate object'"
                )

            obj_tokens, flag_tokens = _split_object_and_flags(obj_and_flags, _KG_INVALIDATE_FLAGS)
            obj = " ".join(obj_tokens)
            kv = _parse_key_value_tokens(flag_tokens)
            params = {"subject": subject, "predicate": predicate, "object": obj}
            if "ended" in kv:
                params["ended"] = kv["ended"]
            return "kg_invalidate", params

        if kg_op == "SUPERSEDE":
            # KG SUPERSEDE subject -> predicate: old_val => new_val [AT date]
            remaining = tokens[2:]
            params = _parse_kg_supersede_tokens(remaining)
            return "kg_supersede", params

    # --- Graph & Tunnels ---
    if first_tok in ("TUNNEL", "CREATE") and len(tokens) > 1:
        if first_tok == "CREATE" and tokens[1].upper() == "TUNNEL":
            remaining = tokens[2:]
            sub = "CREATE"
        elif first_tok == "TUNNEL" and tokens[1].upper() in ("CREATE", "DELETE"):
            sub = tokens[1].upper()
            remaining = tokens[2:]
        else:
            sub = None
            remaining = []

        if sub == "CREATE":
            # Supports both 'src -> tgt' and 'FROM src TO tgt'
            sw, sr, tw, tr = None, None, None, None
            label = None
            if "->" in remaining:
                arrow_idx = remaining.index("->")
                src = remaining[arrow_idx - 1]
                tgt = remaining[arrow_idx + 1]
                if "/" in src and "/" in tgt:
                    sw, sr = src.split("/", 1)
                    tw, tr = tgt.split("/", 1)
                kv = _parse_key_value_tokens(remaining[arrow_idx + 2 :])
                if "label" in kv:
                    label = kv["label"]
            else:
                kv = _parse_key_value_tokens(remaining)
                if "from" in kv and "to" in kv:
                    src = kv["from"]
                    tgt = kv["to"]
                    if "/" in src and "/" in tgt:
                        sw, sr = src.split("/", 1)
                        tw, tr = tgt.split("/", 1)
                if "label" in kv:
                    label = kv["label"]

            if not sw or not sr or not tw or not tr:
                # Check positional tokens without -> or from/to: CREATE TUNNEL src dst [label]
                if len(remaining) >= 2 and "/" in remaining[0] and "/" in remaining[1]:
                    sw, sr = remaining[0].split("/", 1)
                    tw, tr = remaining[1].split("/", 1)
                    if len(remaining) > 2:
                        label = " ".join(remaining[2:]).strip("'\"")

            if not sw or not sr or not tw or not tr:
                raise QueryParseError(
                    "TUNNEL CREATE requires 'src_wing/src_room -> tgt_wing/tgt_room' or 'FROM src_wing/src_room TO tgt_wing/tgt_room'"
                )

            params = {"source_wing": sw, "source_room": sr, "target_wing": tw, "target_room": tr}
            if label:
                params["label"] = label
            return "create_tunnel", params

        if sub == "DELETE":
            if len(remaining) < 1:
                raise QueryParseError("TUNNEL DELETE requires tunnel_id")
            return "delete_tunnel", {"tunnel_id": remaining[0]}

    if first_tok == "HALLWAY" and len(tokens) > 1 and tokens[1].upper() == "DELETE":
        if len(tokens) < 3:
            raise QueryParseError("HALLWAY DELETE requires hallway_id")
        return "delete_hallway", {"hallway_id": tokens[2]}

    # --- Agent Diary ---
    if first_tok == "DIARY":
        if len(tokens) > 1:
            if tokens[1].upper() in ("WRITE", "ADD"):
                start_arg_idx = 2
            else:
                start_arg_idx = 1

            if start_arg_idx >= len(tokens):
                raise QueryParseError("DIARY requires agent_name")

            agent_tok = tokens[start_arg_idx]
            if agent_tok.upper() == "AGENT" and len(tokens) > start_arg_idx + 1:
                agent_name = tokens[start_arg_idx + 1]
                start_arg_idx = start_arg_idx + 2
            elif ":" in agent_tok and agent_tok.lower().startswith("agent:"):
                agent_name = agent_tok.split(":", 1)[1]
                start_arg_idx = start_arg_idx + 1
            else:
                agent_name = agent_tok
                start_arg_idx = start_arg_idx + 1

            # Strip any surrounding quotes or prefix
            agent_name = agent_name.strip("'\"")

            remaining = tokens[start_arg_idx:]
            topic = None
            wing = None
            entry_tokens = []
            i = 0
            n = len(remaining)
            while i < n:
                tok = remaining[i]
                tok_upper = tok.upper()
                if tok_upper == "TOPIC" and i + 1 < n:
                    topic = remaining[i + 1]
                    i += 2
                elif tok_upper == "WING" and i + 1 < n:
                    wing = remaining[i + 1]
                    i += 2
                elif ":" in tok and not tok.startswith("http") and not isinstance(tok, QuotedToken):
                    k, v = tok.split(":", 1)
                    if k.lower() == "topic":
                        topic = v
                    elif k.lower() == "wing":
                        wing = v
                    else:
                        entry_tokens.append(tok)
                    i += 1
                else:
                    entry_tokens.append(tok)
                    i += 1

            entry = " ".join(entry_tokens)
            params = {"agent_name": agent_name, "entry": entry}
            if topic:
                params["topic"] = topic
            if wing:
                params["wing"] = wing
            return "diary_write", params

    raise QueryParseError(f"Unrecognized palace_exec action or command: '{first_tok}'")


def _parse_kg_supersede_tokens(tokens: List[str]) -> Dict[str, Any]:
    """Helper to parse KG SUPERSEDE tokens."""
    if "=>" not in tokens:
        raise QueryParseError("KG SUPERSEDE requires 'old_val => new_val'")

    double_arrow = tokens.index("=>")
    before_arrow = tokens[:double_arrow]
    after_arrow = tokens[double_arrow + 1 :]

    if "->" in before_arrow:
        arrow_idx = before_arrow.index("->")
        subject = " ".join(before_arrow[:arrow_idx])
        pred_and_old = before_arrow[arrow_idx + 1 :]
        if ":" in pred_and_old:
            colon_idx = pred_and_old.index(":")
            predicate = " ".join(pred_and_old[:colon_idx])
            old_val = " ".join(pred_and_old[colon_idx + 1 :])
        elif len(pred_and_old) >= 2:
            predicate = pred_and_old[0]
            old_val = " ".join(pred_and_old[1:])
        else:
            raise QueryParseError("KG SUPERSEDE requires predicate and old_value")
    else:
        if len(before_arrow) < 3:
            raise QueryParseError("KG SUPERSEDE requires subject, predicate, and old_value")
        subject = before_arrow[0]
        predicate = before_arrow[1]
        old_val = " ".join(before_arrow[2:])

    new_val_tokens, flag_tokens = _split_object_and_flags(after_arrow, _KG_SUPERSEDE_FLAGS)
    new_val = " ".join(new_val_tokens)
    kv = _parse_key_value_tokens(flag_tokens)
    params = {
        "subject": subject,
        "predicate": predicate,
        "old_object": old_val,
        "new_object": new_val,
    }
    if "at" in kv:
        params["at"] = kv["at"]
    return params


# ==============================================================================
# 3. PALACE COORDINATE PARSER
# ==============================================================================


def parse_coordinate_input(input_data: Any) -> Tuple[str, Dict[str, Any]]:  # noqa: C901
    """
    Parse input to `palace_coordinate`.

    Returns:
        (coord_action, parsed_parameters_dict)
    """
    # 1. Structured JSON / Dict input
    if isinstance(input_data, dict):
        params = dict(input_data)
        for k in ("command", "input", "dsl", "pql"):
            if k in params and isinstance(params[k], str):
                v_strip = params[k].strip()
                first_w = v_strip.split(None, 1)[0].upper() if v_strip else ""
                if first_w in (
                    "TASK",
                    "EVENT",
                    "EVENTS",
                    "LOGSTREAM",
                    "ARTIFACT",
                    "PATCH",
                    "PEERS",
                    "MESH",
                ):
                    op, parsed_p = parse_coordinate_input(v_strip)
                    for pk, pv in params.items():
                        if pk not in (k, "action", "target") and pk not in parsed_p:
                            parsed_p[pk] = pv
                    return op, parsed_p
        action = params.pop("action", None) or params.pop("target", None)
        if not action:
            if "goal" in params or "branch" in params or "base_commit" in params:
                action = "task_create"
            elif "event_type" in params or (
                "stream" in params and "room" in params and "type" in params
            ):
                action = "event_append"
            elif "artifact_id" in params:
                action = "artifact_get"
            elif "kind" in params and "content" in params and "created_by" in params:
                action = "artifact_put"
            elif "diff" in params:
                action = "patch_submit"
            elif "peers" in params or "mesh" in params:
                action = "mesh_peers"
            else:
                action = "event_list"

        action = action.lower()
        if action in ("task", "create_task", "task_create"):
            action = "task_create"
        elif action in ("event", "append_event", "event_append", "emit"):
            action = "event_append"
        elif action in ("events", "list_events", "event_list", "logstream"):
            action = "event_list"
        elif action in ("ack", "event_ack"):
            action = "event_ack"

        # Alias cleanups
        if "from" in params and "from_agent" not in params:
            params["from_agent"] = params.pop("from")
        elif "from" in params:
            params.pop("from")
        if "to" in params and "to_agent" not in params:
            params["to_agent"] = params.pop("to")
        elif "to" in params:
            params.pop("to")
        if "base" in params and "base_commit" not in params:
            params["base_commit"] = params.pop("base")
        elif "base" in params:
            params.pop("base")
        if "id" in params and "event_id" not in params and action == "event_ack":
            params["event_id"] = params.pop("id")
        if action == "event_append" and "type" not in params and "event_type" in params:
            params["type"] = params.pop("event_type")
        elif "event_type" in params:
            params.pop("event_type")
        if action == "patch_submit" and "content" not in params and "diff" in params:
            params["content"] = params.pop("diff")
        elif "diff" in params:
            params.pop("diff")
        return action, params

    if isinstance(input_data, str) and input_data.strip().startswith("{"):
        try:
            d = json.loads(input_data)
            if isinstance(d, dict):
                return parse_coordinate_input(d)
        except Exception:
            pass

    if not isinstance(input_data, str):
        raise QueryParseError(
            f"Expected string or dict coordinate input, got {type(input_data).__name__}"
        )

    raw_text = input_data.strip()
    if not raw_text:
        return "event_list", {}

    tokens = tokenize_dsl(raw_text)
    if not tokens:
        return "event_list", {}

    first_tok = tokens[0].upper()

    # --- Mesh Peers ---
    if first_tok in ("PEERS", "MESH", "MESH_PEERS") or (
        first_tok == "MESH" and len(tokens) > 1 and tokens[1].upper() == "PEERS"
    ):
        return "mesh_peers", {}

    # --- Task Creation ---
    if first_tok == "TASK" and len(tokens) > 1 and tokens[1].upper() in ("CREATE", "REQUEST"):
        kv = _parse_key_value_tokens(tokens[2:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "to" in kv and "to_agent" not in kv:
            kv["to_agent"] = kv.pop("to")
        elif "to" in kv:
            kv.pop("to")
        if "base" in kv and "base_commit" not in kv:
            kv["base_commit"] = kv.pop("base")
        elif "base" in kv:
            kv.pop("base")

        for req in ("project", "from_agent", "to_agent", "goal", "branch", "base_commit", "done"):
            if req not in kv:
                raise QueryParseError(f"TASK CREATE missing required field: '{req}'")
        return "task_create", kv

    # --- Event Append ---
    if first_tok == "EVENT" and len(tokens) > 1 and tokens[1].upper() in ("APPEND", "EMIT", "POST"):
        kv = _parse_key_value_tokens(tokens[2:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "to" in kv and "to_agent" not in kv:
            kv["to_agent"] = kv.pop("to")
        elif "to" in kv:
            kv.pop("to")
        if "base" in kv and "base_commit" not in kv:
            kv["base_commit"] = kv.pop("base")
        elif "base" in kv:
            kv.pop("base")
        for req in ("type", "stream", "room", "from_agent"):
            if req not in kv:
                raise QueryParseError(f"EVENT APPEND missing required field: '{req}'")
        return "event_append", kv

    # --- Event List ---
    if first_tok in ("LOGSTREAM", "EVENTS") or (
        first_tok == "EVENT" and len(tokens) > 1 and tokens[1].upper() in ("LIST", "FIND")
    ):
        start_idx = 2 if first_tok == "EVENT" else 1
        kv = _parse_key_value_tokens(tokens[start_idx:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "to" in kv and "to_agent" not in kv:
            kv["to_agent"] = kv.pop("to")
        elif "to" in kv:
            kv.pop("to")
        if "since_id" in kv and "since_event_id" not in kv:
            kv["since_event_id"] = str(kv.pop("since_id"))
        elif "since" in kv and "since_event_id" not in kv and "since_created_at" not in kv:
            raw_since = str(kv.pop("since"))
            if _ISO_DATE_RE.match(raw_since):
                kv["since_created_at"] = raw_since
            elif raw_since.startswith("evt_"):
                kv["since_event_id"] = raw_since
            else:
                raise QueryParseError(
                    "EVENT LIST since: is ambiguous; use since_id:<event_id> or a YYYY-MM-DD timestamp"
                )
        if "before_id" in kv and "before_event_id" not in kv:
            kv["before_event_id"] = str(kv.pop("before_id"))
        elif "before" in kv and "before_event_id" not in kv:
            raw_before = str(kv.pop("before"))
            if raw_before.startswith("evt_"):
                kv["before_event_id"] = raw_before
            else:
                raise QueryParseError(
                    "EVENT LIST before: is not a resume cursor; use before_id:<event_id>"
                )
        if "correlation" in kv and "correlation_id" not in kv:
            kv["correlation_id"] = str(kv.pop("correlation"))
        return "event_list", kv

    # --- Event Wait ---
    if first_tok == "EVENT" and len(tokens) > 1 and tokens[1].upper() == "WAIT":
        kv = _parse_key_value_tokens(tokens[2:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "to" in kv and "to_agent" not in kv:
            kv["to_agent"] = kv.pop("to")
        elif "to" in kv:
            kv.pop("to")
        if "since_id" in kv and "since_event_id" not in kv:
            kv["since_event_id"] = str(kv.pop("since_id"))
        elif "since" in kv and "since_event_id" not in kv:
            kv["since_event_id"] = str(kv.pop("since"))
        if "correlation" in kv and "correlation_id" not in kv:
            kv["correlation_id"] = str(kv.pop("correlation"))
        if "timeout" in kv and "timeout_ms" not in kv:
            kv["timeout_ms"] = int(kv.pop("timeout"))
        return "event_wait", kv

    # --- Event Ack ---
    if first_tok == "EVENT" and len(tokens) > 1 and tokens[1].upper() == "ACK":
        kv = _parse_key_value_tokens(tokens[2:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "id" in kv and "event_id" not in kv:
            kv["event_id"] = str(kv.pop("id"))
        elif "id" in kv:
            kv.pop("id")
        if "event_id" not in kv or "from_agent" not in kv:
            raise QueryParseError("EVENT ACK requires 'event_id' and 'from_agent'")
        return "event_ack", kv

    # --- Artifact Put & Get ---
    if first_tok == "ARTIFACT":
        if len(tokens) < 2:
            raise QueryParseError("ARTIFACT requires PUT or GET (or artifact_id)")
        sub = tokens[1].upper()
        if sub in ("PUT", "ADD"):
            kv = _parse_key_value_tokens(tokens[2:])
            if "by" in kv and "created_by" not in kv:
                kv["created_by"] = kv.pop("by")
            elif "by" in kv:
                kv.pop("by")
            for req in ("kind", "content", "created_by"):
                if req not in kv:
                    raise QueryParseError(f"ARTIFACT PUT missing required field: '{req}'")
            return "artifact_put", kv
        if sub == "GET":
            if len(tokens) < 3:
                raise QueryParseError("ARTIFACT GET requires artifact_id")
            return "artifact_get", {"artifact_id": tokens[2]}
        return "artifact_get", {"artifact_id": tokens[1]}

    # --- Patch Submit ---
    if first_tok == "PATCH" and len(tokens) > 1 and tokens[1].upper() in ("SUBMIT", "READY"):
        kv = _parse_key_value_tokens(tokens[2:])
        if "from" in kv and "from_agent" not in kv:
            kv["from_agent"] = kv.pop("from")
        elif "from" in kv:
            kv.pop("from")
        if "to" in kv and "to_agent" not in kv:
            kv["to_agent"] = kv.pop("to")
        elif "to" in kv:
            kv.pop("to")
        if "diff" in kv and "content" not in kv:
            kv["content"] = kv.pop("diff")
        elif "diff" in kv:
            kv.pop("diff")
        if "base" in kv and "base_commit" not in kv:
            kv["base_commit"] = kv.pop("base")
        elif "base" in kv:
            kv.pop("base")
        for req in ("content", "from_agent", "stream"):
            if req not in kv:
                raise QueryParseError(f"PATCH SUBMIT missing required field: '{req}'")
        return "patch_submit", kv

    raise QueryParseError(f"Unrecognized palace_coordinate command: '{first_tok}'")
