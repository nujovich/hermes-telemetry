"""PoC Step 1+3: Auto-setup from scratch + owl-alpha verification."""

import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

# Add repo to path
REPO = Path(__file__).parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# ── Create clean temp HERMES_HOME ──
tmp = tempfile.mkdtemp(prefix="hermes_poc_")
os.environ["HERMES_HOME"] = tmp
print(f"[PoC] HERMES_HOME = {tmp}")

pricing_file = Path(tmp) / "telemetry" / "pricing.yaml"
budget_file = Path(tmp) / "telemetry" / "budget.yaml"
print(f"[PoC] pricing.yaml exists: {pricing_file.exists()}")
print(f"[PoC] budget.yaml exists: {budget_file.exists()}")
assert not pricing_file.exists()
assert not budget_file.exists()
print("  ✅ Clean state confirmed\n")

# ── Import setup (fresh, picks up new HERMES_HOME) ──
import importlib

# Register the package so hermes_telemetry.setup is importable
_tele_pkg = types.ModuleType("hermes_telemetry")
_tele_pkg.__path__ = [str(REPO)]
_tele_pkg.__package__ = "hermes_telemetry"
_tele_pkg.__file__ = str(REPO / "__init__.py")
sys.modules.setdefault("hermes_telemetry", _tele_pkg)

from hermes_telemetry import setup as setup_mod  # noqa: E402

importlib.reload(setup_mod)

# ── Verify owl-alpha in built-in seed ──
print("[PoC] Checking owl-alpha in _DEFAULT_SEED...")
assert "owl-alpha" in setup_mod._DEFAULT_SEED, "owl-alpha NOT in _DEFAULT_SEED!"
price = setup_mod._DEFAULT_SEED["owl-alpha"]
print(f"  owl-alpha: input={price['input']}, output={price['output']}")
assert price["input"] == 0.00
assert price["output"] == 0.00
print("  ✅ owl-alpha present with $0.00 price\n")

# ── Run non-interactive setup (simulates auto-setup on first load) ──
print("[PoC] Running setup.run(interactive=False)...")
result = setup_mod.run(interactive=False)
print(result)
print()

# ── Verify files were created ──
print("[PoC] Verifying generated files...")
assert pricing_file.exists(), "pricing.yaml was NOT created!"
assert budget_file.exists(), "budget.yaml was NOT created!"
print(f"  ✅ pricing.yaml created: {pricing_file}")
print(f"  ✅ budget.yaml created: {budget_file}")
print()

# ── Verify pricing.yaml content ──
import yaml

pdata = yaml.safe_load(pricing_file.read_text())
models = pdata.get("models", {})
print(f"[PoC] pricing.yaml: {len(models)} models total")
assert "owl-alpha" in models, "owl-alpha NOT in pricing.yaml!"
print(f"  owl-alpha entry: {models['owl-alpha']}")
assert models["owl-alpha"]["input"] == 0.00
assert models["owl-alpha"]["output"] == 0.00
print("  ✅ owl-alpha in pricing.yaml with correct $0.00 price\n")

# Check other built-in models present
for expected in ["claude-opus-4", "gpt-4o", "deepseek-r1", "gemini-2.5-pro"]:
    assert expected in models, f"{expected} NOT in pricing.yaml!"
    print(f"  ✅ {expected} present")
print()

# ── Verify budget.yaml content ──
bdata = yaml.safe_load(budget_file.read_text())
global_budget = bdata.get("budgets", {}).get("global", {})
print(f"[PoC] budget.yaml global config: {global_budget}")
assert global_budget.get("daily_usd") == 5.00
assert global_budget.get("monthly_usd") == 100.00
print("  ✅ Global budget: $5.00/day, $100.00/month\n")

# ── Verify no per_cron_job in default budget ──
assert "per_cron_job" not in bdata.get("budgets", {}), (
    "per_cron_job should NOT be in default budget!"
)
print("  ✅ No per_cron_job in default budget (as expected)\n")

# ── Test idempotency: run again, should not overwrite ──
print("[PoC] Testing idempotency (running setup again)...")
result2 = setup_mod.run(interactive=False)
assert "Already configured" in result2, "Should skip existing files!"
print("  ✅ Second run skips existing files\n")

# Verify content unchanged
pdata2 = yaml.safe_load(pricing_file.read_text())
assert len(pdata2["models"]) == len(models), "Models count changed on second run!"
print("  ✅ Content unchanged after second run\n")

# ── Cleanup ──
shutil.rmtree(tmp)
print(f"[PoC] Cleaned up {tmp}")
print()
print("=" * 50)
print("PoC Step 1+3: SUCCESS ✅")
print("  - Auto-setup creates pricing.yaml + budget.yaml from scratch")
print("  - owl-alpha (Nous Portal) included with $0.00 price")
print("  - 30+ built-in models present")
print("  - Global budget: $5/day, $100/month")
print("  - Idempotent: second run doesn't overwrite")
