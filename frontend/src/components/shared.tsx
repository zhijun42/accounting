import {
  dateText,
  directions,
  kinds,
  money,
  payments,
  providers,
  reviews,
} from "../format";
import type { Audit, Provider, Source } from "../models";

export function SourcePill({ provider }: { provider: Provider | null }) {
  return (
    <span className={`source-pill source-${provider ?? "manual"}`}>
      {providers[provider ?? "manual"]}
    </span>
  );
}

export function Options({ labels }: { labels: Record<string, string> }) {
  return Object.entries(labels).map(([value, label]) => (
    <option key={value} value={value}>
      {label}
    </option>
  ));
}

const fields: Record<string, string> = {
  description: "消费标题",
  merged_into_bill_id: "归并到账单",
  deleted_at: "软删除时间",
  kind: "资金性质",
  category_path: "消费分类",
  usage_date: "使用日期",
  personal_base_cents: "本人承担",
  review_status: "核对状态",
  classification_locked: "人工保护",
  direction: "资金方向",
  payment_status: "支付状态",
  inclusion_status: "统计范围",
  exclusion_reason: "排除原因",
  notes: "备注",
  status: "核对事项状态",
};
const actors: Record<Audit["actor_type"], string> = {
  user: "本人",
  ai: "AI",
  importer: "导入器",
  system: "系统",
};
const entities: Record<string, string> = {
  bills: "账单",
  categories: "分类",
  review_items: "核对事项",
  bill_sources: "来源关联",
  bill_relations: "资金关联",
  accounts: "账户",
};

function displayValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "未设置";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

function historyValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "未设置";
  if (field.endsWith("_cents") && typeof value === "number")
    return money(value);
  const labels: Record<string, Record<string, string>> = {
    kind: kinds,
    direction: directions,
    review_status: reviews,
    payment_status: payments,
    status: { open: "待处理", resolved: "已解决", dismissed: "已忽略" },
  };
  if (typeof value === "string" && labels[field])
    return labels[field][value] ?? value;
  if (field === "classification_locked")
    return value ? "保护人工分类" : "未锁定";
  if (field === "inclusion_status")
    return value === "included" ? "纳入统计" : "排除统计";
  return displayValue(value);
}

export function History({
  items,
  descriptions = false,
}: {
  items: Audit[];
  descriptions?: boolean;
}) {
  if (!items.length)
    return (
      <p className="field-help">
        暂时没有人工或 AI 修改。你的修改会从这里开始留下记录。
      </p>
    );
  return items.map((item) => (
    <article key={item.id} className="audit-entry">
      <div className="audit-meta">
        {entities[item.entity_table] ?? item.entity_table} ·{" "}
        {actors[item.actor_type]} · {dateText(item.created_at, true)}
      </div>
      <p className="audit-reason">{item.reason}</p>
      {descriptions && (item.after?.description || item.before?.description) ? (
        <p className="field-help">
          {displayValue(item.after?.description || item.before?.description)}
        </p>
      ) : null}
      {item.operation === "update" ? (
        Object.entries(fields)
          .filter(([field]) => item.before?.[field] !== item.after?.[field])
          .map(([field, label]) => (
            <div key={field} className="change-line">
              {label}：{historyValue(field, item.before?.[field])} →{" "}
              <span>{historyValue(field, item.after?.[field])}</span>
            </div>
          ))
      ) : item.entity_table === "categories" ? (
        <div className="change-line">
          新增分类：<span>{displayValue(item.after?.name)}</span>
        </div>
      ) : null}
    </article>
  ));
}

export function Sources({ items }: { items: Source[] }) {
  const locations: Record<string, string> = {
    page: "页",
    row: "行",
    sheet: "工作表",
    line_start: "起始行",
    line_end: "结束行",
  };
  return items.map((source) => {
    const { headers, values } = source.payload;
    const rows: [string, unknown][] =
      Array.isArray(headers) && Array.isArray(values)
        ? headers.map((header: unknown, index: number) => [
            displayValue(header),
            values[index],
          ])
        : Object.entries(source.payload);
    return (
      <article className="source-card" key={source.id}>
        <SourcePill provider={source.provider} />
        <p className="field-help">
          {source.source_filename}
          <br />
          {Object.entries(source.locator)
            .map(
              ([key, value]) =>
                `${locations[key] ?? key} ${displayValue(value)}`,
            )
            .join(" · ")}
        </p>
        <p className="field-help">
          交易号：{source.transaction_id || "来源未提供，使用指纹识别"}
        </p>
        <details className="raw-table">
          <summary>查看全部原始字段</summary>
          <table>
            <tbody>
              {rows.map(([label, value], index) => (
                <tr key={index}>
                  <td>{label}</td>
                  <td>{displayValue(value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      </article>
    );
  });
}
