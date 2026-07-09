import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from gundi_core.schemas.v2 import Integration

from app.actions.configurations import PullObservationsConfig
from app.actions.handlers import action_pull_observations, _pull_locks


# Position timestamps must stay inside the handler's 30-day hard lookback
# limit, so they are generated relative to the current time.
BASE_TIME = datetime.now(tz=timezone.utc).replace(microsecond=0) - timedelta(days=1)

SAMPLE_POSITIONS = [
    {
        "esn": "300234010753370",
        "datetime": BASE_TIME,
        "latitude": -36.8485,
        "longitude": 174.7633,
        "speed": 120.5,
        "heading": 45.0,
        "altitude": 5000.0,
        "registration": "ZK-ABC",
        "track_id": "track-001",
    },
    {
        "esn": "300234010753371",
        "datetime": BASE_TIME + timedelta(minutes=5),
        "latitude": -37.7870,
        "longitude": 175.2793,
        "speed": 95.0,
        "heading": 180.0,
        "altitude": 3500.0,
        "registration": "ZK-DEF",
        "track_id": "track-002",
    },
]


@pytest.fixture
def integration_with_auth():
    return Integration.parse_obj(
        {
            "id": "aaaa1111-bbbb-cccc-dddd-eeee2222ffff",
            "name": "Spidertracks Test",
            "base_url": "",
            "enabled": True,
            "type": {
                "id": "50229e21-a9fe-4caa-862c-8592dfb2479b",
                "name": "Spidertracks",
                "value": "spidertracks",
                "description": "Spidertracks integration",
                "actions": [
                    {
                        "id": "80448d1c-4696-4b32-a59f-f3494fc949ac",
                        "type": "auth",
                        "name": "Authenticate",
                        "value": "auth",
                    },
                    {
                        "id": "75b3040f-ab1f-42e7-b39f-8965c088b154",
                        "type": "pull",
                        "name": "Pull Observations",
                        "value": "pull_observations",
                    },
                ],
            },
            "owner": {
                "id": "a91b400b-482a-4546-8fcb-ee42b01deeb6",
                "name": "Test Org",
                "description": "",
            },
            "configurations": [
                {
                    "id": "30f8878c-4a98-4c95-88eb-79f73c40fb2f",
                    "integration": "aaaa1111-bbbb-cccc-dddd-eeee2222ffff",
                    "action": {
                        "id": "80448d1c-4696-4b32-a59f-f3494fc949ac",
                        "type": "auth",
                        "name": "Authenticate",
                        "value": "auth",
                    },
                    "data": {
                        "service_username": "testuser",
                        "service_password": "testpass",
                    },
                },
            ],
            "additional": {},
            "default_route": None,
            "status": "healthy",
            "status_details": "",
        }
    )


@pytest.fixture
def pull_config():
    return PullObservationsConfig(
        service_api="https://go.spidertracks.com/api/aff/feed",
        default_lookback_days=14,
    )


@pytest.fixture
def mock_state_manager():
    manager = AsyncMock()
    manager.get_state = AsyncMock(return_value={})
    manager.set_state = AsyncMock()
    return manager


@pytest.fixture
def mock_spidertracks_client():
    client = AsyncMock()
    client.fetch_positions = AsyncMock(return_value=SAMPLE_POSITIONS)
    return client


@pytest.fixture
def mock_send_observations():
    return AsyncMock(return_value={"status": "ok"})


class TestActionPullObservations:

    @pytest.mark.asyncio
    async def test_successful_pull(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            result = await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        assert result["observations_sent"] == 2
        mock_spidertracks_client.fetch_positions.assert_called_once()
        mock_send_observations.assert_called_once()
        mock_state_manager.set_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_first_run_uses_lookback(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        mock_state_manager.get_state.return_value = {}

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        call_args = mock_spidertracks_client.fetch_positions.call_args[0]
        start_time = call_args[0]
        expected_min = datetime.now(tz=timezone.utc) - timedelta(days=15)
        expected_max = datetime.now(tz=timezone.utc) - timedelta(days=13)
        assert expected_min < start_time < expected_max

    @pytest.mark.asyncio
    async def test_subsequent_run_uses_saved_timestamp(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        saved_timestamp = (
            datetime.now(tz=timezone.utc).replace(microsecond=0) - timedelta(days=2)
        ).isoformat()
        mock_state_manager.get_state.return_value = {"latest_timestamp": saved_timestamp}

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        call_args = mock_spidertracks_client.fetch_positions.call_args[0]
        start_time = call_args[0]
        assert start_time == datetime.fromisoformat(saved_timestamp)

    @pytest.mark.asyncio
    async def test_empty_response(
        self, integration_with_auth, pull_config, mock_state_manager, mock_send_observations,
    ):
        mock_client = AsyncMock()
        mock_client.fetch_positions = AsyncMock(return_value=[])

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            result = await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        assert result["observations_sent"] == 0
        mock_send_observations.assert_not_called()
        mock_state_manager.set_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_api_error_propagates(
        self, integration_with_auth, pull_config, mock_state_manager,
    ):
        mock_client = AsyncMock()
        mock_client.fetch_positions = AsyncMock(
            side_effect=Exception("API connection failed")
        )

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_client):

            with pytest.raises(Exception, match="API connection failed"):
                await action_pull_observations(
                    integration=integration_with_auth,
                    action_config=pull_config,
                )

    @pytest.mark.asyncio
    async def test_no_auth_config_raises(self, pull_config, mock_state_manager):
        integration_no_auth = Integration.parse_obj(
            {
                "id": "aaaa1111-bbbb-cccc-dddd-eeee2222ffff",
                "name": "Spidertracks No Auth",
                "base_url": "",
                "enabled": True,
                "type": {
                    "id": "50229e21-a9fe-4caa-862c-8592dfb2479b",
                    "name": "Spidertracks",
                    "value": "spidertracks",
                    "description": "",
                    "actions": [],
                },
                "owner": {
                    "id": "a91b400b-482a-4546-8fcb-ee42b01deeb6",
                    "name": "Test Org",
                    "description": "",
                },
                "configurations": [],
                "additional": {},
                "default_route": None,
                "status": "healthy",
                "status_details": "",
            }
        )

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager):
            with pytest.raises(ValueError, match="No auth configuration found"):
                await action_pull_observations(
                    integration=integration_no_auth,
                    action_config=pull_config,
                )

    @pytest.mark.asyncio
    async def test_state_saved_with_max_timestamp(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        saved_state = mock_state_manager.set_state.call_args[1]["state"]
        assert saved_state["latest_timestamp"] == SAMPLE_POSITIONS[1]["datetime"].isoformat()

    @pytest.mark.asyncio
    async def test_observations_format(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        sent_observations = mock_send_observations.call_args[1]["observations"]
        obs = sent_observations[0]
        assert obs["source"] == "300234010753370"
        assert obs["type"] == "tracking-device"
        assert obs["subject_type"] == "aircraft"
        assert obs["location"]["lat"] == -36.8485
        assert obs["location"]["lon"] == 174.7633
        assert obs["additional"]["speed"] == 120.5
        assert obs["additional"]["track_id"] == "track-001"
        assert obs["additional"]["registration"] == "ZK-ABC"

    @pytest.mark.asyncio
    async def test_skips_when_already_running(
        self, integration_with_auth, pull_config, mock_spidertracks_client,
    ):
        integration_id = str(integration_with_auth.id)
        # Pre-populate and acquire the lock to simulate an in-progress run
        _pull_locks[integration_id] = asyncio.Lock()
        await _pull_locks[integration_id].acquire()

        try:
            result = await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )
        finally:
            _pull_locks[integration_id].release()

        assert result == {"observations_sent": 0, "skipped": True}
        mock_spidertracks_client.fetch_positions.assert_not_called()

    @pytest.mark.asyncio
    async def test_lock_released_after_run(
        self, integration_with_auth, pull_config, mock_state_manager,
        mock_spidertracks_client, mock_send_observations,
    ):
        integration_id = str(integration_with_auth.id)
        # Clear any pre-existing lock so this test is deterministic
        _pull_locks.pop(integration_id, None)

        with patch("app.actions.handlers.IntegrationStateManager", return_value=mock_state_manager), \
             patch("app.actions.handlers.SpidertracksClient", return_value=mock_spidertracks_client), \
             patch("app.actions.handlers.send_observations_to_gundi", mock_send_observations):

            await action_pull_observations(
                integration=integration_with_auth,
                action_config=pull_config,
            )

        assert not _pull_locks[integration_id].locked()
