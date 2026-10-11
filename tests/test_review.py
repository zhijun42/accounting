from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from accounting.database import Database
from accounting.importers import Record, Statement, finalize
from accounting.ingest import import_statements
from accounting.review import apply_review, snapshot


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.seed()

    def seed(self, name="fictional", method="支付宝支付", amount=7300):
        self.db = Database(Path(self.directory.name) / f"{name}.sqlite3")
        base = Record("alipay", "fictional", {"headers": ["收/付款方式"], "values": ["余额"]},
                      {"line_start": 2}, "fictional-pay", None, "2026-09-20T04:00:30Z", "second",
                      7300, "outflow", "餐饮美食", "美团订单-fictional", "美团", "交易成功", "payment")
        order = replace(base, provider="meituan", transaction_id="fictional-order",
                        occurred_at="2026-09-20T04:00:00Z", precision="minute", category=None,
                        description="虚构攀岩馆", status=None,
                        amount_cents=amount, payload={"headers": ["支付方式"], "values": [method]})
        for record in (base, order):
            import_statements(self.db, [finalize(Statement(Path("fictional.csv"), "fake-hash", record.provider, [record], {}))])
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        self.plan = [
            {"index": 1, "kind": "expense", "category": "运动户外", "rename_from": 2, "action": "corrected", "confidence": "high", "reason": "虚构攀岩订单不是餐饮"},
            {"index": 2, "merge_into": 1, "action": "merged", "confidence": "high", "reason": "虚构同金额、同分钟、同支付通道且唯一对应"},
        ]

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_merge_and_rename_keep_sources_and_ai_audit(self):
        raw_before = [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")]
        report = apply_review(self.db, self.sample, self.plan)
        self.assertEqual((report["selected"], report["merged"], report["renamed"]), (2, 1, 1))
        active = self.db.connection.execute("SELECT * FROM bills WHERE deleted_at IS NULL").fetchone()
        self.assertEqual(active["description"], "虚构攀岩馆")
        self.assertIsNone(active["usage_date"])
        self.assertIsNone(active["personal_base_cents"])
        self.assertEqual(active["review_status"], "pending")
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM bill_sources WHERE bill_id=?", (active["id"],)).fetchone()[0], 2)
        self.assertEqual(raw_before, [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")])
        actor = self.db.connection.execute("SELECT actor_type FROM change_batches WHERE id=?", (report["batch_id"],)).fetchone()[0]
        self.assertEqual(actor, "ai")
        event = self.db.connection.execute("SELECT * FROM updates WHERE batch_id=? AND entity_table='bills' AND entity_id=?", (report["batch_id"], active["id"])).fetchone()
        self.assertEqual(json.loads(event["before_json"])["description"], "美团订单-fictional")
        self.assertEqual(json.loads(event["after_json"])["category_path"], "运动户外")
        with self.assertRaises(ValueError):
            apply_review(self.db, self.sample, self.plan)

    def test_stale_snapshot_rolls_back_the_entire_batch(self):
        identifier = self.sample["items"][0]["bill"]["id"]
        with self.db.batch("user", "fixture", "Fictional concurrent edit"):
            self.db.connection.execute("UPDATE bills SET notes='用户刚修改',revision=revision+1 WHERE id=?", (identifier,))
        before = self.db.connection.execute("SELECT COUNT(*) FROM change_batches").fetchone()[0]
        with self.assertRaises(ValueError):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM change_batches").fetchone()[0], before)

    def test_illegal_category_rolls_back_source_moves_and_soft_deletion(self):
        self.plan[0]["category"] = "其他"
        with self.assertRaises(ValueError):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200), self.sample)

    def test_dates_personal_share_and_out_of_scope_merges_are_not_accepted(self):
        for field in ("usage_date", "personal_base_cents", "amount_cents"):
            plan = [{**self.plan[0], field: 1}, self.plan[1]]
            with self.subTest(field=field), self.assertRaises(ValueError):
                apply_review(self.db, self.sample, plan)
        self.plan[1]["merge_into"] = 3
        with self.assertRaises(ValueError):
            apply_review(self.db, self.sample, self.plan)

    def test_human_locked_records_are_not_reclassified(self):
        identifier = self.sample["items"][0]["bill"]["id"]
        with self.db.batch("user", "fixture", "Fictional manual lock"):
            self.db.connection.execute("UPDATE bills SET classification_locked=1,revision=revision+1 WHERE id=?", (identifier,))
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        with self.assertRaises(ValueError):
            apply_review(self.db, self.sample, self.plan)

    def test_uncertain_rows_get_explicit_questions_not_other(self):
        plan = [{"index": i, "kind": "pending", "action": "uncertain", "confidence": "low", "reason": "虚构信息不足，请本人说明用途"} for i in (1, 2)]
        report = apply_review(self.db, self.sample, plan)
        self.assertEqual(report["uncertain"], 2)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM bills WHERE category_id IS NOT NULL").fetchone()[0], 0)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM review_items WHERE topic='nature' AND status='open'").fetchone()[0], 2)

    def test_merge_cannot_ignore_the_payment_channel(self):
        self.db.close()
        self.seed("wrong-channel", method="微信支付")
        with self.assertRaisesRegex(ValueError, "Payment method"):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200), self.sample)

    def test_different_amounts_are_not_merged(self):
        self.db.close()
        self.seed("wrong-amount", amount=7200)
        with self.assertRaisesRegex(ValueError, "Different amounts"):
            apply_review(self.db, self.sample, self.plan)

    def test_ambiguous_equal_amount_candidates_are_not_merged(self):
        extra = Record("alipay", "fictional", {"headers": ["收/付款方式"], "values": ["余额"]},
                       {"line_start": 3}, "fictional-extra", None, "2026-09-20T04:01:00Z", "second",
                       7300, "outflow", "餐饮美食", "另一笔同额支付", "美团", "交易成功", "payment")
        import_statements(self.db, [finalize(Statement(Path("fictional.csv"), "extra-hash", "alipay", [extra], {}))])
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        plan = [{"index": 1, "kind": "pending", "action": "uncertain", "confidence": "low", "reason": "虚构同额歧义"},
                {**self.plan[0], "index": 2, "rename_from": 3}, {**self.plan[1], "index": 3, "merge_into": 2}]
        with self.assertRaisesRegex(ValueError, "not unique"):
            apply_review(self.db, self.sample, plan)

    def test_income_direction_is_normalized_without_rewriting_raw_direction(self):
        plan = [{"index": 1, "kind": "income", "action": "non_expense", "confidence": "high", "reason": "虚构收益"},
                {"index": 2, "kind": "pending", "action": "uncertain", "confidence": "low", "reason": "虚构用途待问"}]
        apply_review(self.db, self.sample, plan)
        identifier = self.sample["items"][0]["bill"]["id"]
        self.assertEqual(self.db.connection.execute("SELECT direction FROM bills WHERE id=?", (identifier,)).fetchone()[0], "inflow")
        self.assertEqual(self.db.connection.execute("SELECT direction FROM raw_records WHERE id=?", (self.sample["items"][0]["sources"][0]["raw_record_id"],)).fetchone()[0], "outflow")


if __name__ == "__main__":
    unittest.main()
