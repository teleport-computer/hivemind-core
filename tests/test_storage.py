import os
import sqlite3
import tempfile
import time

from cryptography.fernet import Fernet

from hivemind.models import Scope
from hivemind.storage import Storage


def test_write_and_read(tmp_db):
    tmp_db.write_record("r1", "hello world", "public", "alice", time.time(), None)
    record = tmp_db.read_record("r1", Scope())
    assert record is not None
    assert record["text"] == "hello world"


def test_scope_user_ids(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "alice text", "public", "alice", t, None)
    tmp_db.write_record("r2", "bob text", "public", "bob", t, None)

    assert tmp_db.read_record("r1", Scope(user_ids=["alice"])) is not None
    assert tmp_db.read_record("r2", Scope(user_ids=["alice"])) is None


def test_scope_record_ids(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    tmp_db.write_record("r2", "text2", "public", "alice", t, None)

    assert tmp_db.read_record("r1", Scope(record_ids=["r1"])) is not None
    assert tmp_db.read_record("r2", Scope(record_ids=["r1"])) is None


def test_scope_combined(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    tmp_db.write_record("r2", "text2", "public", "bob", t, None)

    # Both conditions must match
    scope = Scope(user_ids=["alice"], record_ids=["r1"])
    assert tmp_db.read_record("r1", scope) is not None

    # user_id matches but record_id doesn't
    scope = Scope(user_ids=["alice"], record_ids=["r2"])
    assert tmp_db.read_record("r1", scope) is None


def test_fts_search(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "original text", "public", "alice", t, None)
    tmp_db.write_index(
        "r1", "Payment Migration", "Team decided to move to Stripe",
        "payments,stripe", "Switching to Stripe", "{}", t,
    )

    results = tmp_db.search_index("stripe payments", Scope())
    assert len(results) == 1
    assert results[0]["record_id"] == "r1"


def test_fts_search_scoped(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    tmp_db.write_record("r2", "text2", "public", "bob", t, None)
    tmp_db.write_index(
        "r1", "Alice Notes", "Some notes from alice", "notes", "", "{}", t,
    )
    tmp_db.write_index(
        "r2", "Bob Notes", "Some notes from bob", "notes", "", "{}", t,
    )

    results = tmp_db.search_index("notes", Scope(user_ids=["alice"]))
    assert len(results) == 1
    assert results[0]["record_id"] == "r1"


def test_fts_search_by_space(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "space-a", "alice", t, None)
    tmp_db.write_record("r2", "text2", "space-b", "bob", t, None)
    tmp_db.write_index(
        "r1", "Notes A", "Notes in space A", "notes", "", "{}", t,
    )
    tmp_db.write_index(
        "r2", "Notes B", "Notes in space B", "notes", "", "{}", t,
    )

    results = tmp_db.search_index("notes", Scope(), space_id="space-a")
    assert len(results) == 1
    assert results[0]["record_id"] == "r1"


def test_list_index(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    tmp_db.write_record("r2", "text2", "public", "bob", t + 1, None)
    tmp_db.write_index("r1", "First", "First entry", "tag1", "", "{}", t)
    tmp_db.write_index("r2", "Second", "Second entry", "tag2", "", "{}", t + 1)

    results = tmp_db.list_index(Scope())
    assert len(results) == 2
    assert results[0]["record_id"] == "r2"  # most recent first

    results = tmp_db.list_index(Scope(user_ids=["alice"]))
    assert len(results) == 1
    assert results[0]["record_id"] == "r1"


def test_update_index(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    tmp_db.write_index(
        "r1", "Old Title", "Old summary", "old", "", "{}", t,
    )

    ok = tmp_db.update_index("r1", "New Title", "New summary", "new", "", "{}")
    assert ok is True

    # Verify FTS picks up new content
    results = tmp_db.search_index("New Title", Scope())
    assert len(results) == 1
    assert results[0]["title"] == "New Title"

    # Old content should not match
    results = tmp_db.search_index("Old Title", Scope())
    assert len(results) == 0


def test_delete_record(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text", "public", "alice", t, None)
    tmp_db.write_index("r1", "Title", "Summary", "tag", "", "{}", t)

    assert tmp_db.delete_record("r1") is True
    assert tmp_db.read_record("r1", Scope()) is None
    assert tmp_db.delete_record("r1") is False

    # FTS should also be cleaned up
    results = tmp_db.search_index("Title", Scope())
    assert len(results) == 0


def test_list_spaces(tmp_db):
    t = time.time()
    tmp_db.write_record("r1", "text1", "space-a", "alice", t, None)
    tmp_db.write_record("r2", "text2", "space-a", "bob", t, None)
    tmp_db.write_record("r3", "text3", "space-b", "alice", t, None)

    spaces = tmp_db.list_spaces()
    assert len(spaces) == 2
    assert spaces[0]["space_id"] == "space-a"
    assert spaces[0]["count"] == 2


def test_count_records(tmp_db):
    t = time.time()
    assert tmp_db.count_records() == 0
    tmp_db.write_record("r1", "text1", "public", "alice", t, None)
    assert tmp_db.count_records() == 1
    assert tmp_db.count_records("public") == 1
    assert tmp_db.count_records("other") == 0


def test_encryption_at_rest():
    key = Fernet.generate_key().decode()
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = Storage(db_path, encryption_key=key)
        store.write_record("r1", "secret data", "public", "alice", time.time(), None)

        # Read through Storage — should decrypt transparently
        record = store.read_record("r1", Scope())
        assert record["text"] == "secret data"

        # Read raw SQLite — should be ciphertext, not plaintext
        conn = sqlite3.connect(db_path)
        raw = conn.execute("SELECT text FROM records WHERE id = 'r1'").fetchone()[0]
        conn.close()
        assert raw != "secret data"
        assert len(raw) > len("secret data")  # Fernet output is longer

        store.close()
    finally:
        os.unlink(db_path)


def test_no_encryption_by_default():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    try:
        store = Storage(db_path)
        store.write_record("r1", "plain data", "public", "alice", time.time(), None)

        # Raw SQLite should have plaintext
        conn = sqlite3.connect(db_path)
        raw = conn.execute("SELECT text FROM records WHERE id = 'r1'").fetchone()[0]
        conn.close()
        assert raw == "plain data"

        store.close()
    finally:
        os.unlink(db_path)
