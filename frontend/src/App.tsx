import { useCallback, useEffect, useRef, useState } from "react";
import { api, errorMessage } from "./api";
import { count } from "./format";
import type {
  Audit,
  BillList as List,
  BillUpdate,
  Category,
  Detail,
  Filters,
  Status,
  View,
} from "./models";
import { BillList, statuses, Summary } from "./components/BillList";
import { CategoryDialog, CategoryManager } from "./components/CategoryDialog";
import { ReviewPanel } from "./components/ReviewPanel";
import { History } from "./components/shared";

const initialFilters: Filters = {
  status: "all",
  period: "scope",
  provider: "all",
  kind: "all",
  q: "",
  page: 1,
};
const headings: Record<View, [string, string]> = {
  bills: ["从每一笔，认识生活", "慢慢核对，让记录更接近真实的生活。"],
  categories: ["给生活，你自己的名字", "大类勾勒轮廓，小类记录你在意的细节。"],
  history: [
    "每次修正，都有迹可循",
    "修改前后、执行者与理由，保留在本地数据库。",
  ],
};

export function App() {
  const [view, setView] = useState<View>("bills");
  const [filters, setFilters] = useState(initialFilters);
  const [list, setList] = useState<List | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [categories, setCategories] = useState<Category[]>([]);
  const [history, setHistory] = useState<Audit[]>([]);
  const [token, setToken] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [open, setOpen] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [dialog, setDialog] = useState<{
    parent: string;
    assign: boolean;
  } | null>(null);
  const [assigned, setAssigned] = useState<string | null>(null);
  const sequence = useRef(0);
  const detailSequence = useRef(0);
  const busy = useRef(false);
  const modified = useRef(false);
  const markDirty = useCallback(() => {
    modified.current = true;
    setDirty(true);
  }, []);
  const markClean = useCallback(() => {
    modified.current = false;
    setDirty(false);
  }, []);

  const loadList = useCallback(
    async (
      next: Filters,
      prefer: string | null = null,
      preserved: Detail | null = null,
    ) => {
      const request = ++sequence.current;
      ++detailSequence.current;
      setLoading(true);
      setError("");
      if (!preserved) setDetail(null);
      try {
        let result = await api.list(next);
        const pages = Math.max(1, Math.ceil(result.total / result.page_size));
        if (next.page > pages) {
          next = { ...next, page: pages };
          result = await api.list(next);
        }
        if (request !== sequence.current) return;
        setFilters(next);
        setList(result);
        const id =
          result.items.find((item) => item.id === prefer)?.id ??
          result.items[0]?.id;
        const selected = id
          ? preserved?.bill.id === id
            ? preserved
            : await api.detail(id)
          : null;
        if (request !== sequence.current) return;
        setDetail(selected);
        setAssigned(null);
        markClean();
      } catch (error) {
        if (request === sequence.current) {
          setError(errorMessage(error));
          setList(null);
        }
      } finally {
        if (request === sequence.current) setLoading(false);
      }
    },
    [markClean],
  );

  const boot = useCallback(async () => {
    const request = ++sequence.current;
    setLoading(true);
    setError("");
    try {
      const [session, result] = await Promise.all([
        api.session(),
        api.categories(),
      ]);
      if (request !== sequence.current) return;
      setToken(session.csrf_token);
      setCategories(result.categories);
      await loadList(initialFilters);
    } catch (error) {
      if (request === sequence.current) {
        setError(errorMessage(error));
        setLoading(false);
      }
    }
  }, [loadList]);
  useEffect(() => {
    void boot();
    return () => {
      ++sequence.current;
      ++detailSequence.current;
    };
  }, [boot]);
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (modified.current || busy.current) {
        event.preventDefault();
        event.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => setNotice(""), 6000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  function discardAllowed(): boolean {
    if (busy.current) return false;
    if (
      modified.current &&
      !window.confirm("这笔记录还有未保存的修改。放弃修改并继续吗？")
    )
      return false;
    markClean();
    return true;
  }
  async function select(id: string, force = false) {
    if (!force && !discardAllowed()) return;
    const request = ++detailSequence.current;
    setOpen(true);
    setAssigned(null);
    // A late detail response must not replace a draft entered on a newer selection.
    setDetail(null);
    try {
      const result = await api.detail(id);
      if (request === detailSequence.current) {
        setDetail(result);
        markClean();
      }
    } catch (error) {
      if (request === detailSequence.current) setNotice(errorMessage(error));
    }
  }
  function changeFilters(next: Filters) {
    if (!discardAllowed()) return;
    setFilters(next);
    void loadList(next, detail?.bill.id ?? null);
  }
  async function navigate(next: View, status: Status = filters.status) {
    if (!discardAllowed()) return;
    ++sequence.current;
    ++detailSequence.current;
    setView(next);
    setOpen(false);
    setError("");
    setAssigned(null);
    if (next === "bills") {
      const updated = { ...filters, status, page: 1 };
      setFilters(updated);
      await loadList(updated, detail?.bill.id ?? null);
    } else if (next === "history") {
      const request = sequence.current;
      setLoading(true);
      try {
        const result = await api.history();
        if (request === sequence.current) setHistory(result.items);
      } catch (error) {
        if (request === sequence.current) setError(errorMessage(error));
      } finally {
        if (request === sequence.current) setLoading(false);
      }
    }
  }
  async function save(payload: BillUpdate) {
    if (!detail || busy.current) return;
    const id = detail.bill.id;
    const index = list?.items.findIndex((item) => item.id === id) ?? -1;
    const next = payload.confirm
      ? (list?.items[index + 1]?.id ?? list?.items[index - 1]?.id ?? null)
      : id;
    busy.current = true;
    setSaving(true);
    try {
      const result = await api.update(id, payload, token);
      markClean();
      setDetail(result);
      await loadList(filters, next, payload.confirm ? null : result);
      setNotice(
        payload.confirm
          ? "已确认，人工分类已保护。继续下一笔。"
          : "修改已保存，并记入本地审计历史。",
      );
    } finally {
      busy.current = false;
      setSaving(false);
    }
  }
  const summary = list?.summary;
  const [title, subtitle] = headings[view];
  return (
    <>
      <aside className="sidebar">
        <a
          className="brand"
          href="/"
          aria-label="生活账本首页"
          onClick={(event) => {
            event.preventDefault();
            void navigate("bills", "all");
          }}
        >
          <span className="brand-mark">账</span>
        </a>
        <div className="nav-label">我的账本</div>
        <nav aria-label="主导航">
          {(
            [
              "all",
              "ai_reviewed_pending",
              "ai_unreviewed_pending",
              "confirmed",
            ] as const
          ).map((status, index) => (
            <button
              key={status}
              className={`nav-item${view === "bills" && filters.status === status ? " active" : ""}`}
              aria-label={statuses[status]}
              disabled={saving}
              onClick={() => void navigate("bills", status)}
            >
              <span>{["▤", "◉", "◷", "✓"][index]}</span>
              <div className="nav-text">
                {status === "ai_reviewed_pending" ? (
                  <>
                    AI已检查
                    <br />
                    待核对
                  </>
                ) : status === "ai_unreviewed_pending" ? (
                  <>
                    AI未检查
                    <br />
                    待核对
                  </>
                ) : (
                  statuses[status]
                )}
              </div>
              <small>
                {summary
                  ? count(summary[status === "all" ? "total" : status])
                  : "—"}
              </small>
            </button>
          ))}
          <div className="nav-divider" />
          <button
            className={`nav-item${view === "categories" ? " active" : ""}`}
            disabled={saving}
            onClick={() => void navigate("categories")}
          >
            <span>▦</span>分类管理
          </button>
          <button
            className={`nav-item${view === "history" ? " active" : ""}`}
            disabled={saving}
            onClick={() => void navigate("history")}
          >
            <span>↶</span>修改记录
          </button>
        </nav>
      </aside>
      <main className="workspace">
        <header className="page-header">
          <div>
            <div className="eyebrow">A LITTLE CLARITY, EVERY DAY</div>
            <h1>
              {title}
              <span>。</span>
            </h1>
            <p>{subtitle}</p>
          </div>
        </header>
        {view === "bills" && (
          <section id="bills-view">
            <Summary list={list} />
            <div className="review-layout">
              <BillList
                list={list}
                filters={filters}
                selected={detail?.bill.id ?? null}
                loading={loading}
                saving={saving}
                error={error}
                onFilter={changeFilters}
                onSelect={(id) => void select(id)}
                onRefresh={() => {
                  if (discardAllowed()) {
                    if (token) void loadList(filters, detail?.bill.id ?? null);
                    else void boot();
                  }
                }}
              />
              {detail ? (
                <ReviewPanel
                  key={`${detail.bill.id}:${detail.bill.revision}`}
                  detail={detail}
                  categories={categories}
                  open={open}
                  saving={saving}
                  dirty={dirty}
                  assigned={assigned}
                  onDirty={markDirty}
                  onSave={save}
                  onReload={() => {
                    if (discardAllowed()) void select(detail.bill.id, true);
                  }}
                  onClose={() => {
                    if (discardAllowed()) {
                      setOpen(false);
                      setDetail(null);
                      ++detailSequence.current;
                    }
                  }}
                  onAddCategory={(parent) =>
                    setDialog({ parent, assign: true })
                  }
                />
              ) : (
                <aside className="detail-panel" aria-label="逐笔核对">
                  <div className="detail-empty">
                    <div className="empty-symbol">▤</div>
                    <h2>每一笔，都值得被理解</h2>
                    <p>
                      {loading ? (
                        "正在读取记录…"
                      ) : (
                        <>
                          选择左侧记录，查看原始来源，
                          <br />
                          补充分类与真实的使用日期。
                        </>
                      )}
                    </p>
                  </div>
                </aside>
              )}
            </div>
          </section>
        )}
        {view === "categories" && (
          <CategoryManager
            categories={categories}
            onAdd={(parent) => setDialog({ parent, assign: false })}
          />
        )}
        {view === "history" && (
          <section id="history-view">
            <div className="view-toolbar">
              <div>
                <h2>最近的修改</h2>
                <p>
                  显示最近 100 条人工或 AI 修改；导入记录可在每笔账单中查看。
                </p>
              </div>
              <button
                className="quiet-button"
                onClick={() => void navigate("history")}
              >
                ↻ 刷新
              </button>
            </div>
            <div className="history-container">
              {loading ? (
                "正在读取…"
              ) : error ? (
                <p role="alert">{error}</p>
              ) : (
                <History items={history} descriptions />
              )}
            </div>
          </section>
        )}
      </main>
      {dialog && (
        <CategoryDialog
          categories={categories}
          parent={dialog.parent}
          token={token}
          onBusy={(value) => {
            busy.current = value;
          }}
          onClose={() => setDialog(null)}
          onCreated={(id, updated) => {
            setCategories(updated);
            if (dialog.assign) {
              setAssigned(id);
              markDirty();
            }
            setDialog(null);
            setNotice("新分类已添加。");
          }}
        />
      )}
      {notice && (
        <div className="toast" role="status" aria-live="polite">
          {notice}
        </div>
      )}
    </>
  );
}
