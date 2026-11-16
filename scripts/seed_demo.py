#!/usr/bin/env python3
"""Optional first-boot demo seeder.

Uploads the bundled sample taxonomy package and the four sample XBRL
instances (feasible / rounding / infeasible / pending), then files four
feasible year-to-date quarterly reports and derives one quarterly dataset,
so that /docs has data to explore immediately. Idempotent: skips when a
marker exists.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

BASE = os.environ.get("XRECON_BASE_URL", "http://127.0.0.1:8000")
DATA_DIR = Path(os.environ.get("XRECON_DATA_DIR", "/data"))
_default_samples = Path(__file__).resolve().parents[1] / "samples"
if not _default_samples.exists():  # local checkout layout
    _default_samples = Path(__file__).resolve().parents[1] / "tests" / "samples"
SAMPLES = Path(os.environ.get("XRECON_SAMPLES_DIR", str(_default_samples)))
MARKER = DATA_DIR / ".seeded"

# YTD cumulative demo series (all summation equations hold exactly).
QUARTERLY_YTD = [
    ("03-31", {"Cash": 100, "Receivables": 60, "Inventory": 40, "CurrentAssets": 200,
               "NonCurrentAssets": 300, "Assets": 500, "Revenues": 800, "Expenses": 500,
               "NetIncome": 300}),
    ("06-30", {"Cash": 150, "Receivables": 90, "Inventory": 60, "CurrentAssets": 300,
               "NonCurrentAssets": 300, "Assets": 600, "Revenues": 1600, "Expenses": 1000,
               "NetIncome": 600}),
    ("09-30", {"Cash": 180, "Receivables": 120, "Inventory": 75, "CurrentAssets": 375,
               "NonCurrentAssets": 300, "Assets": 675, "Revenues": 2400, "Expenses": 1500,
               "NetIncome": 900}),
    ("12-31", {"Cash": 260, "Receivables": 150, "Inventory": 90, "CurrentAssets": 500,
               "NonCurrentAssets": 300, "Assets": 800, "Revenues": 3200, "Expenses": 2000,
               "NetIncome": 1200}),
]


def _quarterly_xml(year: int, end: str, facts: dict) -> bytes:
    rows = "\n".join(
        f'  <acct:{name} contextRef="ctx1" unitRef="usd" decimals="0">{value}</acct:{name}>'
        for name, value in facts.items()
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:acct="http://example.com/acct"
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <xbrli:context id="ctx1">
    <xbrli:entity><xbrli:identifier scheme="http://example.com/ids">E-001</xbrli:identifier></xbrli:entity>
    <xbrli:period><xbrli:startDate>{year}-01-01</xbrli:startDate><xbrli:endDate>{year}-{end}</xbrli:endDate></xbrli:period>
  </xbrli:context>
  <xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>
{rows}
</xbrli:xbrl>
""".encode()


def _post(url: str, fields: dict[str, str], files: dict[str, tuple[str, bytes]]):
    boundary = f"----xrecon{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
            f"{value}\r\n".encode()
        )
    for name, (filename, content) in files.items():
        body.extend(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/xml\r\n\r\n".encode()
        )
        body.extend(content)
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def _post_json(url: str, payload, headers: dict | None = None):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def _wait_for_api(retries: int = 30) -> None:
    for _ in range(retries):
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=3) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise RuntimeError("API did not become healthy in time")


def main() -> int:
    if MARKER.exists():
        print("[seed] marker found, skipping")
        return 0
    _wait_for_api()
    xsd = (SAMPLES / "acct.xsd").read_bytes()
    calc = (SAMPLES / "acct-calc.xml").read_bytes()
    status, raw = _post(
        f"{BASE}/packages",
        {"name": "demo-accounting"},
        {"xsd": ("acct.xsd", xsd), "calculation": ("acct-calc.xml", calc)},
    )
    assert status == 201, raw
    package_id = json.loads(raw)["package_id"]
    for scenario in ("feasible", "rounding", "infeasible", "pending"):
        xml = (SAMPLES / f"instance-{scenario}.xml").read_bytes()
        status, raw = _post(
            f"{BASE}/packages/{package_id}/instances",
            {},
            {"instance": (f"instance-{scenario}.xml", xml)},
        )
        assert status == 201, raw
        print(f"[seed] uploaded instance-{scenario}")

    # Four feasible year-to-date reports -> one quarterly dataset.
    report_ids = []
    for end, facts in QUARTERLY_YTD:
        xml = _quarterly_xml(2026, end, facts)
        status, raw = _post(
            f"{BASE}/packages/{package_id}/instances",
            {},
            {"instance": (f"instance-ytd-{end}.xml", xml)},
        )
        assert status == 201, raw
        instance_id = json.loads(raw)["instance_id"]
        status, raw = _post_json(
            f"{BASE}/instances/{instance_id}/checks", None,
            {"Expected-Instance-Revision": "1", "Expected-Package-Revision": "1"},
        )
        assert status == 200, raw
        report = json.loads(raw)["report"]
        assert report["status"] == "feasible", report
        report_ids.append(report["report_id"])
        print(f"[seed] checked YTD report for quarter ending {end}")
    status, raw = _post_json(f"{BASE}/datasets", {"report_ids": report_ids})
    assert status == 201, raw
    print(f"[seed] created quarterly dataset {json.loads(raw)['dataset_id']}")

    MARKER.touch()
    print(f"[seed] done; package_id={package_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
