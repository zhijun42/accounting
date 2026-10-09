import { useState } from "react";
import { count, dateText, kinds, money, providers, reviews } from "../format";
import type { BillList as List, Filters } from "../models";
import { Options, SourcePill } from "./shared";

export const statuses = {
  all: "全部记录",
  pending: "待核对",
  confirmed: "已确认",
};
type Props = {
  list: List | null;
  filters: Filters;
  selected: string | null;
  loading: boolean;
  saving: boolean;
  error: string;
  onFilter: (filters: Filters) => void;
  onSelect: (id: string) => void;
  onRefresh: () => void;
};

export function BillList({
  list,
  filters,
  selected,
  loading,
  saving,
  error,
  onFilter,
  onSelect,
  onRefresh,
}: Props) {
  const [search, setSearch] = useState(filters.q);
  const pages = list ? Math.max(1, Math.ceil(list.total / list.page_size)) : 1;
  return (
    <section
      className="records-panel"
      aria-label="账单列表"
      aria-busy={loading}
    >
      <div className="records-heading">
        <div>
          <h2>{statuses[filters.status]}</h2>
          <span>
            {loading
              ? "正在读取本地数据库…"
              : list
                ? `${count(list.total)} 条记录`
                : "未能加载"}
          </span>
        </div>
        <button className="quiet-button" disabled={saving} onClick={onRefresh}>
          ↻ 刷新
        </button>
      </div>
      <div className="toolbar">
        <form
          className="search-field"
          onSubmit={(event) => {
            event.preventDefault();
            onFilter({ ...filters, q: search.trim(), page: 1 });
          }}
        >
          <span aria-hidden="true">⌕</span>
          <input
            aria-label="搜索商户、商品或交易号"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="搜索商户、商品或交易号"
            maxLength={200}
            disabled={saving}
          />
          <button type="submit" aria-label="搜索" disabled={saving}>
            ↵
          </button>
        </form>
        <select
          aria-label="支付日期范围"
          value={filters.period}
          disabled={saving}
          onChange={(event) =>
            onFilter({ ...filters, period: event.target.value, page: 1 })
          }
        >
          <option value="scope">2026 年 1—9 月</option>
          <option value="all">2026 年全部日期</option>
          {Array.from({ length: 12 }, (_, index) => (
            <option key={index} value={String(index + 1).padStart(2, "0")}>
              {index + 1} 月
            </option>
          ))}
        </select>
        <select
          aria-label="来源平台"
          value={filters.provider}
          disabled={saving}
          onChange={(event) =>
            onFilter({ ...filters, provider: event.target.value, page: 1 })
          }
        >
          <option value="all">全部来源</option>
          <Options labels={providers} />
        </select>
        <select
          aria-label="资金性质筛选"
          value={filters.kind}
          disabled={saving}
          onChange={(event) =>
            onFilter({ ...filters, kind: event.target.value, page: 1 })
          }
        >
          <option value="all">全部性质</option>
          <Options labels={kinds} />
        </select>
      </div>
      <div className="table-scroll">
        <table>
          <thead>
            <tr>
              <th>交易内容 / 日期</th>
              <th>来源</th>
              <th>分类 / 状态</th>
              <th className="amount-cell">金额</th>
            </tr>
          </thead>
          <tbody>
            {loading || error || !list?.items.length ? (
              <tr>
                <td colSpan={4} className="empty-state">
                  {loading ? (
                    "正在读取记录…"
                  ) : error ? (
                    <span role="alert">
                      {error}
                      <br />
                      检查服务后点击“刷新”重试。
                    </span>
                  ) : (
                    <>
                      这里暂时没有记录。
                      <br />
                      试试调整日期、来源或搜索条件。
                    </>
                  )}
                </td>
              </tr>
            ) : (
              list.items.map((bill) => {
                const category = bill.category_name
                  ? [bill.parent_category_name, bill.category_name]
                      .filter(Boolean)
                      .join(" / ")
                  : bill.kind === "pending" || bill.kind === "expense"
                    ? "待分类"
                    : kinds[bill.kind];
                const description =
                  bill.description || bill.counterparty || "来源记录";
                return (
                  <tr
                    key={bill.id}
                    className={`bill-row${bill.id === selected ? " selected" : ""}`}
                    tabIndex={saving ? -1 : 0}
                    aria-selected={bill.id === selected}
                    aria-label={`${description}，${money(bill.amount_cents)}`}
                    onClick={() => {
                      if (!saving) onSelect(bill.id);
                    }}
                    onKeyDown={(event) => {
                      if (!saving && ["Enter", " "].includes(event.key)) {
                        event.preventDefault();
                        onSelect(bill.id);
                      }
                    }}
                  >
                    <td>
                      <div className="bill-description" title={description}>
                        {description}
                      </div>
                      <div className="bill-meta">
                        {dateText(bill.paid_at)} ·{" "}
                        {bill.counterparty || kinds[bill.kind]}
                      </div>
                    </td>
                    <td>
                      <SourcePill provider={bill.source_provider} />
                    </td>
                    <td>
                      <span
                        className={`category-text${bill.category_id ? "" : " unset"}`}
                      >
                        {category}
                      </span>
                      <span
                        className={`status-dot${bill.review_status === "pending" ? "" : " confirmed"}`}
                      >
                        {reviews[bill.review_status]}
                      </span>
                    </td>
                    <td
                      className={`amount-cell${bill.direction === "inflow" ? " inflow" : ""}`}
                    >
                      {bill.direction === "outflow"
                        ? "−"
                        : bill.direction === "inflow"
                          ? "+"
                          : ""}
                      {money(bill.amount_cents)}
                      {bill.inclusion_status === "excluded" && (
                        <small className="amount-note">已排除</small>
                      )}
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>
      <footer className="pagination">
        <span>
          {list?.total
            ? `第 ${(list.page - 1) * list.page_size + 1}—${Math.min(list.page * list.page_size, list.total)} 条 · 共 ${count(list.total)} 条`
            : "没有匹配的记录"}
        </span>
        <div>
          <button
            className="icon-button"
            disabled={loading || saving || filters.page <= 1}
            aria-label="上一页"
            onClick={() => onFilter({ ...filters, page: filters.page - 1 })}
          >
            ‹
          </button>
          <span>
            {filters.page} / {pages}
          </span>
          <button
            className="icon-button"
            disabled={loading || saving || filters.page >= pages}
            aria-label="下一页"
            onClick={() => onFilter({ ...filters, page: filters.page + 1 })}
          >
            ›
          </button>
        </div>
      </footer>
    </section>
  );
}

export function Summary({ list }: { list: List | null }) {
  const summary = list?.summary;
  const percent = summary?.total
    ? Math.round((summary.confirmed / summary.total) * 100)
    : 0;
  return (
    <>
      <div className="stats-grid">
        <article className="stat-card">
          <div className="stat-label">
            当前范围的记录 <span>↗</span>
          </div>
          <div className="stat-number">
            {summary ? count(summary.total) : "—"}
          </div>
          <small>按支付日期筛选 · 非最终消费笔数</small>
        </article>
        <article className="stat-card">
          <div className="stat-label">
            等待你的核对 <span className="amber">◷</span>
          </div>
          <div className="stat-number">
            {summary ? count(summary.pending) : "—"}
          </div>
          <small>从一笔开始，不必一次完成</small>
        </article>
        <article className="stat-card progress-card">
          <div className="stat-label">
            已确认记录 <span className="green">✓</span>
          </div>
          <div className="stat-number">
            {summary ? count(summary.confirmed) : "—"}
            <span className="stat-percent">{percent}%</span>
          </div>
          <progress value={percent} max={100} aria-label="核对进度" />
        </article>
      </div>
      <div className="scope-note">
        <span className="scope-icon">i</span>
        <p>
          <strong>先核对，再洞察。</strong>{" "}
          美团、支付平台和银行卡可能记录同一次消费；跨平台关联尚未完成。确认单条记录不代表已去重，本页不展示消费总额。
        </p>
      </div>
    </>
  );
}
