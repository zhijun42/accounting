from dataclasses import replace
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import json
import re
import sqlite3
import tempfile
import unittest

from accounting.database import Database
from accounting.importers import Record, Statement, finalize
from accounting.ingest import import_statements, issue
from accounting.review import add_item
from accounting.web import APIError, LocalServer, backup_database, bill_detail, create_category, history_rows, list_bills, update_bill


def fictional_statements():
    first = Record("alipay", "fictional-account", {"headers": ["商品"], "values": ["虚构商品"]},
                   {"line_start": 2, "line_end": 2}, "fictional-june", None,
                   "2026-06-01T04:00:00Z", "second", 1200, "outflow", "餐饮美食",
                   "虚构商品 <script> 100%", "虚构商户", "交易成功", "outflow")
    second = replace(first, transaction_id="fictional-september", description="虚构秋日商品",
                     occurred_at="2026-09-01T04:00:00Z", payload={"description": "秋日"})
    third = replace(first, transaction_id="fictional-october", description="虚构十月商品",
                    occurred_at="2026-10-01T04:00:00Z", payload={"description": "十月"})
    return [finalize(Statement(Path("fictional.csv"), "fictional-hash", "alipay", [first, second, third], {"warnings": []}))]


class ReviewFixture(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "test.sqlite3"
        self.db = Database(self.path)
        import_statements(self.db, fictional_statements())
        self.bill_id = self.db.connection.execute("SELECT id FROM bills ORDER BY paid_at LIMIT 1").fetchone()[0]
        self.category_id = self.db.connection.execute("SELECT id FROM categories WHERE name = '餐饮美食'").fetchone()[0]

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def expense(self, **extra):
        return {"revision": 1, "kind": "expense", "category_id": self.category_id,
                "personal_base_cents": 1200, "confirm": True, **extra}


class ReviewTests(ReviewFixture):
    def ai_check(self, topic="nature"):
        with self.db.batch("ai", "fictional-reviewer", "虚构逐笔判断") as batch_id:
            add_item(self.db, self.bill_id, topic, "虚构信息不足，等待本人核对", None, [], batch_id)

    def test_pending_splits_by_ai_decisions_even_without_bill_edits(self):
        self.assertEqual(list_bills(self.db.connection, {"status": "ai_reviewed_pending"})["total"], 0)
        self.ai_check()
        self.ai_check("category")
        reviewed = list_bills(self.db.connection, {"status": "ai_reviewed_pending"})
        self.assertEqual(reviewed["total"], 1)
        self.assertEqual(reviewed["items"][0]["id"], self.bill_id)
        self.assertIs(reviewed["items"][0]["ai_reviewed"], True)
        self.assertEqual(reviewed["items"][0]["kind"], "pending")
        self.assertIsNone(reviewed["items"][0]["category_id"])
        self.assertEqual(reviewed["summary"]["pending"], 2)
        self.assertEqual(reviewed["summary"]["ai_reviewed_pending"], 1)
        self.assertEqual(reviewed["summary"]["ai_unreviewed_pending"], 1)
        unseen = list_bills(self.db.connection, {"status": "ai_unreviewed_pending"})
        self.assertEqual(unseen["total"], 1)
        self.assertIs(unseen["items"][0]["ai_reviewed"], False)
        self.assertIs(bill_detail(self.db.connection, self.bill_id)["bill"]["ai_reviewed"], True)

    def test_category_and_unrelated_ai_edits_do_not_imply_an_ai_check(self):
        self.ai_check("matching")
        with self.db.batch("ai", "fictional-helper", "虚构备注修改并非逐笔检查"):
            self.db.connection.execute("UPDATE bills SET notes='虚构备注',revision=revision+1 WHERE id=?", (self.bill_id,))
        result = update_bill(self.db, self.bill_id, {**self.expense(confirm=False), "revision": 2})
        self.assertIsNotNone(result["bill"]["category_id"])
        self.assertIs(result["bill"]["ai_reviewed"], False)
        self.assertEqual(list_bills(self.db.connection, {"status": "ai_unreviewed_pending"})["total"], 2)
        with self.assertRaises(APIError):
            update_bill(self.db, self.bill_id, {"revision": 3, "ai_reviewed": True})

    def test_user_confirmation_leaves_pending_but_preserves_ai_history_after_restart(self):
        self.ai_check()
        result = update_bill(self.db, self.bill_id, self.expense())
        self.assertEqual(result["bill"]["review_status"], "user_confirmed")
        self.assertIs(result["bill"]["ai_reviewed"], True)
        self.assertFalse(result["review_items"])
        reopened = Database(self.path, readonly=True)
        try:
            self.assertEqual(list_bills(reopened.connection, {"status": "ai_reviewed_pending"})["total"], 0)
            self.assertEqual(list_bills(reopened.connection, {"status": "ai_unreviewed_pending"})["total"], 1)
            self.assertEqual(list_bills(reopened.connection, {"status": "confirmed"})["total"], 1)
            self.assertIs(bill_detail(reopened.connection, self.bill_id)["bill"]["ai_reviewed"], True)
        finally:
            reopened.close()

    def test_ai_filters_compose_with_scope_search_pagination_and_soft_deletion(self):
        self.ai_check()
        expenses = list_bills(self.db.connection, {"status": "ai_reviewed_pending", "kind": "expense"})
        self.assertEqual(expenses["summary"]["ai_reviewed_pending"], 0)
        pending = list_bills(self.db.connection, {"status": "ai_reviewed_pending", "kind": "pending"})
        self.assertEqual(pending["summary"]["ai_reviewed_pending"], 1)
        for parameters in ({"start": "2026-09-01"}, {"provider": "wechat"}, {"q": "不存在的商户"}, {"kind": "expense"}, {"page": "2"}):
            with self.subTest(parameters=parameters):
                result = list_bills(self.db.connection, {"status": "ai_reviewed_pending", **parameters})
                self.assertFalse(result["items"])
        self.assertEqual(list_bills(self.db.connection, {"status": "ai_reviewed_pending", "q": "%"})["total"], 1)
        with self.db.batch("user", "fictional-user", "虚构软删除"):
            self.db.connection.execute("UPDATE bills SET deleted_at='2026-10-11T00:00:00Z',revision=revision+1 WHERE id=?", (self.bill_id,))
        result = list_bills(self.db.connection, {"status": "ai_reviewed_pending"})
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["summary"]["ai_reviewed_pending"], 0)

    def test_date_scope_and_pagination_are_explicit(self):
        scoped = list_bills(self.db.connection, {})
        self.assertEqual(scoped["total"], 2)
        self.assertEqual(scoped["summary"]["pending"], 2)
        self.assertEqual(list_bills(self.db.connection, {"end": "2026-12-31"})["total"], 3)
        for parameters in ({"start": "2026-02-30"}, {"page": "no"}, {"provider": "unknown"}, {"status": "unknown"}):
            with self.subTest(parameters=parameters), self.assertRaises(APIError):
                list_bills(self.db.connection, parameters)

    def test_search_treats_wildcards_literally(self):
        self.assertEqual(list_bills(self.db.connection, {"q": "%"})["total"], 1)
        self.assertEqual(list_bills(self.db.connection, {"q": "_"})["total"], 0)

    def test_income_is_hidden_without_deleting_raw_bills_or_audit(self):
        raw = [dict(row) for row in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")]
        result = update_bill(self.db, self.bill_id, {"revision": 1, "kind": "income", "direction": "inflow", "confirm": True})
        self.assertEqual(result["bill"]["kind"], "income")
        self.ai_check()
        all_rows = list_bills(self.db.connection, {})
        self.assertEqual(all_rows["total"], 1)
        self.assertEqual(all_rows["summary"]["total"], 1)
        self.assertEqual(all_rows["summary"]["confirmed"], 0)
        self.assertEqual(all_rows["summary"]["ai_reviewed_pending"], 0)
        self.assertEqual(list_bills(self.db.connection, {"kind": "income"})["total"], 0)
        self.assertEqual(list_bills(self.db.connection, {"q": "100%"})["total"], 0)
        with self.assertRaises(APIError) as raised:
            bill_detail(self.db.connection, self.bill_id)
        self.assertEqual(raised.exception.status, 404)
        self.assertFalse(history_rows(self.db.connection))
        self.assertEqual(raw, [dict(row) for row in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")])
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM bills").fetchone()[0], 3)
        self.assertGreater(len(history_rows(self.db.connection, self.bill_id)), 1)

    def test_pending_source_proven_income_is_hidden_but_refunds_transfers_and_human_decisions_remain(self):
        first = fictional_statements()[0].records[0]
        records = [
            replace(first, transaction_id="fictional-yield", description="余额宝-2026.08.01-收益发放", direction="none", category="投资理财"),
            replace(first, transaction_id="fictional-principal", description="余额宝-转出到余额", direction="none", category="投资理财"),
            replace(first, transaction_id="fictional-refund", description="虚构退款", direction="inflow"),
            replace(first, transaction_id="fictional-receipt", description="虚构用途未明收款", direction="inflow", category="转账红包"),
            replace(first, transaction_id="fictional-human-decision", description="余额宝-2026.08.02-收益发放", direction="none", category="投资理财"),
            replace(first, transaction_id="fictional-source-income", description="虚构明确收入", direction="inflow", category="收入"),
        ]
        bank = [
            replace(first, provider="icbc", transaction_id="fictional-interest", description="利息", direction="inflow", category=None),
            replace(first, provider="icbc", transaction_id="fictional-deposit", description="银联入账", direction="inflow", category=None),
        ]
        import_statements(self.db, [finalize(Statement(Path("fictional-income.csv"), "fictional-income-hash", "alipay", records)),
                                   finalize(Statement(Path("fictional-bank.csv"), "fictional-bank-hash", "icbc", bank))])
        ids = {row["transaction_id"]: row["id"] for row in self.db.connection.execute("SELECT id,transaction_id FROM bill_details")}
        hidden_id = ids["fictional-yield"]
        update_bill(self.db, hidden_id, {"revision": 1, "description": "虚构改名的收益"})
        update_bill(self.db, self.bill_id, {"revision": 1, "description": "余额宝-2026.08.03-收益发放"})
        update_bill(self.db, ids["fictional-human-decision"], self.expense(direction="outflow", confirm=False))
        changes = self.db.connection.total_changes
        result = list_bills(self.db.connection, {})
        visible = {row["id"] for row in result["items"]}
        for identifier in ("fictional-yield", "fictional-interest", "fictional-source-income"):
            self.assertNotIn(ids[identifier], visible)
        for identifier in ("fictional-principal", "fictional-refund", "fictional-receipt", "fictional-human-decision", "fictional-deposit"):
            self.assertIn(ids[identifier], visible)
        self.assertIn(self.bill_id, visible)
        self.assertEqual(result["total"], 7)
        self.assertEqual(result["summary"]["pending"], 7)
        self.assertEqual(list_bills(self.db.connection, {"q": "虚构改名的收益"})["total"], 0)
        self.assertEqual(self.db.connection.total_changes, changes)
        self.assertEqual(self.db.connection.execute("SELECT kind FROM bills WHERE id=?", (hidden_id,)).fetchone()[0], "pending")

    def test_hidden_income_is_filtered_before_pagination(self):
        first = fictional_statements()[0].records[0]
        records = [replace(first, transaction_id=f"fictional-income-{index}", category="收入", direction="inflow")
                   for index in range(60)]
        import_statements(self.db, [finalize(Statement(Path("fictional-many-income.csv"), "fictional-many-income", "alipay", records))])
        result = list_bills(self.db.connection, {})
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["summary"]["total"], 2)
        self.assertFalse(list_bills(self.db.connection, {"page": "2"})["items"])

    def test_internal_transfer_save_hides_the_bill_without_changing_raw_or_amount(self):
        raw = [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")]
        result = update_bill(self.db, self.bill_id, {"revision": 1, "kind": "internal_transfer", "direction": "none"})
        self.assertEqual((result["bill"]["kind"], result["bill"]["amount_cents"]), ("internal_transfer", 1200))
        self.assertEqual(list_bills(self.db.connection, {})["total"], 1)
        self.assertEqual(list_bills(self.db.connection, {"kind": "internal_transfer"})["total"], 0)
        self.assertFalse(history_rows(self.db.connection))
        self.assertTrue(history_rows(self.db.connection, self.bill_id))
        with self.assertRaises(APIError) as raised:
            bill_detail(self.db.connection, self.bill_id)
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(raw, [tuple(r) for r in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")])

    def test_raw_only_review_history_is_not_hidden_by_a_null_bill_id(self):
        raw_id = self.db.connection.execute("SELECT id FROM raw_records LIMIT 1").fetchone()[0]
        with self.db.batch("ai", "fictional-reviewer", "虚构原始来源问题"):
            issue(self.db, None, raw_id, "parsing", "虚构未关联原始记录问题")
        self.assertEqual(len(history_rows(self.db.connection)), 1)

    def test_unclassified_filter_excludes_non_consumption_and_composes_with_review_filters(self):
        first = fictional_statements()[0].records[0]
        labels = ("expense", "categorized", "income", "refund", "internal_transfer", "deleted")
        records = [replace(first, transaction_id="fictional-filter-" + label,
                           description="虚构筛选-" + label) for label in labels]
        import_statements(self.db, [finalize(Statement(Path("fictional-filter.csv"), "fictional-filter-hash",
                                                        "alipay", records, {"warnings": []}))])
        ids = {row["description"]: row["id"] for row in self.db.connection.execute("SELECT id,description FROM bills")}
        update_bill(self.db, ids["虚构筛选-expense"], self.expense(category_id=None, confirm=False))
        update_bill(self.db, ids["虚构筛选-categorized"], self.expense())
        for kind in ("income", "refund", "internal_transfer"):
            update_bill(self.db, ids["虚构筛选-" + kind], {"revision": 1, "kind": kind, "direction": "inflow", "confirm": True})
        with self.db.batch("user", "fictional-user", "虚构软删除"):
            self.db.connection.execute("UPDATE bills SET deleted_at='2026-10-11T00:00:00Z',revision=revision+1 WHERE id=?",
                                       (ids["虚构筛选-deleted"],))
        self.ai_check()
        result = list_bills(self.db.connection, {"category": "unclassified"})
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["summary"]["total"], 3)
        self.assertEqual(result["summary"]["ai_reviewed_pending"], 1)
        self.assertTrue(all(row["category_id"] is None and row["kind"] in {"pending", "expense"} for row in result["items"]))
        self.assertEqual(list_bills(self.db.connection, {"category": "unclassified", "kind": "expense"})["total"], 1)
        self.assertEqual(list_bills(self.db.connection, {"category": "unclassified", "status": "ai_reviewed_pending"})["total"], 1)
        self.assertEqual(list_bills(self.db.connection, {"category": "unclassified", "status": "ai_unreviewed_pending"})["total"], 2)
        self.assertEqual(list_bills(self.db.connection, {"category": "categorized", "status": "confirmed"})["total"], 1)
        for parameters in ({"provider": "wechat"}, {"kind": "income"}, {"status": "confirmed"}, {"q": "不存在"}, {"page": "2"}):
            with self.subTest(parameters=parameters):
                self.assertFalse(list_bills(self.db.connection, {"category": "unclassified", **parameters})["items"])
        self.assertEqual(list_bills(self.db.connection, {"category": "unclassified", "start": "2026-09-01"})["total"], 1)
        with self.assertRaises(APIError):
            list_bills(self.db.connection, {"category": "unknown"})

    def test_title_edit_is_audited_searchable_and_leaves_raw_and_review_state_unchanged(self):
        old = bill_detail(self.db.connection, self.bill_id)["bill"]
        raw = [dict(row) for row in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")]
        result = update_bill(self.db, self.bill_id, {"revision": 1, "description": "  虚构脱口秀  "})
        self.assertEqual(result["bill"]["description"], "虚构脱口秀")
        for field in ("kind", "category_id", "amount_cents", "paid_at", "usage_date", "personal_base_cents", "review_status", "classification_locked"):
            self.assertEqual(result["bill"][field], old[field])
        self.assertEqual(result["history"][0]["before"]["description"], old["description"])
        self.assertEqual(result["history"][0]["after"]["description"], "虚构脱口秀")
        self.assertEqual(result["history"][0]["actor_type"], "user")
        self.assertEqual(raw, [dict(row) for row in self.db.connection.execute("SELECT * FROM raw_records ORDER BY id")])
        self.assertEqual(list_bills(self.db.connection, {"q": "虚构脱口秀"})["items"][0]["id"], self.bill_id)
        self.assertEqual(list_bills(self.db.connection, {"q": "100%"})["total"], 0)
        reopened = Database(self.path, readonly=True)
        try:
            self.assertEqual(bill_detail(reopened.connection, self.bill_id)["bill"]["description"], "虚构脱口秀")
        finally:
            reopened.close()
        unchanged = update_bill(self.db, self.bill_id, {"revision": 2, "description": " 虚构脱口秀 "})
        self.assertEqual(unchanged["bill"]["revision"], 2)
        self.assertEqual(len(unchanged["history"]), len(result["history"]))
        with self.assertRaises(APIError) as raised:
            update_bill(self.db, self.bill_id, {"revision": 1, "description": "过期的标题"})
        self.assertEqual(raised.exception.status, 409)

    def test_invalid_title_does_not_write_bill_or_audit(self):
        old = bill_detail(self.db.connection, self.bill_id)
        counts = {table: self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("updates", "change_batches")}
        for title in (None, False, 12, [], {}, "", " \n\t", "字" * 501):
            with self.subTest(title=title), self.assertRaises(APIError):
                update_bill(self.db, self.bill_id, {"revision": 1, "description": title})
        self.assertEqual(bill_detail(self.db.connection, self.bill_id), old)
        for table, count in counts.items():
            self.assertEqual(self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)

    def test_title_only_edit_preserves_confirmed_classification(self):
        confirmed = update_bill(self.db, self.bill_id, self.expense())
        result = update_bill(self.db, self.bill_id, {"revision": 2, "description": "虚构已确认的新标题"})
        self.assertEqual(result["bill"]["review_status"], "user_confirmed")
        self.assertEqual(result["bill"]["classification_locked"], 1)
        self.assertEqual(result["bill"]["category_id"], confirmed["bill"]["category_id"])

    def test_confirm_persists_date_category_lock_and_audit(self):
        raw_before = self.db.connection.execute("SELECT raw_payload_json FROM raw_records ORDER BY id").fetchall()
        result = update_bill(self.db, self.bill_id, self.expense(usage_date="2026-10-15", notes="虚构活动"))
        self.assertEqual(result["bill"]["review_status"], "user_confirmed")
        self.assertEqual(result["bill"]["classification_locked"], 1)
        self.assertEqual(result["bill"]["usage_date"], "2026-10-15")
        self.assertEqual(result["bill"]["revision"], 2)
        self.assertFalse(result["review_items"])
        audit = result["history"][0]
        self.assertEqual(audit["actor_type"], "user")
        self.assertEqual(audit["before"]["kind"], "pending")
        self.assertEqual(audit["after"]["category_path"], "餐饮美食")
        self.assertEqual(raw_before, self.db.connection.execute("SELECT raw_payload_json FROM raw_records ORDER BY id").fetchall())
        self.assertEqual(list_bills(self.db.connection, {"status": "confirmed"})["total"], 1)
        reopened = Database(self.path, readonly=True)
        try:
            self.assertEqual(bill_detail(reopened.connection, self.bill_id)["bill"]["notes"], "虚构活动")
        finally:
            reopened.close()

    def test_draft_is_not_confirmation_and_noop_has_no_new_update(self):
        draft = update_bill(self.db, self.bill_id, self.expense(confirm=False))
        self.assertEqual(draft["bill"]["review_status"], "pending")
        self.assertEqual(draft["bill"]["classification_locked"], 0)
        self.assertTrue(draft["review_items"])
        count = self.db.connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0]
        result = update_bill(self.db, self.bill_id, {"revision": 2})
        self.assertEqual(result["bill"]["revision"], 2)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0], count)

    def test_invalid_confirmation_rolls_back_batch_and_log(self):
        original = {table: self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("updates", "change_batches")}
        for payload in ({"revision": 1, "confirm": True}, self.expense(category_id=None), self.expense(personal_base_cents=None)):
            with self.subTest(payload=payload), self.assertRaises(APIError):
                update_bill(self.db, self.bill_id, payload)
        for table, count in original.items():
            self.assertEqual(self.db.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], count)

    def test_stale_revision_is_conflict_not_overwrite(self):
        update_bill(self.db, self.bill_id, {"revision": 1, "notes": "第一份修改"})
        with self.assertRaises(APIError) as raised:
            update_bill(self.db, self.bill_id, {"revision": 1, "notes": "过期修改"})
        self.assertEqual(raised.exception.status, 409)
        self.assertEqual(bill_detail(self.db.connection, self.bill_id)["bill"]["notes"], "第一份修改")

    def test_category_creation_is_two_levels_and_audited(self):
        root = create_category(self.db, {"name": "虚构运动"})["id"]
        child = create_category(self.db, {"name": "虚构攀岩", "parent_id": root})["id"]
        for payload in ({"name": "虚构三级", "parent_id": child}, {"name": ""}):
            with self.subTest(payload=payload), self.assertRaises(APIError):
                create_category(self.db, payload)
        for payload in ({"name": "其他"}, {"name": "虚构运动"}):
            with self.subTest(payload=payload), self.assertRaises(sqlite3.IntegrityError):
                create_category(self.db, payload)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM updates WHERE entity_id IN (?,?)", (root, child)).fetchone()[0], 2)

    def test_money_dates_exclusions_and_unknown_fields_are_validated(self):
        invalid = [self.expense(personal_base_cents=True), self.expense(personal_base_cents=1201),
                   self.expense(usage_date="2026-02-30"), self.expense(inclusion_status="excluded"),
                   {"revision": 1, "amount_cents": 10}, {"revision": True},
                   {"revision": 1, "kind": "income", "category_id": self.category_id}]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(APIError):
                update_bill(self.db, self.bill_id, payload)
        excluded = update_bill(self.db, self.bill_id, self.expense(inclusion_status="excluded", exclusion_reason="虚构垫付"))
        self.assertEqual(excluded["bill"]["inclusion_status"], "excluded")
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM personal_expenses").fetchone()[0], 0)

    def test_refund_can_correct_direction_without_overwriting_source(self):
        result = update_bill(self.db, self.bill_id, {"revision": 1, "kind": "refund", "direction": "inflow", "confirm": True})
        self.assertEqual(result["bill"]["direction"], "inflow")
        self.assertEqual(result["sources"][0]["direction"], "outflow")

    def test_backup_is_independent_and_complete(self):
        backup = backup_database(self.path)
        update_bill(self.db, self.bill_id, {"revision": 1, "notes": "备份之后的修改"})
        copy = Database(backup, readonly=True)
        try:
            self.assertIsNone(bill_detail(copy.connection, self.bill_id)["bill"]["notes"])
            self.assertEqual(copy.connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            copy.close()


class HTTPTests(ReviewFixture):
    def setUp(self):
        super().setUp()
        self.server = LocalServer(self.path, 0)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        super().tearDown()

    def request(self, path, payload=None, headers=None):
        defaults = {"Origin": self.base}
        if payload is not None:
            defaults.update({"Content-Type": "application/json", "X-Accounting-CSRF": self.server.csrf_token})
        defaults.update(headers or {})
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None, headers=defaults)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_static_assets_have_no_store_and_strict_csp(self):
        status, _, html = self.request("/")
        self.assertEqual(status, 200, "Run make build-ui before HTTP tests")
        assets = re.findall(r'(?:src|href)="(/assets/[^\"]+)"', html.decode())
        self.assertTrue(any(path.endswith(".js") for path in assets))
        self.assertTrue(any(path.endswith(".css") for path in assets))
        for path in ("/", *assets):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_static_routes_do_not_expose_source_or_private_files(self):
        for path in ("/assets/../../schema.sql", "/assets/%2e%2e/web.py", "/assets/missing.js", "/src/App.tsx", "/data/accounting.sqlite3", "/app.js"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_cross_site_and_rebinding_and_missing_token_are_rejected(self):
        for headers in ({"Origin": "https://untrusted.example"}, {"Host": "untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("/api/bills", headers=headers)[0], 403)
        self.assertEqual(self.request("/api/categories", {"name": "虚构分类"}, {"X-Accounting-CSRF": ""})[0], 403)

    def test_http_write_persists_and_stale_tab_returns_conflict(self):
        endpoint = "/api/bills/" + self.bill_id
        status, _, body = self.request(endpoint, self.expense())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["bill"]["review_status"], "user_confirmed")
        self.assertEqual(self.request(endpoint, self.expense())[0], 409)
        self.assertEqual(self.request("/api/categories", {"name": "其他"})[0], 422)
        self.assertEqual(self.request("/api/categories", {"name": "虚构分类"})[0], 200)
        self.assertEqual(self.request("/api/history")[0], 200)

    def test_http_ai_review_filter_and_detail_agree(self):
        with self.db.batch("ai", "fictional-reviewer", "虚构逐笔判断") as batch_id:
            add_item(self.db, self.bill_id, "nature", "虚构用途待本人核对", None, [], batch_id)
        status, _, body = self.request("/api/bills?status=ai_reviewed_pending")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["total"], 1)
        self.assertIs(data["items"][0]["ai_reviewed"], True)
        status, _, body = self.request("/api/bills/" + self.bill_id)
        self.assertEqual(status, 200)
        self.assertIs(json.loads(body)["bill"]["ai_reviewed"], True)
        status, _, body = self.request("/api/bills?status=ai_unreviewed_pending")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)

    def test_http_title_save_and_category_filter(self):
        endpoint = "/api/bills/" + self.bill_id
        status, _, body = self.request(endpoint, {"revision": 1, "description": "虚构新标题"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["bill"]["description"], "虚构新标题")
        self.assertEqual(self.request(endpoint, {"revision": 1, "description": "过期标题"})[0], 409)
        self.assertEqual(self.request(endpoint, {"revision": 2, "description": " "})[0], 400)
        self.assertEqual(self.request(endpoint, self.expense(revision=2))[0], 200)
        status, _, body = self.request("/api/bills?category=unclassified")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)
        self.assertNotEqual(json.loads(body)["items"][0]["id"], self.bill_id)
        status, _, body = self.request("/api/bills?category=categorized&status=confirmed")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["description"], "虚构新标题")
        self.assertEqual(self.request("/api/bills?category=unknown")[0], 400)

    def test_http_income_save_succeeds_then_leaves_product_views(self):
        endpoint = "/api/bills/" + self.bill_id
        status, _, body = self.request(endpoint, {"revision": 1, "kind": "income", "direction": "inflow"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["bill"]["kind"], "income")
        self.assertEqual(self.request(endpoint)[0], 404)
        status, _, body = self.request("/api/bills")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["total"], 1)
        status, _, body = self.request("/api/history")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["items"])


if __name__ == "__main__":
    unittest.main()
