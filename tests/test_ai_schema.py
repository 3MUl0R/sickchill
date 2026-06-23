"""
Schema-integrity tests for the AI cache tables.

Unlike the other AI test modules (which mock the database connection entirely), these use
a real, isolated SQLite database per test and exercise the *production* path where the
cache.py migration builds the canonical schema before the AI modules touch it. That is the
scenario that exposed the ``ai_show_preferences`` schema divergence: the migration created
``updated_at NUMERIC NOT NULL`` while the module insert omitted it, so a real install
failed with ``NOT NULL constraint failed: ai_show_preferences.updated_at``.

Each test gets a uniquely named DB file (never the shared ``tests/cache.db``) wired into
every AI manager via ``_get_db``, and the cached connection/file are cleaned up afterwards.
"""

import os
import sqlite3

import pytest

from sickchill.oldbeard import db
from sickchill.oldbeard.ai import cost_tracker, feedback, show_preferences, throttle
from sickchill.oldbeard.databases import cache

# Matches how tests/conftest.py resolves test database paths.
TEST_DIR = os.path.abspath(os.path.dirname(__file__))

AI_TABLES = ("ai_throttle", "ai_cache", "ai_usage", "ai_show_preferences", "ai_decisions", "ai_feedback")
AI_INDEXES = (
    "idx_ai_cache_expires",
    "idx_ai_usage_timestamp",
    "idx_ai_usage_scope",
    "idx_ai_decisions_timestamp",
    "idx_ai_decisions_show",
    "idx_ai_feedback_decision",
)
MANAGERS = (throttle.ThrottleManager, cost_tracker.CostTracker, show_preferences.ShowPreferencesManager, feedback.FeedbackManager)


def _full_path(filename):
    return os.path.join(TEST_DIR, filename)


def _forget(filename):
    """Drop any cached connection for an isolated DB and remove the backing file."""
    key = _full_path(filename)
    connection = db.db_cons.pop(key, None)
    db.db_locks.pop(key, None)
    if connection is not None:
        try:
            connection.close()
        except sqlite3.Error:
            pass
    if os.path.exists(key):
        try:
            os.remove(key)
        except OSError:
            pass


def _point_managers_at(monkeypatch, filename):
    """Route every AI manager's cache connection to the isolated DB file."""

    def _get_db(self):
        return db.DBConnection(filename)

    for manager in MANAGERS:
        monkeypatch.setattr(manager, "_get_db", _get_db)


def _table_info(filename, table):
    """Map column name -> (type, notnull, dflt_value, pk) via PRAGMA table_info.

    Read through the same cached DBConnection so uncommitted DDL is visible.
    """
    rows = db.DBConnection(filename).select(f"PRAGMA table_info({table})")
    return {row["name"]: (str(row["type"]).upper(), row["notnull"], row["dflt_value"], row["pk"]) for row in rows}


def _index_names(filename):
    rows = db.DBConnection(filename).select("SELECT name FROM sqlite_master WHERE type = 'index'")
    return {row["name"] for row in rows}


def _run_migration(filename):
    db.upgrade_database(db.DBConnection(filename), cache.InitialSchema)


@pytest.fixture
def cache_db(request, monkeypatch):
    """A unique, isolated cache DB filename wired into every AI manager."""
    filename = f"ai_schema_{request.node.name}.db"
    _forget(filename)
    _point_managers_at(monkeypatch, filename)
    yield filename
    _forget(filename)


def test_set_preferences_after_migration(cache_db):
    """Production order: the migration builds the schema, then the module writes to it.

    Regression for the original bug, where ``set_preferences`` omitted ``updated_at`` and
    raised ``NOT NULL constraint failed`` against the migration-created table.
    """
    _run_migration(cache_db)

    info = _table_info(cache_db, "ai_show_preferences")
    assert "updated_at" in info
    assert info["updated_at"][1] == 1, "updated_at should be NOT NULL"

    manager = show_preferences.ShowPreferencesManager()
    manager.set_preferences(123, show_preferences.ShowAIPreferences(ai_enabled=True, ai_confidence_threshold=0.9))  # must not raise

    # A fresh manager bypasses the in-memory cache and reads back from the DB.
    reloaded = show_preferences.ShowPreferencesManager().get_preferences(123)
    assert reloaded.ai_enabled is True
    assert reloaded.ai_confidence_threshold == 0.9

    (updated_at,) = db.DBConnection(cache_db).select("SELECT updated_at FROM ai_show_preferences WHERE show_id = ?", [123])[0]
    assert updated_at and updated_at > 0


def test_legacy_two_column_preferences_upgraded(cache_db):
    """A legacy 2-column ai_show_preferences (old module path) is repaired by migration."""
    connection = db.DBConnection(cache_db)
    connection.action("CREATE TABLE db_version (db_version INTEGER)")
    connection.action("INSERT INTO db_version (db_version) VALUES (1)")
    connection.action("CREATE TABLE ai_show_preferences (show_id INTEGER PRIMARY KEY, preferences_json TEXT NOT NULL)")

    _run_migration(cache_db)

    info = _table_info(cache_db, "ai_show_preferences")
    assert "updated_at" in info, "repair should add the missing column"
    assert info["updated_at"][1] == 1, "repaired updated_at should be NOT NULL"

    manager = show_preferences.ShowPreferencesManager()
    manager.set_preferences(7, show_preferences.ShowAIPreferences())  # must not raise
    assert show_preferences.ShowPreferencesManager().get_preferences(7) is not None


def test_module_fallback_repairs_legacy_two_column_table(cache_db):
    """The on-demand module fallback (no migration run) must repair a legacy 2-column table."""
    connection = db.DBConnection(cache_db)
    connection.action("CREATE TABLE ai_show_preferences (show_id INTEGER PRIMARY KEY, preferences_json TEXT NOT NULL)")

    # No migration is run here: _ensure_table() (called by __init__) must add updated_at.
    manager = show_preferences.ShowPreferencesManager()

    info = _table_info(cache_db, "ai_show_preferences")
    assert "updated_at" in info, "fallback should add the missing column"
    assert info["updated_at"][1] == 1, "repaired updated_at should be NOT NULL"

    manager.set_preferences(5, show_preferences.ShowAIPreferences())  # must not raise
    assert show_preferences.ShowPreferencesManager().get_preferences(5) is not None


def test_partial_schema_repaired(cache_db):
    """Only ai_throttle pre-exists; the narrow AITables guard would skip ai_cache.

    The comprehensive repair migration must still create every table and index.
    """
    connection = db.DBConnection(cache_db)
    connection.action("CREATE TABLE db_version (db_version INTEGER)")
    connection.action("INSERT INTO db_version (db_version) VALUES (1)")
    connection.action(
        "CREATE TABLE ai_throttle ("
        "context TEXT, scope TEXT, scope_key TEXT, last_attempt NUMERIC, last_success NUMERIC,"
        " PRIMARY KEY(context, scope, scope_key))"
    )

    _run_migration(cache_db)

    for table in AI_TABLES:
        assert _table_info(cache_db, table), f"missing table: {table}"
    index_names = _index_names(cache_db)
    for index in AI_INDEXES:
        assert index in index_names, f"missing index: {index}"


def test_schema_parity_between_migration_and_module(cache_db, request, monkeypatch):
    """The migration chain and the on-demand module path must produce identical schemas."""
    # DB-A: built by the migration chain.
    _run_migration(cache_db)
    migration_tables = {table: _table_info(cache_db, table) for table in AI_TABLES}
    migration_indexes = _index_names(cache_db)

    # DB-B: built purely by the AI modules' _ensure_* (each manager __init__ runs them).
    module_db = f"ai_schema_{request.node.name}_module.db"
    _forget(module_db)
    _point_managers_at(monkeypatch, module_db)
    try:
        for manager in MANAGERS:
            manager()
        module_tables = {table: _table_info(module_db, table) for table in AI_TABLES}
        module_indexes = _index_names(module_db)
    finally:
        _forget(module_db)

    for table in AI_TABLES:
        assert module_tables[table] == migration_tables[table], f"column definition mismatch for {table}"
    for index in AI_INDEXES:
        assert index in migration_indexes, f"migration missing index {index}"
        assert index in module_indexes, f"module missing index {index}"
