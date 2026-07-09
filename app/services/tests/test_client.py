import pytest
import httpx
from pathlib import Path
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

from app.services.client import SpidertracksClient, HEARTBEAT_ESN


# Real API format: data/posList/acPos with xmlns, acPos attributes, Lat/Long children, telemetry
SAMPLE_XML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<data xmlns="https://www.aff.gov/affSchema" version="2.23" sysID="spidertracks" rptTime="2026-03-10T12:00:00Z">
  <posList listType="Async">
    <acPos esn="300234010753370" dateTime="2026-03-10T12:30:00Z">
      <Lat>-36.8485</Lat>
      <Long>174.7633</Long>
      <altitude units="meters">5000</altitude>
      <speed units="meters/sec">120</speed>
      <heading units="Track-True">45</heading>
      <telemetry name="trackid" source="spider" type="xsd:integer" value="track-001"/>
      <telemetry name="registration" source="spidertracks" type="xsd:string" value="ZK-ABC"/>
    </acPos>
    <acPos esn="300234010753371" dateTime="2026-03-10T12:35:00Z">
      <Lat>-37.7870</Lat>
      <Long>175.2793</Long>
      <altitude units="meters">3500</altitude>
      <speed units="meters/sec">95</speed>
      <heading units="Track-True">180</heading>
      <telemetry name="trackid" source="spider" type="xsd:integer" value="track-002"/>
      <telemetry name="registration" source="spidertracks" type="xsd:string" value="ZK-DEF"/>
    </acPos>
  </posList>
</data>"""

SAMPLE_XML_WITH_HEARTBEAT = """<?xml version="1.0" encoding="UTF-8"?>
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
    <acPos esn="{heartbeat_esn}" dateTime="2026-03-10T12:31:00Z">
      <Lat>0</Lat>
      <Long>0</Long>
      <altitude units="meters">0</altitude>
      <speed units="meters/sec">0</speed>
      <heading units="Track-True">0</heading>
    </acPos>
  </posList>
</data>""".format(heartbeat_esn=HEARTBEAT_ESN)

XML_WITH_MISSING_DATETIME = """<?xml version="1.0" encoding="UTF-8"?>
<data xmlns="https://www.aff.gov/affSchema" version="2.23" sysID="spidertracks" rptTime="2026-03-10T12:00:00Z">
  <posList listType="Async">
    <acPos esn="300234010753370">
      <Lat>-36.8485</Lat>
      <Long>174.7633</Long>
      <altitude units="meters">5000</altitude>
      <speed units="meters/sec">120</speed>
      <heading units="Track-True">45</heading>
      <telemetry name="trackid" value="track-001"/>
      <telemetry name="registration" value="ZK-ABC"/>
    </acPos>
  </posList>
</data>"""

EMPTY_XML_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<data xmlns="https://www.aff.gov/affSchema" version="2.23" sysID="spidertracks" rptTime="2026-03-10T12:00:00Z">
  <posList listType="Async">
  </posList>
</data>"""

MALFORMED_XML = """<not valid xml"""


@pytest.fixture
def client():
    return SpidertracksClient(
        base_url="https://go.spidertracks.com/api/aff/feed",
        username="testuser",
        password="testpass",
    )


@pytest.fixture
def start_time():
    return datetime(2026, 3, 10, 0, 0, 0, tzinfo=timezone.utc)


class TestXmlParsing:

    def test_parse_response_extracts_positions(self, client):
        positions = client.parse_response(SAMPLE_XML_RESPONSE)

        assert len(positions) == 2
        assert positions[0]["esn"] == "300234010753370"
        assert positions[0]["latitude"] == -36.8485
        assert positions[0]["longitude"] == 174.7633
        assert positions[0]["speed"] == 120.0
        assert positions[0]["heading"] == 45.0
        assert positions[0]["altitude"] == 5000.0
        assert positions[0]["registration"] == "ZK-ABC"
        assert positions[0]["track_id"] == "track-001"
        assert positions[0]["datetime"] == datetime(2026, 3, 10, 12, 30, 0, tzinfo=timezone.utc)

    def test_parse_response_second_position(self, client):
        positions = client.parse_response(SAMPLE_XML_RESPONSE)

        assert positions[1]["esn"] == "300234010753371"
        assert positions[1]["latitude"] == -37.7870
        assert positions[1]["registration"] == "ZK-DEF"

    def test_parse_response_missing_datetime_returns_none(self, client):
        positions = client.parse_response(XML_WITH_MISSING_DATETIME)
        assert len(positions) == 1
        assert positions[0]["datetime"] is None

    def test_parse_response_filters_heartbeat(self, client):
        positions = client.parse_response(SAMPLE_XML_WITH_HEARTBEAT)

        assert len(positions) == 1
        assert positions[0]["esn"] == "300234010753370"

    def test_parse_response_empty(self, client):
        positions = client.parse_response(EMPTY_XML_RESPONSE)
        assert len(positions) == 0

    def test_parse_response_malformed_xml(self, client):
        from xml.etree.ElementTree import ParseError

        with pytest.raises(ParseError):
            client.parse_response(MALFORMED_XML)

    def test_parse_response_includes_heartbeat_when_requested(self, client):
        positions = client.parse_response(SAMPLE_XML_WITH_HEARTBEAT, include_heartbeat=True)

        assert len(positions) == 2
        esns = {p["esn"] for p in positions}
        assert HEARTBEAT_ESN in esns

    def test_parse_response_excludes_heartbeat_by_default(self, client):
        positions = client.parse_response(SAMPLE_XML_WITH_HEARTBEAT)

        assert len(positions) == 1
        assert all(p["esn"] != HEARTBEAT_ESN for p in positions)

    def test_parse_response_real_example_file(self, client):
        """Parse the real API example file; guards against namespace/structure changes."""
        example_path = Path(__file__).resolve().parent.parent.parent.parent / "docs" / "data-examples" / "spidertracks-response.example.xml"
        if not example_path.exists():
            pytest.skip("docs/data-examples/spidertracks-response.example.xml not found")
        xml_text = example_path.read_text()
        positions = client.parse_response(xml_text)
        assert len(positions) > 0
        first = positions[0]
        assert "esn" in first and first["esn"]
        assert "datetime" in first and isinstance(first["datetime"], datetime)
        assert "latitude" in first and isinstance(first["latitude"], (int, float))
        assert "longitude" in first and isinstance(first["longitude"], (int, float))
        assert "registration" in first
        assert "track_id" in first


class TestFetchPositions:

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

    @pytest.mark.asyncio
    async def test_fetch_positions_success(self, client, start_time):
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

            positions = await client.fetch_positions(start_time)

        assert len(positions) == 2
        mock_async_client.post.assert_called_once()
        call_kwargs = mock_async_client.post.call_args
        assert call_kwargs[1]["auth"] == ("testuser", "testpass")
        assert "2026-03-10T00:00:00Z" in call_kwargs[1]["content"]

    @pytest.mark.asyncio
    async def test_fetch_positions_empty_response(self, client, start_time):
        mock_response = httpx.Response(
            status_code=200,
            text=EMPTY_XML_RESPONSE,
            request=httpx.Request("POST", client.base_url),
        )

        with patch("app.services.client.httpx.AsyncClient") as mock_client_cls:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = mock_response
            mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
            mock_async_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_async_client

            positions = await client.fetch_positions(start_time)

        assert len(positions) == 0

    @pytest.mark.asyncio
    async def test_fetch_positions_http_error(self, client, start_time):
        mock_response = httpx.Response(
            status_code=500,
            text="Internal Server Error",
            request=httpx.Request("POST", client.base_url),
        )

        with patch("app.services.client.httpx.AsyncClient") as mock_client_cls:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = mock_response
            mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
            mock_async_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_async_client

            with pytest.raises(httpx.HTTPStatusError):
                await client.fetch_positions(start_time)


class TestXmlRequestConstruction:

    @pytest.mark.asyncio
    async def test_request_contains_timestamp(self, client, start_time):
        mock_response = httpx.Response(
            status_code=200,
            text=EMPTY_XML_RESPONSE,
            request=httpx.Request("POST", client.base_url),
        )

        with patch("app.services.client.httpx.AsyncClient") as mock_client_cls:
            mock_async_client = AsyncMock()
            mock_async_client.post.return_value = mock_response
            mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
            mock_async_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_async_client

            await client.fetch_positions(start_time)

        call_kwargs = mock_async_client.post.call_args[1]
        assert "2026-03-10T00:00:00Z" in call_kwargs["content"]
        assert call_kwargs["headers"]["Content-Type"] == "application/xml"
