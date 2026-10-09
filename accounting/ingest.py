from collections import Counter
from pathlib import Path
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid4, uuid5

from .database import Database, json_text, now
from .importers import PARSER_VERSION, parse_statement

CATEGORY_NAMES = (
    "餐饮美食","交通出行","爱车养车","充值缴费","服饰装扮","日用百货","家居家装",
    "数码电器","运动户外","美容美发","母婴亲子","宠物","住房物业","酒店旅游",
    "文化休闲","教育培训","医疗健康","生活服务","公共服务","商业服务","公益捐赠","互助保障","保险",
)


def insert(connection, table, values, conflict=""):
    columns = ",".join(values)
    placeholders = ",".join("?" for _ in values)
    return connection.execute(f"INSERT INTO {table}({columns}) VALUES({placeholders}) {conflict}",tuple(values.values()))


def account_id(record):
    return str(uuid5(NAMESPACE_URL, "personal-accounting:" + json_text([record.provider,record.account_ref])))


def seed_categories(db):
    timestamp = now()
    for order,name in enumerate(CATEGORY_NAMES):
        if db.connection.execute("SELECT 1 FROM categories WHERE parent_id IS NULL AND name = ?",(name,)).fetchone():
            continue
        insert(db.connection,"categories",{
            "id":str(uuid4()),"name":name,"sort_order":order,
            "created_at":timestamp,"updated_at":timestamp})


def ensure_account(db, record):
    identifier = account_id(record)
    if not db.connection.execute("SELECT 1 FROM accounts WHERE id = ?",(identifier,)).fetchone():
        kind = {"alipay":"wallet","wechat":"wallet","meituan":"platform","icbc":"bank_debit"}[record.provider]
        timestamp = now()
        insert(db.connection,"accounts",{
            "id":identifier,"provider":record.provider,"account_kind":kind,
            "display_name":record.provider,"external_ref":record.account_ref,"currency":record.currency,
            "created_at":timestamp,"updated_at":timestamp})
    return identifier


def payment_status(record):
    if record.provider in ("icbc","meituan"):
        return "settled"
    settled = {"交易成功","支付成功","退款成功","已存入零钱","对方已收钱","已转账",
               "还款成功","提现已到账","充值完成","已全额退款"}
    return "settled" if record.status in settled else "pending"


def issue(db, bill_id, raw_id, topic, reason, proposal=None, revision=1):
    if db.connection.execute("""
        SELECT 1 FROM review_items WHERE bill_id IS ? AND raw_record_id IS ?
        AND topic = ? AND status = 'open' AND reason = ?
    """,(bill_id,raw_id,topic,reason)).fetchone():
        return
    timestamp = now()
    insert(db.connection,"review_items",{
        "id":str(uuid4()),"bill_id":bill_id,"raw_record_id":raw_id,"topic":topic,"status":"open",
        "reason":reason,"proposal_json":json_text(proposal) if proposal is not None else None,
        "evidence_json":json_text([{"raw_record_id":raw_id}]),"bill_revision":revision,
        "created_at":timestamp,"updated_at":timestamp})


def import_statements(db, statements):
    report = {"parser_version":PARSER_VERSION,"files":[],"new_raw_records":0,"duplicate_snapshots":0,
              "new_bills":0,"new_snapshots_of_existing_events":0,
              "classification_complete":False,"cross_platform_matching_complete":False}
    with db.batch("importer","statement-importer","导入来源账单；未进行生活消费分类或跨平台自动合并") as batch_id:
        report["batch_id"] = batch_id
        seed_categories(db)
        for statement in statements:
            file_result = {"filename":statement.path.name,"sha256":statement.file_sha256,
                           "provider":statement.provider,"source_rows":len(statement.records),
                           "new_raw_records":0,"duplicate_snapshots":0,"new_bills":0,
                           "validation":statement.validation}
            first_raw = None
            for record in statement.records:
                db.explain("保存原始来源及同源事件身份，不确认消费性质或分类",
                           [{"source_filename":statement.path.name,"locator":record.locator,
                             "identity_method":"external_event_id" if record.transaction_id else "source_fingerprint"}])
                aid = ensure_account(db,record)
                payload = json_text(record.payload)
                digest = sha256(json_text({"parser_version":PARSER_VERSION,"payload":record.payload}).encode()).hexdigest()
                raw_id, timestamp = str(uuid4()), now()
                values = {
                    "id":raw_id,"account_id":aid,"source_event_key":record.event_key,"snapshot_sha256":digest,
                    "transaction_id":record.transaction_id,"merchant_order_id":record.merchant_order_id,
                    "occurred_at":record.occurred_at,"time_precision":record.precision,
                    "amount_cents":record.amount_cents,"currency":record.currency,"direction":record.direction,
                    "original_category":record.category,"description":record.description,
                    "counterparty":record.counterparty,"original_status":record.status,
                    "raw_payload_json":payload,"source_filename":statement.path.name,
                    "source_file_sha256":statement.file_sha256,"source_locator_json":json_text(record.locator),
                    "parser_version":PARSER_VERSION,"import_batch_id":batch_id,"created_at":timestamp}
                result = insert(db.connection,"raw_records",values,
                                "ON CONFLICT(account_id,source_event_key,snapshot_sha256) DO NOTHING")
                if result.rowcount == 0:
                    raw_id = db.connection.execute("""
                        SELECT id FROM raw_records WHERE account_id = ? AND source_event_key = ? AND snapshot_sha256 = ?
                    """,(aid,record.event_key,digest)).fetchone()[0]
                    first_raw = first_raw or raw_id
                    report["duplicate_snapshots"] += 1
                    file_result["duplicate_snapshots"] += 1
                    continue
                first_raw = first_raw or raw_id
                report["new_raw_records"] += 1
                file_result["new_raw_records"] += 1
                previous = db.connection.execute("""
                    SELECT b.id,b.revision FROM raw_records r
                    JOIN bill_sources s ON s.raw_record_id = r.id
                    JOIN bills b ON b.id = s.bill_id
                    WHERE r.account_id = ? AND r.source_event_key = ?
                    ORDER BY s.is_primary DESC LIMIT 1
                """,(aid,record.event_key)).fetchone()
                db.explain("将来源快照关联到同源事件；经济性质和分类保留待核对",
                           [{"raw_record_id":raw_id,"source_event_key":record.event_key}])
                if previous:
                    bill_id, revision = previous["id"], previous["revision"]
                    report["new_snapshots_of_existing_events"] += 1
                else:
                    bill_id, revision = str(uuid4()), 1
                    insert(db.connection,"bills",{
                        "id":bill_id,"entry_origin":"imported","kind":"pending",
                        "payment_status":payment_status(record),"direction":record.direction,
                        "amount_cents":record.amount_cents,"currency":record.currency,
                        "paid_at":record.occurred_at,"time_precision":record.precision,
                        "description":record.description,"counterparty":record.counterparty,
                        "review_status":"pending","created_at":timestamp,"updated_at":timestamp})
                    report["new_bills"] += 1
                    file_result["new_bills"] += 1
                insert(db.connection,"bill_sources",{
                    "id":str(uuid4()),"bill_id":bill_id,"raw_record_id":raw_id,
                    "role":"snapshot" if previous else "order" if record.provider == "meituan" else "payment",
                    "is_primary":0 if previous else 1,
                    "match_method":"exact_id" if record.transaction_id else "composite",
                    "evidence_json":json_text({"account_id":aid,"source_event_key":record.event_key,
                                               "cross_platform_match":False}),
                    "created_at":timestamp})
                issue(db,bill_id,raw_id,"parsing" if previous else "nature",
                      "同源事件出现新快照，保留原账单和人工修改，需检查来源变化" if previous
                      else "来源已导入；资金性质、消费分类和跨平台重复尚未确认",
                      {"original_category":record.category,"original_status":record.status,
                       "source_event_tag":record.event_tag,
                       "identity_method":"external_event_id" if record.transaction_id else "source_fingerprint"},
                      revision)
            if statement.validation.get("warnings") and first_raw:
                db.explain("保留来源校验警告，未将有差异的合计标为校验通过",
                           [{"raw_record_id":first_raw,"warnings":statement.validation["warnings"]}])
                issue(db,None,first_raw,"parsing","来源校验存在警告，请核对本地导入报告",
                      statement.validation,revision=None)
            report["files"].append(file_result)
    return report


def statistics(db):
    connection = db.connection
    counts = {name:connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
              for name in ("accounts","raw_records","categories","bills","bill_sources","bill_relations",
                           "review_items","change_batches","updates")}
    by_provider = [dict(r) for r in connection.execute("""
        SELECT a.provider,COUNT(*) raw_records,
            MIN(date(r.occurred_at,'+8 hours')) first_local_date,
            MAX(date(r.occurred_at,'+8 hours')) last_local_date
        FROM raw_records r JOIN accounts a ON a.id = r.account_id
        GROUP BY a.provider ORDER BY a.provider
    """)]
    return {"counts":counts,"by_provider":by_provider,
            "confirmed_personal_expenses":connection.execute("SELECT COUNT(*) FROM personal_expenses").fetchone()[0],
            "pending_bills":connection.execute("SELECT COUNT(*) FROM bills WHERE review_status = 'pending' AND deleted_at IS NULL").fetchone()[0],
            "january_to_september_source_records":connection.execute("""
                SELECT COUNT(*) FROM raw_records
                WHERE date(occurred_at,'+8 hours') >= '2026-01-01'
                  AND date(occurred_at,'+8 hours') < '2026-10-01'
            """).fetchone()[0],
            "foreign_key_check_passed":not bool(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "integrity_check":connection.execute("PRAGMA integrity_check").fetchone()[0]}


def discover(directory):
    paths = sorted(p for p in Path(directory).iterdir() if p.is_file() and p.suffix.lower() in (".csv",".xlsx",".pdf"))
    if not paths:
        raise ValueError("No CSV, XLSX, or PDF statements found")
    return paths


def run_import(directory, database, profile="personal"):
    statements = [parse_statement(path,profile) for path in discover(directory)]
    with_database = Database(database)
    try:
        report = import_statements(with_database,statements)
        report["database"] = str(Path(database).resolve())
        report["statistics"] = statistics(with_database)
        return report
    finally:
        with_database.close()
