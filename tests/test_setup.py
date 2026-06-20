"""Tests for the setup wizard (hermes_telemetry.setup).

Covers:
  - Non-interactive auto-generate (mocked OpenRouter fetch)
  - Non-interactive minimal (built-in defaults)
  - /setup command handler subcommands
  - Idempotency: skips files that already exist
  - owl-alpha present in pricing after setup
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make the package importable (same trick as conftest.py)
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_telemetry import setup  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def tmp_telemetry(tmp_path):
    """Reload setup against the isolated HERMES_HOME (pointed at this tmp_path
    by the conftest baseline) and expose that path to tests."""
    importlib.reload(setup)
    yield tmp_path
    # Reload again after test so state is clean
    importlib.reload(setup)


# ---------------------------------------------------------------------------
# Tests: Pricing
# ---------------------------------------------------------------------------
class TestSetupPricing:
    def test_auto_generates_pricing_yaml(self, tmp_telemetry):
        """Non-interactive auto mode creates pricing.yaml with defaults + OR fetch."""
        with patch.object(
            setup,
            "_fetch_openrouter_models",
            return_value={"openrouter/some-new-model": {"input": 1.00, "output": 2.00}},
        ):
            setup.run(interactive=False)

        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert pricing_file.exists(), "pricing.yaml should be created"
        content = pricing_file.read_text()
        # Built-in models present
        assert "claude-opus-4" in content
        assert "gpt-4o" in content
        # owl-alpha present (the fix)
        assert "owl-alpha" in content
        # Fetched model present
        assert "openrouter/some-new-model" in content

    def test_minimal_built_in_only(self, tmp_telemetry):
        """/setup pricing minimal writes only built-in defaults, no network call."""
        with patch.object(setup, "_fetch_openrouter_models") as mock_fetch:
            setup.handle_command("pricing minimal")
            mock_fetch.assert_not_called()

        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert pricing_file.exists()
        content = pricing_file.read_text()
        assert "claude-opus-4" in content
        assert "owl-alpha" in content

    def test_skip_pricing(self, tmp_telemetry):
        """Skip pricing creates no file."""
        setup.handle_command("pricing skip")
        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert not pricing_file.exists()

    def test_idempotent_when_pricing_exists(self, tmp_telemetry):
        """If pricing.yaml already exists, setup doesn't overwrite."""
        tele_dir = tmp_telemetry / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        existing = tele_dir / "pricing.yaml"
        existing.write_text("# my custom pricing\nmodels: {}\n")

        setup.run(interactive=False)
        assert existing.read_text() == "# my custom pricing\nmodels: {}\n"


# ---------------------------------------------------------------------------
# Tests: Budget
# ---------------------------------------------------------------------------
class TestSetupBudget:
    def test_default_budget(self, tmp_telemetry):
        """Default budget writes global $5/d, $100/mo."""
        setup.handle_command("budget default")
        budget_file = tmp_telemetry / "telemetry" / "budget.yaml"
        assert budget_file.exists()
        content = budget_file.read_text()
        assert "global" in content
        assert "daily_usd" in content
        # YAML serializer outputs "5.0000" for floats
        assert "5.0000" in content or "5.0" in content

    def test_skip_budget(self, tmp_telemetry):
        """Skip budget creates no file."""
        setup.handle_command("budget skip")
        budget_file = tmp_telemetry / "telemetry" / "budget.yaml"
        assert not budget_file.exists()

    def test_custom_budget_returns_instructions(self, tmp_telemetry):
        """Custom budget returns instructions, doesn't write a file."""
        result = setup.handle_command("budget custom")
        assert "/budget set global" in result

    def test_idempotent_when_budget_exists(self, tmp_telemetry):
        """If budget.yaml already exists, setup doesn't overwrite."""
        tele_dir = tmp_telemetry / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        existing = tele_dir / "budget.yaml"
        existing.write_text("# custom\nbudgets:\n  global:\n    daily_usd: 99.00\n")

        setup.run(interactive=False)
        assert "99.0" in existing.read_text()


# ---------------------------------------------------------------------------
# Tests: Command handler
# ---------------------------------------------------------------------------
class TestSetupCommandHandler:
    def test_status_no_files(self, tmp_telemetry):
        """Status shows NOT FOUND when neither file exists."""
        result = setup.handle_command("")
        assert "NOT FOUND" in result
        assert "pricing auto" in result
        assert "budget default" in result

    def test_both_files_found_status(self, tmp_telemetry):
        """Status shows 'found' when both files exist."""
        tele_dir = tmp_telemetry / "telemetry"
        tele_dir.mkdir(parents=True, exist_ok=True)
        (tele_dir / "pricing.yaml").write_text("models: {}\n")
        (tele_dir / "budget.yaml").write_text("budgets: {}\n")

        result = setup.handle_command("")
        assert "found" in result

    def test_pricing_auto_via_command(self, tmp_telemetry):
        """/setup pricing auto triggers fetch and writes file."""
        with patch.object(setup, "_fetch_openrouter_models", return_value={}):
            setup.handle_command("pricing auto")
        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert pricing_file.exists()

    def test_budget_default_via_command(self, tmp_telemetry):
        """/setup budget default writes file with expected content."""
        setup.handle_command("budget default")
        budget_file = tmp_telemetry / "telemetry" / "budget.yaml"
        assert budget_file.exists()
        content = budget_file.read_text()
        assert "global" in content

    def test_unknown_subcommand(self, tmp_telemetry):
        result = setup.handle_command("unknown thing")
        assert "Usage" in result

    def test_pricing_requires_option(self, tmp_telemetry):
        result = setup.handle_command("pricing")
        assert "Usage" in result

    def test_budget_requires_option(self, tmp_telemetry):
        result = setup.handle_command("budget")
        assert "Usage" in result


# ---------------------------------------------------------------------------
# Tests: OpenRouter fetch
# ---------------------------------------------------------------------------
class TestOpenRouterFetch:
    def _mock_urlopen(self, data: dict):
        """Helper to mock urllib with a JSON response."""
        mock_cm = MagicMock()
        raw = json.dumps(data).encode()
        mock_cm.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=raw)))
        mock_cm.__exit__ = MagicMock(return_value=False)
        return mock_cm

    def test_fetch_returns_models_with_positive_pricing(self):
        """_fetch_openrouter_models parses mocked API response."""
        mock_data = {
            "data": [
                {
                    "id": "anthropic/claude-sonnet-4",
                    "pricing": {"prompt": "0.000003", "completion": "0.000015"},
                },
                {
                    "id": "openrouter/auto",
                    "pricing": {"prompt": "-1", "completion": "-1"},
                },
                {
                    "id": "free-model",
                    "pricing": {"prompt": "0", "completion": "0"},
                },
            ]
        }
        with patch("urllib.request.urlopen", return_value=self._mock_urlopen(mock_data)):
            result = setup._fetch_openrouter_models()

        assert "anthropic/claude-sonnet-4" in result
        assert result["anthropic/claude-sonnet-4"]["input"] == 3.00
        assert result["anthropic/claude-sonnet-4"]["output"] == 15.00
        assert "openrouter/auto" not in result  # negative = excluded
        assert "free-model" not in result  # zero = excluded

    def test_fetch_handles_network_error(self):
        """_fetch_openrouter_models returns {} on network failure."""
        with patch("urllib.request.urlopen", side_effect=Exception("network down")):
            result = setup._fetch_openrouter_models()
        assert result == {}


# ---------------------------------------------------------------------------
# Tests: YAML dump
# ---------------------------------------------------------------------------
class TestYamlDump:
    def test_dump_produces_output_with_keys(self):
        data = {
            "models": {"test-model": {"input": 1.00, "output": 2.00}},
            "defaults": {"cache_read_multiplier": 0.10},
        }
        yaml_str = setup._dump_yaml(data)
        assert "test-model" in yaml_str
        assert "input" in yaml_str
        assert "output" in yaml_str

    def test_dump_round_trip_with_pyyaml(self):
        """If PyYAML is available, output should be parseable."""
        pytest.importorskip("yaml")
        data = {
            "models": {"model-a": {"input": 1.50, "output": 4.50}},
            "defaults": {"cache_read_multiplier": 0.10},
        }
        yaml_str = setup._dump_yaml(data)
        parsed = __import__("yaml").safe_load(yaml_str)
        assert "model-a" in parsed["models"]
        assert parsed["models"]["model-a"]["input"] == 1.50


# ---------------------------------------------------------------------------
# Tests: owl-alpha in defaults
# ---------------------------------------------------------------------------
class TestManualPricingEntries:
    """Manual pricing add/remove/show/list and auto/minimal merge behavior."""

    def test_add_creates_file_and_entry(self, tmp_telemetry):
        msg = setup.pricing_add("my-internal-llm", 1.20, 3.40)
        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert pricing_file.exists()
        assert "my-internal-llm" in pricing_file.read_text()
        assert "manual" in pricing_file.read_text()
        assert "added" in msg.lower()

    def test_add_with_cache_prices(self, tmp_telemetry):
        setup.pricing_add("m", 1.0, 2.0, cache_read=0.1, cache_write=1.25, reasoning=2.0)
        content = (tmp_telemetry / "telemetry" / "pricing.yaml").read_text()
        for k in ("cache_read", "cache_write", "reasoning"):
            assert k in content

    def test_add_then_update(self, tmp_telemetry):
        setup.pricing_add("m", 1.0, 2.0)
        msg = setup.pricing_add("m", 5.0, 10.0)
        assert "updated" in msg.lower()
        # Verify the new prices stuck.
        import yaml

        data = yaml.safe_load((tmp_telemetry / "telemetry" / "pricing.yaml").read_text())
        assert data["models"]["m"]["input"] == 5.0
        assert data["models"]["m"]["output"] == 10.0

    def test_remove(self, tmp_telemetry):
        setup.pricing_add("m", 1.0, 2.0)
        msg = setup.pricing_remove("m")
        assert "Removed" in msg
        import yaml

        data = yaml.safe_load((tmp_telemetry / "telemetry" / "pricing.yaml").read_text())
        assert "m" not in data["models"]

    def test_remove_missing_model(self, tmp_telemetry):
        setup.pricing_add("a", 1.0, 2.0)
        msg = setup.pricing_remove("not-there")
        assert "not found" in msg

    def test_remove_with_no_file(self, tmp_telemetry):
        msg = setup.pricing_remove("any")
        assert "No pricing.yaml" in msg

    def test_show_existing(self, tmp_telemetry):
        setup.pricing_add("m", 1.0, 2.0)
        out = setup.pricing_show("m")
        assert "m:" in out
        assert "input" in out
        assert "manual" in out

    def test_show_missing(self, tmp_telemetry):
        setup.pricing_add("a", 1.0, 2.0)
        out = setup.pricing_show("nope")
        assert "not found" in out

    def test_list_all(self, tmp_telemetry):
        setup.pricing_add("a", 1.0, 2.0)
        setup.pricing_add("b", 3.0, 4.0)
        out = setup.pricing_list()
        assert "a" in out and "b" in out
        assert "2 model" in out

    def test_list_filter_manual(self, tmp_telemetry):
        with patch.object(setup, "_fetch_openrouter_models", return_value={}):
            setup.handle_command("pricing minimal")
        setup.pricing_add("my-llm", 1.0, 2.0)
        manual_only = setup.pricing_list("manual")
        assert "my-llm" in manual_only
        assert "claude-opus-4-8" not in manual_only

    def test_path(self, tmp_telemetry):
        assert setup.pricing_path().endswith("pricing.yaml")

    def test_auto_preserves_manual_entries(self, tmp_telemetry):
        """Re-running auto must NOT clobber a manually-added model."""
        setup.pricing_add("my-internal-llm", 9.99, 19.99)
        with patch.object(setup, "_fetch_openrouter_models", return_value={}):
            setup.handle_command("pricing auto")
        import yaml

        data = yaml.safe_load((tmp_telemetry / "telemetry" / "pricing.yaml").read_text())
        assert data["models"]["my-internal-llm"]["input"] == 9.99
        assert data["models"]["my-internal-llm"]["output"] == 19.99
        assert data["models"]["my-internal-llm"].get("_source") == "manual"
        # Built-in seed should still be present.
        assert "claude-opus-4-8" in data["models"]

    def test_minimal_preserves_manual_entries(self, tmp_telemetry):
        setup.pricing_add("my-llm", 9.99, 19.99)
        setup.handle_command("pricing minimal")
        import yaml

        data = yaml.safe_load((tmp_telemetry / "telemetry" / "pricing.yaml").read_text())
        assert data["models"]["my-llm"]["input"] == 9.99


class TestPricingSubcommands:
    """/setup pricing add|remove|show|list|path slash subcommands."""

    def test_pricing_add_via_command(self, tmp_telemetry):
        out = setup.handle_command("pricing add my-model 1.0 2.0")
        assert "added" in out.lower()
        pricing_file = tmp_telemetry / "telemetry" / "pricing.yaml"
        assert "my-model" in pricing_file.read_text()

    def test_pricing_add_with_cache(self, tmp_telemetry):
        setup.handle_command("pricing add my-model 1.0 2.0 0.1 1.25")
        content = (tmp_telemetry / "telemetry" / "pricing.yaml").read_text()
        assert "cache_read" in content
        assert "cache_write" in content

    def test_pricing_add_missing_args(self, tmp_telemetry):
        out = setup.handle_command("pricing add only-name")
        assert "Usage" in out

    def test_pricing_add_invalid_price(self, tmp_telemetry):
        out = setup.handle_command("pricing add m foo 2.0")
        assert "Error" in out

    def test_pricing_add_negative_price(self, tmp_telemetry):
        out = setup.handle_command("pricing add m -1.0 2.0")
        assert "Error" in out

    def test_pricing_remove_via_command(self, tmp_telemetry):
        setup.handle_command("pricing add my-model 1.0 2.0")
        out = setup.handle_command("pricing remove my-model")
        assert "Removed" in out

    def test_pricing_show_via_command(self, tmp_telemetry):
        setup.handle_command("pricing add my-model 1.0 2.0")
        out = setup.handle_command("pricing show my-model")
        assert "my-model" in out

    def test_pricing_list_via_command(self, tmp_telemetry):
        setup.handle_command("pricing add a 1 2")
        setup.handle_command("pricing add b 3 4")
        out = setup.handle_command("pricing list")
        assert "a" in out and "b" in out

    def test_pricing_path_via_command(self, tmp_telemetry):
        out = setup.handle_command("pricing path")
        assert out.endswith("pricing.yaml")

    def test_pricing_no_subcommand_shows_usage(self, tmp_telemetry):
        out = setup.handle_command("pricing")
        assert "Usage" in out
        assert "add" in out and "remove" in out


class TestCLIPricing:
    """Standalone CLI: hermes-telemetry pricing <subcommand>."""

    def test_cli_pricing_add(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "add", "my-llm", "1.5", "3.5"])
        out = capsys.readouterr().out
        assert "added" in out.lower()
        assert (tmp_telemetry / "telemetry" / "pricing.yaml").exists()

    def test_cli_pricing_add_with_flags(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(
            [
                "pricing",
                "add",
                "m",
                "1.0",
                "2.0",
                "--cache-read",
                "0.1",
                "--cache-write",
                "1.25",
            ]
        )
        content = (tmp_telemetry / "telemetry" / "pricing.yaml").read_text()
        assert "cache_read" in content
        assert "cache_write" in content

    def test_cli_pricing_remove(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "add", "m", "1", "2"])
        capsys.readouterr()
        telemetry_cli.main(["pricing", "remove", "m"])
        out = capsys.readouterr().out
        assert "Removed" in out

    def test_cli_pricing_show(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "add", "m", "1", "2"])
        capsys.readouterr()
        telemetry_cli.main(["pricing", "show", "m"])
        out = capsys.readouterr().out
        assert "m:" in out

    def test_cli_pricing_list(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "add", "a", "1", "2"])
        telemetry_cli.main(["pricing", "add", "b", "3", "4"])
        capsys.readouterr()
        telemetry_cli.main(["pricing", "list"])
        out = capsys.readouterr().out
        assert "a" in out and "b" in out

    def test_cli_pricing_list_filter_manual(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        with patch.object(setup, "_fetch_openrouter_models", return_value={}):
            telemetry_cli.main(["pricing", "minimal"])
        telemetry_cli.main(["pricing", "add", "manual-m", "1", "2"])
        capsys.readouterr()
        telemetry_cli.main(["pricing", "list", "--source", "manual"])
        out = capsys.readouterr().out
        assert "manual-m" in out
        assert "claude-opus-4-8" not in out

    def test_cli_pricing_path(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "path"])
        out = capsys.readouterr().out.strip()
        assert out.endswith("pricing.yaml")

    def test_cli_pricing_minimal_preserves_manual(self, tmp_telemetry, capsys):
        from hermes_telemetry import telemetry_cli

        telemetry_cli.main(["pricing", "add", "manual-m", "9.99", "19.99"])
        telemetry_cli.main(["pricing", "minimal"])
        import yaml

        data = yaml.safe_load((tmp_telemetry / "telemetry" / "pricing.yaml").read_text())
        assert data["models"]["manual-m"]["input"] == 9.99


class TestOwlAlphaInDefaults:
    def test_owl_alpha_in_default_seed(self):
        """owl-alpha should be in the built-in pricing seed."""
        assert "owl-alpha" in setup._DEFAULT_SEED
        assert setup._DEFAULT_SEED["owl-alpha"]["input"] == 0.00
        assert setup._DEFAULT_SEED["owl-alpha"]["output"] == 0.00

    def test_setup_generates_owl_alpha_in_output(self, tmp_telemetry):
        """After setup, pricing.yaml should contain owl-alpha."""
        with patch.object(setup, "_fetch_openrouter_models", return_value={}):
            setup.run(interactive=False)
        content = (tmp_telemetry / "telemetry" / "pricing.yaml").read_text()
        assert "owl-alpha" in content
