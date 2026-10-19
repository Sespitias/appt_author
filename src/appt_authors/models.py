from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AppointmentAuthor:
    id: str
    created_by_user_id: str | None
    last_modified_by_user_id: str | None   # Tebra UpdatedBy; None when 0 (never modified since creation)
    created_at_utc: datetime | None
    updated_at_utc: datetime | None
    was_created_online: bool | None
    is_deleted: bool | None
    prod_last_modified: datetime | None = None  # appointment_prod.LastModifiedDate this row answers for
    fetch_error: str | None = None
