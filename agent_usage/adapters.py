"""Read metrics from native logs without persisting conversation content."""
from .model import context, counts, digest, limits, number, timestamp


def parse(agent, row, state):
    """Return (usage event, telemetry patch), updating only parser counters."""
    ts = timestamp(row.get("timestamp"))
    patch = {}
    event = None
    if agent == "codex":
        payload = row.get("payload") or {}
        if row.get("type") == "turn_context":
            state["model"] = payload.get("model")
            state["service_tier"] = payload.get("service_tier")
        if row.get("type") == "session_meta":
            state["provider"] = payload.get("model_provider")
        if row.get("type") != "event_msg" or payload.get("type") != "token_count":
            return None, {}
        raw_limits = payload.get("rate_limits")
        if isinstance(raw_limits, dict):
            patch["limits"] = limits(raw_limits)
            patch["limits_scope"] = "account"
            patch["plan_type"] = raw_limits.get("plan_type")
        info = payload.get("info") or {}
        total = info.get("total_token_usage")
        last = info.get("last_token_usage") or {}
        if total:
            old = state.get("total", {})
            # Repeated token_count notifications have unchanged cumulative totals.
            delta = {k: max(0, int(number(v) or 0) - int(number(old.get(k)) or 0))
                     for k, v in total.items()}
            if old and any(int(number(total.get(k)) or 0) < int(number(v) or 0)
                           for k, v in old.items()):
                # Counter reset/resume: only the explicit last request is attributable.
                delta = last
            state["total"] = total
            read = delta.get("cached_input_tokens", 0)
            write = delta.get("cache_write_input_tokens", 0)
            tok = counts(input=max(0, delta.get("input_tokens", 0) - read - write),
                         cache_read=read, cache_write=write,
                         output=delta.get("output_tokens"), reasoning=delta.get("reasoning_output_tokens"))
            if any(tok.values()):
                event = {"id": digest([ts, total]), "at": ts, "tokens": tok,
                         "details": {"service_tier": state.get("service_tier"),
                                     "request_input": last.get("input_tokens"),
                                     "provider": state.get("provider")}}
        if last:
            patch["context"] = context(last.get("total_tokens"), info.get("model_context_window"))
        patch["model"] = state.get("model")
        patch["provider"] = state.get("provider")
    elif agent == "claude":
        msg = row.get("message") or {}
        if row.get("type") != "assistant" or not isinstance(msg, dict) or not msg.get("usage"):
            return None, {}
        u = msg["usage"]
        tok = counts(input=u.get("input_tokens"), cache_read=u.get("cache_read_input_tokens"),
                     cache_write=u.get("cache_creation_input_tokens"), output=u.get("output_tokens"),
                     cache_write_1h=(u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens"),
                     reasoning=(u.get("output_tokens_details") or {}).get("thinking_tokens"))
        if not any(tok.values()) or msg.get("model") == "<synthetic>":
            return None, {}
        # Claude writes multiple chunks for one API request, including updated usage.
        key = msg.get("id") or row.get("uuid")
        if not key:
            return None, {}
        event = {"id": digest([row.get("requestId"), key]), "at": ts, "tokens": tok,
                 "details": {"service_tier": u.get("service_tier"), "speed": u.get("speed"),
                             "inference_geo": u.get("inference_geo"), "provider": "anthropic"}}
        patch = {"model": msg.get("model"), "context": context(
            tok["input"] + tok["cache_read"] + tok["cache_write"])}
    elif agent == "kimi":
        msg = row.get("message") or {}
        if msg.get("type") != "StatusUpdate":
            return None, {}
        data = msg.get("payload") or {}
        u = data.get("token_usage")
        if isinstance(u, dict):
            tok = counts(input=u.get("input_other"), cache_read=u.get("input_cache_read"),
                         cache_write=u.get("input_cache_creation"), output=u.get("output"))
            event = {"id": str(data.get("message_id") or digest([ts, u])), "at": ts, "tokens": tok}
        if any(data.get(k) is not None for k in ("context_usage", "context_tokens", "max_context_tokens")):
            prior = state.get("context", {})
            for key in ("context_usage", "context_tokens", "max_context_tokens"):
                if data.get(key) is not None:
                    prior[key] = data[key]
            state["context"] = prior
            fraction = number(prior.get("context_usage"))
            patch["context"] = context(prior.get("context_tokens"), prior.get("max_context_tokens"),
                                       fraction * 100 if fraction is not None else None)
    if patch and ts is not None:
        patch["observed_at"] = ts
    if event:
        event["model"] = patch.get("model") or state.get("model")
    return event if ts is not None else None, patch if ts is not None else {}


def claude_statusline(row, now):
    """Explicit statusLine input: metrics only; never its transcript or directory."""
    c = row.get("context_window") or {}
    u = c.get("current_usage") or {}
    used = sum(number(u.get(k)) or 0 for k in
               ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")) if u else None
    model = row.get("model") or {}
    return {"model": model.get("id"), "observed_at": now,
            "context": context(used, c.get("context_window_size"), c.get("used_percentage")),
            "limits": limits(row.get("rate_limits")), "limits_scope": "account",
            "session_estimated_cost_usd": number((row.get("cost") or {}).get("total_cost_usd"))}
