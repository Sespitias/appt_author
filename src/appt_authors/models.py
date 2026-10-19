from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AppointmentAuthor:
    id: str
    created_by_user_id: str | None
    last_modified_by_user_id: str | None
    created_date: datetime | None
    last_modified_date: datetime | None
    confirmation_status: str | None
