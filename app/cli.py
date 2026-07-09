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


if __name__ == "__main__":
    cli()
