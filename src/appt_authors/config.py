"""Runtime settings, read from environment variables."""
import os
from dataclasses import dataclass, field

DEFAULT_WSDL = "https://webservice.kareo.com/services/soap/2.1/KareoServices.svc?singleWsdl"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    return value.strip() if isinstance(value, str) else value


@dataclass(frozen=True)
class Settings:
    tebra_customer_key: str | None = field(default_factory=lambda: _env("TEBRA_CUSTOMER_KEY"))
    tebra_user: str | None = field(default_factory=lambda: _env("TEBRA_USER"))
    tebra_password: str | None = field(default_factory=lambda: _env("TEBRA_PASSWORD"))
    tebra_wsdl: str = field(default_factory=lambda: _env("TEBRA_WSDL", DEFAULT_WSDL))
    # Tebra throttles GetAppointment to one call per 250 ms.
    min_interval: float = field(default_factory=lambda: float(_env("TEBRA_MIN_INTERVAL", "0.30")))
    # Keep each run well under the 15-minute schedule so runs never overlap.
    max_run_seconds: float = field(default_factory=lambda: float(_env("MAX_RUN_SECONDS", "720")))
    batch_size: int = field(default_factory=lambda: int(_env("BATCH_SIZE", "500")))

    bq_project: str = field(default_factory=lambda: _env("BQ_PROJECT", "weekly-revenue-integration"))
    bq_dataset: str = field(default_factory=lambda: _env("BQ_DATASET", "Telemind_BD"))
    bq_location: str = field(default_factory=lambda: _env("BQ_LOCATION", "US"))

    def table(self, name: str) -> str:
        return f"`{self.bq_project}.{self.bq_dataset}.{name}`"
