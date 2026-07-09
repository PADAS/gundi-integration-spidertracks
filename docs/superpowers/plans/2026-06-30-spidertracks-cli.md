# Spidertracks CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a `python -m app.cli` command-line tool for interrogating the Spidertracks AFF feed API with a customer's credentials to investigate feed problems.

**Architecture:** Refactor `SpidertracksClient` to split fetching raw XML from parsing (with heartbeat filtering as a parameter), so the integration's production behavior is unchanged while the CLI gains access to raw responses and heartbeat data. Build a `click`-based CLI on top of those primitives with three subcommands (`positions`, `summary`, `check`), reusing the client and `app/services/utils.py` helpers.

**Tech Stack:** Python 3.10, click 8.1 (already pinned), httpx, backoff, pydantic v1, pytest + pytest-asyncio. No new dependencies.

## Global Constraints

- Python 3.10; pydantic v1 (1.10.x) — not v2.
- No new dependencies: only stdlib, `click`, `httpx`, `backoff` (all already pinned).
- Results print to **stdout**; errors and status notes print to **stderr**.
- Default endpoint: `https://go.spidertracks.com/api/aff/feed`.
- Heartbeat ESN constant: `300034012609560` (already `HEARTBEAT_ESN` in `app/services/client.py`).
- The production action `action_pull_observations` must behave identically after the client refactor — `fetch_positions()` keeps filtering the heartbeat and retains backoff retries.
- Tests mock HTTP by patching `app.services.client.httpx.AsyncClient` (the existing repo pattern), not respx.

---

### Task 1: Refactor `SpidertracksClient` — split fetch from parse, parameterize heartbeat, add retry control

**Files:**
- Modify: `app/services/client.py`
- Test: `app/services/tests/test_client.py`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `SpidertracksClient.fetch_raw(self, start_time: datetime, retry: bool = True) -> str` (async) — performs the HTTP POST and returns `response.text`. With `retry=True` uses backoff (3 tries, 30s); with `retry=False` makes a single attempt.
  - `SpidertracksClient.parse_response(self, xml_text: str, include_heartbeat: bool = False) -> List[dict]` (sync) — the existing parser; keeps dropping the heartbeat ESN unless `include_heartbeat=True`.
  - `SpidertracksClient.fetch_positions(self, start_time: datetime) -> List[dict]` (async) — unchanged behavior: `parse_response(await fetch_raw(start_time))`.

- [ ] **Step 1: Update existing tests for the rename and add new behavior tests**

In `app/services/tests/test_client.py`, replace every `client._parse_response(` with `client.parse_response(`. Then add these tests to the `TestXmlParsing` class:

```python
    def test_parse_response_includes_heartbeat_when_requested(self, client):
        positions = client.parse_response(SAMPLE_XML_WITH_HEARTBEAT, include_heartbeat=True)

        assert len(positions) == 2
        esns = {p["esn"] for p in positions}
        assert HEARTBEAT_ESN in esns

    def test_parse_response_excludes_heartbeat_by_default(self, client):
        positions = client.parse_response(SAMPLE_XML_WITH_HEARTBEAT)

        assert len(positions) == 1
        assert all(p["esn"] != HEARTBEAT_ESN for p in positions)
```

Add this test to the `TestFetchPositions` class (verifies `fetch_raw` returns the body text):

```python
    @pytest.mark.asyncio
    async def test_fetch_raw_returns_response_text(self, client, start_time):
        mock_response = httpx.Response(
            status_code=200,
            text=SAMPLE_XML_RESPONSE,
            request=httpx.Request("POST", client.base_url),
        )

        with patch("app.services.client.httpx.AsyncClient") as mock_client_cls:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = mock_response
            mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
            mock_async_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_async_client

            raw = await client.fetch_raw(start_time)

        assert raw == SAMPLE_XML_RESPONSE
```

- [ ] **Step 2: Run the tests to verify the new ones fail**

Run: `pytest app/services/tests/test_client.py -v`
Expected: FAIL — `AttributeError: 'SpidertracksClient' object has no attribute 'parse_response'` (and `fetch_raw`).

- [ ] **Step 3: Refactor `app/services/client.py`**

Replace the `fetch_positions` method and the `_parse_response` method (lines ~41–96) with the following. Keep the imports, constants, and all the `_get_*` helper methods below unchanged.

```python
    async def fetch_raw(self, start_time: datetime, retry: bool = True) -> str:
        if retry:
            return await self._fetch_raw_with_backoff(start_time)
        return await self._fetch_raw_once(start_time)

    @backoff.on_exception(
        backoff.expo,
        (httpx.HTTPStatusError, httpx.ReadTimeout, httpx.ConnectTimeout),
        max_tries=3,
        max_time=30,
    )
    async def _fetch_raw_with_backoff(self, start_time: datetime) -> str:
        return await self._fetch_raw_once(start_time)

    async def _fetch_raw_once(self, start_time: datetime) -> str:
        report_time = format_utc_datetime(datetime.now(tz=timezone.utc))
        start_time_str = format_utc_datetime(start_time)
        xml_payload = AFF_REQUEST_TEMPLATE.format(start_time=start_time_str,
        body=start_time_str, report_time=report_time)
        auth = (self.username, self.password)

        query_params = {
            'hiFrequency': 'true',
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.base_url,
                content=xml_payload,
                headers={"Content-Type": "application/xml"},
                auth=auth,
                params=query_params,
            )
            response.raise_for_status()

        return response.text

    async def fetch_positions(self, start_time: datetime) -> List[dict]:
        return self.parse_response(await self.fetch_raw(start_time))

    def parse_response(self, xml_text: str, include_heartbeat: bool = False) -> List[dict]:
        positions = []
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError:
            logger.error("Failed to parse Spidertracks XML response")
            raise

        ac_pos_tag = f"{{{AFF_NS}}}acPos"
        for ac_pos in root.iter(ac_pos_tag):
            esn = ac_pos.get("esn") or ""
            if not esn:
                continue
            if not include_heartbeat and esn == HEARTBEAT_ESN:
                continue

            position = {
                "esn": esn,
                "datetime": parse_aware_datetime(ac_pos.get("dateTime") or ""),
                "latitude": self._get_float_ns(ac_pos, "Lat"),
                "longitude": self._get_float_ns(ac_pos, "Long"),
                "speed": self._get_float_ns(ac_pos, "speed"),
                "heading": self._get_float_ns(ac_pos, "heading"),
                "altitude": self._get_float_ns(ac_pos, "altitude"),
                "registration": self._get_telemetry_value(ac_pos, "registration"),
                "track_id": self._get_telemetry_value(ac_pos, "trackid"),
            }
            positions.append(position)

        return positions
```

- [ ] **Step 4: Run the full client test file to verify all pass**

Run: `pytest app/services/tests/test_client.py -v`
Expected: PASS (all tests, including the renamed and new ones).

- [ ] **Step 5: Commit**

```bash
git add app/services/client.py app/services/tests/test_client.py
git commit -m "refactor: split SpidertracksClient fetch_raw from parse_response

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: CLI module scaffold — helpers, shared options, and the click group

**Files:**
- Create: `app/cli.py`
- Create: `app/tests/__init__.py`
- Create: `app/tests/test_cli.py`

**Interfaces:**
- Consumes: `SpidertracksClient` from Task 1.
- Produces (all in `app/cli.py`):
  - `DEFAULT_ENDPOINT: str`
  - `parse_duration(value: str) -> datetime.timedelta`
  - `resolve_credentials(username: Optional[str], password: Optional[str]) -> tuple[str, str]`
  - `compute_start_time(since: str) -> datetime`
  - `render_table(headers: list[str], rows: list[list]) -> str`
  - `common_options(func)` — decorator stacking `--username/-u`, `--password/-p`, `--endpoint`, `--since`, `--no-retry`.
  - `cli` — the click group (entry point), runnable as `python -m app.cli`.

- [ ] **Step 1: Write failing tests for the pure helpers**

Create `app/tests/__init__.py` (empty file).

Create `app/tests/test_cli.py`:

```python
import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from click.testing import CliRunner

from app import cli as cli_module
from app.cli import cli, parse_duration, render_table, resolve_credentials
from app.services.client import HEARTBEAT_ESN


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<data xmlns="https://www.aff.gov/affSchema" version="2.23" sysID="spidertracks" rptTime="2026-03-10T12:00:00Z">
  <posList listType="Async">
    <acPos esn="300234010753370" dateTime="2026-03-10T12:30:00Z">
      <Lat>-36.8485</Lat>
      <Long>174.7633</Long>
      <altitude units="meters">5000</altitude>
      <speed units="meters/sec">120</speed>
      <heading units="Track-True">45</heading>
      <telemetry name="trackid" value="track-001"/>
      <telemetry name="registration" value="ZK-ABC"/>
    </acPos>
    <acPos esn="300234010753371" dateTime="2026-03-10T12:35:00Z">
      <Lat>-37.7870</Lat>
      <Long>175.2793</Long>
      <altitude units="meters">3500</altitude>
      <speed units="meters/sec">95</speed>
      <heading units="Track-True">180</heading>
      <telemetry name="trackid" value="track-002"/>
      <telemetry name="registration" value="ZK-DEF"/>
    </acPos>
    <acPos esn="{heartbeat}" dateTime="2026-03-10T12:31:00Z">
      <Lat>0</Lat>
      <Long>0</Long>
    </acPos>
  </posList>
</data>""".format(heartbeat=HEARTBEAT_ESN)

EMPTY_XML = """<?xml version="1.0" encoding="UTF-8"?>
<data xmlns="https://www.aff.gov/affSchema" version="2.23"><posList listType="Async"></posList></data>"""

CREDS_ENV = {"SPIDERTRACKS_USERNAME": "acme", "SPIDERTRACKS_PASSWORD": "secret"}


def test_parse_duration_units():
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("12h") == timedelta(hours=12)
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("45s") == timedelta(seconds=45)


def test_parse_duration_invalid_raises():
    with pytest.raises(ValueError):
        parse_duration("banana")


def test_render_table_aligns_columns():
    table = render_table(["A", "BB"], [["1", "222"], ["33", "4"]])
    lines = table.splitlines()
    assert lines[0].startswith("A ")
    assert len(lines) == 4  # header, separator, 2 rows


def test_resolve_credentials_prefers_explicit_over_env(monkeypatch):
    monkeypatch.setenv("SPIDERTRACKS_USERNAME", "envuser")
    monkeypatch.setenv("SPIDERTRACKS_PASSWORD", "envpass")
    assert resolve_credentials("flaguser", "flagpass") == ("flaguser", "flagpass")


def test_resolve_credentials_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("SPIDERTRACKS_USERNAME", "envuser")
    monkeypatch.setenv("SPIDERTRACKS_PASSWORD", "envpass")
    assert resolve_credentials(None, None) == ("envuser", "envpass")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.cli'`.

- [ ] **Step 3: Create `app/cli.py` with helpers and the group**

```python
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from xml.etree import ElementTree

import click
import httpx

from app.services.client import SpidertracksClient
from app.services.utils import format_utc_datetime

DEFAULT_ENDPOINT = "https://go.spidertracks.com/api/aff/feed"

_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd])\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(value: str) -> timedelta:
    match = _DURATION_RE.match(value or "")
    if not match:
        raise ValueError(f"Invalid duration: {value!r}. Use forms like 30m, 12h, 7d.")
    amount = int(match.group(1))
    unit = match.group(2).lower()
    return timedelta(seconds=amount * _UNIT_SECONDS[unit])


def resolve_credentials(username: Optional[str], password: Optional[str]):
    username = username or os.environ.get("SPIDERTRACKS_USERNAME")
    if not username:
        username = click.prompt("Spidertracks username")
    password = password or os.environ.get("SPIDERTRACKS_PASSWORD")
    if not password:
        password = click.prompt("Spidertracks password", hide_input=True)
    return username, password


def compute_start_time(since: str) -> datetime:
    try:
        delta = parse_duration(since)
    except ValueError as exc:
        _die(str(exc))
    return datetime.now(tz=timezone.utc) - delta


def render_table(headers, rows) -> str:
    str_rows = [[str(c) for c in row] for row in rows]
    columns = [headers] + str_rows
    widths = [max(len(row[i]) for row in columns) for i in range(len(headers))]
    fmt = "  ".join("{:<%d}" % w for w in widths)
    lines = [fmt.format(*headers), fmt.format(*["-" * w for w in widths])]
    lines += [fmt.format(*row) for row in str_rows]
    return "\n".join(lines)


def _die(message: str):
    click.echo(message, err=True)
    sys.exit(1)


def _fmt_dt(dt) -> str:
    return format_utc_datetime(dt) if dt else ""


def fetch_raw_xml(client: SpidertracksClient, start_time: datetime, no_retry: bool) -> str:
    try:
        return asyncio.run(client.fetch_raw(start_time, retry=not no_retry))
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            _die(f"Authentication failed (HTTP {code}). Check username/password.")
        body = (exc.response.text or "")[:500]
        _die(f"Request failed (HTTP {code}).\n{body}")
    except httpx.HTTPError as exc:
        _die(f"Could not reach {client.base_url}: {exc}")


def parse_positions(client: SpidertracksClient, xml_text: str, include_heartbeat: bool):
    try:
        return client.parse_response(xml_text, include_heartbeat=include_heartbeat)
    except ElementTree.ParseError as exc:
        _die(f"Failed to parse XML response: {exc}\nRe-run with --raw to inspect the payload.")


def common_options(func):
    func = click.option("--no-retry", is_flag=True, default=False,
                        help="Report the first failure immediately, skipping backoff retries.")(func)
    func = click.option("--since", default="24h", show_default=True,
                        help="How far back to fetch, e.g. 30m, 12h, 7d.")(func)
    func = click.option("--endpoint", default=DEFAULT_ENDPOINT, show_default=True,
                        help="AFF feed endpoint URL.")(func)
    func = click.option("--password", "-p", default=None,
                        help="Spidertracks password (or SPIDERTRACKS_PASSWORD; prompted if omitted).")(func)
    func = click.option("--username", "-u", default=None,
                        help="Spidertracks username (or SPIDERTRACKS_USERNAME; prompted if omitted).")(func)
    return func


@click.group()
def cli():
    """Interrogate the Spidertracks AFF feed API for support investigations."""


if __name__ == "__main__":
    cli()
```

- [ ] **Step 4: Run the helper tests to verify they pass**

Run: `pytest app/tests/test_cli.py -v`
Expected: PASS (the 5 helper tests; command tests come in later tasks).

- [ ] **Step 5: Commit**

```bash
git add app/cli.py app/tests/__init__.py app/tests/test_cli.py
git commit -m "feat: add Spidertracks CLI scaffold and helpers

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `positions` command — table, JSON, raw, and filters

**Files:**
- Modify: `app/cli.py`
- Test: `app/tests/test_cli.py`

**Interfaces:**
- Consumes: all helpers from Task 2.
- Produces: `cli` subcommand `positions` with options `--esn`, `--registration`, `--include-heartbeat`, `--json` (dest `as_json`), `--raw`, plus the shared options.

- [ ] **Step 1: Write failing tests for `positions`**

Add to `app/tests/test_cli.py`:

```python
def _patch_fetch(return_value=None, side_effect=None):
    return patch.object(
        cli_module.SpidertracksClient,
        "fetch_raw",
        new=AsyncMock(return_value=return_value, side_effect=side_effect),
    )


def test_positions_table_lists_aircraft():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["positions"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert "ZK-ABC" in result.output
    assert "ZK-DEF" in result.output
    assert HEARTBEAT_ESN not in result.output  # filtered by default


def test_positions_json_emits_parsed_dicts():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["positions", "--json"], env=CREDS_ENV)
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert len(data) == 2
    assert data[0]["registration"] == "ZK-ABC"


def test_positions_raw_passes_through_xml():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["positions", "--raw"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert "affSchema" in result.output
    assert "<acPos" in result.output


def test_positions_filter_by_registration():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["positions", "--registration", "N12345"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert "ZK-ABC" not in result.output
    assert "No positions found" in result.output


def test_positions_include_heartbeat():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["positions", "--include-heartbeat"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert HEARTBEAT_ESN in result.output


def test_positions_auth_failure_exits_1():
    request = httpx.Request("POST", DEFAULT_ENDPOINT_FOR_TEST)
    response = httpx.Response(status_code=401, text="Unauthorized", request=request)
    error = httpx.HTTPStatusError("401", request=request, response=response)
    runner = CliRunner()
    with _patch_fetch(side_effect=error):
        result = runner.invoke(cli, ["positions"], env=CREDS_ENV)
    assert result.exit_code == 1
    assert "Authentication failed" in result.output


def test_positions_prompts_for_password_when_missing():
    runner = CliRunner()
    with _patch_fetch(return_value=EMPTY_XML):
        result = runner.invoke(cli, ["positions", "-u", "acme"], input="typedpass\n")
    # No positions, but the command ran without error after prompting.
    assert result.exit_code == 0
```

Add this import near the top of the test file (used by the auth test):

```python
from app.cli import DEFAULT_ENDPOINT as DEFAULT_ENDPOINT_FOR_TEST
```

Note: `CliRunner` merges stdout and stderr into `result.output` by default, so stderr assertions like "No positions found" work.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/tests/test_cli.py -v`
Expected: FAIL — `Error: No such command 'positions'.` (exit_code 2 on the new tests).

- [ ] **Step 3: Add the `positions` command to `app/cli.py`**

Insert after the `cli` group definition (before the `if __name__` block):

```python
@cli.command()
@common_options
@click.option("--esn", default=None, help="Show only this ESN.")
@click.option("--registration", default=None, help="Show only this registration.")
@click.option("--include-heartbeat", is_flag=True, default=False,
              help="Include the heartbeat ESN normally filtered out.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON instead of a table.")
@click.option("--raw", is_flag=True, default=False, help="Print the raw XML response (ignores filters).")
def positions(username, password, endpoint, since, no_retry,
              esn, registration, include_heartbeat, as_json, raw):
    """List parsed positions from the feed."""
    username, password = resolve_credentials(username, password)
    start_time = compute_start_time(since)
    client = SpidertracksClient(base_url=endpoint, username=username, password=password)
    xml_text = fetch_raw_xml(client, start_time, no_retry)

    if raw:
        click.echo(xml_text)
        return

    records = parse_positions(client, xml_text, include_heartbeat)
    if esn:
        records = [p for p in records if p["esn"] == esn]
    if registration:
        records = [p for p in records if p["registration"] == registration]

    if not records:
        click.echo(f"No positions found in the last {since}.", err=True)
        return

    if as_json:
        click.echo(json.dumps(records, indent=2, default=str))
        return

    headers = ["ESN", "REGISTRATION", "DATETIME", "LAT", "LON", "SPEED", "HEADING", "ALTITUDE"]
    rows = [
        [p["esn"], p["registration"], _fmt_dt(p["datetime"]),
         p["latitude"], p["longitude"], p["speed"], p["heading"], p["altitude"]]
        for p in records
    ]
    click.echo(render_table(headers, rows))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/tests/test_cli.py -v`
Expected: PASS (all helper and `positions` tests).

- [ ] **Step 5: Commit**

```bash
git add app/cli.py app/tests/test_cli.py
git commit -m "feat: add positions command to Spidertracks CLI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `summary` command — per-aircraft rollup

**Files:**
- Modify: `app/cli.py`
- Test: `app/tests/test_cli.py`

**Interfaces:**
- Consumes: helpers from Task 2; `fetch_raw_xml` / `parse_positions`.
- Produces: `cli` subcommand `summary` with `--include-heartbeat`, `--json`, and shared options; plus module-level `summarize(positions: list[dict]) -> list[dict]` returning per-ESN dicts with keys `esn`, `registration`, `count`, `latest` (datetime or None), sorted by `esn`.

- [ ] **Step 1: Write failing tests for `summary`**

Add to `app/tests/test_cli.py`:

```python
from app.cli import summarize


def test_summarize_groups_by_esn():
    positions = [
        {"esn": "A", "registration": "ZK-A", "datetime": __import__("datetime").datetime(2026, 3, 10, 12, 0, tzinfo=__import__("datetime").timezone.utc)},
        {"esn": "A", "registration": "ZK-A", "datetime": __import__("datetime").datetime(2026, 3, 10, 13, 0, tzinfo=__import__("datetime").timezone.utc)},
        {"esn": "B", "registration": "ZK-B", "datetime": None},
    ]
    rollup = summarize(positions)
    assert [g["esn"] for g in rollup] == ["A", "B"]
    a = rollup[0]
    assert a["count"] == 2
    assert a["latest"].hour == 13


def test_summary_command_table():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["summary"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert "ZK-ABC" in result.output
    assert "COUNT" in result.output


def test_summary_command_json():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["summary", "--json"], env=CREDS_ENV)
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert {row["esn"] for row in data} == {"300234010753370", "300234010753371"}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/tests/test_cli.py -v`
Expected: FAIL — `ImportError: cannot import name 'summarize'` / `No such command 'summary'`.

- [ ] **Step 3: Add `summarize` and the `summary` command to `app/cli.py`**

Insert before the `if __name__` block:

```python
def summarize(positions):
    groups = {}
    for p in positions:
        esn = p["esn"]
        group = groups.get(esn)
        if group is None:
            group = {"esn": esn, "registration": p.get("registration", ""), "count": 0, "latest": None}
            groups[esn] = group
        group["count"] += 1
        if p.get("registration"):
            group["registration"] = p["registration"]
        dt = p.get("datetime")
        if dt and (group["latest"] is None or dt > group["latest"]):
            group["latest"] = dt
    return sorted(groups.values(), key=lambda g: g["esn"])


def _age(dt) -> str:
    if not dt:
        return ""
    seconds = int((datetime.now(tz=timezone.utc) - dt).total_seconds())
    if seconds < 0:
        return "0s ago"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


@cli.command()
@common_options
@click.option("--include-heartbeat", is_flag=True, default=False,
              help="Include the heartbeat ESN normally filtered out.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON instead of a table.")
def summary(username, password, endpoint, since, no_retry, include_heartbeat, as_json):
    """Per-aircraft rollup: count and most recent report per ESN."""
    username, password = resolve_credentials(username, password)
    start_time = compute_start_time(since)
    client = SpidertracksClient(base_url=endpoint, username=username, password=password)
    xml_text = fetch_raw_xml(client, start_time, no_retry)
    records = parse_positions(client, xml_text, include_heartbeat)

    if not records:
        click.echo(f"No positions found in the last {since}.", err=True)
        return

    rollup = summarize(records)

    if as_json:
        click.echo(json.dumps(rollup, indent=2, default=str))
        return

    headers = ["ESN", "REGISTRATION", "COUNT", "LATEST", "AGE"]
    rows = [
        [g["esn"], g["registration"], g["count"], _fmt_dt(g["latest"]), _age(g["latest"])]
        for g in rollup
    ]
    click.echo(render_table(headers, rows))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest app/tests/test_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/cli.py app/tests/test_cli.py
git commit -m "feat: add summary command to Spidertracks CLI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `check` command — auth/connectivity probe

**Files:**
- Modify: `app/cli.py`
- Test: `app/tests/test_cli.py`

**Interfaces:**
- Consumes: helpers from Task 2; `fetch_raw_xml` / `parse_positions`.
- Produces: `cli` subcommand `check` with `--json` and shared options. On success prints HTTP 200, round-trip seconds, and total positions found, exit 0. On any request failure, `fetch_raw_xml` already prints the error to stderr and exits 1.

- [ ] **Step 1: Write failing tests for `check`**

Add to `app/tests/test_cli.py`:

```python
def test_check_success_exits_0():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["check"], env=CREDS_ENV)
    assert result.exit_code == 0
    assert "200" in result.output
    assert "positions" in result.output.lower()


def test_check_json_output():
    runner = CliRunner()
    with _patch_fetch(return_value=SAMPLE_XML):
        result = runner.invoke(cli, ["check", "--json"], env=CREDS_ENV)
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["status"] == "ok"
    assert data["http_status"] == 200


def test_check_auth_failure_exits_1():
    request = httpx.Request("POST", DEFAULT_ENDPOINT_FOR_TEST)
    response = httpx.Response(status_code=401, text="Unauthorized", request=request)
    error = httpx.HTTPStatusError("401", request=request, response=response)
    runner = CliRunner()
    with _patch_fetch(side_effect=error):
        result = runner.invoke(cli, ["check"], env=CREDS_ENV)
    assert result.exit_code == 1
    assert "Authentication failed" in result.output
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest app/tests/test_cli.py -v`
Expected: FAIL — `No such command 'check'.`

- [ ] **Step 3: Add the `check` command to `app/cli.py`**

Insert before the `if __name__` block:

```python
@cli.command()
@common_options
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit JSON instead of text.")
def check(username, password, endpoint, since, no_retry, as_json):
    """Probe auth and connectivity; report status, timing, and position count."""
    username, password = resolve_credentials(username, password)
    start_time = compute_start_time(since)
    client = SpidertracksClient(base_url=endpoint, username=username, password=password)

    started = time.monotonic()
    xml_text = fetch_raw_xml(client, start_time, no_retry)
    elapsed = round(time.monotonic() - started, 3)

    records = parse_positions(client, xml_text, include_heartbeat=True)
    result = {
        "status": "ok",
        "http_status": 200,
        "round_trip_seconds": elapsed,
        "positions_found": len(records),
    }

    if as_json:
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"Status:          ok (HTTP 200)")
    click.echo(f"Round-trip:      {elapsed}s")
    click.echo(f"Positions found: {len(records)} in the last {since}")
```

- [ ] **Step 4: Run the full suite to verify everything passes**

Run: `pytest app/tests/test_cli.py app/services/tests/test_client.py -v`
Expected: PASS (all client and CLI tests).

- [ ] **Step 5: Manual smoke test (no network)**

Run: `python -m app.cli --help`
Expected: usage text listing the `positions`, `summary`, and `check` commands.

Run: `python -m app.cli positions --help`
Expected: usage text listing `--username`, `--since`, `--esn`, `--raw`, etc.

- [ ] **Step 6: Commit**

```bash
git add app/cli.py app/tests/test_cli.py
git commit -m "feat: add check command to Spidertracks CLI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Document the CLI in the README

**Files:**
- Modify: `README.md`

**Interfaces:**
- Consumes: the finished CLI.
- Produces: nothing code-facing.

- [ ] **Step 1: Add a usage section to `README.md`**

Append a section documenting the tool. Include the invocation form, credential options, and one example per subcommand:

```markdown
## Spidertracks feed CLI

A tool for investigating a customer's Spidertracks feed directly. Reuses the
integration's own client, so it shows exactly what the integration sees.

```bash
# Credentials via env vars (or pass -u/-p; password is prompted if omitted)
export SPIDERTRACKS_USERNAME=acme
export SPIDERTRACKS_PASSWORD=...

# Verify credentials and connectivity
python -m app.cli check

# Per-aircraft rollup over the last 7 days
python -m app.cli summary --since 7d

# List positions for one aircraft
python -m app.cli positions --registration ZK-ABC --since 48h

# Dump the raw XML response (for debugging parsing issues)
python -m app.cli positions --raw --since 1h
```

Add `--json` to any command for machine-readable output, `--include-heartbeat`
to keep the heartbeat ESN, and `--no-retry` to surface the first failure
immediately.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: document the Spidertracks feed CLI

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review Notes

- **Spec coverage:** parsed positions (Task 3 table/json), raw XML (Task 3 `--raw`), per-aircraft summary (Task 4), auth/connectivity check (Task 5); credentials via flags/env/prompt (Task 2 `resolve_credentials`); `--since` (Task 2 `compute_start_time`), ESN/registration filter (Task 3), `--include-heartbeat` (Tasks 3–4, client param in Task 1), custom endpoint (Task 2 `common_options`), `--no-retry` (Task 1 `fetch_raw(retry=)`, wired in Task 2 `fetch_raw_xml`); error handling table (Task 2 `fetch_raw_xml`/`parse_positions`); behavior-preserving client refactor (Task 1). README (Task 6).
- **Type consistency:** `fetch_raw(start_time, retry=True)`, `parse_response(xml_text, include_heartbeat=False)`, `summarize(...) -> [{esn, registration, count, latest}]`, `as_json` dest for `--json` — used consistently across tasks.
- **Naming:** `--json` maps to parameter `as_json` everywhere; the heartbeat constant is `HEARTBEAT_ESN`.
