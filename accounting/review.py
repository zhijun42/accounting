"""Apply explicitly authored AI decisions, never infer classifications in the writer."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4
import argparse
import json

from .database import Database, json_text, now
from .ingest import insert
from .matching import MeituanMatcher, user_edited_fields
from .web import backup_database


def review_policy(connection):
    database_path = next((r["file"] for r in connection.execute("PRAGMA database_list") if r["name"] == "main"), "")
    path = Path(database_path).parent / "review-policy.json"
    if not database_path or not path.is_file():
        return {}
    policy = json.loads(path.read_text())
    if not isinstance(policy, dict) or policy.get("version") != 1:
        raise ValueError("Unsupported local review policy")
    return policy


def snapshot(connection, start, end, limit, bill_ids=None):
    if not Database.valid_usage_date(start) or not Database.valid_usage_date(end) or start > end:
        raise ValueError("Invalid date range")
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError("Review at most 200 rows")
    selection, values = "", [start, end]
    if bill_ids is not None:
        if type(bill_ids) is not list or not bill_ids or len(bill_ids) > limit or len(set(bill_ids)) != len(bill_ids):
            raise ValueError("Select distinct bill IDs within the review limit")
        selection = " AND id IN (" + ",".join("?" for _ in bill_ids) + ")"
        values.extend(bill_ids)
    rows = connection.execute("""
        SELECT * FROM bills WHERE deleted_at IS NULL AND date(paid_at,'+8 hours') BETWEEN ? AND ?
    """ + selection + """
        ORDER BY paid_at DESC,id DESC LIMIT ?
    """, [*values, limit]).fetchall()
    if bill_ids is not None and len(rows) != len(bill_ids):
        raise ValueError("Selected bills must be active and inside the date range")
    items = []
    for index, row in enumerate(rows, 1):
        sources = [dict(r) for r in connection.execute("""
            SELECT s.*,r.description,r.counterparty,r.original_category,r.original_status,
                   r.transaction_id,r.merchant_order_id,r.raw_payload_json,r.time_precision,a.provider
            FROM bill_sources s JOIN raw_records r ON r.id=s.raw_record_id
            JOIN accounts a ON a.id=r.account_id WHERE s.bill_id=? ORDER BY s.is_primary DESC,s.id
        """, (row["id"],))]
        questions = [dict(r) for r in connection.execute("SELECT * FROM review_items WHERE bill_id=? AND status='open' ORDER BY id", (row["id"],))]
        items.append({"index": index, "bill": dict(row), "sources": sources, "review_items": questions})
    matcher = MeituanMatcher(connection)
    matches = [matcher.match(item["bill"]["id"]) for item in items if item["bill"]["id"] in matcher.payments]
    result = {"start": start, "end": end, "limit": limit, "items": items,
              "meituan_matches": matches, "review_policy": review_policy(connection)}
    if bill_ids is not None:
        result["bill_ids"] = bill_ids
    return result


def add_item(db, bill_id, topic, reason, proposal, evidence, batch_id, resolved=False):
    stamp = now()
    revision = db.connection.execute("SELECT revision FROM bills WHERE id=?", (bill_id,)).fetchone()[0]
    insert(db.connection, "review_items", {
        "id": str(uuid4()), "bill_id": bill_id, "topic": topic,
        "status": "resolved" if resolved else "open", "reason": reason,
        "proposal_json": json_text(proposal), "evidence_json": json_text(evidence),
        "bill_revision": revision, "resolved_batch_id": batch_id if resolved else None,
        "created_at": stamp, "updated_at": stamp,
    })


def apply_review(db, sample, decisions):
    items = {item["index"]: item for item in sample["items"]}
    if len(items) != len(sample["items"]) or not items or len(items) > 200:
        raise ValueError("Invalid review sample")
    if type(decisions) is not list or len(decisions) != len(items):
        raise ValueError("Each selected row needs exactly one authored decision")
    by_index = {d["index"]: d for d in decisions}
    if set(by_index) != set(items) or len(by_index) != len(decisions):
        raise ValueError("Decision indices must exactly match the sample")
    allowed = {"index", "kind", "category", "reason", "action", "confidence", "merge_into", "rename_from", "matching_note", "payment_status", "resolve_matching"}
    kinds = {"pending", "expense", "income", "refund", "reimbursement", "internal_transfer", "debt_repayment", "investment"}
    for d in decisions:
        if type(d.get("index")) is not int:
            raise ValueError("Decision indices must be integers")
        if set(d) - allowed or not isinstance(d.get("reason"), str) or not d["reason"].strip():
            raise ValueError("Unexpected fields or missing decision reason")
        if d.get("confidence") not in {"high", "medium", "low"}:
            raise ValueError("Decision confidence is required")
        if d.get("action") not in {"retained", "corrected", "inferred", "non_expense", "uncertain", "merged"}:
            raise ValueError("Invalid classification action")
        if "merge_into" in d:
            target = d["merge_into"]
            if d["action"] != "merged" or d["confidence"] != "high" or target not in items or target == d["index"] or "merge_into" in by_index[target]:
                raise ValueError("Only explicit high-confidence, non-chained sample merges are allowed")
        elif d.get("kind") not in kinds or (d["kind"] != "expense" and d.get("category") is not None):
            raise ValueError("Invalid kind or consumption category")
        if d.get("kind") == "expense" and d.get("category") is None:
            if d["action"] != "uncertain":
                raise ValueError("A known expense needs a concrete category")
        if "rename_from" in d:
            source_index = d["rename_from"]
            if source_index not in items or by_index[source_index].get("merge_into") != d["index"]:
                raise ValueError("Description can only come from an explicitly merged Meituan order")
        if "payment_status" in d and d["payment_status"] != "voided":
            raise ValueError("Only source-proven cancellation may change payment status")
        if "resolve_matching" in d:
            identifiers = d["resolve_matching"]
            questions = {q["id"] for q in items[d["index"]]["review_items"] if q["topic"] == "matching"}
            if type(identifiers) is not list or any(type(i) is not str for i in identifiers) or len(set(identifiers)) != len(identifiers) or not set(identifiers) <= questions:
                raise ValueError("Resolve only explicitly selected matching questions on this bill")

    report = {"selected": len(items), "merged": 0, "classified_expenses": 0, "non_expenses": 0, "uncertain": 0, "tentative": 0, "renamed": 0, "voided": 0, "items": []}
    with db.batch("ai", "Codex · 逐笔账单核对", "按本人授权检查所选账单；不推断使用日期或个人承担金额",
                  [{"start": sample["start"], "end": sample["end"], "sample_bill_ids": [i["bill"]["id"] for i in items.values()],
                    "review_policy": sample.get("review_policy", {})}]) as batch_id:
        report["batch_id"] = batch_id
        current = snapshot(db.connection, sample["start"], sample["end"], sample["limit"], sample.get("bill_ids"))
        if current != sample:
            raise ValueError("The selected bills or sources changed; author a fresh review")
        if any(i["bill"]["classification_locked"] or i["bill"]["review_status"] != "pending" for i in items.values()):
            raise ValueError("Do not overwrite previously confirmed decisions")
        matcher = MeituanMatcher(db.connection)
        order_groups = {}
        for index, d in by_index.items():
            if "merge_into" in d and items[index]["sources"][0]["provider"] == "meituan":
                order_groups.setdefault(d["merge_into"], []).append(items[index]["bill"]["id"])
        group_evidence = {}
        for target_index, order_ids in order_groups.items():
            match = matcher.match(items[target_index]["bill"]["id"])
            if match["status"] != "unique" or set(match["order_bill_ids"]) != set(order_ids):
                raise ValueError("Meituan actual-paid group is not unique or complete")
            group_evidence[target_index] = match
        for index, item in items.items():
            d = by_index[index]
            evidence = {"decision": d, "sample_index": index, "bill_id": item["bill"]["id"],
                        "raw_record_ids": [s["raw_record_id"] for s in item["sources"]]}
            db.explain(d["reason"], evidence)
            db.connection.execute("""
                UPDATE review_items SET status='dismissed',resolved_batch_id=?,updated_at=?,revision=revision+1
                WHERE bill_id=? AND status='open' AND topic IN ('nature','category')
            """, (batch_id, now(), item["bill"]["id"]))
            for identifier in d.get("resolve_matching", []):
                db.connection.execute("""
                    UPDATE review_items SET status='resolved',resolved_batch_id=?,updated_at=?,revision=revision+1
                    WHERE id=? AND status='open'
                """, (batch_id, now(), identifier))
        for index, item in items.items():
            d = by_index[index]
            if "merge_into" not in d:
                continue
            target = items[d["merge_into"]]
            old, survivor = item["bill"], target["bill"]
            old_provider = item["sources"][0]["provider"]
            survivor_provider = target["sources"][0]["provider"]
            if old_provider not in {"meituan", "icbc"} or survivor_provider not in {"alipay", "wechat", "icbc"} or old_provider == survivor_provider:
                raise ValueError("Unsupported platform merge")
            if old_provider == "meituan":
                if any(s["provider"] != "meituan" for s in item["sources"]):
                    raise ValueError("Do not merge an already-linked payment as an order")
            else:
                if len(item["sources"]) != 1 or len(target["sources"]) != 1:
                    raise ValueError("Already-linked source groups require a separate review")
                if any(old[key] != survivor[key] for key in ("amount_cents", "currency", "direction", "payment_status")):
                    raise ValueError("Different amounts, currency, direction or status cannot be merged")
                if abs((datetime.fromisoformat(old["paid_at"]) - datetime.fromisoformat(survivor["paid_at"])).total_seconds()) > 120:
                    raise ValueError("Composite matches must be within two minutes")
                payload = json.loads(target["sources"][0]["raw_payload_json"])
                fields = dict(zip(payload.get("headers", []), payload.get("values", [])))
                if "工商银行" not in fields.get("支付方式", fields.get("收/付款方式", "")):
                    raise ValueError("Payment method does not support the proposed match")
                for a, b, provider in ((old, survivor, survivor_provider), (survivor, old, old_provider)):
                    candidates = db.connection.execute("""
                        SELECT id FROM bill_details WHERE deleted_at IS NULL AND source_provider=?
                        AND amount_cents=? AND currency=? AND direction=? AND payment_status=?
                        AND abs(unixepoch(paid_at)-unixepoch(?))<=120
                    """, (provider, a["amount_cents"], a["currency"], a["direction"], a["payment_status"], a["paid_at"])).fetchall()
                    if len(candidates) != 1 or candidates[0]["id"] != b["id"]:
                        raise ValueError("Composite match is not unique in both directions")
            if user_edited_fields(db.connection, old["id"]):
                raise ValueError("Do not discard personal edits while merging")
            if old["usage_date"] is not None or old["personal_base_cents"] is not None or old["notes"] is not None or old["inclusion_status"] != "included":
                raise ValueError("Do not discard personal edits while merging")
            if db.connection.execute("SELECT 1 FROM bill_relations WHERE origin_bill_id=? OR related_bill_id=?", (old["id"], old["id"])).fetchone():
                raise ValueError("Allocated bills require a separate merge workflow")
            evidence = {"decision": d, "surviving_bill_id": survivor["id"], "merged_bill_id": old["id"],
                        "raw_record_ids": [s["raw_record_id"] for s in item["sources"] + target["sources"]]}
            if old_provider == "meituan":
                evidence["payment_group"] = group_evidence[d["merge_into"]]
            db.explain(d["reason"], evidence)
            db.connection.execute("UPDATE bill_sources SET bill_id=?,is_primary=0,role=?,match_method='composite',evidence_json=? WHERE bill_id=?",
                                  (survivor["id"], "order" if old_provider == "meituan" else "funding", json_text(evidence), old["id"]))
            db.connection.execute("UPDATE bills SET deleted_at=?,merged_into_bill_id=?,updated_at=?,revision=revision+1 WHERE id=?",
                                  (now(), survivor["id"], now(), old["id"]))
            add_item(db, old["id"], "duplicate", d["reason"], {"merged_into_bill_id": survivor["id"]}, evidence, batch_id, True)
            report["merged"] += 1
        for index, item in items.items():
            d = by_index[index]
            old = item["bill"]
            if "merge_into" in d:
                report["items"].append({"index": index, "bill_id": old["id"], **d})
                continue
            category_id = None
            if d.get("category"):
                found = db.connection.execute("SELECT id FROM categories WHERE name=? AND parent_id IS NULL AND is_active=1", (d["category"],)).fetchone()
                if found is None:
                    raise ValueError("Use an existing explicit root category")
                category_id = found["id"]
            changes = {"kind": d["kind"], "category_id": category_id}
            if d["kind"] in {"income", "refund", "reimbursement"}:
                changes["direction"] = "inflow"
            elif d["kind"] == "debt_repayment":
                changes["direction"] = "outflow"
            if d.get("payment_status"):
                if item["sources"][0]["original_status"] not in {"交易关闭", "已关闭", "已取消"}:
                    raise ValueError("Cancellation must be explicitly present in the original source")
                changes["payment_status"] = "voided"
            evidence = {"decision": d, "sample_index": index, "bill_id": old["id"],
                        "raw_record_ids": [r[0] for r in db.connection.execute("SELECT raw_record_id FROM bill_sources WHERE bill_id=?", (old["id"],))]}
            if "rename_from" in d:
                source = items[d["rename_from"]]["sources"][0]
                if source["provider"] != "meituan" or not source["description"]:
                    raise ValueError("Use the actual Meituan order title, not an invented description")
                changes["description"] = source["description"]
            changes = {key: value for key, value in changes.items() if old[key] != value}
            if set(changes).intersection(user_edited_fields(db.connection, old["id"])):
                raise ValueError("Do not overwrite personal edits, even before confirmation")
            db.explain(d["reason"], evidence)
            if changes:
                assignments = ",".join(f"{key}=?" for key in changes)
                db.connection.execute(f"UPDATE bills SET {assignments},updated_at=?,revision=revision+1 WHERE id=?",
                                      [*changes.values(), now(), old["id"]])
            resolved = d["confidence"] == "high" and d["action"] != "uncertain"
            add_item(db, old["id"], "category" if d["kind"] == "expense" else "nature", d["reason"], d, evidence, batch_id, resolved)
            if d["kind"] == "expense":
                if category_id:
                    report["classified_expenses"] += 1
                add_item(db, old["id"], "personal_share", "分类已整理；本人承担金额尚未确认，本轮不推断", None, evidence, batch_id)
            elif d["kind"] != "pending":
                report["non_expenses"] += 1
            if d["action"] == "uncertain":
                report["uncertain"] += 1
            elif d["confidence"] != "high":
                report["tentative"] += 1
            report["voided"] += int(d.get("payment_status") == "voided")
            if d.get("matching_note"):
                add_item(db, old["id"], "matching", d["matching_note"], None, evidence, batch_id)
            report["renamed"] += int("description" in changes)
            report["items"].append({"index": index, "bill_id": old["id"], "description": changes.get("description", old["description"]), **d})
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("snapshot", "apply"))
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--sample", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--start", default="2026-04-01")
    parser.add_argument("--end", default="2026-09-30")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--bill-id", action="append")
    args = parser.parse_args()
    if args.command == "snapshot":
        db = Database(args.database, readonly=True)
        try:
            data = snapshot(db.connection, args.start, args.end, args.limit, args.bill_id)
            args.sample.parent.mkdir(parents=True, exist_ok=True)
            args.sample.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            print(json.dumps({"selected": len(data["items"]), "sample": str(args.sample)}, ensure_ascii=False))
        finally:
            db.close()
        return
    if args.plan is None or args.report is None:
        parser.error("apply requires --plan and --report")
    sample = json.loads(args.sample.read_text())
    decisions = json.loads(args.plan.read_text())
    backup = backup_database(args.database)
    db = Database(args.database)
    try:
        result = apply_review(db, sample, decisions)
        result["backup"] = str(backup)
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in result.items() if k != "items"}, ensure_ascii=False))
    finally:
        db.close()


if __name__ == "__main__":
    main()
