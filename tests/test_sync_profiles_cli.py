import argparse

import hermes_telemetry.telemetry_cli as tcli


def _args(**kw):
    base = dict(
        command="sync-profiles",
        names=[],
        apply=False,
        yes=False,
        telemetry_home=None,
        json=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_dry_run_does_not_mutate(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    tcli._handle_sync_profiles(_args())
    out = capsys.readouterr().out
    assert "would set" in out
    assert not (named / ".env").exists()


def test_apply_from_default_mutates(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    named = tmp_path / "profiles" / "coder"
    named.mkdir(parents=True)
    tcli._handle_sync_profiles(_args(apply=True))
    out = capsys.readouterr().out
    assert "env-written" in out
    assert (named / ".env").read_text() == f"HERMES_TELEMETRY_HOME={tmp_path}\n"


def test_apply_from_named_profile_refuses_without_yes(tmp_path, monkeypatch, capsys):
    base = tmp_path / "profiles" / "coder"
    (base / "profiles").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(base))
    tcli._handle_sync_profiles(_args(apply=True))
    out = capsys.readouterr().out
    assert "Refusing to apply" in out


def test_apply_from_named_profile_proceeds_with_yes(tmp_path, monkeypatch, capsys):
    base = tmp_path / "profiles" / "coder"
    (base / "profiles" / "child").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(base))
    tcli._handle_sync_profiles(_args(apply=True, yes=True))
    out = capsys.readouterr().out
    assert "Refusing to apply" not in out
    assert (base / "profiles" / "child" / ".env").exists()


def test_parser_accepts_subcommand():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    tcli._build_parser_into(sub)
    args = parser.parse_args(["sync-profiles", "coder", "--apply", "--yes"])
    assert args.command == "sync-profiles"
    assert args.names == ["coder"]
    assert args.apply is True and args.yes is True
