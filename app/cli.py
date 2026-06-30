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
