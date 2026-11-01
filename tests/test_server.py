"""Exercise the server through a real MCP client, connected in-process."""
import pytest
from mcp import Client

from notes_mcp.server import build_server
from notes_mcp.store import NoteStore


@pytest.fixture
def server():
    return build_server(NoteStore())


async def test_lists_all_tools_with_safety_annotations(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {"add_note", "search_notes", "get_note", "update_note", "delete_note", "list_tags"}
    assert tools["search_notes"].annotations.read_only_hint is True
    assert tools["delete_note"].annotations.destructive_hint is True
    assert tools["add_note"].annotations.read_only_hint is False


async def test_add_search_get_flow(server):
    async with Client(server) as client:
        args = {"title": "Vector DBs", "body": "pgvector vs qdrant", "tags": ["ai"]}
        added = await client.call_tool("add_note", args)
        note_id = added.structured_content["id"]

        found = await client.call_tool("search_notes", {"query": "qdrant"})
        assert found.structured_content["result"][0]["id"] == note_id

        fetched = await client.call_tool("get_note", {"note_id": note_id})
        assert fetched.structured_content["body"] == "pgvector vs qdrant"


async def test_expected_errors_reach_the_model_with_their_message(server):
    async with Client(server) as client:
        result = await client.call_tool("get_note", {"note_id": 404})
    assert result.is_error
    assert "note 404 does not exist" in result.content[0].text


async def test_invalid_input_is_reported_not_crashed(server):
    async with Client(server) as client:
        result = await client.call_tool("add_note", {"title": " ", "body": "x"})
    assert result.is_error and "title must not be empty" in result.content[0].text


async def test_delete_then_get_fails(server):
    async with Client(server) as client:
        note_id = (await client.call_tool("add_note", {"title": "tmp", "body": "x"})).structured_content["id"]
        assert not (await client.call_tool("delete_note", {"note_id": note_id})).is_error
        assert (await client.call_tool("get_note", {"note_id": note_id})).is_error


async def test_resources(server):
    async with Client(server) as client:
        empty = await client.read_resource("notes://recent")
        assert "No notes yet" in empty.contents[0].text
        note_id = (await client.call_tool("add_note", {"title": "Hello", "body": "World", "tags": ["x"]})
                   ).structured_content["id"]
        note = await client.read_resource(f"notes://{note_id}")
        assert note.contents[0].text.startswith("# Hello")
        recent = await client.read_resource("notes://recent")
        assert f"#{note_id} **Hello**" in recent.contents[0].text


async def test_list_tags(server):
    async with Client(server) as client:
        await client.call_tool("add_note", {"title": "a", "body": "x", "tags": ["python", "ai"]})
        await client.call_tool("add_note", {"title": "b", "body": "y", "tags": ["python"]})
        tags = (await client.call_tool("list_tags", {})).structured_content
    assert tags == {"python": 2, "ai": 1}
