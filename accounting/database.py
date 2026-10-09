from contextlib import contextmanager
from datetime import date, datetime, timezone
from importlib.resources import files
from pathlib import Path
import json
import sqlite3
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class Database:
    audited_tables = ("accounts", "categories", "bills", "bill_sources", "bill_relations", "review_items")

    def __init__(self, path, readonly=False):
        self.context = {}
        self.readonly = readonly
        if readonly:
            uri = Path(path).resolve().as_uri() + "?mode=ro"
            self.connection = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.connection = sqlite3.connect(path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        if self.connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Foreign key enforcement is unavailable")
        for name, key in (
            ("audit_batch_id", "batch_id"), ("audit_reason", "reason"),
            ("audit_evidence_json", "evidence"), ("audit_actor_type", "actor_type"),
        ):
            self.connection.create_function(name, 0, lambda key=key: self.context.get(key))
        self.connection.create_function("valid_usage_date", 1, self.valid_usage_date)
        self.connection.create_function("valid_utc_time", 1, self.valid_utc_time)
        version = self.connection.execute("PRAGMA user_version").fetchone()[0]
        if version == 0 and not readonly:
            self.migrate()
        elif version != 1:
            raise RuntimeError(f"Unsupported database schema version: {version}")

    @staticmethod
    def valid_usage_date(value):
        if value is None:
            return 1
        try:
            return int(date.fromisoformat(value).isoformat() == value)
        except (ValueError, TypeError):
            return 0

    @staticmethod
    def valid_utc_time(value):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return int(value.endswith("Z") and parsed.utcoffset().total_seconds() == 0)
        except (ValueError, TypeError, AttributeError):
            return 0

    def migrate(self):
        if sqlite3.sqlite_version_info < (3, 38, 0):
            raise RuntimeError("SQLite >= 3.38.0 is required")
        self.connection.execute("SELECT json_valid('{}')")
        schema = files("accounting").joinpath("schema.sql").read_text()
        self.connection.executescript("BEGIN IMMEDIATE;\n" + schema)
        try:
            for table in self.audited_tables:
                self.create_audit_triggers(table)
            self.create_business_triggers()
            self.connection.execute("PRAGMA user_version = 1")
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def create_audit_triggers(self, table):
        columns = [r["name"] for r in self.connection.execute(f"PRAGMA table_info({table})")]
        business = [c for c in columns if c not in ("updated_at", "revision")]
        changed = " OR ".join(f"OLD.{c} IS NOT NEW.{c}" for c in business)
        snapshots = {}
        for prefix in ("OLD", "NEW"):
            fields = [f"'{c}', {prefix}.{c}" for c in columns]
            if table == "bills":
                fields.append(f"""'category_path', (SELECT CASE WHEN p.id IS NULL THEN c.name
                    ELSE p.name || '/' || c.name END FROM categories c
                    LEFT JOIN categories p ON p.id = c.parent_id WHERE c.id = {prefix}.category_id)""")
            snapshots[prefix] = "json_object(" + ", ".join(fields) + ")"
        for operation in ("INSERT", "UPDATE", "DELETE"):
            condition = f" WHEN {changed}" if operation == "UPDATE" else ""
            before = snapshots["OLD"] if operation != "INSERT" else "NULL"
            after = snapshots["NEW"] if operation != "DELETE" else "NULL"
            row = "OLD" if operation == "DELETE" else "NEW"
            self.connection.execute(f"""
                CREATE TRIGGER audit_guard_{table}_{operation}
                BEFORE {operation} ON {table}{condition}
                BEGIN
                    SELECT CASE WHEN audit_batch_id() IS NULL
                        OR NOT EXISTS(SELECT 1 FROM change_batches WHERE id = audit_batch_id())
                        OR audit_reason() IS NULL OR length(trim(audit_reason())) = 0
                        OR audit_evidence_json() IS NULL OR NOT json_valid(audit_evidence_json())
                        THEN RAISE(ABORT, 'missing audit context') END;
                END""")
            self.connection.execute(f"""
                CREATE TRIGGER audit_log_{table}_{operation}
                AFTER {operation} ON {table}{condition}
                BEGIN
                    INSERT INTO updates(batch_id, entity_table, entity_id, operation,
                        before_json, after_json, reason, evidence_json, created_at)
                    VALUES(audit_batch_id(), '{table}', {row}.id, '{operation.lower()}',
                        {before}, {after}, audit_reason(), audit_evidence_json(),
                        strftime('%Y-%m-%dT%H:%M:%fZ','now'));
                END""")
            if operation == "UPDATE" and "revision" in columns:
                self.connection.execute(f"""
                    CREATE TRIGGER revision_{table} BEFORE UPDATE ON {table}
                    WHEN ({changed}) AND NEW.revision != OLD.revision + 1
                    BEGIN SELECT RAISE(ABORT, 'revision must increase by one'); END""")

    def create_business_triggers(self):
        for table in ("raw_records", "change_batches", "updates"):
            for operation in ("UPDATE", "DELETE"):
                self.connection.execute(f"""
                    CREATE TRIGGER immutable_{table}_{operation} BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'immutable record'); END""")
        self.connection.execute("""
            CREATE TRIGGER raw_import_context BEFORE INSERT ON raw_records
            WHEN audit_batch_id() IS NULL OR NEW.import_batch_id IS NOT audit_batch_id()
            BEGIN SELECT RAISE(ABORT, 'missing import audit context'); END""")
        self.connection.execute("""
            CREATE TRIGGER updates_context BEFORE INSERT ON updates
            WHEN audit_batch_id() IS NULL OR NEW.batch_id IS NOT audit_batch_id()
            BEGIN SELECT RAISE(ABORT, 'missing update audit context'); END""")
        for operation in ("INSERT", "UPDATE"):
            self.connection.execute(f"""
                CREATE TRIGGER category_tree_{operation} BEFORE {operation} ON categories
                WHEN NEW.parent_id IS NOT NULL AND (
                    NOT EXISTS(SELECT 1 FROM categories WHERE id = NEW.parent_id AND parent_id IS NULL)
                    OR EXISTS(SELECT 1 FROM categories WHERE parent_id = NEW.id))
                BEGIN SELECT RAISE(ABORT, 'category tree must have at most two levels'); END""")
            self.connection.execute(f"""
                CREATE TRIGGER bill_dates_{operation} BEFORE {operation} ON bills
                WHEN NOT valid_usage_date(NEW.usage_date) OR NOT valid_utc_time(NEW.paid_at)
                BEGIN SELECT RAISE(ABORT, 'invalid bill date'); END""")
            active_condition = "" if operation == "INSERT" else " AND NEW.category_id IS NOT OLD.category_id"
            self.connection.execute(f"""
                CREATE TRIGGER bill_category_{operation} BEFORE {operation} ON bills
                WHEN NEW.category_id IS NOT NULL{active_condition}
                    AND NOT EXISTS(SELECT 1 FROM categories WHERE id = NEW.category_id AND is_active = 1)
                BEGIN SELECT RAISE(ABORT, 'category is inactive or missing'); END""")
            self.connection.execute(f"""
                CREATE TRIGGER source_identity_{operation} BEFORE {operation} ON bill_sources
                WHEN EXISTS(
                    SELECT 1 FROM raw_records n JOIN raw_records r
                      ON r.account_id = n.account_id AND r.source_event_key = n.source_event_key
                    JOIN bill_sources s ON s.raw_record_id = r.id
                    WHERE n.id = NEW.raw_record_id AND s.bill_id != NEW.bill_id AND s.id != NEW.id)
                BEGIN SELECT RAISE(ABORT, 'source event already belongs to another bill'); END""")
            self.connection.execute(f"""
                CREATE TRIGGER bill_merge_cycle_{operation} BEFORE {operation} ON bills
                WHEN NEW.merged_into_bill_id IS NOT NULL AND EXISTS(
                    WITH RECURSIVE chain(id) AS (
                        SELECT NEW.merged_into_bill_id UNION
                        SELECT b.merged_into_bill_id FROM bills b JOIN chain ON b.id = chain.id
                        WHERE b.merged_into_bill_id IS NOT NULL)
                    SELECT 1 FROM chain WHERE id = NEW.id)
                BEGIN SELECT RAISE(ABORT, 'bill merge cycle'); END""")
        self.connection.execute("""
            CREATE TRIGGER protect_classification BEFORE UPDATE ON bills
            WHEN audit_actor_type() != 'user' AND OLD.classification_locked = 1 AND (
                NEW.kind IS NOT OLD.kind OR NEW.category_id IS NOT OLD.category_id
                OR NEW.personal_base_cents IS NOT OLD.personal_base_cents
                OR NEW.classification_locked IS NOT OLD.classification_locked)
            BEGIN SELECT RAISE(ABORT, 'human classification is locked'); END""")
        for operation in ("INSERT", "UPDATE"):
            change = "NEW.usage_date IS NOT NULL" if operation == "INSERT" else "NEW.usage_date IS NOT OLD.usage_date"
            self.connection.execute(f"""
                CREATE TRIGGER protect_usage_{operation} BEFORE {operation} ON bills
                WHEN audit_actor_type() != 'user' AND ({change})
                BEGIN SELECT RAISE(ABORT, 'usage date is user supplied'); END""")
        for table, operations in (("bill_relations", ("INSERT","UPDATE","DELETE")), ("bills", ("UPDATE",))):
            for operation in operations:
                self.connection.execute(f"""
                    CREATE TRIGGER relations_valid_{table}_{operation} AFTER {operation} ON {table}
                    WHEN EXISTS(SELECT 1 FROM invalid_bill_relations)
                    BEGIN SELECT RAISE(ABORT, 'invalid or excessive bill allocation'); END""")

    @contextmanager
    def batch(self, actor_type, actor_label, reason, evidence=None):
        if self.readonly or self.context or self.connection.in_transaction:
            raise RuntimeError("A writable, non-nested transaction is required")
        identifier = str(uuid4())
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "INSERT INTO change_batches(id,actor_type,actor_label,reason,created_at) VALUES(?,?,?,?,?)",
                (identifier,actor_type,actor_label,reason,now()))
            self.context = {"batch_id":identifier,"actor_type":actor_type,"reason":reason,
                            "evidence":json_text(evidence or [])}
            yield identifier
            self.validate()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self.context = {}

    def explain(self, reason, evidence):
        if not self.context:
            raise RuntimeError("No audit batch")
        self.context.update(reason=reason, evidence=json_text(evidence))

    def validate(self):
        if self.connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RuntimeError("Foreign key check failed")
        if self.connection.execute("SELECT 1 FROM invalid_bill_relations LIMIT 1").fetchone():
            raise RuntimeError("Invalid bill relation")
        if self.connection.execute("""
            SELECT b.id FROM bills b LEFT JOIN bill_sources s ON s.bill_id = b.id AND s.is_primary = 1
            WHERE b.entry_origin = 'imported' AND b.deleted_at IS NULL AND s.id IS NULL LIMIT 1
        """).fetchone():
            raise RuntimeError("Imported bill has no primary source")

    def close(self):
        self.connection.close()
