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
