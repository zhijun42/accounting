import { useEffect, useRef, useState } from "react";
import { APIError, errorMessage } from "../api";
import {
  categorySelection,
  dateText,
  directions,
  kinds,
  money,
  parseMoney,
  payments,
  reviewLabel,
} from "../format";
import {
  directionSchema,
  inclusionSchema,
  kindSchema,
  paymentSchema,
} from "../models";
import type { BillUpdate, Category, Detail } from "../models";
import { History, Options, SourcePill, Sources } from "./shared";

type Draft = Omit<
  BillUpdate,
  | "revision"
  | "confirm"
  | "category_id"
  | "personal_base_cents"
  | "usage_date"
  | "notes"
  | "exclusion_reason"
> & {
  root: string;
  child: string;
  personal: string;
  usage: string;
  notes: string;
  exclusion: string;
};
type Props = {
  detail: Detail;
  categories: Category[];
  open: boolean;
  saving: boolean;
  dirty: boolean;
  assigned: string | null;
  onDirty: () => void;
  onSave: (payload: BillUpdate) => Promise<void>;
  onReload: () => void;
  onClose: () => void;
  onAddCategory: (parent: string) => void;
};

export function ReviewPanel({
  detail,
  categories,
  open,
  saving,
  dirty,
  assigned,
  onDirty,
  onSave,
  onReload,
  onClose,
  onAddCategory,
}: Props) {
  const { bill } = detail;
  const [draft, setDraft] = useState<Draft>(() => ({
    kind: bill.kind,
    direction: bill.direction,
    payment_status: bill.payment_status,
    ...categorySelection(bill.category_id, categories),
    personal:
      bill.personal_base_cents === null
        ? ""
        : (bill.personal_base_cents / 100).toFixed(2),
    usage: bill.usage_date ?? "",
    notes: bill.notes ?? "",
    inclusion_status: bill.inclusion_status,
    exclusion: bill.exclusion_reason ?? "",
  }));
  const [error, setError] = useState("");
  const panel = useRef<HTMLElement>(null);
  useEffect(() => {
    panel.current?.scrollTo?.(0, 0);
  }, []);
  useEffect(() => {
    if (assigned)
      setDraft((old) => ({
        ...old,
        ...categorySelection(assigned, categories),
      }));
  }, [assigned, categories]);
  const change = <K extends keyof Draft>(key: K, value: Draft[K]) => {
    setDraft((old) => ({ ...old, [key]: value }));
    onDirty();
  };
  const expense = draft.kind === "expense";
  async function save(confirm: boolean) {
    if (saving) return;
    setError("");
    try {
      await onSave({
        revision: bill.revision,
        kind: draft.kind,
        direction: draft.direction,
        payment_status: draft.payment_status,
        category_id: expense ? draft.child || draft.root || null : null,
        personal_base_cents: expense ? parseMoney(draft.personal) : null,
        usage_date: expense ? draft.usage || null : null,
        inclusion_status: draft.inclusion_status,
        exclusion_reason:
          draft.inclusion_status === "excluded"
            ? draft.exclusion || null
            : null,
        notes: draft.notes || null,
        confirm,
      });
    } catch (error) {
      setError(
        errorMessage(error) +
          (error instanceof APIError && error.status === 409
            ? " 当前输入已保留；点击右上角刷新后重新核对。"
            : ""),
      );
    }
  }
  return (
    <aside
      ref={panel}
      id="detail-panel"
      className={`detail-panel${open ? " open" : ""}`}
      aria-label="逐笔核对"
      aria-busy={saving}
    >
      <div className="detail-heading">
        <div>
          <h2>逐笔核对</h2>
          <small>{dirty ? "未保存修改" : reviewLabel(bill)}</small>
        </div>
        <div>
          <button
            type="button"
            disabled={saving}
            onClick={onReload}
            aria-label="重新加载这笔记录"
          >
            ↻
          </button>
          <button
            type="button"
            disabled={saving}
            onClick={onClose}
            aria-label="关闭核对面板"
          >
            ×
          </button>
        </div>
      </div>
      <div className="detail-content">
        <SourcePill provider={bill.source_provider} />
        <div className="detail-amount">{money(bill.amount_cents)}</div>
        <h3 className="detail-description">
          {bill.description || bill.counterparty || "来源记录"}
        </h3>
        <p className="detail-date">
          {dateText(bill.paid_at, true)} · {directions[bill.direction]}
        </p>
        <div className="source-hint">
          平台原分类：<strong>{bill.original_category || "未提供"}</strong>
          <br />
          原始状态：{bill.original_status || "未提供"}
          <br />
          原始数据不会因下面的修改而改变。
        </div>
        <div className="form-divider" />
        {detail.review_items.some(
          (item) => item.topic !== "personal_share",
        ) && (
          <section className="source-hint" aria-label="需要本人核对">
            <strong>需要本人核对</strong>
            {detail.review_items
              .filter((item) => item.topic !== "personal_share")
              .map((item) => (
                <p key={item.id}>{item.reason}</p>
              ))}
          </section>
        )}
        <form
          className="review-form"
          onSubmit={(event) => {
            event.preventDefault();
            void save(false);
          }}
        >
          <fieldset disabled={saving}>
            <div className="field-row">
              <div>
                <label htmlFor="edit-kind">资金性质</label>
                <select
                  id="edit-kind"
                  value={draft.kind}
                  onChange={(event) => {
                    const kind = kindSchema.parse(event.target.value);
                    setDraft((old) => ({
                      ...old,
                      kind,
                      direction:
                        kind === "expense" || kind === "debt_repayment"
                          ? "outflow"
                          : ["income", "refund", "reimbursement"].includes(kind)
                            ? "inflow"
                            : old.direction,
                      personal:
                        kind === "expense" && !old.personal
                          ? (bill.amount_cents / 100).toFixed(2)
                          : old.personal,
                    }));
                    onDirty();
                  }}
                >
                  <Options labels={kinds} />
                </select>
              </div>
              <div>
                <label htmlFor="edit-direction">资金方向</label>
                <select
                  id="edit-direction"
                  value={draft.direction}
                  onChange={(event) =>
                    change(
                      "direction",
                      directionSchema.parse(event.target.value),
                    )
                  }
                >
                  <Options labels={directions} />
                </select>
              </div>
            </div>
            <label htmlFor="edit-payment">支付状态</label>
            <select
              id="edit-payment"
              value={draft.payment_status}
              onChange={(event) =>
                change(
                  "payment_status",
                  paymentSchema.parse(event.target.value),
                )
              }
            >
              <Options labels={payments} />
            </select>
            <div className="field-label">
              <label htmlFor="edit-root">生活分类</label>
              <button
                type="button"
                className="text-button"
                onClick={() => onAddCategory(draft.root)}
              >
                ＋ 新分类
              </button>
            </div>
            <div className="field-row">
              <select
                id="edit-root"
                aria-label="消费大类"
                disabled={!expense}
                value={draft.root}
                onChange={(event) => {
                  setDraft((old) => ({
                    ...old,
                    root: event.target.value,
                    child: "",
                  }));
                  onDirty();
                }}
              >
                <option value="">选择大类</option>
                {categories
                  .filter((item) => !item.parent_id)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
              </select>
              <select
                id="edit-child"
                aria-label="消费小类"
                disabled={!expense || !draft.root}
                value={draft.child}
                onChange={(event) => change("child", event.target.value)}
              >
                <option value="">不细分小类</option>
                {categories
                  .filter((item) => item.parent_id === draft.root)
                  .map((item) => (
                    <option key={item.id} value={item.id}>
                      {item.name}
                    </option>
                  ))}
              </select>
            </div>
            <p className="field-help">
              {expense
                ? "大类必选，小类按需。没有合适的分类，可以自己添加。"
                : "先选择“消费”才能填写生活分类；转账、退款等单独核对资金性质。"}
            </p>
            <div className="field-row">
              <div>
                <label htmlFor="edit-usage">使用日期</label>
                <input
                  id="edit-usage"
                  type="date"
                  disabled={!expense}
                  value={draft.usage}
                  onChange={(event) => change("usage", event.target.value)}
                />
              </div>
              <div>
                <label htmlFor="edit-personal">本人承担（元）</label>
                <input
                  id="edit-personal"
                  inputMode="decimal"
                  disabled={!expense}
                  value={draft.personal}
                  onChange={(event) => change("personal", event.target.value)}
                  placeholder="未设置"
                  autoComplete="off"
                />
              </div>
            </div>
            <p className="field-help">
              {draft.usage
                ? "使用日期由你填写。"
                : `未填使用日期，默认支付日期：${bill.effective_usage_date}。`}{" "}
              不确定可以晚些补充；年卡暂不分摊。
            </p>
            <label htmlFor="edit-inclusion">消费统计范围</label>
            <select
              id="edit-inclusion"
              value={draft.inclusion_status}
              onChange={(event) =>
                change(
                  "inclusion_status",
                  inclusionSchema.parse(event.target.value),
                )
              }
            >
              <option value="included">纳入统计（确认消费后生效）</option>
              <option value="excluded">排除统计，保留原始记录</option>
            </select>
            {draft.inclusion_status === "excluded" && (
              <>
                <label htmlFor="edit-exclusion">排除原因</label>
                <input
                  id="edit-exclusion"
                  maxLength={500}
                  value={draft.exclusion}
                  onChange={(event) => change("exclusion", event.target.value)}
                  placeholder="例如：替朋友垫付，非本人消费"
                />
              </>
            )}
            <label htmlFor="edit-notes">给这笔记录留个备注</label>
            <textarea
              id="edit-notes"
              maxLength={2000}
              value={draft.notes}
              onChange={(event) => change("notes", event.target.value)}
              placeholder="可以记录一次活动、一次相聚…"
            />
            {error && (
              <p className="form-error" role="alert">
                {error}
              </p>
            )}
            <div className="detail-actions">
              <button type="submit" className="secondary-button">
                {saving ? "保存中…" : "保存修改"}
              </button>
              <button
                type="button"
                className="primary-button"
                onClick={() => void save(true)}
              >
                确认并下一笔 →
              </button>
            </div>
            <p className="save-hint">
              确认后保护人工分类 · 所有修改自动留存历史
              <br />
              单条确认不代表跨平台关联已完成
            </p>
          </fieldset>
        </form>
        <div className="form-divider" />
        <details className="audit-details">
          <summary>
            原始来源 <span>{detail.sources.length} 条</span>
          </summary>
          <Sources items={detail.sources} />
        </details>
        <details className="audit-details">
          <summary>
            修改历史 <span>{detail.history.length} 条</span>
          </summary>
          <History items={detail.history} />
        </details>
      </div>
    </aside>
  );
}
