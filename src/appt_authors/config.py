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
    # Timezone of the naive datetimes Tebra returns; "UTC" stores them as-is.
    tebra_timezone: str = field(default_factory=lambda: _env("TEBRA_TIMEZONE", "UTC"))

    # Tebra field / filter names. Confirm with scripts/discover_fields.py (see README).
    created_by_field: str = field(default_factory=lambda: _env("TEBRA_CREATED_BY_FIELD", "CreatedByUserID"))
    modified_by_field: str = field(default_factory=lambda: _env("TEBRA_MODIFIED_BY_FIELD", "LastModifiedByUserID"))
    from_modified_filter: str = field(default_factory=lambda: _env("TEBRA_FROM_MODIFIED_FILTER", "FromLastModifiedDate"))
    to_modified_filter: str = field(default_factory=lambda: _env("TEBRA_TO_MODIFIED_FILTER", "ToLastModifiedDate"))

    bq_project: str = field(default_factory=lambda: _env("BQ_PROJECT", "weekly-revenue-integration"))
    bq_dataset: str = field(default_factory=lambda: _env("BQ_DATASET", "Telemind_BD"))
    bq_location: str = field(default_factory=lambda: _env("BQ_LOCATION", "US"))

    def table(self, name: str) -> str:
        return f"`{self.bq_project}.{self.bq_dataset}.{name}`"
