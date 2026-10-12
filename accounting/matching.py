"""Discover payment/order groups without changing financial records."""

from datetime import datetime
from itertools import combinations
import json


WINDOW_SECONDS = 120
MAX_NEARBY_ORDERS = 14


def payment_provider(payload):
    fields = dict(zip(payload.get("headers", []), payload.get("values", [])))
    method = fields.get("支付方式", "")
    providers = [provider for provider, token in (("alipay", "支付宝"), ("wechat", "微信"), ("icbc", "工商银行"))
                 if token in method]
    return providers[0] if len(providers) == 1 else None


def user_edited_fields(connection, bill_id):
    fields = set()
    for row in connection.execute("""
        SELECT u.before_json,u.after_json FROM updates u JOIN change_batches b ON b.id=u.batch_id
        WHERE u.entity_table='bills' AND u.entity_id=? AND u.operation='update' AND b.actor_type='user'
    """, (bill_id,)):
        before, after = json.loads(row["before_json"]), json.loads(row["after_json"])
        fields.update(key for key in after if before.get(key) != after[key]
                      and key not in {"revision", "updated_at", "category_path"})
    return fields


class MeituanMatcher:
    def __init__(self, connection):
        self.rows = {}
        for row in connection.execute("""
            SELECT b.*,a.provider,r.id raw_record_id,r.description raw_description,r.counterparty raw_counterparty,
                   r.raw_payload_json,
                   EXISTS(SELECT 1 FROM bill_sources x JOIN raw_records y ON y.id=x.raw_record_id
                          JOIN accounts z ON z.id=y.account_id WHERE x.bill_id=b.id AND z.provider='meituan') has_order
            FROM bills b JOIN bill_sources s ON s.bill_id=b.id AND s.is_primary=1
            JOIN raw_records r ON r.id=s.raw_record_id JOIN accounts a ON a.id=r.account_id
            WHERE b.deleted_at IS NULL AND b.inclusion_status='included' AND b.kind IN ('pending','expense')
              AND b.direction='outflow' AND b.payment_status='settled' AND b.amount_cents>0
              AND b.time_precision IN ('second','minute')
              AND COALESCE(r.original_status,'') NOT IN ('交易关闭','已关闭','已取消')
            ORDER BY b.id
        """):
            item = dict(row)
            item["time"] = datetime.fromisoformat(item["paid_at"])
            item["payment_provider"] = payment_provider(json.loads(item["raw_payload_json"]))
            self.rows[item["id"]] = item
        self.orders = [r for r in self.rows.values() if r["provider"] == "meituan"]
        self.payments = {r["id"]: r for r in self.rows.values()
                         if r["provider"] in {"alipay", "wechat", "icbc"} and not r["has_order"]
                         and (r["provider"] == "icbc" or "美团" in (r["raw_counterparty"] or "") + (r["raw_description"] or ""))}
        self.cache = {}

    @staticmethod
    def compatible(payment, order):
        return (order["payment_provider"] == payment["provider"] and order["currency"] == payment["currency"]
                and abs((payment["time"] - order["time"]).total_seconds()) <= WINDOW_SECONDS)

    def groups(self, payment):
        if payment["id"] in self.cache:
            return self.cache[payment["id"]]
        orders = [r for r in self.orders if self.compatible(payment, r) and r["amount_cents"] <= payment["amount_cents"]]
        if len(orders) > MAX_NEARBY_ORDERS:
            result = ([], True)
        else:
            groups = []
            for size in range(1, len(orders) + 1):
                for group in combinations(orders, size):
                    if sum(r["amount_cents"] for r in group) == payment["amount_cents"]:
                        groups.append(tuple(r["id"] for r in group))
                        if len(groups) > 1:
                            break
                if len(groups) > 1:
                    break
            result = (groups, False)
        self.cache[payment["id"]] = result
        return result

    def match(self, payment_id):
        payment = self.payments.get(payment_id)
        if payment is None:
            return {"status": "unmatched", "payment_bill_id": payment_id, "reason": "Not an unlinked Meituan payment"}
        groups, limited = self.groups(payment)
        result = {"payment_bill_id": payment_id, "candidate_order_groups": [list(group) for group in groups]}
        if limited:
            return {**result, "status": "blocked", "reason": "Too many nearby orders; do not select an arbitrary subset"}
        if len(groups) != 1:
            return {**result, "status": "ambiguous" if groups else "unmatched", "reason": "Expected one exact actual-paid group"}
        order_ids = set(groups[0])
        for other in self.payments.values():
            if other["id"] == payment_id or not any(self.compatible(other, self.rows[identifier]) for identifier in order_ids):
                continue
            alternatives, limited = self.groups(other)
            if limited or any(order_ids.intersection(group) for group in alternatives):
                return {**result, "status": "ambiguous", "reason": "An order can also belong to another payment"}
        orders = [self.rows[identifier] for identifier in groups[0]]
        return {**result, "status": "unique", "order_bill_ids": list(groups[0]),
                "amount_cents": payment["amount_cents"], "order_total_cents": sum(r["amount_cents"] for r in orders),
                "currency": payment["currency"], "provider": payment["provider"],
                "window_seconds": WINDOW_SECONDS,
                "sources": [{"bill_id": r["id"], "raw_record_id": r["raw_record_id"], "amount_cents": r["amount_cents"],
                             "paid_at": r["paid_at"], "time_precision": r["time_precision"]} for r in [payment, *orders]]}
