from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import csv
import io
import json
import sqlite3
import tempfile
import unittest

from accounting.database import Database, now
from accounting.importers import (
    Record, Statement, bank_page_records, cents, finalize, identifier, parse_csv, utc_time,
)
from accounting.ingest import import_statements, insert


def sample_record(identifier="fake-event", status="支付成功"):
    return Record(
        "alipay","fake-account",{"headers":["交易订单号","交易状态"],"values":[identifier,status]},
        {"line_start":2,"line_end":2},identifier,"fake-order","2026-06-01T04:00:00Z",
        "second",1200,"outflow","餐饮美食","示例商品","示例商户",status,"outflow")


def statement(records, name="fictional.csv"):
    return finalize(Statement(Path(name),"fake-file-hash","alipay",records,{"warnings":[]}))


class ParsingTests(unittest.TestCase):
    def test_money_uses_exact_cents(self):
        self.assertEqual(cents("¥1,234.56"),123456)
        self.assertEqual(cents(Decimal("7.30")),730)
        self.assertEqual(cents("-12.00"),-1200)
        for bad in ("1.001","NaN","Infinity","not-money"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                cents(bad)

    def test_identifiers_preserve_leading_zeroes(self):
        self.assertEqual(identifier("  '000123\t"),"000123")
        for bad in (12345,1.2e20,"1.23E+19"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                identifier(bad)

    def test_local_time_boundary_and_minute_precision(self):
        self.assertEqual(utc_time("10/1/26 00:30"),("2026-09-30T16:30:00Z","minute"))

    def test_payment_and_refund_can_share_external_id(self):
        payment = sample_record()
        refund = replace(payment,event_tag="refund",direction="inflow",payload={"kind":"refund"})
        result = statement([payment,refund])
        self.assertNotEqual(result.records[0].event_key,result.records[1].event_key)

    def test_no_id_identical_observations_keep_multiplicity(self):
        first = replace(sample_record(),transaction_id=None)
        second = replace(first)
        result = statement([first,second])
        self.assertNotEqual(result.records[0].event_key,result.records[1].event_key)

    def test_csv_encoding_and_declared_net_refund_control(self):
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(["共2笔记录"])
        writer.writerow(["支出：1笔 8.00元"])
        writer.writerow(["不计收支：1笔 2.00元"])
        headers = ["交易时间","交易分类","交易对方","对方账号","商品说明","收/支","金额","收/付款方式","交易状态","交易订单号","商家订单号","备注"]
        writer.writerow(headers)
        writer.writerow(["2026-06-01 12:00:00","餐饮美食","示例商户","","示例商品","支出","10.00","","交易成功","fake-pay","fake-order",""])
        writer.writerow(["2026-06-02 12:00:00","退款","示例商户","","示例商品","不计收支","2.00","","退款成功","fake-refund","fake-order",""])
        parsed = parse_csv(Path("fictional.csv"),stream.getvalue().encode("gb18030"),"test")
        control = parsed.validation["direction_controls"]["outflow"]
        self.assertEqual(len(parsed.records),2)
        self.assertFalse(control["amount_matches"])
        self.assertTrue(control["net_of_successful_refunds_matches"])
        self.assertEqual(control["declared_cents"],800)
        self.assertEqual(parsed.records[0].amount_cents,1000)

    def test_bad_declared_count_is_fatal(self):
        body = ("共2笔记录\n交易创建时间,交易成功时间,交易类型,订单标题,收/支,支付方式,订单金额,实付金额,交易单号,商家单号,备注\n"
                "6/1/26 12:00,6/1/26 12:00,消费,示例,支出,支付宝支付,12.00,12.00,fake-id,fake-order,\n")
        parsed = parse_csv(Path("fictional.csv"),body.encode(),"test")
        self.assertEqual(len(parsed.records),1)
        alipay = ("共2笔记录\n交易时间,交易分类,交易对方,对方账号,商品说明,收/支,金额,收/付款方式,交易状态,交易订单号,商家订单号,备注\n"
                  "2026-06-01 12:00:00,餐饮美食,示例,,示例,支出,12.00,,交易成功,fake-id,fake-order,\n")
        with self.assertRaises(ValueError):
            parse_csv(Path("fictional.csv"),alipay.encode(),"test")

    def test_bank_page_controls_and_signed_amounts(self):
        headers = ["交易日期","账号","储种","序号","币种","钞汇","摘要","地区","收入/支出金额","余额","对方户名","对方账号","渠道"]
        row = ["2026-06-01\n12:00:00","fake-account","活期","00000","人民币","钞","消费","0000","-12.00","88.00","示例商户","fake-counterparty","快捷支付"]
        footer = "本页交易笔数：1 本页支出算术合计：12.00 本页收入算术合计：0.00"
        records,balances,control = bank_page_records([headers,row],footer,1)
        self.assertEqual(records[0].amount_cents,1200)
        self.assertEqual(records[0].direction,"outflow")
        self.assertEqual(balances,[(-1200,8800)])
        self.assertTrue(control["outgoing_matches"])
        with self.assertRaises(ValueError):
            bank_page_records([headers,row],footer.replace("12.00","11.00"),1)


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.directory.name)/"test.sqlite3")
        with self.db.batch("user","test","Create fictional category"):
            stamp = now()
            insert(self.db.connection,"categories",{"id":"food","name":"测试餐饮","created_at":stamp,"updated_at":stamp})

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def bill(self, identifier, amount=10000, personal=10000, kind="expense", paid="2026-06-01T04:00:00Z", **extra):
        stamp = now()
        values = {"id":identifier,"entry_origin":"manual","kind":kind,"payment_status":"settled",
                  "direction":"inflow" if kind in ("refund","reimbursement","income") else "outflow",
                  "amount_cents":amount,"currency":"CNY","paid_at":paid,"time_precision":"second",
                  "category_id":"food" if kind=="expense" else None,
                  "personal_base_cents":personal if kind=="expense" else None,
                  "review_status":"auto_confirmed","created_at":stamp,"updated_at":stamp}
        values.update(extra)
        insert(self.db.connection,"bills",values)

    def relation(self, identifier, origin, related, amount, kind="refund", personal=None):
        stamp = now()
        insert(self.db.connection,"bill_relations",{
            "id":identifier,"origin_bill_id":origin,"related_bill_id":related,
            "relation_type":kind,"amount_cents":amount,"personal_refund_cents":personal,
            "created_at":stamp,"updated_at":stamp})

    def test_nine_tables_no_import_metadata_tables(self):
        names = {r[0] for r in self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(len(names),9)
        self.assertNotIn("import_files",names)
        self.assertNotIn("import_rows",names)

    def test_no_context_writes_fail(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.bill("unexplained")

    def test_transaction_failure_rolls_back_rows_and_logs(self):
        initial = self.db.connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0]
        with self.assertRaises(RuntimeError):
            with self.db.batch("user","test","Fictional failing batch"):
                self.bill("rolled-back")
                raise RuntimeError("deliberate failure")
        self.assertIsNone(self.db.connection.execute("SELECT id FROM bills WHERE id='rolled-back'").fetchone())
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM updates").fetchone()[0],initial)
        self.assertEqual(self.db.context,{})

    def test_category_tree_and_reserved_category(self):
        with self.db.batch("user","test","Fictional child category"):
            stamp = now()
            insert(self.db.connection,"categories",{"id":"child","parent_id":"food","name":"测试小类","created_at":stamp,"updated_at":stamp})
        for values in ({"id":"grandchild","parent_id":"child","name":"测试三级"}, {"id":"other","name":"其他"}):
            with self.subTest(values=values), self.assertRaises(sqlite3.IntegrityError):
                with self.db.batch("user","test","Invalid category"):
                    insert(self.db.connection,"categories",{**values,"created_at":now(),"updated_at":now()})

    def test_audit_keeps_before_after_and_category_name(self):
        with self.db.batch("user","test","Fictional expense"):
            self.bill("expense")
        with self.db.batch("user","test","User supplied use date"):
            self.db.connection.execute("UPDATE bills SET usage_date='2026-07-01',revision=revision+1 WHERE id='expense'")
        row = self.db.connection.execute("SELECT * FROM updates WHERE entity_id='expense' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(json.loads(row["before_json"])["usage_date"],None)
        self.assertEqual(json.loads(row["after_json"])["usage_date"],"2026-07-01")
        self.assertEqual(json.loads(row["after_json"])["category_path"],"测试餐饮")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute("DELETE FROM updates")

    def test_ai_cannot_change_user_date_or_unlock_classification(self):
        with self.db.batch("user","test","Fictional locked expense"):
            self.bill("locked",classification_locked=1,usage_date="2026-07-01")
        for assignment in ("usage_date='2026-08-01'","personal_base_cents=9000","classification_locked=0"):
            with self.subTest(assignment=assignment), self.assertRaises(sqlite3.IntegrityError):
                with self.db.batch("ai","test-model","Disallowed change"):
                    self.db.connection.execute("UPDATE bills SET "+assignment+",revision=revision+1 WHERE id='locked'")

    def test_invalid_calendar_date_rejected(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.batch("user","test","Impossible date"):
                self.bill("bad-date",usage_date="2026-02-30")

    def test_shared_refund_and_reimbursement_do_not_double_subtract(self):
        with self.db.batch("user","test","Fictional shared purchase"):
            self.bill("expense",personal=6000)
            self.bill("refund",amount=2000,kind="refund")
            self.relation("refund-link","expense","refund",2000,personal=1200)
            self.bill("returned",amount=3200,kind="reimbursement")
            self.relation("return-link","expense","returned",3200,kind="reimbursement")
        self.assertEqual(self.db.connection.execute("SELECT personal_net_cents FROM personal_expenses WHERE id='expense'").fetchone()[0],4800)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.batch("user","test","Overpaid reimbursement"):
                self.bill("over-return",amount=1,kind="reimbursement")
                self.relation("excess","expense","over-return",1,kind="reimbursement")

    def test_excessive_refund_and_later_bill_changes_rejected(self):
        with self.db.batch("user","test","Fictional partial refund"):
            self.bill("expense",personal=6000)
            self.bill("refund",amount=2000,kind="refund")
            self.relation("refund-link","expense","refund",2000,personal=1200)
        for assignment in ("personal_base_cents=1000","inclusion_status='excluded',exclusion_reason='test'"):
            with self.subTest(assignment=assignment), self.assertRaises(sqlite3.IntegrityError):
                with self.db.batch("user","test","Invalid change with active relation"):
                    self.db.connection.execute("UPDATE bills SET "+assignment+",revision=revision+1 WHERE id='expense'")
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.batch("user","test","Excessive second refund"):
                self.bill("second",amount=9000,kind="refund")
                self.relation("excessive","expense","second",9000,personal=6000)

    def test_usage_date_and_timezone_boundaries(self):
        with self.db.batch("user","test","Fictional date boundaries"):
            self.bill("manual",usage_date="2026-10-15",paid="2026-08-01T04:00:00Z")
            self.bill("utc",paid="2026-09-30T16:30:00Z")
            self.bill("january",paid="2025-12-31T16:00:00Z")
        rows = {r["id"]:r["effective_usage_date"] for r in self.db.connection.execute("SELECT * FROM personal_expenses")}
        self.assertEqual(rows,{"manual":"2026-10-15","utc":"2026-10-01","january":"2026-01-01"})

    def test_import_overlap_and_rerun_are_idempotent(self):
        records = [statement([sample_record()],"first.csv"),statement([sample_record()],"overlap.csv")]
        first = import_statements(self.db,records)
        self.assertEqual((first["new_raw_records"],first["new_bills"],first["duplicate_snapshots"]),(1,1,1))
        second = import_statements(self.db,records)
        self.assertEqual((second["new_raw_records"],second["new_bills"],second["duplicate_snapshots"]),(0,0,2))

    def test_new_snapshot_preserves_human_result_and_soft_deletion(self):
        import_statements(self.db,[statement([sample_record()])])
        identifier = self.db.connection.execute("SELECT id FROM bills").fetchone()[0]
        with self.db.batch("user","test","User reviewed imported bill"):
            self.db.connection.execute("""
                UPDATE bills SET kind='expense',category_id='food',personal_base_cents=1200,
                review_status='user_confirmed',classification_locked=1,usage_date='2026-07-01',
                deleted_at='2026-08-01T00:00:00Z',revision=revision+1 WHERE id=?
            """,(identifier,))
        updated = sample_record(status="交易成功")
        report = import_statements(self.db,[statement([updated],"new-export.csv")])
        self.assertEqual(report["new_bills"],0)
        self.assertEqual(report["new_snapshots_of_existing_events"],1)
        row = self.db.connection.execute("SELECT * FROM bills WHERE id=?",(identifier,)).fetchone()
        self.assertEqual(row["usage_date"],"2026-07-01")
        self.assertEqual(row["classification_locked"],1)
        self.assertIsNotNone(row["deleted_at"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.connection.execute("UPDATE raw_records SET description='overwrite'")


if __name__ == "__main__":
    unittest.main()
