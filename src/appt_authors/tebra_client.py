"""Tebra (Kareo) SOAP access: fetch appointment author fields by LastModifiedDate window."""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Settings
from .models import AppointmentAuthor

log = logging.getLogger(__name__)

DATE_FORMATS = ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%m/%d/%Y")
NULL_USER_IDS = {"", "0"}
MIN_WINDOW = timedelta(hours=1)

FetchWindow = Callable[[datetime, datetime], list[AppointmentAuthor]]


class TebraError(RuntimeError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: ET.Element, name: str) -> str | None:
    for child in el:
        if _local(child.tag) == name:
            return child.text.strip() if child.text else None
    return None


def _user_id(value: str | None) -> str | None:
    if value is None or value.strip() in NULL_USER_IDS:
        return None
    return value.strip()


def parse_tebra_datetime(value: str | None, tz: str = "UTC") -> datetime | None:
    if not value:
        return None
    for fmt in DATE_FORMATS:
        try:
            naive = datetime.strptime(value, fmt)
        except ValueError:
            continue
        return naive.replace(tzinfo=ZoneInfo(tz)).astimezone(timezone.utc)
    raise ValueError(f"Unrecognised Tebra date: {value!r}")


def parse_appointments(xml: bytes | str, settings: Settings | None = None, tz: str | None = None) -> list[AppointmentAuthor]:
    s = settings or Settings()
    tz = tz or s.tebra_timezone
    root = ET.fromstring(xml)

    for el in root.iter():
        if _local(el.tag) == "ErrorResponse" and (_child_text(el, "IsError") or "").lower() == "true":
            raise TebraError(_child_text(el, "ErrorMessage") or "Tebra returned IsError=true")

    out = []
    for el in root.iter():
        if _local(el.tag) != "AppointmentData":
            continue
        appt_id = _child_text(el, "ID")
        if not appt_id:
            continue
        out.append(AppointmentAuthor(
            id=appt_id,
            created_by_user_id=_user_id(_child_text(el, s.created_by_field)),
            last_modified_by_user_id=_user_id(_child_text(el, s.modified_by_field)),
            created_date=parse_tebra_datetime(_child_text(el, "CreatedDate"), tz),
            last_modified_date=parse_tebra_datetime(_child_text(el, "LastModifiedDate"), tz),
            confirmation_status=_child_text(el, "ConfirmationStatus"),
        ))
    return out


class TebraClient:
    def __init__(self, settings: Settings | None = None):
        from zeep import Client  # imported lazily so unit tests don't need the WSDL
        from zeep.transports import Transport

        self.s = settings or Settings()
        if not (self.s.tebra_customer_key and self.s.tebra_user and self.s.tebra_password):
            raise TebraError("Set TEBRA_CUSTOMER_KEY, TEBRA_USER and TEBRA_PASSWORD")
        # Bounded waits: a hung call must fail well before the next 15-min run starts.
        self.client = Client(self.s.tebra_wsdl, transport=Transport(timeout=60, operation_timeout=300))

    def _tebra_time(self, dt: datetime) -> str:
        return dt.astimezone(ZoneInfo(self.s.tebra_timezone)).strftime("%Y-%m-%dT%H:%M:%S")

    def get_modified(self, start: datetime, end: datetime) -> list[AppointmentAuthor]:
        fields = ["ID", "ConfirmationStatus", "CreatedDate", "LastModifiedDate",
                  self.s.created_by_field, self.s.modified_by_field]
        request = {
            "RequestHeader": {
                "CustomerKey": self.s.tebra_customer_key,
                "User": self.s.tebra_user,
                "Password": self.s.tebra_password,
            },
            "Fields": {f: True for f in fields},
            "Filter": {
                self.s.from_modified_filter: self._tebra_time(start),
                self.s.to_modified_filter: self._tebra_time(end),
            },
        }
        with self.client.settings(raw_response=True):
            response = self.client.service.GetAppointments(request=request)
        response.raise_for_status()
        return parse_appointments(response.content, self.s)


def _fetch_window(call: FetchWindow, start: datetime, end: datetime) -> list[AppointmentAuthor]:
    try:
        return call(start, end)
    except Exception as exc:
        if end - start <= MIN_WINDOW:
            raise
        mid = start + (end - start) / 2
        log.warning("Tebra call %s→%s failed (%s); splitting window", start, end, exc)
        return _fetch_window(call, start, mid) + _fetch_window(call, mid, end)


def fetch_appointment_authors(
    start: datetime,
    end: datetime,
    *,
    chunk: timedelta = timedelta(days=1),
    call: FetchWindow | None = None,
) -> list[AppointmentAuthor]:
    """All appointments whose LastModifiedDate is in [start, end), one row per ID (latest wins)."""
    call = call or TebraClient().get_modified
    rows: list[AppointmentAuthor] = []
    cursor = start
    while cursor < end:
        nxt = min(cursor + chunk, end)
        rows.extend(_fetch_window(call, cursor, nxt))
        cursor = nxt

    latest: dict[str, AppointmentAuthor] = {}
    floor = datetime.min.replace(tzinfo=timezone.utc)
    for row in rows:
        cur = latest.get(row.id)
        if cur is None or (row.last_modified_date or floor) > (cur.last_modified_date or floor):
            latest[row.id] = row
    return list(latest.values())
