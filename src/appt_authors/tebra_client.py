"""Tebra (Kareo) SOAP access: creator / last modifier of an appointment via GetAppointment."""
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Callable, Iterable, Iterator

from .config import Settings
from .models import AppointmentAuthor

log = logging.getLogger(__name__)

NULL_USER_IDS = {"", "0"}
# Errors that are about this appointment and won't go away by retrying. Anything else Tebra reports is
# treated as transient, so an outage never marks thousands of appointments as permanently failed.
PERMANENT_MESSAGE = re.compile(r"does not exist|not found|deleted|not authorized|permission", re.I)
RETRIES = 3
BACKOFF_SECONDS = 2.0
# This many appointments in a row failing after retries means Tebra is down: stop the run.
MAX_CONSECUTIVE_FAILURES = 5
SERIES_ID = re.compile(r"^(\d+)")


class TebraError(RuntimeError):
    pass


class TebraAuthError(TebraError):
    """Bad credentials / customer key / permissions: retrying cannot help."""


class TebraTransientError(TebraError):
    """Throttling or a temporary failure: worth retrying."""


RETRYABLE = (TebraTransientError, OSError, ET.ParseError)  # OSError covers requests/zeep transport errors


def tebra_appointment_id(prod_id: str) -> str | None:
    """Tebra ID to ask for. Recurring occurrences appear in prod as <series id><suffix> (e.g. 163379KKF)."""
    match = SERIES_ID.match(prod_id or "")
    return match.group(1) if match else None


def _failed(appointment_id: str, error: str) -> AppointmentAuthor:
    return AppointmentAuthor(appointment_id, None, None, None, None, None, None, fetch_error=error[:500])


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el: ET.Element, name: str) -> ET.Element | None:
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _child_text(el: ET.Element | None, name: str) -> str | None:
    child = _find(el, name) if el is not None else None
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _user_id(value: str | None) -> str | None:
    if value is None or value.strip() in NULL_USER_IDS:
        return None
    return value.strip()


def _bool(value: str | None) -> bool | None:
    return None if value is None else value.lower() == "true"


def _utc(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def parse_get_appointment(xml: bytes, appointment_id: str) -> AppointmentAuthor:
    root = ET.fromstring(xml)
    result = next((el for el in root.iter() if _local(el.tag) == "GetAppointmentResult"), None)
    if result is None:
        raise TebraTransientError(f"Unexpected GetAppointment response for {appointment_id}")

    error, security = _find(result, "ErrorResponse"), _find(result, "SecurityResponse")
    message = _child_text(error, "ErrorMessage") or "Tebra returned IsError=true"
    if any(_child_text(security, f) == "false" for f in ("Authenticated", "CustomerKeyValid")):
        raise TebraAuthError(message)
    if _child_text(security, "Authorized") == "false":  # this appointment's practice, not the whole account
        return _failed(appointment_id, f"Not authorized: {message}")
    if _child_text(error, "IsError") == "true":
        if PERMANENT_MESSAGE.search(message):
            return _failed(appointment_id, message)
        raise TebraTransientError(message)

    appt = _find(result, "Appointment")
    return AppointmentAuthor(
        id=appointment_id,
        created_by_user_id=_user_id(_child_text(appt, "CreatedBy")),
        last_modified_by_user_id=_user_id(_child_text(appt, "UpdatedBy")),
        created_at_utc=_utc(_child_text(appt, "CreatedAt")),
        updated_at_utc=_utc(_child_text(appt, "UpdatedAt")),
        was_created_online=_bool(_child_text(appt, "WasCreatedOnline")),
        is_deleted=_bool(_child_text(appt, "IsDeleted")),
    )


class TebraClient:
    def __init__(self, settings: Settings | None = None):
        from zeep import Client  # imported lazily so unit tests don't need the WSDL
        from zeep.transports import Transport

        self.s = settings or Settings()
        if not (self.s.tebra_customer_key and self.s.tebra_user and self.s.tebra_password):
            raise TebraAuthError("Set TEBRA_CUSTOMER_KEY, TEBRA_USER and TEBRA_PASSWORD")
        # Bounded waits: a hung call must fail well before the next 15-min run starts.
        self.client = Client(self.s.tebra_wsdl, transport=Transport(timeout=60, operation_timeout=30))

    def get_appointment(self, appointment_id: str) -> bytes:
        request = {
            "RequestHeader": {
                "CustomerKey": self.s.tebra_customer_key,
                "User": self.s.tebra_user,
                "Password": self.s.tebra_password,
            },
            "Appointment": {"AppointmentId": int(appointment_id)},
        }
        with self.client.settings(raw_response=True):
            response = self.client.service.GetAppointment(request=request)
        if response.status_code >= 500:
            raise TebraTransientError(f"HTTP {response.status_code}")
        response.raise_for_status()
        return response.content

    def fetch(self, appointment_id: str) -> AppointmentAuthor:
        return parse_get_appointment(self.get_appointment(appointment_id), appointment_id)


def fetch_authors(
    ids: Iterable[str],
    *,
    call: Callable[[str], AppointmentAuthor],
    deadline: float,
    min_interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[AppointmentAuthor]:
    """Yield one AppointmentAuthor per ID, throttled, until `deadline` (a `clock()` value).

    - Transient errors are retried RETRIES times with exponential backoff (never past the deadline).
      If an ID still fails it is skipped (it stays pending for the next run); after
      MAX_CONSECUTIVE_FAILURES skipped IDs in a row the last error propagates (Tebra is down).
    - Any other exception is specific to that appointment: it is yielded as a fetch_error row.
    - TebraAuthError propagates at once.
    """
    last_call = None
    consecutive_failures = 0
    for appointment_id in ids:
        if clock() >= deadline:
            return
        result, last_error = None, None
        for attempt in range(RETRIES):
            if last_call is not None:
                wait = last_call + min_interval - clock()
                if wait > 0:
                    sleep(wait)
            last_call = clock()
            try:
                result = call(appointment_id)
                break
            except TebraAuthError:
                raise
            except RETRYABLE as exc:
                last_error = exc
                pause = BACKOFF_SECONDS * 2 ** attempt
                if attempt == RETRIES - 1 or clock() + pause >= deadline:
                    break
                log.warning("GetAppointment %s failed (%s); retry %d", appointment_id, exc, attempt + 1)
                sleep(pause)
            except Exception as exc:
                result = _failed(appointment_id, f"{type(exc).__name__}: {exc}")
                break
        if result is None:
            consecutive_failures += 1
            log.warning("GetAppointment %s skipped after retries: %s", appointment_id, last_error)
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise last_error
            continue
        consecutive_failures = 0
        yield result
