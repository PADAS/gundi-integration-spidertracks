import json
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from click.testing import CliRunner

from app import cli as cli_module
from app.cli import cli, parse_duration, render_table, resolve_credentials
from app.cli import DEFAULT_ENDPOINT as DEFAULT_ENDPOINT_FOR_TEST
from app.cli import summarize
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
