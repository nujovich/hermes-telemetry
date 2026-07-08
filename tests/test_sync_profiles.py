import json

import hermes_telemetry.sync_profiles as sp

# --- Task 1: path/env helpers ---


def test_default_base_home_reads_hermes_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert sp.default_base_home() == tmp_path / "h"


def test_resolve_target_prefers_telemetry_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_TELEMETRY_HOME", str(tmp_path / "shared"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert sp.resolve_target_home() == tmp_path / "shared"


def test_resolve_target_falls_back_to_hermes_home(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_TELEMETRY_HOME", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert sp.resolve_target_home() == tmp_path / "h"


def test_is_default_profile_true_for_plain_home(tmp_path):
    assert sp.is_default_profile(tmp_path) is True


def test_is_default_profile_false_for_named_profile(tmp_path):
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    assert sp.is_default_profile(named) is False


# --- Task 2: profile enumeration ---


def test_iter_profiles_default_only(tmp_path):
    assert sp.iter_profiles(tmp_path) == [("default", tmp_path)]


def test_iter_profiles_lists_named_sorted(tmp_path):
    (tmp_path / "profiles" / "b").mkdir(parents=True)
    (tmp_path / "profiles" / "a").mkdir(parents=True)
    (tmp_path / "profiles" / "note.txt").write_text("x")
    result = sp.iter_profiles(tmp_path)
    assert result == [
        ("default", tmp_path),
        ("a", tmp_path / "profiles" / "a"),
        ("b", tmp_path / "profiles" / "b"),
    ]


# --- Task 3: .env read + comment-preserving atomic upsert ---


def test_read_env_var_absent_file(tmp_path):
    assert sp.read_env_var(tmp_path / ".env", sp.ENV_KEY) is None


def test_read_env_var_last_wins_ignores_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text(
        "# HERMES_TELEMETRY_HOME=/commented\nHERMES_TELEMETRY_HOME=/a\nHERMES_TELEMETRY_HOME=/b\n"
    )
    assert sp.read_env_var(env, sp.ENV_KEY) == "/b"


def test_upsert_creates_file(tmp_path):
    env = tmp_path / ".env"
    changed = sp.upsert_env_var(env, sp.ENV_KEY, "/shared")
    assert changed is True
    assert env.read_text() == "HERMES_TELEMETRY_HOME=/shared\n"


def test_upsert_preserves_other_lines_and_comments(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my config\nFOO=1\nHERMES_TELEMETRY_HOME=/old\nBAR=2\n")
    changed = sp.upsert_env_var(env, sp.ENV_KEY, "/shared")
    assert changed is True
    assert env.read_text() == "# my config\nFOO=1\nHERMES_TELEMETRY_HOME=/shared\nBAR=2\n"


def test_upsert_idempotent_returns_false(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HERMES_TELEMETRY_HOME=/shared\n")
    assert sp.upsert_env_var(env, sp.ENV_KEY, "/shared") is False


def test_upsert_drops_later_duplicates(tmp_path):
    env = tmp_path / ".env"
    env.write_text("HERMES_TELEMETRY_HOME=/a\nKEEP=1\nHERMES_TELEMETRY_HOME=/b\n")
    sp.upsert_env_var(env, sp.ENV_KEY, "/shared")
    assert env.read_text() == "HERMES_TELEMETRY_HOME=/shared\nKEEP=1\n"


# --- Task 4: read-only plugin status ---


def _write_config(home, enabled):
    import yaml

    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump({"plugins": {"enabled": enabled}}))


def test_plugin_status_no_config(tmp_path):
    assert sp.plugin_status(tmp_path) == ("no-config", False)


def test_plugin_status_enabled(tmp_path):
    _write_config(tmp_path, ["hermes-telemetry", "other"])
    assert sp.plugin_status(tmp_path) == ("enabled", False)


def test_plugin_status_not_enabled(tmp_path):
    _write_config(tmp_path, ["other"])
    assert sp.plugin_status(tmp_path) == ("not-enabled", False)


def test_plugin_status_detects_install_via_symlink(tmp_path):
    _write_config(tmp_path, ["hermes-telemetry"])
    real = tmp_path / "real_plugin"
    real.mkdir()
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "hermes-telemetry").symlink_to(real, target_is_directory=True)
    state, installed = sp.plugin_status(tmp_path)
    assert (state, installed) == ("enabled", True)


def test_plugin_status_unreadable_config(tmp_path):
    (tmp_path / "config.yaml").write_text("plugins: [unbalanced\n")
    state, _ = sp.plugin_status(tmp_path)
    assert state == "unreadable"


# --- Task 5: detect() ---


def test_detect_marks_target_and_env_states(tmp_path):
    named = tmp_path / "profiles" / "coder"
    _write_config(named, ["hermes-telemetry"])
    statuses = sp.detect(tmp_path, target=tmp_path)
    by_name = {s.name: s for s in statuses}
    assert by_name["default"].is_target is True
    assert by_name["coder"].is_target is False
    assert by_name["coder"].env_state == "missing"
    assert by_name["coder"].plugin_enabled == "enabled"


def test_detect_env_ok_and_mismatch(tmp_path):
    ok = tmp_path / "profiles" / "ok"
    ok.mkdir(parents=True)
    (ok / ".env").write_text(f"HERMES_TELEMETRY_HOME={tmp_path}\n")
    bad = tmp_path / "profiles" / "bad"
    bad.mkdir(parents=True)
    (bad / ".env").write_text("HERMES_TELEMETRY_HOME=/somewhere/else\n")
    by_name = {s.name: s for s in sp.detect(tmp_path, target=tmp_path)}
    assert by_name["ok"].env_state == "ok"
    assert by_name["bad"].env_state == "mismatch"
    assert by_name["bad"].env_current == "/somewhere/else"


def test_detect_honors_only_filter(tmp_path):
    (tmp_path / "profiles" / "a").mkdir(parents=True)
    (tmp_path / "profiles" / "b").mkdir(parents=True)
    names = {s.name for s in sp.detect(tmp_path, target=tmp_path, only=["a"])}
    assert names == {"a"}


# --- Task 6: apply() ---


def test_apply_writes_missing_skips_target_and_ok(tmp_path):
    ok = tmp_path / "profiles" / "ok"
    ok.mkdir(parents=True)
    (ok / ".env").write_text(f"HERMES_TELEMETRY_HOME={tmp_path}\n")
    miss = tmp_path / "profiles" / "miss"
    miss.mkdir(parents=True)
    statuses = sp.detect(tmp_path, target=tmp_path)
    results = {r.name: r for r in sp.apply(statuses, target=tmp_path)}
    assert results["default"].action == "skipped"
    assert results["ok"].action == "skipped"
    assert results["miss"].action == "env-written"
    assert (miss / ".env").read_text() == f"HERMES_TELEMETRY_HOME={tmp_path}\n"


def test_apply_isolates_per_profile_errors(tmp_path, monkeypatch):
    bad = tmp_path / "profiles" / "bad"
    bad.mkdir(parents=True)
    good = tmp_path / "profiles" / "good"
    good.mkdir(parents=True)
    statuses = sp.detect(tmp_path, target=tmp_path)
    real_upsert = sp.upsert_env_var

    def flaky(env_path, key, value):
        if "bad" in str(env_path):
            raise OSError("permission denied")
        return real_upsert(env_path, key, value)

    monkeypatch.setattr(sp, "upsert_env_var", flaky)
    results = {r.name: r for r in sp.apply(statuses, target=tmp_path)}
    assert results["bad"].action == "error"
    assert "permission denied" in results["bad"].detail
    assert results["good"].action == "env-written"


# --- Task 7: rendering ---


def test_render_dry_run_shows_target_plan_and_warning(tmp_path):
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    statuses = sp.detect(tmp_path, target=tmp_path)
    text = sp.render(statuses, target=tmp_path)
    assert str(tmp_path) in text
    assert "would set" in text
    assert "hermes plugins enable hermes-telemetry --profile coder" in text


def test_render_applied_shows_actions(tmp_path):
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    statuses = sp.detect(tmp_path, target=tmp_path)
    results = sp.apply(statuses, target=tmp_path)
    text = sp.render(statuses, target=tmp_path, results=results)
    assert "env-written" in text


def test_to_json_shape(tmp_path):
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    statuses = sp.detect(tmp_path, target=tmp_path)
    data = json.loads(sp.to_json(statuses, target=tmp_path))
    assert data["target"] == str(tmp_path)
    coder = next(p for p in data["profiles"] if p["name"] == "coder")
    assert coder["env_state"] == "missing"
    assert coder["plugin_enabled"] == "no-config"
