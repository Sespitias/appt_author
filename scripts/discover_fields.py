"""Task 1 spike: find which GetAppointments fields/filters carry user IDs.

Usage:
    TEBRA_CUSTOMER_KEY=... TEBRA_USER=... TEBRA_PASSWORD=... \
    python scripts/discover_fields.py --date 2026-10-18 --out discovery.xml

Prints the user/date-related element names declared in the WSDL and writes the
raw XML of up to --limit appointments returned for that day (all fields).
The output contains PHI: anonymise it before committing it as a test fixture.
"""
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from zeep import Client  # noqa: E402

from appt_authors.config import Settings  # noqa: E402

INTERESTING = re.compile(r"user|createdby|modifiedby|lastmodified|author", re.I)


def wsdl_element_names(client: Client, type_name: str) -> list[str]:
    for t in client.wsdl.types.types:
        if t.name == type_name:
            return [name for name, _ in t.elements]
    return []


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD (appointment start date)")
    parser.add_argument("--out", required=True)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()

    s = Settings()
    if not (s.tebra_customer_key and s.tebra_user and s.tebra_password):
        sys.exit("Set TEBRA_CUSTOMER_KEY, TEBRA_USER and TEBRA_PASSWORD")

    client = Client(s.tebra_wsdl)
    for type_name in ("AppointmentFieldsToReturn", "AppointmentFilterCriteria", "AppointmentFilter", "AppointmentData"):
        names = wsdl_element_names(client, type_name)
        if names:
            print(f"{type_name}: {len(names)} elements")
            print("  user/date related:", [n for n in names if INTERESTING.search(n)])

    request = {
        "RequestHeader": {
            "CustomerKey": s.tebra_customer_key,
            "User": s.tebra_user,
            "Password": s.tebra_password,
        },
        "Filter": {"StartDate": f"{args.date}T00:00:00", "EndDate": f"{args.date}T23:59:59"},
    }
    with client.settings(raw_response=True):
        response = client.service.GetAppointments(request=request)
    response.raise_for_status()

    root = ET.fromstring(response.content)
    appts = [el for el in root.iter() if el.tag.endswith("AppointmentData")]
    print(f"Appointments returned: {len(appts)}")
    if appts:
        print("Fields present in first appointment:",
              [c.tag.split("}")[-1] for c in appts[0] if INTERESTING.search(c.tag)])
    error = [el.text for el in root.iter() if el.tag.endswith("ErrorMessage") and el.text]
    if error:
        print("Tebra error:", error)

    # Keep only the first N appointments to limit PHI in the dump.
    for parent in root.iter():
        kids = [c for c in parent if c.tag.endswith("AppointmentData")]
        for extra in kids[args.limit:]:
            parent.remove(extra)
    Path(args.out).write_bytes(ET.tostring(root, encoding="utf-8"))
    print(f"Raw XML written to {args.out}")


if __name__ == "__main__":
    main()
