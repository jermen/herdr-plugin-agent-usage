# Herdr Agent Usage

Data collection for [DMDOX-315](https://dmdox.atlassian.net/browse/DMDOX-315).
An original Python standard-library plugin with no UI, model calls or dependency
on another Herdr plugin. Requires Python 3.11+ and Herdr 0.9.1 on Linux or macOS.

## Install

Keep the checkout at a stable location, then run from it:

```sh
python3 install.py --timezone Europe/Prague
```

This links/enables `jermen.agent-usage`, starts its watcher, and automatically
wraps the existing Claude statusLine command. Its output and settings are preserved,
with a full settings backup before the change. Re-running the installer keeps the
same wrapper and original command without adding another wrapper or backup.
`CLAUDE_CONFIG_DIR` selects a custom Claude profile; otherwise the installer uses
`~/.claude/settings.json`. The previous `--claude-statusline` flag remains accepted.

Use `--no-claude-statusline` to leave Claude settings untouched (including any
previously installed wrapper). Without the wrapper, tokens and calculated costs
still work, but context percentage, current quota and the native session-cost
estimate need statusLine input. In particular, spending alongside `ctx --` can
mean that the wrapper is missing. Run the normal installer to enable it.
After Claude's next response, allow one collection interval (normally 30 seconds)
for context to appear. Existing Claude sessions may need a restart to load the setting.
Nothing is installed into Codex or Kimi. Herdr's normal integrations must report
agent session IDs.

Configuration: `~/.config/herdr/plugins/config/jermen.agent-usage/config.json`.
The installer only creates it if absent. State defaults to
`$XDG_STATE_HOME/herdr-agent-usage` or `~/.local/state/herdr-agent-usage`.
Each endpoint gets a subdirectory derived from its socket path.

```sh
python3 usage.py path       # Prints the resolved snapshot.json path
python3 usage.py snapshot   # Reads the last snapshot, without collecting
python3 usage.py collect --config config.example.json --state-dir /tmp/usage-test
```

Run inside Herdr or pass `--socket /path/to/herdr.sock`. `start` returns immediately;
one watcher per endpoint is enforced by a lock. `collect` runs once. The watcher
exits when its socket disappears or is replaced. Failures retain the last complete
snapshot; check `generated_at`. No panes, titles, sidebar or terminal contents are
modified. Startup and agent events ensure the watcher starts.

## Sources and coverage

| Agent | Tokens | Current context | Limits | Token cost |
| --- | --- | --- | --- | --- |
| Claude Code | Session JSONL, deduplicated request/message IDs | Last input; statusLine adds capacity/percentage | statusLine with reset times | Model, input/output, cache reads, 5m/1h writes |
| Codex | Native cumulative token_count deltas | Last usage and context window | Native rate_limits and original timestamp | Model rates, cache, long-context pricing |
| Kimi | Native wire.jsonl StatusUpdate | Tokens/capacity/ratio | Optional Coding /usages API | Configured model when absent from wire log |

Only exact session IDs reported by Herdr are followed. Sessions sharing a cwd
remain separate; the same session in two panes counts once. Missing/ambiguous
bindings are explicit. No latest-file guess is used. With the Claude statusLine
wrapper enabled, Claude's exact `transcript_path` takes precedence over directory
discovery. The path must be absolute and named for the matching session ID; it
is retained only in private collector state, not in the consumer snapshot.
This supports per-session `CLAUDE_CONFIG_DIR` profiles even when Herdr's daemon
has a different environment. The first statusLine update can register a session
before the first poll only after verifying its exact live Herdr binding.
Without a reported path, set `roots.claude`, `roots.codex` or `roots.kimi` for
custom data homes.

Retained transcript usage is backfilled on first observation. Later passes seek
to the committed byte offset. Partial final lines are retried; malformed complete
records are skipped and the pass marked partial. Rotation/truncation triggers an
idempotent replay. Each pass reads at most 64 MiB per transcript; a large initial
log catches up over subsequent polls. Records over 16 MiB fail explicitly.
No conversation text, tool arguments, credentials or raw API responses are stored.

`transcript_missing` means that the bound session's file could not be found; it
does not indicate an API-key or subscription failure. API-key sessions use the
same transcript token estimates without subscription quotas. Check the session
ID and actual data directory on the machine running Claude, and enable the
wrapper there with `python3 install.py`. A custom Claude
profile must have the wrapper in that profile's settings. After updating code,
restart the collector watcher so it loads the new discovery logic. If Claude
is not saving a transcript, period costs remain unavailable rather than being
inferred from the current session-cost total.

Closed sessions remain in reports but their transcripts are no longer tailed:
coverage ends at the last collection before closing. Status history starts with
the collector; quota history can be backfilled when native logs contain it.
This is observed local session usage, not an inventory of other machines or
hidden subagents. OpenCode is outside this initial release.

## Consumer interface: schema version 1

Read `snapshot.json` or use `usage.py snapshot`. Publication uses atomic rename.
Check `schema_version` and `generated_at`; consumers need not open SQLite.

- `panes[]`: `pane_id`, `agent`, current `status`, `session_key`,
  `collection_status`: ok, catching_up, partial, session_unreported,
  transcript_missing, transcript_ambiguous or transcript_error.
- `sessions[]`: `key` (agent:session-id), `session_id`, `active`, `first_seen`,
  `last_seen`, `current`, and `periods`.
- `current`: optional model, context, limits, limits_scope, native Claude
  session_estimated_cost_usd, and per-metric source timestamps in
  `metric_observed_at`. Context is current only, never aggregated. Missing means
  unavailable. Snapshot freshness does not imply that idle-session quota is fresh.
- `periods.today`: configured local midnight through now. `last_7_days` and
  `last_31_days`: rolling 7x24 and 31x24 hours. Each has explicit boundaries,
  tokens, usage_records_observed, estimated_spend, billed_spend, spend_coverage,
  status_seconds_observed, and quota_samples.
- Token input excludes cache reads/writes. cache_write_1h is a subset of
  cache_write; reasoning is a subset of output. Neither subset is counted twice
  in total. A usage record is not necessarily a whole API request.
- Status duration is sampled; a sample lasts at most two poll intervals, so
  collector downtime is not charged to the preceding status. Missing time is
  unobserved. Quota histories contain account/provider window samples; never add
  percentages or interpret them as a per-session allowance.
- `account_billing`: optional, separate organization totals that may include
  other users/apps. Never add these totals to session costs.

```python
import json
from pathlib import Path

snapshot = json.loads(Path(snapshot_path).read_text())
assert snapshot['schema_version'] == 1
for session in snapshot['sessions']:
    today = session['periods']['today']
    print(session['key'], today['tokens']['total'], today['estimated_spend'])
```

Files are private to the OS user. Usage, spending and observation records are
retained for 35 days. Session IDs, latest metrics and cursors remain for replay.

## Token-based spending: no billing feed required

`estimated_spend` multiplies token categories by model prices using Decimal. It
reports USD, coverage, unpriced models/tiers, assumptions, and the catalog date.
Unknown prices yield null, not zero cost. Partial pricing reports unpriced record
counts. A period without records has no measured usage; check collection_status
before presenting it as zero consumption.

The [catalog](agent_usage/prices.json) was verified against official sources on
2026-09-21. It covers the locally used Claude/Codex models and current Kimi API
models. Prices are not scraped at runtime. Historical usage is valued using the
current configured catalog, not claimed to reproduce historical invoices.
Update the catalog or override exact models through pricing.models.

For subscriptions this is **API-equivalent usage value, not an extra bill**.
Tool fees, taxes, contract discounts and unconfigured regional uplifts are
excluded. Standard tier is assumed when the log lacks it; set
`pricing.service_tiers.codex` to `priority` for that rate. The output labels the
assumption. Known fast-mode and long-context rates apply before period sums.

Merge pricing overrides into configuration, for example:

```json
{"pricing": {
  "default_models": {"kimi": "kimi-k2.6"},
  "service_tiers": {"codex": "priority"},
  "aliases": {"my-deployment": "kimi-k2.6"},
  "multipliers": {"codex": "1.0"},
  "models": {"my-model": {"tiers": {"standard": {
    "input": "1.0", "cache_read": "0.1", "output": "3.0"
  }}}}
}}
```

Kimi's wire status does not include a model ID. Set default_models.kimi only if
that price applies to the collected sessions. This is labelled as an assumption;
otherwise tokens work and costs remain unpriced. Custom prices are USD per
million tokens. Only explicit aliases and dated model suffixes are normalized.

## Optional Kimi quota

Enable only for Kimi sessions sharing the selected account:

```json
{"kimi_quota": {"key_env": "KIMI_USAGE_TOKEN"}}
```

Alternatively use credentials_file with `~/.kimi/credentials/kimi-code.json`.
Only an unexpired access token is read; it is never refreshed, rewritten or logged.
Requests use Kimi CLI's `https://api.kimi.com/coding/v1/usages` endpoint at most
every five minutes, with a timeout and redirects disabled. Missing credentials
and failures are exposed in quota_status.

## Optional actual spending

Token estimates work without this. To enable organization cost reports, configure
existing environment-variable names, never secret values:

```json
{"billing_poll_seconds": 900, "billing": {
  "openai": {"key_env": "OPENAI_ADMIN_KEY"},
  "anthropic": {"key_env": "ANTHROPIC_ADMIN_KEY"}
}}
```

Keys must exist in Herdr's server environment. Regular inference keys usually
cannot access admin reports. Fetches use official endpoints, pagination, Decimal
USD amounts (Anthropic reports cents), timeouts and no redirects. Errors keep the
last good data marked stale. Missing credentials never become a zero bill.
Provider reports can lag and exclude some products/service tiers.

Provider billing uses UTC daily buckets, so its today/7/31 summaries use UTC
calendar days including today, explicitly marked by period_basis and boundaries.
Exact local-midnight and rolling-hour splits are not available from those buckets.

For actual session-attributed charges, pipe trusted gateway/billing records to
`python3 usage.py import-spend` after the session has been observed:

```json
{"session_key":"codex:SESSION_ID","event_id":"charge-123",
 "timestamp":"2026-09-21T10:00:00Z","amount":"0.123456",
 "currency":"USD","source":"my-gateway"}
```

Stable source/event IDs make retries idempotent. The next collection exposes
billed_spend with explicit reported-records-only coverage.

## Verification and rollback

```sh
python3 -m unittest discover -s tests -v
herdr plugin list
herdr plugin action invoke collect --plugin jermen.agent-usage
python3 usage.py snapshot
```

Disable/unlink jermen.agent-usage and terminate this checkout's `usage.py watch`
process to stop collection: Herdr startup commands are not supervised. Restore
only the prior statusLine object from claude-statusline.json's original field
(or remove it if originally absent), preserving other settings changed since
installation. The full settings backup is also available. Private state can be
retained or explicitly removed. No other agent settings are changed.

## Primary references

- [Claude statusLine](https://code.claude.com/docs/en/statusline)
- [Codex app server](https://developers.openai.com/codex/app-server)
- [Kimi wire](https://moonshotai.github.io/kimi-cli/en/customization/wire-mode.html)
- [Kimi quota implementation](https://github.com/MoonshotAI/kimi-cli/blob/main/src/kimi_cli/ui/shell/usage.py)
- [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
- [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Kimi pricing](https://platform.kimi.ai/docs/pricing/chat)
- [OpenAI costs](https://developers.openai.com/api/reference/resources/admin/subresources/organization/subresources/usage/methods/costs)
- [Anthropic costs](https://platform.claude.com/docs/en/manage-claude/usage-cost-api)

The [reference quota plugin](https://github.com/levi-qiao/herdr-agent-quota) was
inspected for context; this implementation neither runs nor vendors it.
