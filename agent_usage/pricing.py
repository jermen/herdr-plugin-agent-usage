"""Decimal token-cost estimates, separate from actual billed amounts."""
import json
import re
from decimal import Decimal
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name("prices.json").read_text())


def estimate(events, agent, config):
    models = {**CATALOG["models"], **config.get("models", {})}
    total = Decimal(0)
    priced, missing = 0, set()
    assumptions = set()
    for event in events:
        name = event["model"] or config.get("default_models", {}).get(agent)
        if not event["model"] and name:
            assumptions.add("configured_model_for_unidentified_events")
        # Only an explicit dated suffix is stripped; unknown families stay unknown.
        canonical = re.sub(r"-\d{8}$", "", name or "")
        canonical = config.get("aliases", {}).get(canonical, canonical)
        price = models.get(canonical)
        if not price:
            missing.add(name or "unknown")
            continue
        tokens = json.loads(event["tokens"])
        details = json.loads(event["details"]) if "details" in event.keys() else {}
        tier = details.get("service_tier") or config.get("service_tiers", {}).get(agent, "standard")
        if tier in ("default", "auto"):
            tier = "standard"
        rates = price.get("tiers", {}).get(tier)
        if rates is None:
            missing.add((name or "unknown") + ":tier:" + tier)
            continue
        rates = dict(rates)
        assumed_tier = not details.get("service_tier")
        if assumed_tier:
            assumptions.add("configured_or_standard_service_tier")
        request_input = details.get("request_input")
        if request_input is None:
            request_input = sum(tokens.get(k, 0) for k in ("input", "cache_read", "cache_write"))
        threshold = price.get("long_context_threshold")
        if threshold and request_input > threshold:
            for k in ("input", "cache_read", "cache_write", "cache_write_1h"):
                if k in rates:
                    rates[k] = str(Decimal(str(rates[k])) * Decimal("2"))
            rates["output"] = str(Decimal(str(rates["output"])) * Decimal("1.5"))
        if details.get("speed") == "fast":
            multiplier = price.get("fast_multiplier")
            if multiplier is None:
                missing.add((name or "unknown") + ":fast")
                continue
            rates = {k: str(Decimal(str(v)) * Decimal(str(multiplier))) for k, v in rates.items()}
        multiplier = Decimal(str(config.get("multipliers", {}).get(agent, "1")))
        if not multiplier.is_finite() or multiplier < 0:
            raise ValueError("invalid price multiplier")
        one_hour = min(tokens.get("cache_write_1h", 0), tokens.get("cache_write", 0))
        quantities = {k: tokens.get(k, 0) for k in ("input", "cache_read", "cache_write", "output")}
        quantities["cache_write"] -= one_hour
        quantities["cache_write_1h"] = one_hour
        if any(q and key not in rates for key, q in quantities.items()):
            missing.add((name or "unknown") + ":token_rate")
            continue
        cost = Decimal(0)
        for key, quantity in quantities.items():
            rate = Decimal(str(rates.get(key, 0)))
            if not rate.is_finite() or rate < 0:
                raise ValueError("invalid token price")
            cost += Decimal(quantity) * rate / 1_000_000
        total += cost * multiplier
        priced += 1
    return {"kind": "estimated", "basis": "API-equivalent token value; not a subscription charge",
            "currency": "USD", "amount": str(total) if priced or not events else None,
            "priced_events": priced, "unpriced_events": len(events) - priced,
            "coverage": "complete" if priced == len(events) else "partial" if priced else "unavailable",
            "unpriced_models": sorted(missing), "assumptions": sorted(assumptions),
            "price_catalog_date": CATALOG["verified_at"], "price_basis": "current catalog, including historical usage",
            "excludes": ["tool fees", "tax", "contract discounts", "unconfigured regional uplift"]}
