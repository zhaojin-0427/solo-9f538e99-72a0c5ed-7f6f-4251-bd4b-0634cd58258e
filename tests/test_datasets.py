"""Quarterly datasets: four feasible YTD reports -> Q1 cumulative + Q2..Q4 differences."""
from __future__ import annotations

from conftest import check, create_instance, create_package

ENDS = {"Q1": "03-31", "Q2": "06-30", "Q3": "09-30", "Q4": "12-31"}

# Year-to-date cumulative amounts per quarter; all equations hold exactly.
YTD = {
    "Q1": {"Cash": 100, "Receivables": 60, "Inventory": 40, "CurrentAssets": 200,
           "NonCurrentAssets": 300, "Assets": 500, "Revenues": 800, "Expenses": 500,
           "NetIncome": 300, "Liabilities": 150},
    "Q2": {"Cash": 150, "Receivables": 90, "Inventory": 60, "CurrentAssets": 300,
           "NonCurrentAssets": 300, "Assets": 600, "Revenues": 1600, "Expenses": 1000,
           "NetIncome": 600, "Liabilities": 250},
    # Liabilities deliberately absent in Q3: unreferenced by equations, so the
    # report stays feasible and the dataset must mark the concept pending.
    "Q3": {"Cash": 180, "Receivables": 120, "Inventory": 75, "CurrentAssets": 375,
           "NonCurrentAssets": 300, "Assets": 675, "Revenues": 2400, "Expenses": 1500,
           "NetIncome": 900},
    "Q4": {"Cash": 260, "Receivables": 150, "Inventory": 90, "CurrentAssets": 500,
           "NonCurrentAssets": 300, "Assets": 800, "Revenues": 3200, "Expenses": 2000,
           "NetIncome": 1200, "Liabilities": 400},
}


def ytd_instance(year, end, facts, entity="E-001", currency="USD",
                 start="01-01", instant=False):
    rows = "\n".join(
        f'  <acct:{c} contextRef="ctx1" unitRef="u" decimals="0">{v}</acct:{c}>'
        for c, v in facts.items()
    )
    if instant:
        period = f'<xbrli:instant>{year}-{end}</xbrli:instant>'
    else:
        period = (f'<xbrli:startDate>{year}-{start}</xbrli:startDate>'
                  f'<xbrli:endDate>{year}-{end}</xbrli:endDate>')
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:acct="http://example.com/acct"
            xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
  <xbrli:context id="ctx1">
    <xbrli:entity><xbrli:identifier scheme="http://example.com/ids">{entity}</xbrli:identifier></xbrli:entity>
    <xbrli:period>{period}</xbrli:period>
  </xbrli:context>
  <xbrli:unit id="u"><xbrli:measure>iso4217:{currency}</xbrli:measure></xbrli:unit>
{rows}
</xbrli:xbrl>
""".encode()


def feasible_quarter(client, package_id, quarter, pkg_rev=1, **kw):
    """Upload one YTD instance for the quarter and return its feasible report."""
    inst = create_instance(
        client, package_id,
        ytd_instance(2026, ENDS[quarter], YTD[quarter], **kw),
    )
    r = check(client, inst["instance_id"], 1, pkg_rev)
    assert r.status_code == 200, r.text
    report = r.json()["report"]
    assert report["status"] == "feasible", report
    return report


def four_quarters(client, samples, **kw):
    pkg = create_package(client, samples)
    reports = {q: feasible_quarter(client, pkg["package_id"], q, **kw)
               for q in ENDS}
    return pkg, reports


def post_dataset(client, report_ids):
    return client.post("/datasets", json={"report_ids": list(report_ids)})


def facts_by_concept(dataset):
    return {f["concept"]: f for f in dataset["facts"]}


# ------------------------------------------------------------------ happy path
def test_dataset_quarterly_amounts_and_intervals(client, samples):
    pkg, reports = four_quarters(client, samples)

    # submission order does not matter; quarters are inferred from periods
    order = ["Q3", "Q1", "Q4", "Q2"]
    r = post_dataset(client, [reports[q]["report_id"] for q in order])
    assert r.status_code == 201, r.text
    ds = r.json()

    assert ds["current"] is True
    assert ds["entity"] == "E-001" and ds["currency"] == "USD"
    assert ds["year"] == 2026
    assert ds["package_id"] == pkg["package_id"] and ds["package_revision"] == 1
    # sources saved in quarter order with the exact reports/instances used
    assert [s["quarter"] for s in ds["sources"]] == ["Q1", "Q2", "Q3", "Q4"]
    assert [s["report_id"] for s in ds["sources"]] == [
        reports[q]["report_id"] for q in ("Q1", "Q2", "Q3", "Q4")]
    assert all(s["instance_revision"] == 1 for s in ds["sources"])
    assert ds["sources"][0]["period"] == "duration=2026-01-01/2026-03-31"

    facts = facts_by_concept(ds)
    assert len(facts) == 10  # 9 equation concepts + Liabilities

    cash = facts["acct:Cash"]
    assert cash["status"] == "ok"
    got = {q["quarter"]: q for q in cash["quarters"]}
    # Q1 = cumulative amount with its own rounding interval (decimals=0 -> ±0.5)
    assert got["Q1"]["reported_value"] == 100.0
    assert (got["Q1"]["lower"], got["Q1"]["upper"]) == (99.5, 100.5)
    # Q2..Q4 = adjacent YTD differences; lower=cur.lower-prev.upper, upper=cur.upper-prev.lower
    assert got["Q2"]["reported_value"] == 50.0
    assert (got["Q2"]["lower"], got["Q2"]["upper"]) == (49.0, 51.0)
    assert got["Q3"]["reported_value"] == 30.0
    assert (got["Q3"]["lower"], got["Q3"]["upper"]) == (29.0, 31.0)
    assert got["Q4"]["reported_value"] == 80.0
    assert (got["Q4"]["lower"], got["Q4"]["upper"]) == (79.0, 81.0)
    # witness-based amounts stay inside the derived intervals
    for q in cash["quarters"]:
        assert q["lower"] <= q["true_value"] <= q["upper"]

    # concept missing from one source: whole concept pending, never zero-filled
    liab = facts["acct:Liabilities"]
    assert liab["status"] == "pending"
    assert liab["missing_in"] == ["Q3"]
    assert liab["quarters"] == []
    assert ds["detail"]["pending_concept_count"] == 1
    assert ds["detail"]["concept_count"] == 10

    # readable via list and single-get
    listed = client.get("/datasets").json()
    assert [d["dataset_id"] for d in listed] == [ds["dataset_id"]]
    again = client.get(f"/datasets/{ds['dataset_id']}")
    assert again.status_code == 200 and again.json()["facts"] == ds["facts"]
    only = client.get("/datasets?current_only=true").json()
    assert [d["dataset_id"] for d in only] == [ds["dataset_id"]]


def test_dataset_creation_does_not_rewrite_reports(client, samples):
    _pkg, reports = four_quarters(client, samples)
    ids = [reports[q]["report_id"] for q in ("Q1", "Q2", "Q3", "Q4")]
    before = {rid: client.get(f"/reports/{rid}").json() for rid in ids}

    r = post_dataset(client, ids)
    assert r.status_code == 201, r.text

    for rid in ids:
        after = client.get(f"/reports/{rid}").json()
        assert after == before[rid], "dataset creation must not rewrite reports"
        inst = after["instance_id"]
        history = client.get(f"/instances/{inst}/reports").json()["history"]
        assert len(history) == 1  # no new reports were filed as a side effect


# ------------------------------------------------------------------ rejections
def test_dataset_requires_exactly_four_distinct_reports(client, samples):
    _pkg, reports = four_quarters(client, samples)
    ids = [reports[q]["report_id"] for q in ("Q1", "Q2", "Q3", "Q4")]

    assert post_dataset(client, ids[:3]).status_code == 422
    assert post_dataset(client, ids + [ids[0]]).status_code == 422
    r = post_dataset(client, [ids[0], ids[0], ids[1], ids[2]])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_report_set"

    r = post_dataset(client, ["rpt_missing"] + ids[:3])
    assert r.status_code == 404

    r = client.get("/datasets/ds_missing")
    assert r.status_code == 404


def test_dataset_requires_feasible_reports(client, samples):
    pkg = create_package(client, samples)
    reports = {q: feasible_quarter(client, pkg["package_id"], q)
               for q in ("Q1", "Q2", "Q3")}
    bad = create_instance(client, pkg["package_id"], samples["infeasible"])
    r = check(client, bad["instance_id"], 1, 1)
    assert r.json()["report"]["status"] == "infeasible"

    r = post_dataset(client, [reports[q]["report_id"] for q in ("Q1", "Q2", "Q3")]
                     + [r.json()["report"]["report_id"]])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "report_not_feasible"
    assert client.get("/datasets").json() == []  # no partial dataset left behind


def test_dataset_rejects_entity_and_currency_mismatch(client, samples):
    pkg = create_package(client, samples)
    reports = {q: feasible_quarter(client, pkg["package_id"], q) for q in ENDS}
    other = feasible_quarter(client, pkg["package_id"], "Q4", entity="E-002")
    r = post_dataset(client, [reports["Q1"]["report_id"], reports["Q2"]["report_id"],
                              reports["Q3"]["report_id"], other["report_id"]])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_mismatch"

    other = feasible_quarter(client, pkg["package_id"], "Q4", currency="EUR")
    r = post_dataset(client, [reports["Q1"]["report_id"], reports["Q2"]["report_id"],
                              reports["Q3"]["report_id"], other["report_id"]])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_mismatch"


def test_dataset_rejects_package_revision_mismatch(client, samples):
    pkg = create_package(client, samples)
    pid = pkg["package_id"]
    reports = {q: feasible_quarter(client, pid, q) for q in ("Q1", "Q2")}
    # revise the package, then file Q3/Q4 against revision 2
    r = client.post(
        f"/packages/{pid}/revisions",
        headers={"Expected-Revision": "1"},
        files={"xsd": ("acct.xsd", samples["xsd"], "application/xml"),
               "calculation": ("acct-calc.xml", samples["calc"], "application/xml")},
    )
    assert r.status_code == 200
    for q in ("Q3", "Q4"):
        reports[q] = feasible_quarter(client, pid, q, pkg_rev=2)

    r = post_dataset(client, [reports[q]["report_id"] for q in ENDS])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "source_mismatch"


def test_dataset_rejects_bad_periods(client, samples):
    pkg = create_package(client, samples)
    pid = pkg["package_id"]
    reports = {q: feasible_quarter(client, pid, q) for q in ENDS}
    base = [reports[q]["report_id"] for q in ENDS]

    # instant period instead of a Jan-1-to-quarter-end duration
    inst = create_instance(client, pid, ytd_instance(2026, "03-31", YTD["Q1"], instant=True))
    r = check(client, inst["instance_id"], 1, 1)
    assert r.json()["report"]["status"] == "feasible"
    r = post_dataset(client, [r.json()["report"]["report_id"]] + base[1:])
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_period"

    # duration not starting on Jan 1
    inst = create_instance(client, pid, ytd_instance(2026, "06-30", YTD["Q2"], start="04-01"))
    r = check(client, inst["instance_id"], 1, 1)
    r = post_dataset(client, [base[0], r.json()["report"]["report_id"]] + base[2:])
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_period"

    # quarter end that is not a quarter end
    inst = create_instance(client, pid, ytd_instance(2026, "05-31", YTD["Q2"]))
    r = check(client, inst["instance_id"], 1, 1)
    r = post_dataset(client, [base[0], r.json()["report"]["report_id"]] + base[2:])
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_period"

    # two reports for the same quarter (Q4 missing)
    inst = create_instance(client, pid, ytd_instance(2026, "03-31", YTD["Q1"]))
    r = check(client, inst["instance_id"], 1, 1)
    r = post_dataset(client, [base[0], r.json()["report"]["report_id"], base[1], base[2]])
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_period"

    # different calendar years
    inst = create_instance(client, pid, ytd_instance(2025, "12-31", YTD["Q4"]))
    r = check(client, inst["instance_id"], 1, 1)
    r = post_dataset(client, base[:3] + [r.json()["report"]["report_id"]])
    assert r.status_code == 422 and r.json()["error"]["code"] == "bad_period"


def test_dataset_rejects_revision_changes_during_generation(client, samples):
    pkg = create_package(client, samples)
    pid = pkg["package_id"]
    reports = {q: feasible_quarter(client, pid, q) for q in ENDS}
    ids = [reports[q]["report_id"] for q in ENDS]

    # instance corrected after its report: revision moved -> 412
    q2_inst = reports["Q2"]["instance_id"]
    r = client.post(
        f"/instances/{q2_inst}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", ytd_instance(2026, "06-30", YTD["Q2"]),
                            "application/xml")},
    )
    assert r.status_code == 200
    r = post_dataset(client, ids)
    assert r.status_code == 412
    assert r.json()["error"]["code"] == "stale_revision"
    assert client.get("/datasets").json() == []  # failure leaves no dataset

    # package revised after the reports: revision moved -> 412
    pkg2 = create_package(client, samples, name="acct2")
    reports2 = {q: feasible_quarter(client, pkg2["package_id"], q) for q in ENDS}
    r = client.post(
        f"/packages/{pkg2['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={"xsd": ("acct.xsd", samples["xsd"], "application/xml"),
               "calculation": ("acct-calc.xml", samples["calc"], "application/xml")},
    )
    assert r.status_code == 200
    r = post_dataset(client, [reports2[q]["report_id"] for q in ENDS])
    assert r.status_code == 412

    # a superseded report (re-checked at the same revisions) is also rejected
    pkg3 = create_package(client, samples, name="acct3")
    reports3 = {q: feasible_quarter(client, pkg3["package_id"], q) for q in ENDS}
    q1 = reports3["Q1"]
    r = check(client, q1["instance_id"], 1, 1)  # supersedes the first Q1 report
    assert r.status_code == 200
    replacement_id = r.json()["report"]["report_id"]
    r = post_dataset(client, [q1["report_id"]]
                     + [reports3[q]["report_id"] for q in ("Q2", "Q3", "Q4")])
    assert r.status_code == 412
    # ...but the fresh replacement report works
    r = post_dataset(client, [replacement_id]
                     + [reports3[q]["report_id"] for q in ("Q2", "Q3", "Q4")])
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ currency of results
def test_dataset_current_flag_after_source_correction(client, samples):
    pkg = create_package(client, samples)
    pid = pkg["package_id"]
    reports = {q: feasible_quarter(client, pid, q) for q in ENDS}
    ds = post_dataset(client, [reports[q]["report_id"] for q in ENDS]).json()
    assert ds["current"] is True

    # source correction: Q2 instance revised and re-checked -> old report superseded
    q2_inst = reports["Q2"]["instance_id"]
    client.post(
        f"/instances/{q2_inst}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", ytd_instance(2026, "06-30", YTD["Q2"]),
                            "application/xml")},
    )
    r = check(client, q2_inst, 2, 1)
    assert r.status_code == 200

    # history remains readable but is no longer the current result
    got = client.get(f"/datasets/{ds['dataset_id']}")
    assert got.status_code == 200
    assert got.json()["current"] is False
    assert got.json()["facts"] == ds["facts"]  # frozen computation
    assert client.get("/datasets?current_only=true").json() == []
    listed = {d["dataset_id"]: d["current"] for d in client.get("/datasets").json()}
    assert listed[ds["dataset_id"]] is False


def test_dataset_survives_restart(client, samples):
    from app import db

    pkg = create_package(client, samples)
    reports = {q: feasible_quarter(client, pkg["package_id"], q) for q in ENDS}
    ds = post_dataset(client, [reports[q]["report_id"] for q in ENDS]).json()

    db.reset_conn()
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c2:
        r = c2.get(f"/datasets/{ds['dataset_id']}")
        assert r.status_code == 200
        assert r.json()["current"] is True
        assert len(r.json()["facts"]) == 10
        assert len(c2.get("/datasets").json()) == 1
