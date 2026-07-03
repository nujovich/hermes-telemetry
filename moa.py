"""MoA (Mixture-of-Agents) awareness for hermes-telemetry.

Hermes MoA is a *virtual provider*. When a user selects a MoA preset as their
model, ``post_api_request`` fires with ``provider="moa"`` and
``model="<preset>"`` (the preset NAME, not a real model id), while ``usage`` and
``response_model`` belong to the preset's *aggregator* — the acting model that
actually ran and spent the money.

Two consequences this module addresses:

1. **Pricing / attribution.** ``provider="moa"`` is not a real provider, so the
   pricing engine can't price it and the provider-aware guard would reject the
   aggregator's true (e.g. OpenRouter) rate as a ``provider_assumed`` fallback
   (noisy warning, possibly wrong). We resolve the preset to its aggregator's
   real ``provider`` / ``model`` so the call is priced and grouped under the
   model that actually ran.

2. **Reference-model blind spot.** A MoA iteration also runs N *reference*
   models. Those calls go through Hermes' auxiliary ``call_llm`` path, which
   fires NO plugin hooks (verified against ``agent/auxiliary_client.py`` — it
   contains no ``invoke_hook``), so their tokens are unrecoverable from
   telemetry — the same class of limitation as subagent token attribution. We
   record the aggregator under its real provider and tag the row with the preset
   name so ``/stats`` and the dashboard can flag that the recorded cost is a
   lower bound (references untracked).

Verified against ``NousResearch/hermes-agent@main``:

- ``agent/agent_init.py`` (``provider == "moa"`` ⇒ ``agent.model`` = preset name,
  ``agent.base_url = "moa://local"``, client is ``MoAClient``);
- ``agent/moa_loop.py`` (references + aggregator run via auxiliary ``call_llm``;
  ``MoAChatCompletions.create`` returns the *aggregator* response);
- ``agent/auxiliary_client.py`` (no hook dispatch);
- ``agent/conversation_loop.py`` (``post_api_request`` fires once with
  ``provider="moa"``, ``model=<preset>``, ``response_model`` = aggregator id,
  ``usage`` = aggregator usage);
- ``hermes_cli/moa_config.py`` (``resolve_moa_preset(config, name)`` returns
  ``{reference_models: [{provider, model}], aggregator: {provider, model}, ...}``).
"""

from __future__ import annotations

import logging
from typing import Any

tele_log = logging.getLogger("hermes_telemetry")

# The virtual provider label Hermes reports for every MoA preset call.
MOA_PROVIDER = "moa"


def is_moa(provider: str | None) -> bool:
    """True when a ``post_api_request`` call came from the MoA virtual provider."""
    return (provider or "").strip().lower() == MOA_PROVIDER


def aggregator_from_preset(preset: dict[str, Any] | None) -> tuple[str, str]:
    """Return ``(provider, model)`` of the aggregator slot from a resolved preset.

    Returns ``("", "")`` when the preset is missing or malformed. Pure — no I/O.
    """
    if not isinstance(preset, dict):
        return "", ""
    agg = preset.get("aggregator")
    if not isinstance(agg, dict):
        return "", ""
    return str(agg.get("provider") or "").strip(), str(agg.get("model") or "").strip()


def reference_labels(preset: dict[str, Any] | None) -> list[str]:
    """Return ``provider:model`` labels for the preset's reference models.

    Advisory only — reference-model token usage is not captured (see module
    docstring). Pure — no I/O.
    """
    if not isinstance(preset, dict):
        return []
    labels: list[str] = []
    for slot in preset.get("reference_models") or []:
        if isinstance(slot, dict):
            prov = str(slot.get("provider") or "").strip()
            model = str(slot.get("model") or "").strip()
            labels.append(f"{prov}:{model}")
    return labels


def _resolve_preset_via_hermes(preset_name: str) -> dict[str, Any] | None:
    """Resolve a preset from Hermes' live config. Isolated for test monkeypatch.

    Uses ``hermes_cli`` (available inside the runtime). ``load_config()`` honors
    ``HERMES_HOME`` via ``get_hermes_home()``, so it respects the test-isolation
    contract. May raise if ``hermes_cli`` is unavailable or the preset is
    unknown; the public wrapper below swallows that.
    """
    from hermes_cli.config import load_config
    from hermes_cli.moa_config import resolve_moa_preset

    cfg = load_config().get("moa") or {}
    return resolve_moa_preset(cfg, preset_name)


def resolve_preset(preset_name: str) -> dict[str, Any] | None:
    """Best-effort resolve a MoA preset name → normalized preset dict, or ``None``.

    Swallows every error (``hermes_cli`` missing, config unreadable, unknown
    preset) so the caller falls back to the raw hook values. Never raises.
    """
    if not preset_name:
        return None
    try:
        preset = _resolve_preset_via_hermes(preset_name)
    except Exception as exc:  # pragma: no cover - defensive
        tele_log.debug("MoA preset resolution failed for %r: %s", preset_name, exc)
        return None
    return preset if isinstance(preset, dict) else None
