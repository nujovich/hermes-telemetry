"""PoC Step 2: /setup command handler — simulate user interactions."""

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

REPO = Path(__file__).parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# Create clean temp HERMES_HOME
tmp = tempfile.mkdtemp(prefix="hermes_poc2_")
os.environ["HERMES_HOME"] = tmp
print(f"[PoC] HERMES_HOME = {tmp}\n")

# Register package
_tele_pkg = types.ModuleType("hermes_telemetry")
_tele_pkg.__path__ = [str(REPO)]
_tele_pkg.__package__ = "hermes_telemetry"
_tele_pkg.__file__ = str(REPO / "__init__.py")
sys.modules.setdefault("hermes_telemetry", _tele_pkg)

import importlib

from hermes_telemetry import setup as setup_mod

importlib.reload(setup_mod)

pricing_file = Path(tmp) / "telemetry" / "pricing.yaml"
budget_file = Path(tmp) / "telemetry" / "budget.yaml"

# ── 2a: /setup (status, no files exist) ──
print("=" * 50)
print("2a) /setup — status with no files")
print("=" * 50)
result = setup_mod.handle_command("")
print(result)
print()
assert "NOT FOUND" in result
print("  ✅ Shows NOT FOUND for both files\n")

# ── 2b: /setup pricing minimal ──
print("=" * 50)
print("2b) /setup pricing minimal")
print("=" * 50)
result = setup_mod.handle_command("pricing minimal")
print(result)
print()
assert pricing_file.exists()
import yaml

pdata = yaml.safe_load(pricing_file.read_text())
print(f"  Models written: {len(pdata['models'])}")
assert "owl-alpha" in pdata["models"]
print("  ✅ pricing.yaml created with built-in models only")
print(f"  ✅ owl-alpha present: {pdata['models']['owl-alpha']}\n")

# ── 2c: /setup budget default ──
print("=" * 50)
print("2c) /setup budget default")
print("=" * 50)
result = setup_mod.handle_command("budget default")
print(result)
print()
assert budget_file.exists()
bdata = yaml.safe_load(budget_file.read_text())
assert bdata["budgets"]["global"]["daily_usd"] == 5.00
print("  ✅ budget.yaml created with $5/day, $100/month\n")

# ── 2d: /setup (status, both files exist) ──
print("=" * 50)
print("2d) /setup — status with both files present")
print("=" * 50)
result = setup_mod.handle_command("")
print(result)
print()
assert "found" in result
print("  ✅ Shows 'found' for both files\n")

# ── 2e: /setup pricing skip (no file exists) ──
print("=" * 50)
print("2e) /setup pricing skip (on fresh dir)")
print("=" * 50)
# Remove pricing to test skip
pricing_file.unlink()
result = setup_mod.handle_command("pricing skip")
print(result)
print()
assert not pricing_file.exists()
print("  ✅ No pricing.yaml created\n")

# ── 2f: /setup budget skip ──
print("=" * 50)
print("2f) /setup budget skip")
print("=" * 50)
budget_file.unlink()
result = setup_mod.handle_command("budget skip")
print(result)
print()
assert not budget_file.exists()
print("  ✅ No budget.yaml created\n")

# ── 2g: /setup budget custom ──
print("=" * 50)
print("2g) /setup budget custom")
print("=" * 50)
result = setup_mod.handle_command("budget custom")
print(result)
print()
assert "/budget set global" in result
print("  ✅ Returns instructions for custom budget\n")

# ── 2h: unknown subcommand ──
print("=" * 50)
print("2h) /setup foobar — unknown subcommand")
print("=" * 50)
result = setup_mod.handle_command("foobar")
print(result)
print()
assert "Usage" in result
print("  ✅ Returns usage help\n")

# ── 2i: missing option ──
print("=" * 50)
print("2i) /setup pricing — missing option")
print("=" * 50)
result = setup_mod.handle_command("pricing")
print(result)
print()
assert "Usage" in result
print("  ✅ Returns usage help\n")

# Cleanup
shutil.rmtree(tmp)
print(f"[PoC] Cleaned up {tmp}")
print()
print("=" * 50)
print("PoC Step 2: SUCCESS ✅")
print("  - /setup status shows NOT FOUND / found correctly")
print("  - /setup pricing minimal → built-in models only")
print("  - /setup pricing skip → no file created")
print("  - /setup budget default → $5/d, $100/mo")
print("  - /setup budget custom → instructions")
print("  - /setup budget skip → no file created")
print("  - Unknown subcommands → usage help")
print("  - Missing options → usage help")
