from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import json
import re
import secrets
import sqlite3

from .database import Database, json_text, now
from .ingest import insert


KINDS = {"pending", "expense", "income", "refund", "reimbursement", "internal_transfer", "debt_repayment", "investment"}
PROVIDERS = {"alipay", "wechat", "meituan", "icbc", "manual"}

# A decision can leave every bill field unchanged, so edits alone do not prove an AI review.
REVIEWED_BILLS = """
    WITH ai_checks AS (
        SELECT DISTINCT json_extract(u.after_json, '$.bill_id') AS bill_id
        FROM updates u JOIN change_batches b ON b.id = u.batch_id
        WHERE b.actor_type = 'ai' AND u.entity_table = 'review_items' AND u.operation = 'insert'
          AND json_extract(u.after_json, '$.topic') IN ('nature', 'category')
    ), reviewed_bills AS (
        SELECT d.*, a.bill_id IS NOT NULL AS ai_reviewed
        FROM bill_details d LEFT JOIN ai_checks a ON a.bill_id = d.id
    )
"""


def bill_json(row):
    return {**dict(row), "ai_reviewed": bool(row["ai_reviewed"])}


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise APIError(400, f"{label}不合法")
    return value


def text(value, maximum, label, nullable=False):
    if nullable and value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise APIError(400, f"{label}不合法")
    return value.strip() or None


def category_rows(connection):
    return [dict(row) for row in connection.execute("""
        SELECT c.*, p.name parent_name FROM categories c
        LEFT JOIN categories p ON p.id = c.parent_id
        WHERE c.is_active = 1 ORDER BY c.sort_order, c.name
    """)]


def filters(parameters):
    start = parameters.get("start", "2026-04-01")
    end = parameters.get("end", "2026-09-30")
    if not start or not end or not Database.valid_usage_date(start) or not Database.valid_usage_date(end) or start > end:
        raise APIError(400, "请选择有效的起止日期")
    clauses = ["d.deleted_at IS NULL", "date(d.paid_at,'+8 hours') BETWEEN ? AND ?"]
    values = [start, end]
    provider = parameters.get("provider", "all")
    if provider != "all":
        if provider not in PROVIDERS:
            raise APIError(400, "来源平台不合法")
        clauses.append("d.source_provider = ?")
        values.append(provider)
    kind = parameters.get("kind", "all")
    if kind != "all":
        if kind not in KINDS:
            raise APIError(400, "资金性质不合法")
        clauses.append("d.kind = ?")
        values.append(kind)
    query = parameters.get("q", "").strip()
    if len(query) > 200:
        raise APIError(400, "搜索内容过长")
    if query:
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append("(d.description LIKE ? ESCAPE '\\' OR d.counterparty LIKE ? ESCAPE '\\' OR d.transaction_id LIKE ? ESCAPE '\\')")
        values.extend(["%" + escaped + "%"] * 3)
    return clauses, values


def list_bills(connection, parameters):
    clauses, values = filters(parameters)
    base = " AND ".join(clauses)
    summary = dict(connection.execute(REVIEWED_BILLS + f"""
        SELECT COUNT(*) total,
            COALESCE(SUM(review_status = 'pending'),0) pending,
            COALESCE(SUM(review_status = 'pending' AND ai_reviewed = 1),0) ai_reviewed_pending,
            COALESCE(SUM(review_status = 'pending' AND ai_reviewed = 0),0) ai_unreviewed_pending,
            COALESCE(SUM(review_status != 'pending'),0) confirmed,
            COALESCE(SUM(category_id IS NOT NULL),0) categorized
        FROM reviewed_bills d WHERE {base}
    """, values).fetchone())
    status = parameters.get("status", "all")
    if status == "pending":
        clauses.append("d.review_status = 'pending'")
    elif status in {"ai_reviewed_pending", "ai_unreviewed_pending"}:
        clauses.append("d.review_status = 'pending'")
        clauses.append("d.ai_reviewed = ?")
        values.append(int(status == "ai_reviewed_pending"))
    elif status == "confirmed":
        clauses.append("d.review_status != 'pending'")
    elif status != "all":
        raise APIError(400, "核对状态不合法")
    try:
        page = integer(int(parameters.get("page", "1")), 1, 100000, "页码")
    except ValueError as error:
        raise APIError(400, "页码不合法") from error
    where = " AND ".join(clauses)
    total = connection.execute(REVIEWED_BILLS + f"SELECT COUNT(*) FROM reviewed_bills d WHERE {where}", values).fetchone()[0]
    rows = connection.execute(REVIEWED_BILLS + f"""
        SELECT d.*, c.name category_name, p.name parent_category_name
        FROM reviewed_bills d LEFT JOIN categories c ON c.id = d.category_id
        LEFT JOIN categories p ON p.id = c.parent_id
        WHERE {where} ORDER BY d.paid_at DESC, d.id DESC LIMIT 50 OFFSET ?
    """, [*values, (page - 1) * 50]).fetchall()
    return {"items": [bill_json(row) for row in rows], "total": total, "page": page,
            "page_size": 50, "summary": summary}


def history_rows(connection, bill_id=None):
    where, values = ("u.entity_table = 'bills' AND u.entity_id = ?", [bill_id]) if bill_id else ("b.actor_type != 'importer'", [])
    rows = connection.execute(f"""
        SELECT u.*, b.actor_type, b.actor_label FROM updates u
        JOIN change_batches b ON b.id = u.batch_id
        WHERE {where} ORDER BY u.id DESC LIMIT 100
    """, values).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        for field in ("before_json", "after_json", "evidence_json"):
            item[field.removesuffix("_json")] = json.loads(item.pop(field)) if item[field] is not None else None
        result.append(item)
    return result


def bill_detail(connection, bill_id):
    row = connection.execute(REVIEWED_BILLS + "SELECT * FROM reviewed_bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
    if row is None:
        raise APIError(404, "找不到这笔记录")
    sources = []
    for source in connection.execute("""
        SELECT r.*, a.provider, s.role, s.is_primary FROM bill_sources s
        JOIN raw_records r ON r.id = s.raw_record_id
        JOIN accounts a ON a.id = r.account_id WHERE s.bill_id = ?
        ORDER BY s.is_primary DESC, r.created_at
    """, (bill_id,)):
        item = dict(source)
        item["payload"] = json.loads(item.pop("raw_payload_json"))
        item["locator"] = json.loads(item.pop("source_locator_json"))
        sources.append(item)
    return {"bill": bill_json(row), "sources": sources,
            "history": history_rows(connection, bill_id),
            "review_items": [dict(r) for r in connection.execute(
                "SELECT * FROM review_items WHERE bill_id = ? AND status = 'open'", (bill_id,))]}


def update_bill(db, bill_id, payload):
    allowed = {"revision", "kind", "direction", "payment_status", "category_id", "personal_base_cents",
               "usage_date", "notes", "inclusion_status", "exclusion_reason", "confirm"}
    if set(payload) - allowed:
        raise APIError(400, "包含不支持的修改字段")
    revision = integer(payload.get("revision"), 1, 2**53 - 1, "记录版本")
    confirm = payload.get("confirm", False)
    if type(confirm) is not bool:
        raise APIError(400, "确认标记不合法")
    with db.batch("user", "local-review-ui", "在本地界面核对账单", [{"bill_id": bill_id}] ) as batch_id:
        old = db.connection.execute("SELECT * FROM bills WHERE id = ? AND deleted_at IS NULL", (bill_id,)).fetchone()
        if old is None:
            raise APIError(404, "找不到这笔记录")
        if old["revision"] != revision:
            raise APIError(409, "这笔记录已被修改，请重新加载后核对；本次修改未保存")
        values = {key: payload.get(key, old[key]) for key in allowed - {"revision", "confirm"}}
        if values["kind"] not in KINDS or values["direction"] not in {"outflow", "inflow", "none"}:
            raise APIError(400, "资金性质或方向不合法")
        if values["payment_status"] not in {"settled", "pending", "voided"} or values["inclusion_status"] not in {"included", "excluded"}:
            raise APIError(400, "支付或统计状态不合法")
        for field, maximum in (("notes", 2000), ("exclusion_reason", 500), ("category_id", 100), ("usage_date", 10)):
            values[field] = text(values[field], maximum, field, nullable=True)
        if not Database.valid_usage_date(values["usage_date"]):
            raise APIError(400, "使用日期不是有效的日历日期")
        if values["kind"] == "expense":
            if values["personal_base_cents"] is not None:
                integer(values["personal_base_cents"], 0, old["amount_cents"], "本人承担金额")
        elif any(values[field] is not None for field in ("category_id", "personal_base_cents", "usage_date")):
            raise APIError(400, "非消费记录不填写消费分类、本人承担金额或使用日期")
        if values["inclusion_status"] == "included":
            values["exclusion_reason"] = None
        elif not values["exclusion_reason"]:
            raise APIError(400, "排除记录时请填写原因")
        if confirm and values["kind"] == "pending":
            raise APIError(400, "确认前请先选择资金性质")
        review_status = "user_confirmed" if confirm or old["review_status"] != "pending" else "pending"
        if values["kind"] == "pending":
            review_status = "pending"
        if review_status != "pending" and values["kind"] == "expense" and (
            not values["category_id"] or values["personal_base_cents"] is None or values["direction"] != "outflow"
        ):
            raise APIError(400, "确认消费前请填写分类、本人承担金额，并将方向设为支出")
        values.update(review_status=review_status, classification_locked=int(review_status == "user_confirmed"))
        changed = {key: value for key, value in values.items() if value != old[key]}
        if changed:
            db.explain("本人在界面确认账单" if confirm else "本人在界面修改账单",
                       [{"bill_id": bill_id, "previous_revision": revision, "fields": sorted(changed)}])
            assignments = ",".join(f"{key} = ?" for key in changed)
            db.connection.execute(f"UPDATE bills SET {assignments}, updated_at = ?, revision = revision + 1 WHERE id = ?",
                                  [*changed.values(), now(), bill_id])
        if confirm:
            db.connection.execute("""
                UPDATE review_items SET status = 'resolved', resolved_batch_id = ?, updated_at = ?,
                    revision = revision + 1
                WHERE bill_id = ? AND status = 'open' AND topic IN ('nature','category','personal_share')
            """, (batch_id, now(), bill_id))
    return bill_detail(db.connection, bill_id)


def create_category(db, payload):
    if set(payload) - {"name", "parent_id"}:
        raise APIError(400, "包含不支持的分类字段")
    name = text(payload.get("name"), 40, "分类名称")
    parent_id = text(payload.get("parent_id"), 100, "上级分类", nullable=True)
    if not name:
        raise APIError(400, "请填写分类名称")
    identifier, timestamp = str(uuid4()), now()
    with db.batch("user", "local-review-ui", "本人添加消费分类", [{"name": name, "parent_id": parent_id}]):
        if parent_id and not db.connection.execute(
            "SELECT 1 FROM categories WHERE id = ? AND parent_id IS NULL AND is_active = 1", (parent_id,)
        ).fetchone():
            raise APIError(400, "请选择有效的大类")
        insert(db.connection, "categories", {"id": identifier, "parent_id": parent_id, "name": name,
                                             "created_at": timestamp, "updated_at": timestamp})
    return {"id": identifier, "categories": category_rows(db.connection)}


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, database_path, port):
        self.database_path = Path(database_path).resolve()
        self.csrf_token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), Handler)

    @property
    def allowed_hosts(self):
        return {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalAccounting"

    def log_message(self, format, *args):
        pass

    def respond(self, status, data, content_type="application/json; charset=utf-8"):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; font-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, write=False):
        db = None
        try:
            if self.headers.get("Host") not in self.server.allowed_hosts:
                raise APIError(403, "仅允许本机访问")
            origin = self.headers.get("Origin")
            if origin is not None and origin not in {"http://" + host for host in self.server.allowed_hosts}:
                raise APIError(403, "拒绝跨站请求")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                raise APIError(403, "拒绝跨站请求")
            path = urlsplit(self.path).path
            if not write and (path == "/" or path.startswith("/assets/")):
                root = files("accounting").joinpath("static", "dist")
                if path == "/":
                    asset, mime = root.joinpath("index.html"), "text/html"
                    if not asset.is_file():
                        raise APIError(503, "前端尚未构建，请先运行 make build-ui")
                else:
                    match = re.fullmatch(r"/assets/([A-Za-z0-9_-]+\.(js|css))", path)
                    if match is None:
                        raise APIError(404, "静态资源不存在")
                    asset = root.joinpath("assets", match[1])
                    mime = {"js": "text/javascript", "css": "text/css"}[match[2]]
                    if not asset.is_file():
                        raise APIError(404, "静态资源不存在")
                return self.respond(200, asset.read_bytes(), mime + "; charset=utf-8")
            if not write and path == "/api/session":
                return self.respond(200, {"csrf_token": self.server.csrf_token, "database": self.server.database_path.name})
            payload = None
            if write:
                if not secrets.compare_digest(self.headers.get("X-Accounting-CSRF", ""), self.server.csrf_token):
                    raise APIError(403, "安全校验失效，请刷新页面")
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise APIError(415, "只接受 JSON 请求")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 16384:
                        raise ValueError()
                    payload = json.loads(self.rfile.read(length))
                except (ValueError, UnicodeDecodeError) as error:
                    raise APIError(400, "请求内容不合法或过长") from error
                if not isinstance(payload, dict):
                    raise APIError(400, "请求必须是 JSON 对象")
            db = Database(self.server.database_path, readonly=not write)
            parameters = {key: values[-1] for key, values in parse_qs(urlsplit(self.path).query, max_num_fields=20).items()}
            if not write and path == "/api/bills":
                result = list_bills(db.connection, parameters)
            elif not write and path == "/api/categories":
                result = {"categories": category_rows(db.connection)}
            elif not write and path == "/api/history":
                result = {"items": history_rows(db.connection)}
            elif path.startswith("/api/bills/") and "/" not in path.removeprefix("/api/bills/"):
                bill_id = path.removeprefix("/api/bills/")
                result = update_bill(db, bill_id, payload) if write else bill_detail(db.connection, bill_id)
            elif write and path == "/api/categories":
                result = create_category(db, payload)
            else:
                raise APIError(404, "页面或接口不存在")
            self.respond(200, result)
        except APIError as error:
            self.respond(error.status, {"error": str(error)})
        except sqlite3.IntegrityError:
            self.respond(422, {"error": "修改不符合数据库约束：请检查分类是否重复或为“其他”、资金方向，以及已有退款或垫付关联"})
        except (ValueError, TypeError):
            self.respond(400, {"error": "请求参数不合法"})
        except (sqlite3.Error, OSError, RuntimeError):
            self.respond(500, {"error": "本地数据库暂时无法访问，修改未确认保存，请重新加载检查"})
        finally:
            if db is not None:
                db.close()

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(write=True)


def backup_database(path):
    path = Path(path).resolve()
    destination = path.parent / "backups"
    destination.mkdir(exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = destination / f"before-ui-{timestamp}-{uuid4().hex[:8]}.sqlite3"
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        backup = sqlite3.connect(target)
        try:
            source.backup(backup)
        finally:
            backup.close()
    finally:
        source.close()
    return target


def serve(database_path, port=8765):
    integer(port, 1, 65535, "端口")
    database = Database(database_path, readonly=True)
    database.close()
    server = LocalServer(database_path, port)
    try:
        backup = backup_database(database_path)
        print(f"生活账本：http://127.0.0.1:{server.server_port}", flush=True)
        print(f"仅本机访问；启动前备份：{backup}", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
