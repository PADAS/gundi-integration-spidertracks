import logging
from datetime import datetime, timezone
from typing import List
from xml.etree import ElementTree

import backoff
import httpx

from app.services.utils import format_utc_datetime, parse_aware_datetime


logger = logging.getLogger(__name__)

HEARTBEAT_ESN = "300034012609560"
AFF_NS = "https://www.aff.gov/affSchema"

# AFF_REQUEST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
# <aff-request>
#   <data xmlns="https://aff.gov/affSchema" sysId="DAS" rptTime="{report_time}" version="2.23">
#     <msgRequest to="spidertracks" from="DAS" msgType="Data Request" subject="Async" dateTime="{start_time}">
#   <body>{start_time}</body>
#   </msgRequest>
#   </data>
# </aff-request>"""


AFF_REQUEST_TEMPLATE = '''<?xml version="1.0" encoding="utf-8"?>
<data xmlns="https://aff.gov/affSchema" sysId="DAS" rptTime="{report_time}" version="2.23">
<msgRequest to="spidertracks" from="DAS" msgType="Data Request" subject="Async" dateTime="{start_time}">
<body>{start_time}</body>
</msgRequest>
</data>'''

class SpidertracksClient:

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url
        self.username = username
        self.password = password

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

    def _get_telemetry_value(self, ac_pos, name: str) -> str:
        telemetry_tag = f"{{{AFF_NS}}}telemetry"
        for telemetry in ac_pos.findall(telemetry_tag):
            if telemetry.get("name") == name:
                return telemetry.get("value") or ""
        return ""

    def _get_float_ns(self, element, local_tag: str) -> float:
        tag = f"{{{AFF_NS}}}{local_tag}"
        return self._get_float(element, tag)

    @staticmethod
    def _get_text(element, tag: str) -> str:
        child = element.find(tag)
        return child.text.strip() if child is not None and child.text else ""

    @staticmethod
    def _get_float(element, tag: str) -> float:
        child = element.find(tag)
        if child is not None and child.text:
            try:
                return float(child.text.strip())
            except ValueError:
                return 0.0
        return 0.0
