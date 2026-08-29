"""Unit tests: tool registry, unified protocol, extension, truncation."""

from __future__ import annotations

import sys

import pytest

from minicode.tools import (
    BashTool,
    EditTool,
    ReadTool,
    ToolContext,
    ToolError,
    ToolRegistry,
    ToolResult,
    WriteTool,
    build_default_registry,
    truncate_output,
    validate_args,
)
from minicode.tools.base import BaseTool, SchemaError

pytestmark = pytest.mark.unit


class EchoTool(BaseTool):
    name = "echo"
    permission = "read"
    description = "Echo back a message."
    parameters = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
    }

    def run(self, args, ctx):
        return ToolResult(title="echo", output=args["message"], metadata={"len": len(args["message"])})


def test_register_and_lookup():
    registry = ToolRegistry([EchoTool()])
    assert "echo" in registry
    assert registry.get("echo").name == "echo"
    assert registry.names() == ["echo"]
    assert len(registry) == 1


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(ValueError, match="already registered"):
        registry.register(EchoTool())
    registry.register(EchoTool(), replace=True)
    assert len(registry) == 1


def test_unknown_tool_returns_structured_error():
    registry = ToolRegistry()
    result = registry.execute("nope", {}, ToolContext(cwd="."))
    assert not result.ok
    assert result.error.code == "unknown_tool"
    assert "nope" in result.error.message


def test_schemas_are_exported_for_the_model():
    registry = build_default_registry()
    schemas = registry.schemas()
    names = {schema["name"] for schema in schemas}
    assert {"read", "write", "edit", "apply_patch", "glob", "grep", "bash"} <= names
    for schema in schemas:
        assert schema["description"]
        assert schema["parameters"]["type"] == "object"


def test_registry_execute_never_raises(project):
    registry = build_default_registry(cwd=str(project))
    result = registry.execute("read", {"file_path": "does/not/exist.py"}, ToolContext(cwd=str(project)))
    assert not result.ok
    assert result.error.code == "file_not_found"


def test_subset_and_describe():
    registry = build_default_registry()
    subset = registry.subset(["read", "grep"])
    assert set(subset.names()) == {"read", "grep"}
    assert "- read:" in registry.describe()


def test_extension_via_module(monkeypatch, tmp_path):
    module = tmp_path / "mytools.py"
    module.write_text(
        "from minicode.tools.base import BaseTool, ToolResult\n"
        "class ShoutTool(BaseTool):\n"
        "    name = 'shout'\n"
        "    permission = 'read'\n"
        "    description = 'Shout.'\n"
        "    parameters = {'type': 'object', 'properties': {'text': {'type': 'string'}}, 'required': ['text']}\n"
        "    def run(self, args, ctx):\n"
        "        return ToolResult(title='shout', output=args['text'].upper())\n"
        "def register_tools(registry):\n"
        "    registry.register(ShoutTool())\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    registry = build_default_registry()
    registry.load_module("mytools")
    result = registry.execute("shout", {"text": "hi"}, ToolContext(cwd="."))
    assert result.ok
    assert result.output == "HI"


def test_validate_args_required_and_types():
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    assert validate_args({"n": "5"}, schema)["n"] == 5  # coerced
    with pytest.raises(SchemaError, match="missing required"):
        validate_args({}, schema)
    with pytest.raises(SchemaError, match="must be of type"):
        validate_args({"n": "abc"}, schema)


def test_validate_args_enum():
    schema = {"type": "object", "properties": {"mode": {"type": "string", "enum": ["a", "b"]}}, "required": []}
    assert validate_args({"mode": "a"}, schema) == {"mode": "a"}
    with pytest.raises(SchemaError, match="must be one of"):
        validate_args({"mode": "c"}, schema)


def test_invalid_arguments_become_structured_error(project):
    registry = build_default_registry(cwd=str(project))
    result = registry.execute("read", {}, ToolContext(cwd=str(project)))
    assert result.error.code == "invalid_arguments"


def test_truncate_output_saves_full_text(tmp_path, monkeypatch):
    monkeypatch.setenv("MINICODE_DATA_DIR", str(tmp_path / "data"))
    text = "\n".join(f"line {i}" for i in range(500))
    result = truncate_output(text, max_lines=10, max_bytes=10_000)
    assert result.truncated
    assert result.removed > 0
    assert "truncated" in result.content
    assert result.output_path is not None
    from pathlib import Path

    assert Path(result.output_path).read_text(encoding="utf-8") == text


def test_truncate_output_keeps_short_text():
    result = truncate_output("hello", max_lines=10, max_bytes=100)
    assert not result.truncated
    assert result.content == "hello"
    assert result.output_path is None


def test_truncate_output_tail_direction():
    text = "\n".join(str(i) for i in range(100))
    result = truncate_output(text, max_lines=5, max_bytes=10_000, direction="tail")
    assert result.truncated
    assert result.content.rstrip().endswith("99")


def test_tool_error_renders_hint():
    error = ToolError(code="boom", message="it broke", hint="try again")
    rendered = error.render()
    assert "code='boom'" in rendered
    assert "try again" in rendered


def test_bash_tool_reports_nonzero_exit_as_error(project):
    tool = BashTool(default_timeout=30, cwd=str(project))
    result = tool.execute(
        {"command": f'"{sys.executable}" -c "import sys; sys.exit(3)"'}, ToolContext(cwd=str(project))
    )
    assert not result.ok
    assert result.error.code == "nonzero_exit"
    assert result.metadata["returncode"] == 3


def test_bash_tool_captures_stdout(project):
    tool = BashTool(default_timeout=30, cwd=str(project))
    result = tool.execute({"command": f'"{sys.executable}" -c "print(6*7)"'}, ToolContext(cwd=str(project)))
    assert result.ok
    assert "42" in result.output


def test_tool_classes_expose_permissions():
    assert ReadTool().permission == "read"
    assert WriteTool().permission == "write"
    assert EditTool().permission == "edit"
    assert BashTool().permission == "bash"
