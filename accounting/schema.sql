CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL CHECK(provider IN ('alipay','wechat','meituan','icbc','manual')),
    account_kind TEXT NOT NULL CHECK(account_kind IN ('wallet','bank_debit','platform','credit','cash')),
    display_name TEXT NOT NULL CHECK(length(trim(display_name)) > 0),
    external_ref TEXT,
    currency TEXT NOT NULL DEFAULT 'CNY',
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0)
) STRICT;
CREATE UNIQUE INDEX accounts_external_ref ON accounts(provider, external_ref) WHERE external_ref IS NOT NULL;

CREATE TABLE change_batches (
    id TEXT PRIMARY KEY,
    actor_type TEXT NOT NULL CHECK(actor_type IN ('user','ai','importer','system')),
    actor_label TEXT NOT NULL,
    model_id TEXT,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    undo_of_batch_id TEXT REFERENCES change_batches(id),
    created_at TEXT NOT NULL
) STRICT;

CREATE TABLE raw_records (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    source_event_key TEXT NOT NULL,
    snapshot_sha256 TEXT NOT NULL,
    transaction_id TEXT,
    merchant_order_id TEXT,
    occurred_at TEXT,
    time_precision TEXT NOT NULL CHECK(time_precision IN ('second','minute','day','unknown')),
    amount_cents INTEGER CHECK(amount_cents >= 0),
    currency TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('outflow','inflow','none','unknown')),
    original_category TEXT,
    description TEXT,
    counterparty TEXT,
    original_status TEXT,
    raw_payload_json TEXT NOT NULL CHECK(json_valid(raw_payload_json)),
    source_filename TEXT NOT NULL,
    source_file_sha256 TEXT NOT NULL,
    source_locator_json TEXT NOT NULL CHECK(json_valid(source_locator_json)),
    parser_version TEXT NOT NULL,
    import_batch_id TEXT NOT NULL REFERENCES change_batches(id),
    created_at TEXT NOT NULL,
    UNIQUE(account_id, source_event_key, snapshot_sha256)
) STRICT;
CREATE INDEX raw_event ON raw_records(account_id, source_event_key);
CREATE INDEX raw_transaction ON raw_records(account_id, transaction_id);
CREATE INDEX raw_merchant_order ON raw_records(account_id, merchant_order_id);
CREATE INDEX raw_matching ON raw_records(amount_cents, occurred_at);

CREATE TABLE categories (
    id TEXT PRIMARY KEY,
    parent_id TEXT REFERENCES categories(id),
    name TEXT NOT NULL CHECK(name = trim(name) AND length(name) > 0
        AND lower(name) NOT IN ('其他','其它','other','others','misc','miscellaneous')),
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    CHECK(parent_id IS NULL OR parent_id != id)
) STRICT;
CREATE UNIQUE INDEX category_roots ON categories(name COLLATE NOCASE) WHERE parent_id IS NULL;
CREATE UNIQUE INDEX category_children ON categories(parent_id, name COLLATE NOCASE) WHERE parent_id IS NOT NULL;

CREATE TABLE bills (
    id TEXT PRIMARY KEY,
    entry_origin TEXT NOT NULL CHECK(entry_origin IN ('imported','manual')),
    kind TEXT NOT NULL CHECK(kind IN ('expense','income','refund','reimbursement','internal_transfer','debt_repayment','investment','pending')),
    payment_status TEXT NOT NULL CHECK(payment_status IN ('settled','pending','voided')),
    direction TEXT NOT NULL CHECK(direction IN ('outflow','inflow','none')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents >= 0),
    currency TEXT NOT NULL,
    paid_at TEXT NOT NULL,
    time_precision TEXT NOT NULL CHECK(time_precision IN ('second','minute','day')),
    payment_account_id TEXT REFERENCES accounts(id),
    usage_date TEXT,
    description TEXT,
    counterparty TEXT,
    category_id TEXT REFERENCES categories(id),
    personal_base_cents INTEGER,
    review_status TEXT NOT NULL CHECK(review_status IN ('pending','auto_confirmed','user_confirmed')),
    classification_locked INTEGER NOT NULL DEFAULT 0 CHECK(classification_locked IN (0,1)),
    inclusion_status TEXT NOT NULL DEFAULT 'included' CHECK(inclusion_status IN ('included','excluded')),
    exclusion_reason TEXT,
    notes TEXT,
    merged_into_bill_id TEXT REFERENCES bills(id),
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    CHECK(kind != 'pending' OR review_status = 'pending'),
    CHECK(kind = 'expense' OR (category_id IS NULL AND personal_base_cents IS NULL AND usage_date IS NULL)),
    CHECK(personal_base_cents IS NULL OR personal_base_cents BETWEEN 0 AND amount_cents),
    CHECK(kind != 'expense' OR review_status = 'pending'
        OR (amount_cents > 0 AND direction = 'outflow' AND category_id IS NOT NULL AND personal_base_cents IS NOT NULL)),
    CHECK(kind NOT IN ('income','refund','reimbursement') OR direction = 'inflow'),
    CHECK(kind != 'debt_repayment' OR direction = 'outflow'),
    CHECK((inclusion_status = 'included' AND exclusion_reason IS NULL)
        OR (inclusion_status = 'excluded' AND length(trim(exclusion_reason)) > 0 AND exclusion_reason IS NOT NULL)),
    CHECK(merged_into_bill_id IS NULL OR (merged_into_bill_id != id AND deleted_at IS NOT NULL))
) STRICT;
CREATE INDEX bills_paid_at ON bills(paid_at);
CREATE INDEX bills_usage_date ON bills(usage_date);
CREATE INDEX bills_category ON bills(category_id);
CREATE INDEX bills_review ON bills(review_status, kind) WHERE deleted_at IS NULL AND inclusion_status = 'included';

CREATE TABLE bill_sources (
    id TEXT PRIMARY KEY,
    bill_id TEXT NOT NULL REFERENCES bills(id),
    raw_record_id TEXT NOT NULL UNIQUE REFERENCES raw_records(id),
    role TEXT NOT NULL CHECK(role IN ('order','payment','funding','snapshot')),
    is_primary INTEGER NOT NULL CHECK(is_primary IN (0,1)),
    match_method TEXT NOT NULL CHECK(match_method IN ('exact_id','composite','manual')),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL
) STRICT;
CREATE INDEX sources_bill ON bill_sources(bill_id);
CREATE UNIQUE INDEX primary_bill_source ON bill_sources(bill_id) WHERE is_primary = 1;

CREATE TABLE bill_relations (
    id TEXT PRIMARY KEY,
    origin_bill_id TEXT NOT NULL REFERENCES bills(id),
    related_bill_id TEXT NOT NULL REFERENCES bills(id),
    relation_type TEXT NOT NULL CHECK(relation_type IN ('refund','reimbursement','internal_transfer','repayment')),
    amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
    personal_refund_cents INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    UNIQUE(origin_bill_id, related_bill_id, relation_type),
    CHECK(origin_bill_id != related_bill_id),
    CHECK((relation_type = 'refund' AND personal_refund_cents IS NOT NULL
        AND personal_refund_cents BETWEEN 0 AND amount_cents)
        OR (relation_type != 'refund' AND personal_refund_cents IS NULL))
) STRICT;
CREATE INDEX relations_origin ON bill_relations(origin_bill_id, relation_type);
CREATE INDEX relations_related ON bill_relations(related_bill_id, relation_type);

CREATE TABLE review_items (
    id TEXT PRIMARY KEY,
    bill_id TEXT REFERENCES bills(id),
    raw_record_id TEXT REFERENCES raw_records(id),
    topic TEXT NOT NULL CHECK(topic IN ('nature','category','matching','personal_share','refund_allocation','parsing','duplicate')),
    status TEXT NOT NULL CHECK(status IN ('open','resolved','dismissed')),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    proposal_json TEXT CHECK(proposal_json IS NULL OR json_valid(proposal_json)),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    bill_revision INTEGER,
    resolved_batch_id TEXT REFERENCES change_batches(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    CHECK(bill_id IS NOT NULL OR raw_record_id IS NOT NULL),
    CHECK((status = 'open' AND resolved_batch_id IS NULL) OR (status != 'open' AND resolved_batch_id IS NOT NULL))
) STRICT;
CREATE INDEX review_open ON review_items(topic, bill_id) WHERE status = 'open';

CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES change_batches(id),
    entity_table TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('insert','update','delete')),
    before_json TEXT CHECK(before_json IS NULL OR json_valid(before_json)),
    after_json TEXT CHECK(after_json IS NULL OR json_valid(after_json)),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    CHECK((operation = 'insert' AND before_json IS NULL AND after_json IS NOT NULL)
       OR (operation = 'delete' AND before_json IS NOT NULL AND after_json IS NULL)
       OR (operation = 'update' AND before_json IS NOT NULL AND after_json IS NOT NULL))
) STRICT;
CREATE INDEX updates_entity ON updates(entity_table, entity_id, id);
CREATE INDEX updates_batch ON updates(batch_id, id);

CREATE VIEW invalid_bill_relations AS
SELECT r.id
FROM bill_relations r
JOIN bills o ON o.id = r.origin_bill_id
JOIN bills t ON t.id = r.related_bill_id
WHERE o.currency != t.currency
   OR o.review_status = 'pending' OR t.review_status = 'pending'
   OR o.payment_status != 'settled' OR t.payment_status != 'settled'
   OR o.inclusion_status != 'included' OR t.inclusion_status != 'included'
   OR o.deleted_at IS NOT NULL OR t.deleted_at IS NOT NULL
   OR (r.relation_type = 'refund' AND (o.kind != 'expense' OR t.kind != 'refund'))
   OR (r.relation_type = 'reimbursement' AND (o.kind != 'expense' OR t.kind != 'reimbursement'))
   OR (r.relation_type = 'repayment' AND (o.kind != 'expense' OR t.kind != 'debt_repayment'))
   OR (r.relation_type = 'internal_transfer' AND
       (o.kind != 'internal_transfer' OR t.kind != 'internal_transfer' OR o.direction != 'outflow' OR t.direction != 'inflow'))
   OR (SELECT SUM(x.amount_cents) FROM bill_relations x WHERE x.related_bill_id = t.id) > t.amount_cents
   OR (SELECT SUM(x.amount_cents) FROM bill_relations x WHERE x.origin_bill_id = o.id AND x.relation_type = r.relation_type) > o.amount_cents
   OR (o.kind = 'expense' AND (
       COALESCE((SELECT SUM(x.personal_refund_cents) FROM bill_relations x WHERE x.origin_bill_id = o.id AND x.relation_type = 'refund'), 0) > o.personal_base_cents
       OR COALESCE((SELECT SUM(x.amount_cents - x.personal_refund_cents) FROM bill_relations x WHERE x.origin_bill_id = o.id AND x.relation_type = 'refund'), 0) > o.amount_cents - o.personal_base_cents
       OR COALESCE((SELECT SUM(x.amount_cents) FROM bill_relations x WHERE x.origin_bill_id = o.id AND x.relation_type = 'reimbursement'), 0)
          + COALESCE((SELECT SUM(x.amount_cents - x.personal_refund_cents) FROM bill_relations x WHERE x.origin_bill_id = o.id AND x.relation_type = 'refund'), 0)
          > o.amount_cents - o.personal_base_cents));

CREATE VIEW personal_expenses AS
WITH refunds AS (
    SELECT r.origin_bill_id, SUM(r.personal_refund_cents) refunded_cents
    FROM bill_relations r JOIN bills b ON b.id = r.related_bill_id
    WHERE r.relation_type = 'refund' AND b.kind = 'refund'
      AND b.payment_status = 'settled' AND b.review_status != 'pending'
      AND b.inclusion_status = 'included' AND b.deleted_at IS NULL
    GROUP BY r.origin_bill_id
)
SELECT b.id, b.category_id,
       COALESCE(b.usage_date, date(b.paid_at, '+8 hours')) effective_usage_date,
       CASE WHEN b.usage_date IS NULL THEN 'payment_default' ELSE 'manual' END usage_date_source,
       b.personal_base_cents - COALESCE(r.refunded_cents, 0) personal_net_cents
FROM bills b LEFT JOIN refunds r ON r.origin_bill_id = b.id
WHERE b.kind = 'expense' AND b.payment_status = 'settled'
  AND b.review_status != 'pending' AND b.inclusion_status = 'included'
  AND b.deleted_at IS NULL AND b.currency = 'CNY';

CREATE VIEW bill_details AS
SELECT b.*, COALESCE(b.usage_date, date(b.paid_at, '+8 hours')) effective_usage_date,
       a.provider source_provider, r.transaction_id, r.merchant_order_id,
       r.original_category, r.original_status, r.source_filename, r.source_locator_json
FROM bills b
LEFT JOIN bill_sources s ON s.bill_id = b.id AND s.is_primary = 1
LEFT JOIN raw_records r ON r.id = s.raw_record_id
LEFT JOIN accounts a ON a.id = r.account_id;
