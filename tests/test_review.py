from dataclasses import replace
from pathlib import Path
import json
import tempfile
import unittest

from accounting.database import Database
from accounting.importers import Record, Statement, finalize
from accounting.ingest import import_statements
from accounting.matching import MeituanMatcher
from accounting.review import add_item, apply_review, snapshot
from accounting.web import update_bill


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
        self.assertEqual(MeituanMatcher(self.db.connection).match(self.sample["items"][0]["bill"]["id"])["status"], "unmatched")
        with self.assertRaisesRegex(ValueError, "actual-paid group"):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200), self.sample)

    def test_different_amounts_are_not_merged(self):
        self.db.close()
        self.seed("wrong-amount", amount=7200)
        with self.assertRaisesRegex(ValueError, "actual-paid group"):
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

    def grouped_fixture(self, amount=6800, extras=(500,), method="支付宝支付"):
        self.db.close()
        self.seed("group", amount=amount)
        for index, cents in enumerate(extras):
            self.add_order("fictional-addon-" + str(index), cents, method)
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        self.group_plan()

    def add_order(self, identifier, amount, method="支付宝支付", **extra):
        record = Record("meituan", "fictional", {"headers": ["支付方式", "订单金额", "实付金额"],
                        "values": [method, "999.00", f"{amount // 100}.{amount % 100:02d}"]},
                        {"line_start": 8}, identifier, None, "2026-09-20T04:00:00Z", "minute", amount,
                        "outflow", None, "虚构优惠券包", None, None, "payment")
        record = replace(record, **extra)
        import_statements(self.db, [finalize(Statement(Path("fictional-addons.csv"), identifier, "meituan", [record], {}))])

    def group_plan(self):
        payment = next(i["index"] for i in self.sample["items"] if i["sources"][0]["provider"] == "alipay")
        main = next(i["index"] for i in self.sample["items"] if i["sources"][0]["transaction_id"] == "fictional-order")
        self.plan = [
            {"index": i["index"], "kind": "expense", "category": "运动户外", "rename_from": main,
             "action": "corrected", "confidence": "high", "reason": "虚构活动与券包组合，以实际付款为准"}
            if i["index"] == payment else
            {"index": i["index"], "merge_into": payment, "action": "merged", "confidence": "high",
             "reason": "虚构实付金额组合、同渠道同分钟，双向唯一"}
            for i in self.sample["items"]
        ]
        return self.sample["items"][payment - 1]["bill"]["id"]

    def test_one_payment_multiple_orders_is_discovered_and_audited_without_double_counting(self):
        self.grouped_fixture()
        raw = [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")]
        match = next(m for m in self.sample["meituan_matches"] if m["status"] == "unique")
        self.assertEqual((match["amount_cents"], match["order_total_cents"]), (7300, 7300))
        self.assertEqual(len(match["order_bill_ids"]), 2)
        report = apply_review(self.db, self.sample, self.plan)
        self.assertEqual((report["merged"], report["renamed"]), (2, 1))
        active = self.db.connection.execute("SELECT * FROM bills WHERE deleted_at IS NULL").fetchall()
        self.assertEqual(len(active), 1)
        self.assertEqual((active[0]["amount_cents"], active[0]["description"]), (7300, "虚构攀岩馆"))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM bill_sources WHERE bill_id=?", (active[0]["id"],)).fetchone()[0], 3)
        self.assertEqual(raw, [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")])
        audits = self.db.connection.execute("SELECT evidence_json FROM updates WHERE batch_id=? AND entity_table='bill_sources'", (report["batch_id"],)).fetchall()
        self.assertTrue(all(json.loads(r[0])["payment_group"]["order_total_cents"] == 7300 for r in audits))
        self.assertFalse(MeituanMatcher(self.db.connection).payments)

    def test_group_supports_more_than_two_orders_and_uses_actual_paid_not_list_price(self):
        self.grouped_fixture(amount=6500, extras=(300, 500))
        report = apply_review(self.db, self.sample, self.plan)
        self.assertEqual(report["merged"], 3)
        self.assertEqual(self.db.connection.execute("SELECT amount_cents FROM bills WHERE deleted_at IS NULL").fetchone()[0], 7300)

    def test_incomplete_group_cannot_drop_the_coupon(self):
        self.grouped_fixture()
        addon = next(i["index"] for i in self.sample["items"] if i["sources"][0]["transaction_id"] == "fictional-addon-0")
        self.plan = [{"index": addon, "kind": "pending", "action": "uncertain", "confidence": "low", "reason": "虚构未合并券包"}
                     if d["index"] == addon else d for d in self.plan]
        with self.assertRaisesRegex(ValueError, "complete"):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200), self.sample)

    def test_small_unexplained_difference_does_not_authorize_a_consumer_merge(self):
        self.grouped_fixture(extras=(480,))
        with self.assertRaisesRegex(ValueError, "actual-paid group"):
            apply_review(self.db, self.sample, self.plan)

    def test_coupon_channel_must_match_the_payment(self):
        self.grouped_fixture(method="微信支付")
        with self.assertRaisesRegex(ValueError, "actual-paid group"):
            apply_review(self.db, self.sample, self.plan)

    def test_equal_total_alternative_order_is_ambiguous(self):
        self.grouped_fixture()
        self.add_order("fictional-full-order", 7300)
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        self.assertEqual(self.sample["meituan_matches"][0]["status"], "ambiguous")
        self.group_plan()
        with self.assertRaisesRegex(ValueError, "not unique"):
            apply_review(self.db, self.sample, self.plan)

    def test_an_order_cannot_be_claimed_by_two_payments_even_of_different_amounts(self):
        self.grouped_fixture()
        rival = Record("alipay", "fictional", {"headers": [], "values": []}, {"line_start": 9},
                       "fictional-rival", None, "2026-09-20T04:00:40Z", "second", 6800, "outflow", None,
                       "虚构美团付款", "美团", "交易成功", "payment")
        import_statements(self.db, [finalize(Statement(Path("fictional-rival.csv"), "rival", "alipay", [rival], {}))])
        self.assertEqual(MeituanMatcher(self.db.connection).match(self.group_plan())["status"], "ambiguous")

    def test_refund_voided_and_day_precision_orders_do_not_fill_a_missing_amount(self):
        self.grouped_fixture(extras=())
        for name, extra in (("refund", {"direction": "inflow", "event_tag": "refund"}),
                            ("voided", {"status": "交易关闭"}), ("day", {"precision": "day"}),
                            ("late", {"occurred_at": "2026-09-20T04:05:00Z"}), ("currency", {"currency": "USD"})):
            self.add_order("fictional-excluded-" + name, 500, **extra)
        self.assertEqual(MeituanMatcher(self.db.connection).match(self.group_plan())["status"], "unmatched")

    def test_manual_category_save_is_protected_even_without_confirmation(self):
        identifier = self.sample["items"][0]["bill"]["id"]
        category = self.db.connection.execute("SELECT id FROM categories WHERE name='餐饮美食'").fetchone()[0]
        update_bill(self.db, identifier, {"revision": 1, "kind": "expense", "category_id": category})
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        self.assertEqual(self.sample["items"][0]["bill"]["classification_locked"], 0)
        with self.assertRaisesRegex(ValueError, "personal edits"):
            apply_review(self.db, self.sample, self.plan)
        self.assertEqual(snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200), self.sample)

    def test_merge_preserves_manual_edits_on_an_order(self):
        self.grouped_fixture()
        order = next(i["bill"] for i in self.sample["items"] if i["sources"][0]["transaction_id"] == "fictional-order")
        update_bill(self.db, order["id"], {"revision": 1, "description": "本人改好的虚构活动名"})
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        self.group_plan()
        with self.assertRaisesRegex(ValueError, "personal edits"):
            apply_review(self.db, self.sample, self.plan)

    def test_focused_snapshot_contains_local_policy_and_still_checks_global_match_ambiguity(self):
        policy = {"version": 1, "classification_preferences": [{"category_name": "虚构交流", "guidance": "虚构前沿社交活动"}]}
        (Path(self.directory.name) / "review-policy.json").write_text(json.dumps(policy))
        identifier = self.sample["items"][0]["bill"]["id"]
        focused = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200, [identifier])
        self.assertEqual(focused["review_policy"], policy)
        self.assertEqual(len(focused["items"]), 1)
        self.assertEqual(len(focused["meituan_matches"][0]["order_bill_ids"]), 1)
        with self.assertRaisesRegex(ValueError, "active"):
            snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200, ["fictional-missing"])

    def test_candidate_limit_blocks_instead_of_assuming_uniqueness(self):
        self.grouped_fixture(extras=tuple(500 for _ in range(14)))
        self.assertEqual(MeituanMatcher(self.db.connection).match(self.group_plan())["status"], "blocked")

    def test_only_authored_matching_questions_are_resolved_and_audited(self):
        identifier = self.sample["items"][0]["bill"]["id"]
        with self.db.batch("ai", "fictional-reviewer", "虚构旧匹配疑问") as batch:
            add_item(self.db, identifier, "matching", "虚构需要确认付款与订单", None, [], batch)
        self.sample = snapshot(self.db.connection, "2026-04-01", "2026-09-30", 200)
        question = next(q["id"] for q in self.sample["items"][0]["review_items"] if q["topic"] == "matching")
        self.plan[0]["resolve_matching"] = ["fictional-outside-question"]
        with self.assertRaisesRegex(ValueError, "matching questions"):
            apply_review(self.db, self.sample, self.plan)
        self.plan[0]["resolve_matching"] = [question]
        report = apply_review(self.db, self.sample, self.plan)
        row = self.db.connection.execute("SELECT status,resolved_batch_id FROM review_items WHERE id=?", (question,)).fetchone()
        self.assertEqual((row["status"], row["resolved_batch_id"]), ("resolved", report["batch_id"]))
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM updates WHERE batch_id=? AND entity_id=? AND operation='update'", (report["batch_id"],question)).fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
