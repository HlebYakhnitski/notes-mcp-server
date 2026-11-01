import pytest

from notes_mcp.store import InvalidNote, NoteNotFound, NoteStore, normalise_tags, to_fts_query


@pytest.fixture
def store():
    s = NoteStore()
    s.add("Python packaging", "Use pyproject.toml and build wheels with python -m build.", ["python", "tooling"])
    s.add("Weekly sync", "Discussed the release schedule and hiring.", ["meetings"])
    s.add("Docker tips", "Multi-stage builds keep python images small.", ["docker", "python"])
    yield s
    s.close()


def test_add_and_get_round_trip(store):
    note = store.add("  Title  ", "Body", ["Python", " python ", "AI"])
    fetched = store.get(note.id)
    assert fetched.title == "Title"
    assert fetched.tags == ["ai", "python"]          # trimmed, lower-cased, de-duplicated, sorted


def test_search_ranks_title_match_first(store):
    hits = store.search("python")
    assert hits[0].title == "Python packaging"       # title is weighted above body
    assert {h.title for h in hits} == {"Python packaging", "Docker tips"}


def test_search_uses_stemming(store):
    assert store.search("builds")[0].title in {"Python packaging", "Docker tips"}
    assert store.search("building")                    # porter stemmer: building -> build


def test_search_snippet_highlights_terms(store):
    assert "[release]" in store.search("release")[0].snippet


def test_search_tag_filter(store):
    assert [h.title for h in store.search("python", tag="docker")] == ["Docker tips"]


@pytest.mark.parametrize("hostile", ['"', "python OR", "NOT python", "py*", "title:python", "NEAR(a b)", "-"])
def test_fts_syntax_in_user_input_never_raises(store, hostile):
    store.search(hostile)   # must not raise sqlite3.OperationalError


def test_fts_query_quotes_every_word():
    assert to_fts_query('NOT "python" -docker') == '"NOT" "python" "docker"'
    assert to_fts_query("!!!") == ""


def test_search_index_follows_updates_and_deletes(store):
    note = store.search("sync")[0]
    store.update(note.id, body="Now about budgets.")
    assert store.search("release") == []
    assert store.search("budgets")[0].id == note.id
    store.delete(note.id)
    assert store.search("budgets") == []


def test_update_only_changes_given_fields(store):
    original = store.search("docker")[0]
    updated = store.update(original.id, title="Docker, revisited")
    assert updated.title == "Docker, revisited"
    assert updated.tags == ["docker", "python"]


def test_missing_note_raises(store):
    with pytest.raises(NoteNotFound):
        store.get(999)
    with pytest.raises(NoteNotFound):
        store.delete(999)


@pytest.mark.parametrize("title, body", [("", "x"), ("   ", "x"), ("t" * 201, "x"), ("t", "b" * 20_001)])
def test_validation(store, title, body):
    with pytest.raises(InvalidNote):
        store.add(title, body)


def test_invalid_tags():
    with pytest.raises(InvalidNote):
        normalise_tags(["has space"])


def test_tag_counts_sorted_by_frequency(store):
    assert next(iter(store.tag_counts().items())) == ("python", 2)


def test_persists_to_disk(tmp_path):
    path = tmp_path / "sub" / "notes.db"
    s = NoteStore(path)
    s.add("Persisted", "yes")
    s.close()
    assert NoteStore(path).search("persisted")[0].title == "Persisted"


def test_safe_to_use_from_worker_threads():
    """The MCP SDK calls sync tools from a thread pool - the store must allow that."""
    from concurrent.futures import ThreadPoolExecutor

    s = NoteStore()
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: s.add(f"note {i}", "concurrent body"), range(50)))
    assert len(s.search("concurrent", limit=50)) == 50
