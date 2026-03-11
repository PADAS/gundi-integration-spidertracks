import logging
from datetime import datetime, timedelta, timezone

from gundi_core.schemas.v2 import Integration

from app.actions.configurations import PullObservationsConfig, SpidertracksAuthConfig
from app.services.client import SpidertracksClient
from app.services.gundi import send_observations_to_gundi
from app.services.state import IntegrationStateManager
from app.services.utils import find_config_for_action, format_utc_datetime, generate_batches
from app.services.action_scheduler import crontab_schedule

logger = logging.getLogger(__name__)

BATCH_SIZE = 200
MAX_LOOKBACK_DAYS = 30

DEFAULT_SERVICE_API = "https://go.spidertracks.com/api/aff/feed"


async def action_credentials(
    integration: Integration,
    action_config: SpidertracksAuthConfig,
) -> dict:
    """Validate Spidertracks credentials by performing a minimal API request."""
    logger.info(f"Running auth check for integration {integration.id}")
    client = SpidertracksClient(
        base_url=DEFAULT_SERVICE_API,
        username=action_config.username,
        password=action_config.password.get_secret_value(),
    )
    start_time = datetime.now(tz=timezone.utc) - timedelta(days=1)
    await client.fetch_positions(start_time)
    logger.info(f"Auth check passed for integration {integration.id}")
    return {"status": "success"}

@crontab_schedule("*/2 * * * *")
async def action_pull_observations(
    integration: Integration,
    action_config: PullObservationsConfig,
) -> dict:
    logger.info(f"Starting pull_observations for integration {integration.id}")

    # Extract auth credentials
    auth_config = _get_auth_config(integration)
    if not auth_config:
        raise ValueError(f"No auth configuration found for integration {integration.id}")

    # Create client
    client = SpidertracksClient(
        base_url=DEFAULT_SERVICE_API,
        username=auth_config.get("username", ""),
        password=auth_config.get("password", ""),
    )

    # Load state
    state_manager = IntegrationStateManager()
    state = await state_manager.get_state(
        integration_id=str(integration.id),
        action_id="pull_observations",
    )

    # Determine start time
    if latest := state.get("latest_timestamp"):
        start_time = datetime.fromisoformat(latest)
    else:
        start_time = datetime.now(tz=timezone.utc) - timedelta(
            days=action_config.default_lookback_days
        )

    # Hard limit: do not look back more than 30 days
    now_utc = datetime.now(tz=timezone.utc)
    min_start = now_utc - timedelta(days=MAX_LOOKBACK_DAYS)
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    start_time = max(start_time, min_start)

    logger.info(f"Fetching positions since {start_time.isoformat()}")

    # Fetch positions
    positions = await client.fetch_positions(start_time)
    if not positions:
        logger.info("No new positions found")
        return {"observations_sent": 0}

    # Transform to Gundi observations
    observations = []
    max_timestamp = start_time
    for pos in positions:
        obs = {
            "source": pos["esn"],
            "source_name": pos["registration"],
            "type": "tracking-device",
            "subject_type": "aircraft",
            "recorded_at": pos["datetime"].isoformat(),
            "location": {
                "lat": pos["latitude"],
                "lon": pos["longitude"],
            },
            "additional": {
                "speed": pos["speed"],
                "heading": pos["heading"],
                "altitude": pos["altitude"],
                "track_id": pos["track_id"],
                "registration": pos["registration"],
            },
        }
        observations.append(obs)

        # Track max timestamp
        max_timestamp = max(max_timestamp, pos["datetime"])
    # Send in batches
    total_sent = 0
    for batch in generate_batches(observations, BATCH_SIZE):
        await send_observations_to_gundi(
            observations=batch,
            integration_id=str(integration.id),
        )
        total_sent += len(batch)
        logger.info(f"Sent batch of {len(batch)} observations ({total_sent}/{len(observations)})")

    # Save state
    await state_manager.set_state(
        integration_id=str(integration.id),
        action_id="pull_observations",
        state={"latest_timestamp": max_timestamp.isoformat()},
    )

    logger.info(f"Completed pull_observations: {total_sent} observations sent")
    return {"observations_sent": total_sent}


def _get_auth_config(integration: Integration) -> dict:
    for config in integration.configurations:
        if config.action.type == "auth":
            return config.data or {}
    return None
