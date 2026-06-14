# PR #27 Manual Testing Guide

End-to-end manual validation steps for the `feat/provider-guard-and-nim-pricing`
branch. Covers the provider-aware pricing guard (#24), NVIDIA NIM price seeds
(#12 Phase 1), and the silent-$0 treatment for subscription models.

Each section maps to a concrete fix in the PR. Steps 1–5 exercise
`pricing.estimate_cost()` directly (deterministic, no network). Step 6 is the
automated suite. Steps 7+ are the **end-to-end** validation that spawns real
agent runs and inspects what the plugin actually wrote to `telemetry.db`.

---

## 0. Preparation (mandatory isolation)

Never touch your real `~/.hermes`. Use a temporary `HERMES_HOME`:

```bash
cd /path/to/hermes-telemetry
export HERMES_HOME=$(mktemp -d)
mkdir -p "$HERMES_HOME/telemetry"
echo "HERMES_HOME=$HERMES_HOME"
```

Create a `pricing.yaml` that exercises the three relevant cases:

```bash
cat > "$HERMES_HOME/telemetry/pricing.yaml" <<'YAML'
models:
  # Case A — entry sourced from OpenRouter (the kind auto-refresh produces)
  "qwen/qwen-2.5-72b-instruct":
    input: 0.90
    output: 0.90
    _source: openrouter

  # Case B — same Qwen model but served by Nous under subscription → $0, no warning
  "hermes-4-qwen-72b":
    input: 0.00
    output: 0.00
    _subscription: true

  # Case C — id collision: nemotron exists in NIM (seed) and OpenRouter (this yaml)
  "nvidia/nemotron-70b-instruct":
    input: 5.00
    output: 5.00
    _source: openrouter
YAML
```

---

## 1. Provider-aware guard (#24)

Verifies that a price marked `_source: openrouter` is **not** applied to a call
served by a different provider.

```bash
python3 - <<'PY'
import pricing
u = dict(input_tokens=1_000_000, output_tokens=0)
# Served by OpenRouter → uses the price ($0.90)
print("openrouter:", pricing.estimate_cost(u, "qwen/qwen-2.5-72b-instruct", "openrouter"))
# Served by Nous → guard blocks the entry, falls to unknown → $0.00 + 1 warning
print("nous:      ", pricing.estimate_cost(u, "qwen/qwen-2.5-72b-instruct", "nous"))
# No provider (backward-compat) → eligible → $0.90
print("empty:     ", pricing.estimate_cost(u, "qwen/qwen-2.5-72b-instruct", ""))
PY
```

**Expected:** `openrouter: 0.9` · `nous: 0.0` (with a WARNING on stderr) · `empty: 0.9`.

---

## 2. Subscription model → $0 with no warning

```bash
python3 - <<'PY'
import logging, pricing
logging.basicConfig(level=logging.WARNING)
u = dict(input_tokens=1_000_000, output_tokens=1_000_000)
print("subscription:", pricing.estimate_cost(u, "hermes-4-qwen-72b", "nous"))
PY
```

**Expected:** `0.0` and **no warning** — a legitimate flat-rate $0, not a
missing-price $0.

---

## 3. NVIDIA NIM seeds (#12 Phase 1)

The five NIM models resolve even when the user has no `pricing.yaml`. Test them
with an empty `HERMES_HOME` to confirm the prices come from code:

```bash
python3 - <<'PY'
import os, tempfile
os.environ["HERMES_HOME"] = tempfile.mkdtemp()  # no pricing.yaml
import pricing
u = dict(input_tokens=1_000_000, output_tokens=0)
for m in ["nvidia/nemotron-70b-instruct",
          "nvidia/nemotron-nano-9b",
          "nvidia/nemotron-3-super-120b-a12b"]:
    print(m, "->", pricing.estimate_cost(u, m, "nvidia"))
PY
```

**Expected:**
```
nvidia/nemotron-70b-instruct -> 1.2
nvidia/nemotron-nano-9b -> 0.04
nvidia/nemotron-3-super-120b-a12b -> 0.1
```

---

## 4. NIM vs OpenRouter collision (same id)

With the `pricing.yaml` from step 0 (which has `nvidia/nemotron-70b-instruct` at
$5 marked `_source: openrouter`):

```bash
export HERMES_HOME=$HERMES_HOME  # the one from step 0
python3 - <<'PY'
import pricing
u = dict(input_tokens=1_000_000, output_tokens=0)
# provider=nvidia → guard blocks the OpenRouter yaml entry, falls to seed → 1.20
print("nvidia:    ", pricing.estimate_cost(u, "nvidia/nemotron-70b-instruct", "nvidia"))
# provider=openrouter → uses the yaml → 5.00
print("openrouter:", pricing.estimate_cost(u, "nvidia/nemotron-70b-instruct", "openrouter"))
PY
```

**Expected:** `nvidia: 1.2` (seed) · `openrouter: 5.0` (yaml). Confirms the
collided id routes by provider.

---

## 5. `:free` variant → $0

```bash
python3 - <<'PY'
import pricing
u = dict(input_tokens=1_000_000, output_tokens=1_000_000)
print(pricing.estimate_cost(u, "nvidia/nemotron-3-ultra:free", "nvidia"))
PY
```

**Expected:** `0.0` (falls through to the unknown-model fallback; free promos do
not need a seed).

> ⚠️ **Known cosmetic issue:** the call returns `0.0` correctly but emits a
> `"no price for model"` warning. Functionally the cost is right (free models
> are $0 by design), but the log noise is misleading. Follow-up: short-circuit
> the `:free` suffix to silence the warning. Not blocking for this PR.

---

## 6. Automated suite + lint (CI parity)

```bash
ruff format --check pricing.py pricing_refresh.py db.py __init__.py \
  tests/test_pricing.py tests/test_pricing_refresh.py tests/test_db.py

ruff check pricing.py pricing_refresh.py db.py __init__.py \
  tests/test_pricing.py tests/test_pricing_refresh.py tests/test_db.py

pytest tests/test_pricing.py tests/test_pricing_refresh.py tests/test_db.py -v
```

**Expected:** ruff format clean, ruff check passes, **93 tests passed**.

> The global `ruff check .` still fails on pre-existing issues in `budget.py`
> and `dashboard/serve.py` that this PR explicitly does not touch. Scope the
> lint to the files in the PR.

---

## 7. End-to-end validation (real LLM calls)

Steps 1–5 verify the pricing function in isolation. Steps 7–10 verify that the
**plugin actually writes the right `provider` and `cost_usd` to
`telemetry.db`** when Hermes Agent runs against a real provider.

### 7.1 Why `HERMES_HOME` isolation does not work end-to-end

`HERMES_HOME` is read by both the telemetry plugin **and** by Hermes Agent
itself (for `.env`, `config.yaml`, credentials). A fresh `HERMES_HOME` makes
the plugin happy but breaks Hermes — it can't find your provider keys and
fails with `"No inference provider configured"`.

**Approach:** snapshot the real `~/.hermes` files you'll touch, run with the
default `HERMES_HOME`, filter the resulting DB rows by timestamp.

```bash
unset HERMES_HOME

# Snapshot telemetry data (DB + yaml)
mkdir -p ~/.hermes-pr27/backup
cp -a ~/.hermes/telemetry ~/.hermes-pr27/backup/telemetry-$(date +%Y%m%d-%H%M%S)

# Install the test pricing.yaml at the real location
if [ -f ~/.hermes/telemetry/pricing.yaml ]; then
  mv ~/.hermes/telemetry/pricing.yaml ~/.hermes/telemetry/pricing.yaml.original
fi
cp /path/to/test-pricing.yaml ~/.hermes/telemetry/pricing.yaml
```

### 7.2 Sync the installed plugin with the PR branch

The plugin Hermes actually loads lives at `~/.hermes/plugins/hermes-telemetry/`
— it is **not** the same checkout as your dev clone. Replace it:

```bash
mv ~/.hermes/plugins/hermes-telemetry ~/.hermes/plugins/hermes-telemetry.bak
cp -r ~/hermes-telemetry ~/.hermes/plugins/hermes-telemetry
rm -rf ~/.hermes/plugins/hermes-telemetry/.git
grep -n "def estimate_cost" ~/.hermes/plugins/hermes-telemetry/pricing.py
# Expected: def estimate_cost(usage, model, provider=...)   (3 args)
```

### 7.3 Disable aggressive fallbacks

If `~/.hermes/config.yaml` has `fallback_providers:` configured, every failed
call will silently route to a fallback — and the DB will record the fallback's
provider, not the one you asked for. Comment them out **for the duration of
the test**:

```yaml
fallback_providers: []
```

Restart the gateway so the new config and the new plugin are picked up. On WSL
without user-systemd:

```bash
pkill -f "hermes_cli.main gateway"
sleep 3
nohup /home/$USER/.hermes/hermes-agent/venv/bin/python \
  -m hermes_cli.main gateway run > /tmp/hermes-gateway.log 2>&1 &
disown
sleep 3
pgrep -af "hermes_cli.main gateway"  # exactly one PID expected
```

### 7.4 Capture a baseline timestamp

```bash
date -u +%FT%H:%M
# Save this. The DB stores `ts` as ISO 8601 strings,
# so filter with: WHERE ts >= '2026-06-14T14:00'
```

---

## 8. Real-call validation #1 — `provider=nous` for an OpenRouter-looking model

The default model in many Nous Portal setups is `openrouter/owl-alpha` with
`provider: nous`. The id looks OpenRouter but the call is really served by
Nous — exactly the #24 bug scenario.

```bash
hermes -z "Reply with one word: ok"
```

Inspect the new row:

```bash
sqlite3 -header -column ~/.hermes/telemetry/telemetry.db "
  SELECT ts, provider, model, tokens_in, tokens_out, ROUND(cost_usd,6) AS cost_usd
    FROM llm_calls WHERE ts >= '2026-06-14T14:00' ORDER BY ts DESC LIMIT 3;"
```

**Expected:** a row with `provider=nous`, `model=owl-alpha`, `cost_usd=0.0`.
**Not** `provider=openrouter`. That column is the authoritative answer the
guard relies on.

---

## 9. Real-call validation #2 — subscription path is silent end-to-end

Add a subscription marker for `owl-alpha` so the next call gets cost=0
**without** the "no price for model" warning:

```bash
cat >> ~/.hermes/telemetry/pricing.yaml <<'YAML'

  # Nous Portal subscription model
  "owl-alpha":
    input: 0.00
    output: 0.00
    _subscription: true
YAML
```

Then run a function-level probe that captures warnings (running the agent
again is fine but doesn't tell you whether the warning was emitted; the Python
harness is more precise):

```bash
python3 - <<'PY'
import logging, io, pricing
buf = io.StringIO()
handler = logging.StreamHandler(buf)
handler.setLevel(logging.WARNING)
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.WARNING)
u = dict(input_tokens=18047, output_tokens=16)
cost = pricing.estimate_cost(u, "owl-alpha", "nous")
print(f"cost: {cost}")
print(f"warnings: {buf.getvalue().strip()!r}")
PY
```

**Expected:** `cost: 0.0` · `warnings: ''`. Confirms the subscription marker
distinguishes a legitimate $0 from an unknown-model $0.

---

## 10. Real-call validation #3 — model id family vs real serving provider

Same NVIDIA model family, two different providers, two different DB rows.
Demonstrates the plugin labels by **who actually served the call**, not by the
shape of the id.

```bash
# (a) Through OpenRouter
hermes -z "Reply ok" -m nvidia/nemotron-nano-9b-v2:free --provider openrouter

# (b) Through Nous Portal (NVIDIA free tier lives here, not on OpenRouter)
hermes -z "Reply ok" -m nvidia/nemotron-3-ultra:free --provider nous
```

```bash
sqlite3 -header -column ~/.hermes/telemetry/telemetry.db "
  SELECT ts, provider, model, ROUND(cost_usd,6) AS cost_usd
    FROM llm_calls WHERE ts >= '2026-06-14T14:00' ORDER BY ts DESC LIMIT 5;"
```

**Expected:** two rows whose `model` both start with `nvidia/...:free`, one with
`provider=openrouter` and one with `provider=nous`. Both `cost_usd=0.0` from
the `:free` suffix. The crucial part is the **divergent provider column** for
visually-similar model ids.

> A non-`:free` NVIDIA model going through a paid provider is **not testable
> with stock Hermes setups** because:
> - OpenRouter exposes NVIDIA under different names (`nvidia/llama-3.x-nemotron-*`),
>   not the NIM-style ids.
> - Nous Portal only serves NVIDIA in free tier.
> - NVIDIA NIM direct requires its own API key and provider config.
>
> Paid behaviour is covered by step 4 (Python) and by
> `test_nim_openrouter_collision_excluded_for_nvidia` in the suite.

---

## 11. Cleanup

```bash
# Restore real pricing.yaml
mv ~/.hermes/telemetry/pricing.yaml ~/.hermes/telemetry/pricing.yaml.test-leftover
mv ~/.hermes/telemetry/pricing.yaml.original ~/.hermes/telemetry/pricing.yaml

# Restore real plugin
rm -rf ~/.hermes/plugins/hermes-telemetry
mv ~/.hermes/plugins/hermes-telemetry.bak ~/.hermes/plugins/hermes-telemetry

# Restore fallback_providers in ~/.hermes/config.yaml (from the backup you made)

# Restart gateway
pkill -f "hermes_cli.main gateway"
sleep 3
nohup /home/$USER/.hermes/hermes-agent/venv/bin/python \
  -m hermes_cli.main gateway run > /tmp/hermes-gateway.log 2>&1 &
disown
```

Test rows in `telemetry.db` are **real LLM calls** — leave them as legitimate
history, or delete them by timestamp range if you want a clean slate.

---

## Findings checklist

- [x] **#24 guard:** `provider=nous` recorded for an `openrouter/...` id; same
  model family routes to two different `provider` values depending on who
  served the call (steps 1, 8, 10).
- [x] **Subscription silent-$0:** `_subscription: true` returns 0 with no
  warning (steps 2, 9).
- [x] **NIM seeds (#12):** five NVIDIA NIM models resolve with no `pricing.yaml`
  (step 3).
- [x] **Provider-aware collision resolution:** same id, two providers, two
  prices (step 4).
- [x] **`:free` short-circuit:** returns 0 (step 5, step 10).
- [x] **CI parity:** ruff format + ruff check + 93 tests pass (step 6).

## Known follow-ups (not blocking this PR)

1. **`:free` warning noise** — `pricing.estimate_cost(..., "nvidia/...:free", ...)`
   returns `0.0` correctly but emits `"no price for model"`. A short-circuit on
   the `:free` suffix would silence it.
2. **Gemini cost=0 under OpenRouter** — observed in one real-call row
   (`google/gemini-2.5-flash-lite` served by `openrouter`, `cost_usd=0`). Could
   be a hardcoded gemini default tagged `_source: google` getting blocked by
   the guard when the provider differs. Worth confirming before the next PR.
3. **Repo not pip-installable** — `pip install -e .` fails with
   `Multiple .egg-info directories found` because both `setup.py` and
   `pyproject.toml` exist alongside a flat layout. Not caused by this PR but
   surfaced during testing. Tests still run via `python -c "import pricing"`
   from the repo root.
