"""Manual test battery: :free suffix pricing + free->paid id-change alert (#32).

Exercises the REAL plugin lifecycle (register() + the actual hook closures
Hermes would call) against an isolated HERMES_HOME, the same pattern as
poc_setup.py / poc_setup_cmd.py. Not a substitute for pytest — this is the
end-to-end sanity check that the unit tests can't show: hooks wired together,
in hook-firing order, exactly as Hermes would drive them.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

tmp = tempfile.mkdtemp(prefix="hermes_poc_free32_")
os.environ["HERMES_HOME"] = tmp
os.environ["HERMES_TELEMETRY_NO_SETUP"] = "1"  # skip auto pricing/budget generation
print(f"[PoC] HERMES_HOME = {tmp}\n")

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "hermes_telemetry", REPO / "__init__.py", submodule_search_locations=[str(REPO)]
)
plugin_mod = importlib.util.module_from_spec(_spec)
sys.modules["hermes_telemetry"] = plugin_mod
_spec.loader.exec_module(plugin_mod)

from hermes_telemetry import db, pricing  # noqa: E402


class FakeCtx:
    """Minimal stand-in for Hermes' PluginContext: just records hooks."""

    def __init__(self):
        self.hooks: dict[str, list] = {}
        self.commands: dict[str, object] = {}

    def register_hook(self, name, fn):
        self.hooks.setdefault(name, []).append(fn)

    def register_command(self, name, fn, **_kw):
        self.commands[name] = fn

    # register_cli_command intentionally NOT defined: register() catches
    # AttributeError/TypeError for Hermes versions without this API.


def fire(ctx, hook_name, **kwargs):
    results = []
    for fn in ctx.hooks.get(hook_name, []):
        results.append(fn(**kwargs))
    return results


ok = True


def check(label, cond):
    global ok
    mark = "✅" if cond else "❌"
    print(f"  {mark} {label}")
    if not cond:
        ok = False


print("=" * 70)
print("register() — wiring hooks against isolated HERMES_HOME")
print("=" * 70)
ctx = FakeCtx()
plugin_mod.register(ctx)
for h in ("on_session_start", "post_api_request", "pre_llm_call"):
    check(f"hook registered: {h}", h in ctx.hooks)
print()

SESSION = "sess-free32-001"
PROVIDER_OR = "openrouter"
PROVIDER_NIM = "nvidia"

print("=" * 70)
print("Phase 1 — promo live: short :free id (NIM-style) costs $0")
print("=" * 70)
fire(
    ctx,
    "on_session_start",
    session_id=SESSION,
    model="nvidia/nemotron-3-ultra:free",
    platform="cli",
)
fire(
    ctx,
    "post_api_request",
    session_id=SESSION,
    model="nvidia/nemotron-3-ultra:free",
    provider=PROVIDER_NIM,
    api_duration=1.2,
    usage={"input_tokens": 1_000_000, "output_tokens": 500_000},
    api_call_count=1,
)
run = db.get_run(SESSION)
check("session cost after free call == 0.0", run["cost_usd"] == 0.0)
check(
    "nvidia/nemotron-3-ultra:free recorded as known-free",
    db.is_known_free_model("nvidia/nemotron-3-ultra:free", PROVIDER_NIM),
)
alerts = fire(ctx, "pre_llm_call", session_id=SESSION)
check("no free->paid alert injected while still free", all(a is None for a in alerts))
print()

print("=" * 70)
print("Phase 2 — promo live: OpenRouter LONG :free id ALSO costs $0")
print("(this is the exact case the user found on openrouter.ai)")
print("=" * 70)
SESSION2 = "sess-free32-002"
LONG_FREE = "nvidia/nemotron-3-ultra-550b-a55b:free"
fire(ctx, "on_session_start", session_id=SESSION2, model=LONG_FREE, platform="cli")
fire(
    ctx,
    "post_api_request",
    session_id=SESSION2,
    model=LONG_FREE,
    provider=PROVIDER_OR,
    api_duration=0.8,
    usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    api_call_count=1,
)
run2 = db.get_run(SESSION2)
check(f"{LONG_FREE} costs $0.00 (was $3.00 before the fix)", run2["cost_usd"] == 0.0)
check("recorded as known-free", db.is_known_free_model(LONG_FREE, PROVIDER_OR))
print()

print("=" * 70)
print("Phase 3 — promo ENDS: gateway drops :free, bills the bare id")
print("=" * 70)
fire(
    ctx,
    "post_api_request",
    session_id=SESSION,
    model="nvidia/nemotron-3-ultra",
    provider=PROVIDER_NIM,
    api_duration=1.0,
    usage={"input_tokens": 1_000_000, "output_tokens": 500_000},
    api_call_count=2,
)
run = db.get_run(SESSION)
check("bare paid id now costs > $0", run["cost_usd"] > 0.0)
alerts = fire(ctx, "pre_llm_call", session_id=SESSION)
injected = [a for a in alerts if a]
check("free->paid alert injected this turn", len(injected) == 1)
if injected:
    ctx_text = injected[0]["context"]
    print(f"    context: {ctx_text.splitlines()[0][:90]}...")
    check("alert mentions the model id", "nemotron-3-ultra" in ctx_text)
alerts_again = fire(ctx, "pre_llm_call", session_id=SESSION)
check("alert does NOT repeat next turn (one-shot)", all(a is None for a in alerts_again))
print()

print("=" * 70)
print("Phase 4 — promo ENDS: gateway bills the OpenRouter LONG suffixed id")
print("(id-change case: never seen as paid before, :free row had the long id)")
print("=" * 70)
fire(
    ctx,
    "post_api_request",
    session_id=SESSION2,
    model="nvidia/nemotron-3-ultra-550b-a55b",
    provider=PROVIDER_OR,
    api_duration=0.9,
    usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    api_call_count=2,
)
run2 = db.get_run(SESSION2)
check("suffixed paid id now costs > $0", run2["cost_usd"] > 0.0)
alerts2 = fire(ctx, "pre_llm_call", session_id=SESSION2)
injected2 = [a for a in alerts2 if a]
check("free->paid alert injected for the long-form id change", len(injected2) == 1)
print()

print("=" * 70)
print("Phase 5 — regression check: pre-existing super:free seed")
print("=" * 70)
SESSION3 = "sess-free32-003"
SUPER_FREE = "nvidia/nemotron-3-super-120b-a12b:free"
fire(ctx, "on_session_start", session_id=SESSION3, model=SUPER_FREE, platform="cli")
fire(
    ctx,
    "post_api_request",
    session_id=SESSION3,
    model=SUPER_FREE,
    provider=PROVIDER_OR,
    api_duration=0.5,
    usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000},
    api_call_count=1,
)
run3 = db.get_run(SESSION3)
check(f"{SUPER_FREE} costs $0.00 (was $0.60 before the fix)", run3["cost_usd"] == 0.0)
print()

print("=" * 70)
print("Phase 6 — unrelated unknown model: still warns, never known-free")
print("=" * 70)
SESSION4 = "sess-free32-004"
UNKNOWN = "totally-unrelated-unknown-model"
fire(ctx, "on_session_start", session_id=SESSION4, model=UNKNOWN, platform="cli")
fire(
    ctx,
    "post_api_request",
    session_id=SESSION4,
    model=UNKNOWN,
    provider="",
    api_duration=0.3,
    usage={"input_tokens": 1000, "output_tokens": 1000},
    api_call_count=1,
)
check("unknown model is NOT known-free", not db.is_known_free_model(UNKNOWN, ""))
print()

print("=" * 70)
print("Phase 7 — explicit pricing.yaml entry still overrides the :free rule")
print("=" * 70)
pricing_file = Path(tmp) / "telemetry" / "pricing.yaml"
pricing_file.parent.mkdir(parents=True, exist_ok=True)
pricing_file.write_text(
    'models:\n  "some-vendor/special:free":\n    input: 9.99\n    output: 0.0\n'
)
pricing.reload_custom_pricing()
cost = pricing.estimate_cost({"input_tokens": 1_000_000}, "some-vendor/special:free")
check("explicit :free entry overrides the built-in $0 rule", abs(cost - 9.99) < 1e-9)
print()

print("=" * 70)
print("Phase 8 — /stats reflects everything end-to-end")
print("=" * 70)
from hermes_telemetry import stats  # noqa: E402

out = stats.handle("")
print(out)
check("/stats runs without error", isinstance(out, str) and len(out) > 0)
print()

shutil.rmtree(tmp, ignore_errors=True)
print(f"[PoC] Cleaned up {tmp}\n")

print("=" * 70)
if ok:
    print("ALL CHECKS PASSED ✅ — issue #32 :free suffix fix verified end-to-end")
else:
    print("SOME CHECKS FAILED ❌ — see ❌ markers above")
    sys.exit(1)
