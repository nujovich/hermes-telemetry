"""Shared-home (HERMES_TELEMETRY_HOME) cross-profile attribution.

Topology this covers: several profiles point ``HERMES_TELEMETRY_HOME`` at ONE
directory, so their rows land in a single ``telemetry.db``. In that store
``runs.session_id`` is the PRIMARY KEY with no profile dimension, so two profiles
whose sessions share an id merge into one row set, and rows below ``runs`` carried
no profile at all. v19 puts the owning profile on every ``tool_calls`` /
``llm_calls`` row so the attribution is self-describing.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location("hermes_telemetry", str(_ROOT / "__init__.py"))
_init_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_init_mod)


@pytest.fixture(autouse=True)
def isolated_db():
    import hermes_telemetry.db as db_mod

    db_mod._local.conn = None
    yield
    if getattr(db_mod._local, "conn", None):
        db_mod._local.conn.close()
        db_mod._local.conn = None


class MockPluginContext:
    """Minimal stand-in for Hermes PluginContext, with a profile_name."""

    def __init__(self, profile_name="default"):
        self.hooks: dict = {}
        self.commands: dict = {}
        self.profile_name = profile_name

    def register_hook(self, name, fn):
        self.hooks[name] = fn

    def register_command(self, name, fn, description="", args_hint=""):
        self.commands[name] = fn

    def register_cli_command(self, *a, **k):
        pass

    def fire(self, hook_name, **kwargs):
        fn = self.hooks.get(hook_name)
        return fn(**kwargs) if fn else None


# ---------------------------------------------------------------------------
# 1. the reported DB path must be the one actually opened
# ---------------------------------------------------------------------------


def test_db_path_info_honours_telemetry_home(tmp_path, monkeypatch):
    """``_db_path_info`` reads only HERMES_HOME, so with a shared cost centre
    configured it printed a path the plugin never writes to."""
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    assert _init_mod._db_path_info() == str(tmp_path / "shared" / "telemetry" / "telemetry.db")


def test_db_path_info_falls_back_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TELEMETRY_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    assert _init_mod._db_path_info() == str(tmp_path / "profile" / "telemetry" / "telemetry.db")


# ---------------------------------------------------------------------------
# 2. the v19 migration
# ---------------------------------------------------------------------------


def _pre_v19_db(path: Path) -> sqlite3.Connection:
    """A store as v18 left it: no profile column below `runs`, marker at 18."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
        CREATE TABLE runs (
            session_id  TEXT PRIMARY KEY,
            profile     TEXT,
            started_at  TEXT NOT NULL
        );
        CREATE TABLE tool_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          TEXT NOT NULL,
            tool_name   TEXT NOT NULL,
            ok          INTEGER NOT NULL DEFAULT 1,
            latency_ms  INTEGER,
            turn_id     TEXT,
            tool_call_id TEXT
        );
        CREATE TABLE llm_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL,
            ts          TEXT NOT NULL
        );
        INSERT INTO schema_version(version, applied_at) VALUES (18, '2026-01-01T00:00:00+00:00');
        INSERT INTO runs(session_id, profile, started_at)
            VALUES ('s1', 'coder', '2026-01-01T00:00:00+00:00');
        INSERT INTO tool_calls(session_id, ts, tool_name)
            VALUES ('s1', '2026-01-01T00:00:01+00:00', 'terminal');
        INSERT INTO llm_calls(session_id, ts)
            VALUES ('s1', '2026-01-01T00:00:02+00:00');
        """
    )
    return conn


def test_migrate_v19_adds_columns_and_backfills(tmp_path):
    import hermes_telemetry.db as db

    conn = _pre_v19_db(tmp_path / "pre19.db")
    try:
        db._migrate_v19(conn)

        tool_cols = {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}
        llm_cols = {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
        assert "profile" in tool_cols
        assert "profile" in llm_cols

        # existing rows are labelled from their run where the run knows the profile
        assert conn.execute("SELECT profile FROM tool_calls").fetchone()[0] == "coder"
        assert conn.execute("SELECT profile FROM llm_calls").fetchone()[0] == "coder"

        idx = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE '%_profile'"
            )
        }
        assert {"idx_tool_profile", "idx_llm_profile"} <= idx

        assert (
            conn.execute("SELECT version FROM schema_version WHERE version = 19").fetchone()
            is not None
        )
    finally:
        conn.close()


def test_migrate_v19_is_idempotent(tmp_path):
    import hermes_telemetry.db as db

    conn = _pre_v19_db(tmp_path / "idem.db")
    try:
        db._migrate_v19(conn)
        before = conn.execute(
            "SELECT id, session_id, profile FROM tool_calls ORDER BY id"
        ).fetchall()
        db._migrate_v19(conn)  # second run must not raise, duplicate, or relabel
        after = conn.execute(
            "SELECT id, session_id, profile FROM tool_calls ORDER BY id"
        ).fetchall()
        assert [tuple(r) for r in before] == [tuple(r) for r in after]
        assert (
            conn.execute("SELECT COUNT(*) FROM schema_version WHERE version = 19").fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_migrate_v19_self_heals_marker_without_columns(tmp_path):
    """A half-applied migration (marker written, columns missing) must self-heal."""
    import hermes_telemetry.db as db

    conn = _pre_v19_db(tmp_path / "half.db")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO schema_version(version, applied_at) VALUES (19, '2026-01-02T00:00:00+00:00')"
        )
        db._migrate_v19(conn)
        assert "profile" in {r[1] for r in conn.execute("PRAGMA table_info(tool_calls)")}
        assert "profile" in {r[1] for r in conn.execute("PRAGMA table_info(llm_calls)")}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 3. writers persist the profile
# ---------------------------------------------------------------------------


def test_record_tool_call_persists_profile():
    import hermes_telemetry.db as db

    db.record_tool_call("s1", db._utcnow(), "terminal", True, 5, profile="coder")
    conn = db._get_conn()
    assert (
        conn.execute("SELECT profile FROM tool_calls WHERE session_id='s1'").fetchone()[0]
        == "coder"
    )


def test_record_llm_call_persists_profile():
    import hermes_telemetry.db as db

    db.record_llm_call("s2", db._utcnow(), "m", "p", 1, 1, 0.0, 5, profile="ops")
    conn = db._get_conn()
    assert (
        conn.execute("SELECT profile FROM llm_calls WHERE session_id='s2'").fetchone()[0] == "ops"
    )


def test_profile_defaults_to_null_when_absent():
    import hermes_telemetry.db as db

    db.record_tool_call("s3", db._utcnow(), "terminal", True, 5)
    conn = db._get_conn()
    assert (
        conn.execute("SELECT profile FROM tool_calls WHERE session_id='s3'").fetchone()[0] is None
    )


# ---------------------------------------------------------------------------
# 4. hooks pass ctx.profile_name through
# ---------------------------------------------------------------------------


def test_post_tool_call_hook_passes_profile():
    import hermes_telemetry.db as db

    ctx = MockPluginContext(profile_name="coder")
    _init_mod.register(ctx)
    ctx.fire(
        "post_tool_call", session_id="h1", tool_name="terminal", result={"ok": True}, duration_ms=5
    )
    conn = db._get_conn()
    assert (
        conn.execute("SELECT profile FROM tool_calls WHERE session_id='h1'").fetchone()[0]
        == "coder"
    )


# ---------------------------------------------------------------------------
# 5. the cross-profile case this exists for
# ---------------------------------------------------------------------------


def test_two_profiles_same_session_id_rows_are_separable(tmp_path, monkeypatch):
    """Two profiles, ONE shared store, the SAME session id.

    `runs` still merges (session_id is the PK) — that limitation is asserted, not
    hidden. The tool/llm rows stay separable because each carries its own profile.
    """
    import hermes_telemetry.db as db

    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))

    db.start_run("dup", "m", "cli", profile="alpha")
    db.record_tool_call("dup", db._utcnow(), "terminal", True, 5, profile="alpha")
    db.record_llm_call("dup", db._utcnow(), "m", "p", 1, 1, 0.0, 5, profile="alpha")

    db.record_tool_call("dup", db._utcnow(), "read_file", True, 3, profile="beta")
    db.record_llm_call("dup", db._utcnow(), "m", "p", 2, 2, 0.0, 6, profile="beta")

    conn = db._get_conn()
    tools = [
        tuple(r) for r in conn.execute("SELECT tool_name, profile FROM tool_calls ORDER BY id")
    ]
    llms = [tuple(r) for r in conn.execute("SELECT model, profile FROM llm_calls ORDER BY id")]

    assert tools == [("terminal", "alpha"), ("read_file", "beta")]
    assert llms == [("m", "alpha"), ("m", "beta")]

    # the residual limitation, stated plainly: runs merges and first-writer-wins
    run = db.get_run("dup")
    assert run["tool_calls"] == 2
    assert run["profile"] == "alpha"
