# notes-mcp-server

A [Model Context Protocol](https://modelcontextprotocol.io) server that gives an AI assistant a persistent, searchable notebook. Claude (or any MCP client) can save notes, find them with full-text search, update and tag them — stored locally in a single SQLite file.

```
You:    remember that we chose pgvector over qdrant because ops already runs Postgres
Claude: [add_note] Saved as note #12, tagged "architecture".

You:    why did we pick pgvector again?
Claude: [search_notes "pgvector"] → note #12: "...chose [pgvector] over qdrant because ops already runs Postgres"
```

Built on the official Python SDK (`mcp` 2.x, `MCPServer`).

## Tools

| Tool | Annotation | Purpose |
| --- | --- | --- |
| `search_notes(query, tag?, limit?)` | read-only | Full-text search over title, body and tags; best match first, matching words `[bracketed]` in the snippet |
| `get_note(note_id)` | read-only | Full text of one note |
| `list_tags()` | read-only | Tags in use with counts |
| `add_note(title, body, tags?)` | write | Create a note |
| `update_note(note_id, title?, body?, tags?)` | write | Change only the fields passed |
| `delete_note(note_id)` | destructive | Permanent delete — the server instructions tell the model to confirm with the user first |

Resources: `notes://recent` (ten latest notes) and `notes://{id}` (one note as markdown).

Every tool returns structured output with a JSON schema, so clients get typed fields rather than text to parse.

## Design notes

**Search is safe against FTS syntax in user input.** SQLite FTS5 treats quotes, `*`, `-`, `NOT` and `NEAR(...)` as operators; passing a model's query straight into `MATCH` either throws or silently changes the meaning. Every word is quoted as a literal before matching. The test suite throws hostile strings at it to prove nothing raises.

**Ranking favours titles.** `bm25()` is weighted 10 : 1 : 5 for title : body : tags, and the Porter tokenizer matches `building` to `builds`. The FTS index is kept in sync with triggers on insert, update and delete.

**Errors are useful to the model, not leaky.** Expected failures (missing note, empty title, malformed tag) are raised as `ToolError`, so the model sees `note 404 does not exist` and can recover. Anything unexpected is left to the SDK, which reports a generic error without exposing internals.

**Thread-safe store.** The SDK runs synchronous tools in a worker-thread pool, while a default SQLite connection only works on the thread that opened it — the server would fail on its second call. The store opens the connection with `check_same_thread=False` and serialises access with a lock. This bug was invisible in unit tests of the store and only showed up when the tests went through a real MCP client, which is why most server tests do.

**Input limits.** Titles up to 200 characters, bodies up to 20,000, tags restricted to `[a-z0-9_-]`, search results capped at 50.

## Install

```bash
pip install git+https://github.com/HlebYakhnitski/notes-mcp-server
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "notes": {
      "command": "notes-mcp",
      "env": { "NOTES_DB": "~/.notes-mcp/notes.db" }
    }
  }
}
```

On Windows, if `notes-mcp` is not on `PATH`, use `"command": "python", "args": ["-m", "notes_mcp.server"]`.

### Any other client

The server speaks MCP over stdio: `python -m notes_mcp.server`. The database path comes from `NOTES_DB` (default `~/.notes-mcp/notes.db`).

## Development

```bash
pip install -e ".[dev]"
pytest
```

31 tests. The storage layer is tested directly; the server is tested through the SDK's `Client` connected in-process, which exercises tool schemas, structured output, annotations, error reporting and resources exactly as a real client sees them. A concurrency test writes from eight threads at once.

## License

MIT
