"""Print the author fields Tebra returns for given appointment IDs (no patient data).

Usage:
    python scripts/probe_appointment.py --ids 251242,252274 [--save-xml DIR]

--save-xml writes each raw response with patient-identifying elements removed
(PatientSummary, PatientSummaries, Notes) so it can be used as a test fixture.
"""
import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from appt_authors.tebra_client import TebraClient, parse_get_appointment  # noqa: E402

PHI_ELEMENTS = {"PatientSummary", "PatientSummaries", "Notes"}


def sanitize(xml: bytes) -> bytes:
    root = ET.fromstring(xml)
    for parent in list(root.iter()):
        for child in list(parent):
            if child.tag.rsplit("}", 1)[-1] in PHI_ELEMENTS:
                parent.remove(child)
    return ET.tostring(root, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", required=True, help="comma-separated appointment IDs")
    parser.add_argument("--save-xml", type=Path)
    args = parser.parse_args()

    client = TebraClient()
    for appt_id in [i.strip() for i in args.ids.split(",") if i.strip()]:
        xml = client.get_appointment(appt_id)
        print(parse_get_appointment(xml, appt_id))
        if args.save_xml:
            args.save_xml.mkdir(parents=True, exist_ok=True)
            (args.save_xml / f"get_appointment_{appt_id}.xml").write_bytes(sanitize(xml))


if __name__ == "__main__":
    main()
