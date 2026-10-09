from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from pathlib import Path
import csv
import io
import re

from .database import json_text

LOCAL_TIME = timezone(timedelta(hours=8))
PARSER_VERSION = "1"


@dataclass
class Record:
    provider: str
    account_ref: str
    payload: dict
    locator: dict
    transaction_id: str | None
    merchant_order_id: str | None
    occurred_at: str
    precision: str
    amount_cents: int
    direction: str
    category: str | None
    description: str | None
    counterparty: str | None
    status: str | None
    event_tag: str
    currency: str = "CNY"
    event_key: str = ""


@dataclass
class Statement:
    path: Path
    file_sha256: str
    provider: str
    records: list[Record]
    validation: dict = field(default_factory=dict)


def clean(value):
    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
    return str(value).strip()


def identifier(value):
    if value is None or clean(value) in ("", "/", "-", "--"):
        return None
    if not isinstance(value, str):
        raise ValueError("Transaction identifiers must be text, not Excel numeric values")
    result = value.strip().removeprefix("'")
    if re.fullmatch(r"\d+(?:\.\d+)?[Ee][+-]?\d+", result):
        raise ValueError("Scientific-notation identifier cannot be trusted")
    return result


def cents(value):
    text = clean(value).replace(",", "").replace(" ", "")
    text = text.removeprefix("人民币").removeprefix("CNY").removeprefix("¥").removeprefix("￥")
    try:
        amount = Decimal(text)
    except InvalidOperation as error:
        raise ValueError("Invalid money value") from error
    if not amount.is_finite() or amount * 100 != (amount * 100).to_integral_value():
        raise ValueError("Money must be finite and have at most two decimal places")
    result = int(amount * 100)
    if abs(result) > 2**63 - 1:
        raise ValueError("Money exceeds SQLite integer range")
    return result


def utc_time(value):
    text = clean(value)
    if isinstance(value, datetime):
        parsed = value
        precision = "second"
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
        precision = "day"
    else:
        parsed = None
        for pattern, precision in (
            ("%Y-%m-%d %H:%M:%S", "second"),
            ("%Y-%m-%d %H:%M", "minute"),
            ("%Y-%m-%dT%H:%M:%S", "second"),
            ("%m/%d/%y %H:%M", "minute"),
            ("%Y-%m-%d", "day"),
        ):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError("Unrecognized statement date")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TIME)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), precision


def direction(value):
    result = {"支出":"outflow", "收入":"inflow", "不计收支":"none", "/":"none"}.get(clean(value))
    if result is None:
        raise ValueError("Unknown source direction")
    return result


def payload_values(values):
    return [v.isoformat() if isinstance(v, (date, datetime)) else v for v in values]


def event_tag(row, flow):
    source_type = clean(row.get("交易类型"))
    status = clean(row.get("交易状态", row.get("当前状态")))
    if "退款" in source_type or clean(row.get("交易分类")) == "退款" or status == "退款成功":
        return "refund"
    return flow


def platform_record(provider, account_ref, headers, values, locator, cell_types=None):
    row = dict(zip(headers, values))
    flow = direction(row["收/支"])
    if provider == "meituan":
        raw_time = row.get("交易成功时间") or row["交易创建时间"]
        amount, description = row["实付金额"], row["订单标题"]
        transaction, merchant = row["交易单号"], row["商家单号"]
        counterparty, category = None, None
        status = None
    elif provider == "alipay":
        raw_time, amount, description = row["交易时间"], row["金额"], row["商品说明"]
        transaction, merchant = row["交易订单号"], row["商家订单号"]
        counterparty, category, status = row["交易对方"], row["交易分类"], row["交易状态"]
    else:
        raw_time, amount, description = row["交易时间"], row["金额(元)"], row["商品"]
        transaction, merchant = row["交易单号"], row["商户单号"]
        counterparty, category, status = row["交易对方"], None, row["当前状态"]
    occurred_at, precision = utc_time(raw_time)
    payload = {"headers":headers,"values":payload_values(values)}
    if cell_types is not None:
        payload["cell_types"] = cell_types
    return Record(provider, account_ref, payload, locator, identifier(transaction),
                  identifier(merchant), occurred_at, precision, abs(cents(amount)), flow,
                  clean(category) or None, clean(description) or None,
                  clean(counterparty) or None, clean(status) or None, event_tag(row, flow))


def decode_csv(blob):
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return blob.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("CSV is neither UTF-8 nor GB18030")


def parse_csv(path, blob, profile):
    text, encoding = decode_csv(blob)
    reader = csv.reader(io.StringIO(text))
    preamble, records = [], []
    headers = None
    provider = None
    account_ref = "profile:" + profile
    last_line = 0
    for values in reader:
        start_line, last_line = last_line + 1, reader.line_num
        if not any(clean(v) for v in values):
            continue
        if headers is None:
            candidate = [clean(v) for v in values]
            if candidate[0] == "交易时间" and "交易订单号" in candidate:
                headers, provider = candidate, "alipay"
            elif candidate[0] == "交易创建时间" and "实付金额" in candidate:
                headers, provider = candidate, "meituan"
            else:
                preamble.append(values)
                match = re.search(r"支付宝账户[：:]\s*(.+)", ",".join(values))
                if match:
                    account_ref = match.group(1).strip()
            continue
        if len(values) != len(headers):
            raise ValueError(f"Unexpected CSV column count at line {start_line}")
        records.append(platform_record(provider, account_ref, headers, values,
                                       {"line_start":start_line,"line_end":last_line}))
    if headers is None or not records:
        raise ValueError("Missing supported CSV header or transactions")
    validation = {"encoding":encoding,"source_rows":len(records),"warnings":[]}
    if provider == "alipay":
        source = "\n".join(",".join(r) for r in preamble)
        declared = re.search(r"共\s*(\d+)\s*笔记录", source)
        if declared and int(declared.group(1)) != len(records):
            raise ValueError("Alipay source record count does not match")
        validation["declared_count_matches"] = bool(declared)
        controls = {}
        successful_refunds = sum(r.amount_cents for r in records if r.status == "退款成功")
        for label, flow in (("收入","inflow"), ("支出","outflow"), ("不计收支","none")):
            match = re.search(label + r"[：:]\s*(\d+)笔\s*([\d,.]+)元", source)
            if match:
                group = [r for r in records if r.direction == flow]
                if len(group) != int(match.group(1)):
                    raise ValueError("Alipay source direction count does not match")
                actual, expected = sum(r.amount_cents for r in group), cents(match.group(2))
                controls[flow] = {"rows":len(group),"computed_cents":actual,
                                  "declared_cents":expected,"amount_matches":actual == expected}
                if actual != expected:
                    if flow == "outflow" and actual - successful_refunds == expected:
                        controls[flow]["net_of_successful_refunds_cents"] = actual - successful_refunds
                        controls[flow]["net_of_successful_refunds_matches"] = True
                        validation.setdefault("notes", []).append(
                            "Declared outflow reconciles with gross outflow minus successful refunds; refund links are not yet verified")
                    else:
                        validation["warnings"].append("Declared " + label + " total differs from detail rows")
        validation["direction_controls"] = controls
    return Statement(path, sha256(blob).hexdigest(), provider, records, validation)


def parse_xlsx(path, blob, profile):
    import openpyxl
    workbook = openpyxl.load_workbook(io.BytesIO(blob), read_only=True, data_only=False)
    records = []
    try:
        for sheet in workbook:
            iterator = sheet.iter_rows()
            first = next(iterator, None)
            if first is None:
                continue
            headers = [clean(c.value) for c in first]
            required = {"交易时间","交易类型","交易对方","商品","收/支","金额(元)","支付方式","当前状态","交易单号","商户单号","备注"}
            if not required.issubset(headers):
                raise ValueError("Unrecognized WeChat sheet header")
            for row_number, cells in enumerate(iterator, 2):
                values = [c.value for c in cells]
                if not any(v is not None and clean(v) for v in values):
                    continue
                if any(c.data_type == "f" for c in cells):
                    raise ValueError("Formula cells are not accepted as statement facts")
                records.append(platform_record("wechat","profile:" + profile,headers,values,
                                               {"sheet":sheet.title,"row":row_number},
                                               [c.data_type for c in cells]))
    finally:
        workbook.close()
    if not records:
        raise ValueError("WeChat workbook has no transactions")
    return Statement(path,sha256(blob).hexdigest(),"wechat",records,{"source_rows":len(records),"warnings":[]})


def bank_page_records(table, footer, page_number):
    if not table or len(table[0]) != 13 or clean(table[0][0]) != "交易日期":
        raise ValueError("Unrecognized bank PDF table")
    records, balances = [], []
    for row_number, values in enumerate(table[1:], 1):
        if len(values) != 13 or any(v is None for v in values[:10]):
            raise ValueError("Incomplete bank table row")
        text = [clean(v).replace("\n", "") for v in values]
        occurred_at, precision = utc_time(clean(values[0]).replace("\n"," "))
        amount = cents(text[8])
        currency = "CNY" if text[4] in ("人民币","CNY") else text[4]
        if currency != "CNY":
            raise ValueError("Non-CNY bank balances require a separate currency parser")
        flow = "outflow" if amount < 0 else "inflow" if amount > 0 else "none"
        record = Record(
            "icbc",text[1],{"headers":table[0],"values":values},
            {"page":page_number,"row":row_number},None,None,occurred_at,precision,
            abs(amount),flow,None,text[6],text[10] or None,None,
            "refund" if text[6] == "退款" else flow,currency)
        records.append(record)
        balances.append((amount,cents(text[9])))
    matches = {
        "count":re.search(r"本页交易笔数[：:]\s*(\d+)",footer),
        "out":re.search(r"本页支出算术合计[：:]\s*([\d,]+\.\d+)",footer),
        "in":re.search(r"本页收入算术合计[：:]\s*([\d,]+\.\d+)",footer),
    }
    if not all(matches.values()):
        raise ValueError("Bank PDF footer controls are missing")
    computed_out = sum(-amount for amount,_ in balances if amount < 0)
    computed_in = sum(amount for amount,_ in balances if amount > 0)
    if len(records) != int(matches["count"].group(1)):
        raise ValueError("Bank PDF page record count does not match")
    if computed_out != cents(matches["out"].group(1)) or computed_in != cents(matches["in"].group(1)):
        raise ValueError("Bank PDF page amount totals do not match")
    return records, balances, {"page":page_number,"rows":len(records),"count_matches":True,
                               "outgoing_matches":True,"incoming_matches":True,
                               "outgoing_cents":computed_out,"incoming_cents":computed_in}


def parse_pdf(path, blob, profile):
    import pdfplumber
    records, ledger, controls = [], [], []
    with pdfplumber.open(io.BytesIO(blob)) as pdf:
        for number,page in enumerate(pdf.pages,1):
            # Large diagonal watermark glyphs otherwise contaminate numeric cells.
            clean_page = page.filter(lambda obj: obj.get("object_type") != "char" or obj.get("size",0) < 8)
            tables = clean_page.extract_tables()
            if len(tables) != 1:
                raise ValueError("Bank PDF page must have one recognized table")
            page_records, balances, control = bank_page_records(tables[0], clean_page.extract_text() or "", number)
            records.extend(page_records)
            ledger.extend((r.account_ref,r.occurred_at,amount,balance)
                          for r,(amount,balance) in zip(page_records,balances))
            controls.append(control)
    previous = {}
    for account, occurred_at, amount, balance in ledger:
        if account in previous:
            prior_time, prior_balance = previous[account]
            if occurred_at < prior_time or prior_balance + amount != balance:
                raise ValueError("Bank PDF chronological balance continuity failed")
        previous[account] = (occurred_at,balance)
    if not records:
        raise ValueError("Bank PDF has no transactions")
    return Statement(path,sha256(blob).hexdigest(),"icbc",records,
                     {"source_rows":len(records),"pages":controls,
                      "balance_continuity_matches":True,"warnings":[]})


def finalize(statement):
    occurrences = Counter()
    external_seen = defaultdict(list)
    for record in statement.records:
        if record.transaction_id:
            key = "external:" + json_text([record.transaction_id,record.event_tag])
            external_seen[(record.account_ref,key)].append(record)
        else:
            # The ordinal preserves truly identical no-ID observations within an export.
            fingerprint = sha256(json_text(record.payload).encode()).hexdigest()
            occurrences[(record.account_ref,fingerprint)] += 1
            key = f"fingerprint:{fingerprint}:{occurrences[(record.account_ref,fingerprint)]}"
        record.event_key = key
    for repeated in external_seen.values():
        if len(repeated) > 1 and len({json_text(r.payload) for r in repeated}) != len(repeated):
            statement.validation["warnings"].append("Repeated external event ID within one source file")
    statement.validation["identity_method"] = "external_event_id" if all(r.transaction_id for r in statement.records) else "source_fingerprint"
    return statement


def parse_statement(path, profile="personal"):
    path = Path(path)
    blob = path.read_bytes()
    if path.suffix.lower() == ".csv":
        result = parse_csv(path,blob,profile)
    elif path.suffix.lower() == ".xlsx":
        result = parse_xlsx(path,blob,profile)
    elif path.suffix.lower() == ".pdf":
        result = parse_pdf(path,blob,profile)
    else:
        raise ValueError("Unsupported statement format")
    return finalize(result)
