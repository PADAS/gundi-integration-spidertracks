import pydantic

from app.actions.core import AuthActionConfiguration, PullActionConfiguration, ExecutableActionMixin
from app.services.utils import FieldWithUIOptions, GlobalUISchemaOptions


class SpidertracksAuthConfig(AuthActionConfiguration, ExecutableActionMixin):
    """Credentials for your Spidertracks Account"""

    username: str
    password: pydantic.SecretStr

    ui_global_options = GlobalUISchemaOptions(
        order=["username", "password"],
    )


class PullObservationsConfig(PullActionConfiguration):
    """Configuration for reading a Spidertracks feed."""

    service_api: str = FieldWithUIOptions(
        "https://go.spidertracks.com/api/aff/feed",
        title="API Endpoint",
        description="Spidertracks AFF feed URL",
    )
    default_lookback_days: int = FieldWithUIOptions(
        14,
        ge=1,
        le=30,
        title="Default lookback days",
        description="Days to look back on first fetch",
    )

    ui_global_options = GlobalUISchemaOptions(
        order=["service_api", "default_lookback_days"],
    )
