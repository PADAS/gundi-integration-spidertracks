# Spidertracks CLI — Design

**Date:** 2026-06-30
**Status:** Approved, ready for implementation planning

## Purpose

A command-line tool for interrogating the Spidertracks AFF feed API directly with a
customer's credentials. When a user reports a problem with their feed (missing
aircraft, stale positions, nothing arriving), this CLI lets an operator run the same
request the integration runs and see exactly what Spidertracks returns — from the raw
XML through to the parsed positions and a per-aircraft rollup.

The guiding principle: **the CLI shows what the integration sees.** It reuses
`SpidertracksClient` so parsing behavior cannot drift from production.

## Background

`SpidertracksClient` (`app/services/client.py`) POSTs an AFF "Data Request" XML
document to `https://go.spidertracks.com/api/aff/feed` using HTTP basic auth, then
parses `acPos` elements from the XML response into position dicts with keys: `esn`,
`datetime`, `latitude`, `longitude`, `speed`, `heading`, `altitude`, `registration`,
`track_id`. The current `fetch_positions()` hardcodes dropping the heartbeat ESN
(`300034012609560`) and discards the raw XML.

`click~=8.1.7`, `httpx`, and `backoff` are already pinned dependencies. **No new
dependencies are required.**

## Scope

In scope:
- A `click`-based CLI run as `python -m app.cli`.
- Three subcommands: `positions`, `summary`, `check`.
- Credentials via flags / env / interactive prompt.
- Query controls: `--since`, `--esn`/`--registration` filter, `--include-heartbeat`,
  `--endpoint`, `--no-retry`.
- Output formats: human-readable table (default), `--json`, `--raw` (XML).
- A behavior-preserving refactor of `SpidertracksClient` to expose the raw response
  and make heartbeat filtering a parameter.
- Tests for the refactored client and the CLI.

Out of scope:
- Writing to Gundi, persisting state, or any side effects beyond reading the feed.
- Named credential profiles or pulling credentials from Gundi config (rejected during
  brainstorming in favor of flags + env).
- Packaging as an installed console entry point (run as a module instead).

## Architecture

```
app/cli.py              ← new: click group + subcommands, `python -m app.cli`
app/services/client.py  ← refactor: fetch_raw() + parse_response(include_heartbeat=)
app/tests/test_cli.py   ← new: CLI tests via click CliRunner with mocked client
app/services/tests/test_client.py ← update: rename _parse_response references; add tests
```

The CLI imports `SpidertracksClient` and reuses `format_utc_datetime` /
`parse_aware_datetime` from `app/services/utils.py`. Sync click commands drive the
async client via `asyncio.run()`. Results go to **stdout**; errors and status notes go
to **stderr**, keeping `--json` / `--raw` output clean for piping.

## Client refactor (behavior-preserving)

Split `fetch_positions()` into two primitives; keep it as a thin wrapper so the
production action (`action_pull_observations`) behaves identically.

```python
@backoff.on_exception(backoff.expo, (HTTPStatusError, ReadTimeout, ConnectTimeout),
                      max_tries=3, max_time=30)
async def fetch_raw(self, start_time) -> str:
    # existing HTTP POST; returns response.text

def parse_response(self, xml_text, include_heartbeat=False) -> List[dict]:
    # existing _parse_response body; heartbeat drop gated on include_heartbeat:
    #   if not include_heartbeat and esn == HEARTBEAT_ESN: continue

async def fetch_positions(self, start_time) -> List[dict]:
    return self.parse_response(await self.fetch_raw(start_time))
```

- `_parse_response` is renamed to `parse_response` (now public; the CLI uses it).
  Existing tests in `test_client.py` that call `client._parse_response(...)` must be
  updated to `client.parse_response(...)`.
- Default `include_heartbeat=False` preserves `fetch_positions` behavior exactly.
- `--no-retry`: the CLI needs a path that skips the `@backoff` retries so the *first*
  failure surfaces immediately during debugging. Implementation chooses the cleanest
  mechanism (e.g. a `retry: bool` parameter on the fetch path, or the CLI invoking the
  underlying request without the decorated wrapper). Requirement: with `--no-retry`,
  a failing request raises/reports after one attempt.

## CLI command structure

Invoked as `python -m app.cli`. A click group with shared options applied to every
subcommand.

**Shared options:**
- `--username` / `-u` — or `SPIDERTRACKS_USERNAME` env var.
- `--password` / `-p` — or `SPIDERTRACKS_PASSWORD` env var; if neither is given,
  prompt with hidden input.
- `--endpoint` — defaults to `https://go.spidertracks.com/api/aff/feed`.
- `--since` — default `24h`. Accepts a duration like `30m`, `12h`, `7d`; converted to
  `start_time = now(utc) - duration`.
- `--no-retry` — skip backoff retries; report the first failure immediately.

**Subcommands:**

| Command | Purpose | Flags |
|---|---|---|
| `positions` | Fetch & list parsed positions (the main command). | `--esn`, `--registration` (filter, applied after parsing), `--include-heartbeat`, `--json`, `--raw` |
| `summary` | Per-aircraft rollup: one row per ESN — registration, position count, latest report time, age. | `--include-heartbeat`, `--json` |
| `check` | Auth/connectivity probe: HTTP status, round-trip time, total positions found. Exit 0 on success, 1 on failure. | `--json` |

`--raw` lives on `positions` and dumps the XML response verbatim; it ignores
`--esn`/`--registration` (raw is raw).

Typical investigation:
```
python -m app.cli check    -u acme
python -m app.cli summary   -u acme --since 7d
python -m app.cli positions -u acme --registration N12345 --since 48h
python -m app.cli positions -u acme --raw --since 1h
```

## Output formats

- **Table (default):** aligned plain-text columns via stdlib string formatting (no
  `rich` dependency).
  - `positions` columns: ESN, registration, datetime (UTC ISO), lat, lon, speed,
    heading, altitude.
  - `summary` columns: ESN, registration, count, latest report (UTC ISO), age
    (e.g. `3m ago`).
- **`--json`:** parsed position dicts via `json.dumps(..., indent=2, default=str)` so
  datetimes serialize. For `summary`, a list of per-ESN objects.
- **`--raw`:** the XML response printed verbatim.
- **Empty result:** print `No positions found in the last <window>.` to stderr; exit 0.

## Error handling

Errors must be legible (this is a diagnostic tool, not a crash). All error messages go
to stderr; exit code 1 unless noted.

| Condition | Behavior |
|---|---|
| HTTP 401 / 403 | `Authentication failed (HTTP <code>). Check username/password.` |
| Other `HTTPStatusError` | Show status code + leading portion of the response body. |
| `ReadTimeout` / `ConnectTimeout` / connection error | `Could not reach <endpoint>: <reason>` |
| `ElementTree.ParseError` | Message + hint to re-run with `--raw` to inspect the malformed payload. |
| Empty (valid) response | `No positions found in the last <window>.` (stderr), exit 0. |

With `--no-retry`, the first failure is reported without waiting through backoff.

## Testing

Use `pytest` + `pytest-asyncio`, matching the existing `test_client.py` style:
HTTP is mocked by patching `app.services.client.httpx.AsyncClient` with
`unittest.mock.patch` / `AsyncMock` (the pattern already used in the repo — **not**
respx). CLI commands are exercised with click's `CliRunner` against a mocked client.

**Client (`test_client.py`):**
- Update existing `_parse_response` calls to `parse_response`.
- `fetch_raw` returns the response body text.
- `parse_response(include_heartbeat=False)` drops the heartbeat ESN (regression guard).
- `parse_response(include_heartbeat=True)` retains the heartbeat ESN.
- `fetch_positions` still filters the heartbeat (composition regression guard).

**CLI (`test_cli.py`):**
- `positions` renders a table with expected rows.
- `positions --json` emits valid JSON of the parsed dicts.
- `positions --raw` prints the XML passthrough unchanged.
- `positions --esn` / `--registration` filters the output.
- `summary` groups by ESN with correct counts and latest times.
- `check` exits 0 on success.
- HTTP 401 → exit code 1 with the auth-failure message on stderr.
- Credential resolution: flag > env; password prompt when neither supplied.

## Key decisions (from brainstorming)

- Approach B (split fetch from parse) over wrapping the client as-is (can't support
  `--raw`/`--include-heartbeat`) or reimplementing HTTP+parsing (would drift from
  production).
- Credentials via flags + env + prompt; not profiles, not Gundi config lookup.
- Run as a module (`python -m app.cli`), not an installed console script.
- Plain-text tables, no `rich` dependency.
