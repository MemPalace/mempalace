import inspect
import os

from mempalace.mcp_http_server import _annotation_for_property, _make_tool_wrapper


def test_annotation_for_property_maps_basic_json_schema_types():
    assert _annotation_for_property({"type": "string"}, required=True) is str
    assert _annotation_for_property({"type": "integer"}, required=True) is int
    assert _annotation_for_property({"type": "number"}, required=True) is float
    assert _annotation_for_property({"type": "boolean"}, required=True) is bool
    assert _annotation_for_property({"type": "object"}, required=True) is dict
    assert _annotation_for_property({"type": "array"}, required=True) is list


def test_make_tool_wrapper_preserves_schema_shape_and_calls_handler():
    captured = {}

    def handler(query, limit=5, wing=None):
        captured["query"] = query
        captured["limit"] = limit
        captured["wing"] = wing
        return {"ok": True}

    wrapper = _make_tool_wrapper(
        tool_name="mempalace_search",
        description="Search the palace",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
                "wing": {"type": "string"},
            },
            "required": ["query"],
        },
        handler=handler,
    )

    signature = inspect.signature(wrapper)
    assert list(signature.parameters) == ["query", "limit", "wing"]
    assert signature.parameters["query"].default is inspect.Parameter.empty
    assert signature.parameters["limit"].default is None
    assert signature.return_annotation is not inspect.Signature.empty
    assert wrapper.__doc__ == "Search the palace"

    result = __import__("asyncio").run(wrapper(query="JWT", limit=3))

    assert result == {"ok": True}
    assert captured == {"query": "JWT", "limit": 3, "wing": None}


def test_load_tool_registry_disables_stdio_redirect_before_import():
    import mempalace.mcp_http_server as http_mod

    os.environ.pop("MEMPALACE_DISABLE_STDIO_REDIRECT", None)
    http_mod._load_tool_registry("/tmp/palace")

    assert os.environ["MEMPALACE_DISABLE_STDIO_REDIRECT"] == "1"
    assert os.environ["MEMPALACE_PALACE_PATH"] == "/tmp/palace"
