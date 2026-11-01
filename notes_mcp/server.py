"""MCP tools and resources on top of NoteStore.

Run over stdio (what Claude Desktop and most clients expect):

    NOTES_DB=~/notes.db python -m notes_mcp.server
"""
from __future__ import annotations

import os
from dataclasses import asdict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from notes_mcp.store import InvalidNote, NoteNotFound, NoteStore

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False)

INSTRUCTIONS = (
    "A personal notebook. Search before adding, to avoid duplicates. "
    "Use tags for topics (lowercase, e.g. 'python', 'meeting-notes'). "
    "Ask the user before deleting a note."
)


class NoteOut(BaseModel):
    id: int
    title: str
    body: str
    tags: list[str]
    created_at: str
    updated_at: str


class HitOut(BaseModel):
    id: int
    title: str
    snippet: str
    tags: list[str]
    score: float


class Deleted(BaseModel):
    deleted_id: int


def build_server(store: NoteStore) -> MCPServer:
    server = MCPServer("notes", instructions=INSTRUCTIONS)

    def _guard(fn, *args, **kwargs):
        """Translate expected failures into ToolError so the model sees a useful message.
        Anything unexpected is left to the SDK, which hides internals from the client."""
        try:
            return fn(*args, **kwargs)
        except (NoteNotFound, InvalidNote) as err:
            raise ToolError(str(err)) from err

    @server.tool(annotations=WRITE)
    def add_note(title: str, body: str, tags: list[str] | None = None) -> NoteOut:
        """Create a note. Tags are optional lowercase labels used for filtering."""
        return NoteOut(**asdict(_guard(store.add, title, body, tags)))

    @server.tool(annotations=READ_ONLY)
    def search_notes(query: str, tag: str | None = None, limit: int = 10) -> list[HitOut]:
        """Full-text search over titles, bodies and tags, best match first.
        Matching words are wrapped in [brackets] in the snippet. Optionally filter by one tag."""
        return [HitOut(**asdict(h)) for h in store.search(query, tag, limit)]

    @server.tool(annotations=READ_ONLY)
    def get_note(note_id: int) -> NoteOut:
        """Return the full text of one note."""
        return NoteOut(**asdict(_guard(store.get, note_id)))

    @server.tool(annotations=WRITE)
    def update_note(
        note_id: int, title: str | None = None, body: str | None = None, tags: list[str] | None = None
    ) -> NoteOut:
        """Change a note. Only the fields you pass are updated; passing tags replaces all tags."""
        return NoteOut(**asdict(_guard(store.update, note_id, title, body, tags)))

    @server.tool(annotations=DESTRUCTIVE)
    def delete_note(note_id: int) -> Deleted:
        """Permanently delete a note. Confirm with the user first."""
        _guard(store.delete, note_id)
        return Deleted(deleted_id=note_id)

    @server.tool(annotations=READ_ONLY)
    def list_tags() -> dict[str, int]:
        """All tags in use, with how many notes carry each one."""
        return store.tag_counts()

    @server.resource("notes://recent", mime_type="text/markdown")
    def recent_notes() -> str:
        """The ten most recently changed notes, as a markdown list."""
        notes = store.recent(10)
        if not notes:
            return "_No notes yet._"
        return "\n".join(f"- #{n.id} **{n.title}** ({', '.join(n.tags) or 'untagged'})" for n in notes)

    @server.resource("notes://{note_id}", mime_type="text/markdown")
    def note_resource(note_id: str) -> str:
        """One note as markdown."""
        note = _guard(store.get, int(note_id))
        tag_line = f"\n\nTags: {', '.join(note.tags)}" if note.tags else ""
        return f"# {note.title}\n\n{note.body}{tag_line}"

    return server


def main() -> None:
    path = os.environ.get("NOTES_DB", "~/.notes-mcp/notes.db")
    build_server(NoteStore(path)).run("stdio")


if __name__ == "__main__":
    main()
