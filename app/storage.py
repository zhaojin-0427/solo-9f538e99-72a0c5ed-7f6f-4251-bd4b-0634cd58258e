"""Service / persistence operations with optimistic revision checks."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone

from . import db
from .errors import BadPayload, NotFound, StaleRevision
from .instance import parse_instance
from .solver import check_facts
from .taxonomy import parse_package


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------- packages
def create_package(name: str, xsd: bytes, calc: bytes) -> dict:
    info = parse_package(xsd, calc)
    conn = db.get_conn()
    pid = _new_id("pkg")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO packages(id, name, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (pid, name, now, now),
        )
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, 1, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
    return get_package(pid)


def revise_package(pid: str, expected_revision: int, xsd: bytes, calc: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT latest_rev FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    if row["latest_rev"] != expected_revision:
        raise StaleRevision(
            f"expected package revision {expected_revision}, server has {row['latest_rev']}"
        )
    info = parse_package(xsd, calc)
    new_rev = expected_revision + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
            "target_ns, prefix, concepts_json, equations_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (pid, new_rev, xsd, calc, info["target_ns"], info["prefix"],
             json.dumps(info["concepts"]), json.dumps(info["equations"]), now),
        )
        conn.execute(
            "UPDATE packages SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, pid),
        )
    return get_package(pid)


def _package_revision_row(pid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM package_revisions WHERE package_id=? AND revision=?",
        (pid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' revision {revision} not found")
    return dict(row)


def get_package(pid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise NotFound(f"package '{pid}' not found")
    rev = _package_revision_row(pid, row["latest_rev"])
    return {
        "package_id": row["id"],
        "name": row["name"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "package_id": pid,
            "revision": rev["revision"],
            "target_ns": rev["target_ns"],
            "prefix": rev["prefix"],
            "concepts": json.loads(rev["concepts_json"]),
            "equations": json.loads(rev["equations_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_packages() -> list[dict]:
    rows = db.get_conn().execute("SELECT id FROM packages ORDER BY created_at").fetchall()
    return [get_package(r["id"]) for r in rows]


# ---------------------------------------------------------------- instances
def create_instance(pid: str, raw: bytes) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM packages WHERE id=?", (pid,)).fetchone() is None:
        raise NotFound(f"package '{pid}' not found")
    pkg = get_package(pid)
    info = parse_instance(raw, pkg["latest"])
    iid = _new_id("inst")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instances(id, package_id, latest_rev, created_at, updated_at) "
            "VALUES (?,?,1,?,?)",
            (iid, pid, now, now),
        )
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, 1, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
    return get_instance(iid)


def revise_instance(iid: str, expected_package_revision: int, raw: bytes) -> dict:
    conn = db.get_conn()
    row = conn.execute(
        "SELECT package_id, latest_rev FROM instances WHERE id=?", (iid,)
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    pkg = get_package(row["package_id"])
    # 受理时校验两侧所见修订：实例提交必须针对当前 package 修订。
    if pkg["latest_revision"] != expected_package_revision:
        raise StaleRevision(
            f"expected package revision {expected_package_revision}, "
            f"server has {pkg['latest_revision']}"
        )
    info = parse_instance(raw, pkg["latest"])
    new_rev = row["latest_rev"] + 1
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
            "period, currency, facts_json, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (iid, new_rev, raw, info["entity"], info["period"], info["currency"],
             json.dumps(info["facts"]), now),
        )
        conn.execute(
            "UPDATE instances SET latest_rev=?, updated_at=? WHERE id=?",
            (new_rev, now, iid),
        )
    return get_instance(iid)


def _instance_revision_row(iid: str, revision: int) -> dict:
    row = db.get_conn().execute(
        "SELECT * FROM instance_revisions WHERE instance_id=? AND revision=?",
        (iid, revision),
    ).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' revision {revision} not found")
    return dict(row)


def get_instance(iid: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if row is None:
        raise NotFound(f"instance '{iid}' not found")
    rev = _instance_revision_row(iid, row["latest_rev"])
    return {
        "instance_id": row["id"],
        "package_id": row["package_id"],
        "latest_revision": row["latest_rev"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "latest": {
            "instance_id": iid,
            "package_id": row["package_id"],
            "revision": rev["revision"],
            "entity": rev["entity"],
            "period": rev["period"],
            "currency": rev["currency"],
            "facts": json.loads(rev["facts_json"]),
            "created_at": rev["created_at"],
        },
    }


def list_instances(pid: str | None = None) -> list[dict]:
    conn = db.get_conn()
    if pid:
        rows = conn.execute(
            "SELECT id FROM instances WHERE package_id=? ORDER BY created_at", (pid,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT id FROM instances ORDER BY created_at").fetchall()
    return [get_instance(r["id"]) for r in rows]


# ---------------------------------------------------------------- reports
def _report_out(row: dict) -> dict:
    return {
        "report_id": row["id"],
        "instance_id": row["instance_id"],
        "package_id": row["package_id"],
        "instance_revision": row["instance_revision"],
        "package_revision": row["package_revision"],
        "status": row["status"],
        "is_correction": bool(row["is_correction"]),
        "superseded": bool(row["superseded"]),
        "late": bool(row["late"]),
        "witness": json.loads(row["witness_json"]) if row["witness_json"] else None,
        "conflicts": json.loads(row["conflicts_json"]) if row["conflicts_json"] else None,
        "missing_facts": json.loads(row["missing_json"]) if row["missing_json"] else None,
        "detail": json.loads(row["detail_json"]),
        "created_at": row["created_at"],
        "has_instance_snapshot": bool(row["instance_snapshot"]),
        "has_xsd_snapshot": bool(row["xsd_snapshot"]),
        "has_calc_snapshot": bool(row["calc_snapshot"]),
    }


def run_check(iid: str, expected_instance_revision: int,
              expected_package_revision: int, late: bool = False) -> dict:
    conn = db.get_conn()
    inst_row = conn.execute("SELECT * FROM instances WHERE id=?", (iid,)).fetchone()
    if inst_row is None:
        raise NotFound(f"instance '{iid}' not found")

    pkg = get_package(inst_row["package_id"])

    # 受理时校验两侧所见修订：
    # - 客户端所见修订领先于服务器 → 过期拒绝（412）
    # - 落后于服务器：默认仍拒绝；仅当显式声明为迟到补报时受理，仅存历史。
    if expected_instance_revision > inst_row["latest_rev"]:
        raise StaleRevision(
            f"expected instance revision {expected_instance_revision} is ahead of "
            f"server latest {inst_row['latest_rev']}"
        )
    if expected_package_revision > pkg["latest_revision"]:
        raise StaleRevision(
            f"expected package revision {expected_package_revision} is ahead of "
            f"server latest {pkg['latest_revision']}"
        )
    behind = (expected_instance_revision < inst_row["latest_rev"]
              or expected_package_revision < pkg["latest_revision"])
    if behind and not late:
        raise StaleRevision(
            f"revisions ({expected_instance_revision},"
            f"{expected_package_revision}) are behind server latest "
            f"({inst_row['latest_rev']},{pkg['latest_revision']}); "
            f"re-check with late=true to file a late historical report"
        )

    i_rev = _instance_revision_row(iid, expected_instance_revision)
    p_rev = _package_revision_row(inst_row["package_id"], expected_package_revision)

    # 迟到报告：依据的修订已落后于两侧最新组合，仅存历史，不取代当前报告。
    is_late = behind
    facts = json.loads(i_rev["facts_json"])
    equations = json.loads(p_rev["equations_json"])

    result = check_facts(facts, equations)

    now = _now()
    report_id = _new_id("rpt")
    prior = conn.execute(
        "SELECT id FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    is_correction = len(prior) > 0

    detail = {
        "entity": i_rev["entity"],
        "period": i_rev["period"],
        "currency": i_rev["currency"],
        "fact_count": len(facts),
        "equation_count": len(equations),
        "solver": "scipy.optimize.linprog(highs)",
    }
    payload = {
        "id": report_id,
        "instance_id": iid,
        "package_id": inst_row["package_id"],
        "instance_revision": i_rev["revision"],
        "package_revision": p_rev["revision"],
        "status": result["status"],
        "is_correction": 1 if is_correction else 0,
        "late": 1 if is_late else 0,
        "witness_json": json.dumps(result["witness"]) if "witness" in result else None,
        "conflicts_json": json.dumps(result["conflicts"]) if "conflicts" in result else None,
        "missing_json": json.dumps(result["missing_facts"]) if "missing_facts" in result else None,
        "detail_json": json.dumps(detail),
        "instance_snapshot": i_rev["instance_raw"],
        "xsd_snapshot": p_rev["xsd_raw"],
        "calc_snapshot": p_rev["calc_raw"],
        "created_at": now,
    }

    superseded_ids: list[str] = []
    with conn:
        conn.execute(
            "INSERT INTO reports(id, instance_id, package_id, instance_revision, "
            "package_revision, status, is_correction, superseded, late, witness_json, "
            "conflicts_json, missing_json, detail_json, instance_snapshot, xsd_snapshot, "
            "calc_snapshot, created_at) VALUES (:id,:instance_id,:package_id,"
            ":instance_revision,:package_revision,:status,:is_correction,0,:late,"
            ":witness_json,:conflicts_json,:missing_json,:detail_json,:instance_snapshot,"
            ":xsd_snapshot,:calc_snapshot,:created_at)",
            payload,
        )
        if not is_late:
            old = conn.execute(
                "SELECT id FROM reports WHERE instance_id=? AND id!=? AND superseded=0",
                (iid, report_id),
            ).fetchall()
            superseded_ids = [r["id"] for r in old]
            conn.execute(
                "UPDATE reports SET superseded=1 WHERE instance_id=? AND id!=?",
                (iid, report_id),
            )

    out = _report_out(dict(conn.execute(
        "SELECT * FROM reports WHERE id=?", (report_id,)
    ).fetchone()))
    return {"report": out, "superseded_report_ids": superseded_ids}


def get_report(rid: str) -> dict:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    return _report_out(dict(row))


def get_report_snapshot(rid: str, kind: str) -> tuple[bytes, str]:
    row = db.get_conn().execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
    if row is None:
        raise NotFound(f"report '{rid}' not found")
    column = {"instance": "instance_snapshot", "xsd": "xsd_snapshot",
              "calc": "calc_snapshot"}[kind]
    media = {"instance": "application/xml", "xsd": "application/xml",
             "calc": "application/xml"}[kind]
    return bytes(row[column]), media


def list_reports(iid: str) -> dict:
    conn = db.get_conn()
    if conn.execute("SELECT 1 FROM instances WHERE id=?", (iid,)).fetchone() is None:
        raise NotFound(f"instance '{iid}' not found")
    rows = conn.execute(
        "SELECT * FROM reports WHERE instance_id=? ORDER BY created_at, rowid", (iid,)
    ).fetchall()
    history = [_report_out(dict(r)) for r in rows]
    # 迟到报告仅存历史，永远不作为当前报告。
    current = next(
        (r for r in reversed(history) if not r["superseded"] and not r["late"]), None
    )
    return {"instance_id": iid, "current": current, "history": history}


# ---------------------------------------------------------------- datasets
QUARTERS = ("Q1", "Q2", "Q3", "Q4")
_QUARTER_ENDS = {(3, 31): "Q1", (6, 30): "Q2", (9, 30): "Q3", (12, 31): "Q4"}


def _ytd_quarter(period: str) -> tuple[int, str]:
    """Map a ``duration=YYYY-01-01/YYYY-MM-DD`` year-to-date period to (year, quarter)."""
    prefix = "duration="
    if not period.startswith(prefix):
        raise BadPayload(
            f"report period '{period}' is not a Jan-1-to-quarter-end duration",
            code="bad_period",
        )
    try:
        start_s, end_s = period[len(prefix):].split("/", 1)
        start = date.fromisoformat(start_s)
        end = date.fromisoformat(end_s)
    except ValueError:
        raise BadPayload(
            f"report period '{period}' is not a valid duration", code="bad_period"
        )
    quarter = _QUARTER_ENDS.get((end.month, end.day))
    if quarter is None or (start.month, start.day) != (1, 1) or start.year != end.year:
        raise BadPayload(
            f"report period '{period}' must run from Jan 1 to a quarter end "
            "of one calendar year",
            code="bad_period",
        )
    return end.year, quarter


def create_dataset(report_ids: list[str]) -> dict:
    """Publish a quarterly dataset from four feasible year-to-date reports.

    All validation happens before the single INSERT, so a rejected publish
    leaves no partial dataset and the source reports are never rewritten.
    """
    if len(report_ids) != 4:
        raise BadPayload(
            f"exactly four report IDs are required, got {len(report_ids)}",
            code="bad_report_set",
        )
    if len(set(report_ids)) != 4:
        raise BadPayload("the four report IDs must be distinct", code="bad_report_set")

    conn = db.get_conn()
    reports: list[dict] = []
    for rid in report_ids:
        row = conn.execute("SELECT * FROM reports WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise NotFound(f"report '{rid}' not found")
        reports.append(dict(row))

    not_feasible = [r["id"] for r in reports if r["status"] != "feasible"]
    if not_feasible:
        raise BadPayload(
            "only feasible reports can form a quarterly dataset; not feasible: "
            + ", ".join(not_feasible),
            code="report_not_feasible",
        )

    details = [json.loads(r["detail_json"]) for r in reports]
    if (len({r["package_id"] for r in reports}) != 1
            or len({r["package_revision"] for r in reports}) != 1):
        raise BadPayload(
            "the four reports must share one relationship package and revision",
            code="source_mismatch",
        )
    if len({d["entity"] for d in details}) != 1:
        raise BadPayload(
            "the four reports must share one entity", code="source_mismatch"
        )
    if len({d["currency"] for d in details}) != 1:
        raise BadPayload(
            "the four reports must share one currency", code="source_mismatch"
        )

    # 期间：同一自然年 1 月 1 日至四个季末，每季度恰好一份。
    by_quarter: dict[str, tuple[dict, dict]] = {}
    years: set[int] = set()
    for r, d in zip(reports, details):
        year, quarter = _ytd_quarter(d["period"])
        if quarter in by_quarter:
            raise BadPayload(
                f"two reports cover {quarter}; submit exactly one report per quarter",
                code="bad_period",
            )
        by_quarter[quarter] = (r, d)
        years.add(year)
    if len(years) != 1:
        raise BadPayload(
            "the four reports must belong to one calendar year", code="bad_period"
        )
    missing_q = [q for q in QUARTERS if q not in by_quarter]
    if missing_q:
        raise BadPayload(
            "missing year-to-date reports for quarters: " + ", ".join(missing_q),
            code="bad_period",
        )
    year = years.pop()
    package_id = reports[0]["package_id"]
    package_revision = reports[0]["package_revision"]

    # 受理时校验：四份报告必须仍是当前报告，且生成期间两侧修订未变化。
    for r in reports:
        if r["superseded"] or r["late"]:
            raise StaleRevision(
                f"report '{r['id']}' is no longer a current report; "
                "rebuild the dataset from current reports"
            )
        inst = conn.execute(
            "SELECT latest_rev FROM instances WHERE id=?", (r["instance_id"],)
        ).fetchone()
        if inst is None or inst["latest_rev"] != r["instance_revision"]:
            raise StaleRevision(
                f"instance '{r['instance_id']}' changed during dataset generation "
                f"(report '{r['id']}' used revision {r['instance_revision']})"
            )
    pkg = conn.execute(
        "SELECT latest_rev FROM packages WHERE id=?", (package_id,)
    ).fetchone()
    if pkg is None or pkg["latest_rev"] != package_revision:
        raise StaleRevision(
            f"package '{package_id}' changed during dataset generation "
            f"(reports used revision {package_revision})"
        )

    # 按概念生成季度事实：Q1 累计额；Q2..Q4 相邻累计差额（区间算术）。
    witnesses: dict[str, dict[str, dict]] = {}
    for quarter, (r, _d) in by_quarter.items():
        witness = json.loads(r["witness_json"]) if r["witness_json"] else []
        witnesses[quarter] = {w["concept"]: w for w in witness}

    concepts = sorted({c for w in witnesses.values() for c in w})
    facts: list[dict] = []
    for concept in concepts:
        missing_in = [q for q in QUARTERS if concept not in witnesses[q]]
        if missing_in:
            # 任一来源缺事实：整概念标待补，不补零。
            facts.append({
                "concept": concept,
                "status": "pending",
                "missing_in": missing_in,
                "quarters": [],
            })
            continue
        quarters: list[dict] = []
        prev: dict | None = None
        for q in QUARTERS:
            w = witnesses[q][concept]
            if prev is None:  # Q1：年初累计额
                reported = w["reported_value"]
                true = w["true_value"]
                lower, upper = w["lower"], w["upper"]
            else:  # Q2..Q4：本期减上期的相邻累计差额
                reported = w["reported_value"] - prev["reported_value"]
                true = w["true_value"] - prev["true_value"]
                lower = w["lower"] - prev["upper"]
                upper = w["upper"] - prev["lower"]
            quarters.append({
                "quarter": q,
                "reported_value": round(reported, 10),
                "true_value": round(true, 10),
                "lower": round(lower, 10),
                "upper": round(upper, 10),
            })
            prev = w
        facts.append({"concept": concept, "status": "ok", "quarters": quarters})

    sources = [
        {
            "quarter": q,
            "report_id": by_quarter[q][0]["id"],
            "instance_id": by_quarter[q][0]["instance_id"],
            "instance_revision": by_quarter[q][0]["instance_revision"],
            "period": by_quarter[q][1]["period"],
        }
        for q in QUARTERS
    ]
    detail = {
        "entity": details[0]["entity"],
        "currency": details[0]["currency"],
        "year": year,
        "package_id": package_id,
        "package_revision": package_revision,
        "concept_count": len(facts),
        "pending_concept_count": sum(1 for f in facts if f["status"] == "pending"),
        "rule": (
            "Q1 = year-to-date cumulative amount; Q2..Q4 = adjacent YTD "
            "difference with lower=current.lower-previous.upper and "
            "upper=current.upper-previous.lower; concepts missing from any "
            "source are marked pending, never zero-filled"
        ),
    }
    dataset_id = _new_id("ds")
    now = _now()
    with conn:
        conn.execute(
            "INSERT INTO datasets(id, package_id, package_revision, entity, currency, "
            "year, sources_json, facts_json, detail_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (dataset_id, package_id, package_revision, details[0]["entity"],
             details[0]["currency"], year, json.dumps(sources), json.dumps(facts),
             json.dumps(detail), now),
        )
    return get_dataset(dataset_id)


def _dataset_out(conn, row: dict) -> dict:
    sources = json.loads(row["sources_json"])
    # 当前结果 ⟺ 四份来源报告都未被取代；来源更正后历史仍可读但不再是当前结果。
    current = True
    for s in sources:
        r = conn.execute(
            "SELECT superseded, late FROM reports WHERE id=?", (s["report_id"],)
        ).fetchone()
        if r is None or r["superseded"] or r["late"]:
            current = False
            break
    return {
        "dataset_id": row["id"],
        "package_id": row["package_id"],
        "package_revision": row["package_revision"],
        "entity": row["entity"],
        "currency": row["currency"],
        "year": row["year"],
        "current": current,
        "sources": sources,
        "facts": json.loads(row["facts_json"]),
        "detail": json.loads(row["detail_json"]),
        "created_at": row["created_at"],
    }


def get_dataset(dataset_id: str) -> dict:
    conn = db.get_conn()
    row = conn.execute("SELECT * FROM datasets WHERE id=?", (dataset_id,)).fetchone()
    if row is None:
        raise NotFound(f"dataset '{dataset_id}' not found")
    return _dataset_out(conn, dict(row))


def list_datasets(current_only: bool = False) -> list[dict]:
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT * FROM datasets ORDER BY created_at, rowid"
    ).fetchall()
    out = [_dataset_out(conn, dict(r)) for r in rows]
    if current_only:
        out = [d for d in out if d["current"]]
    return out
