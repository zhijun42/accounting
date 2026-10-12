import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, expect, it, vi } from "vitest";
import { App } from "../App";
import { api } from "../api";
import type { BillList } from "../models";
import { bill, categories, detailFor, listFor } from "./fixtures";

beforeEach(() => {
  vi.spyOn(api, "session").mockResolvedValue({
    csrf_token: "fictional-token",
    database: "fictional.sqlite3",
  });
  vi.spyOn(api, "categories").mockResolvedValue({ categories });
  vi.spyOn(api, "list").mockResolvedValue(listFor());
  vi.spyOn(api, "detail").mockImplementation(async (id) =>
    detailFor(
      id === bill.id ? bill : { ...bill, id, description: "虚构第二笔" },
    ),
  );
});
it("prioritizes bill records without dashboard cards while keeping review controls", async () => {
  render(<App />);
  await screen.findByLabelText("资金性质");
  expect(
    screen.getByRole("heading", { level: 1, name: "账单核对" }),
  ).toBeInTheDocument();
  const records = screen.getByRole("region", { name: "滚动账单记录" });
  expect(records).toHaveAttribute("tabindex", "0");
  expect(records).toContainElement(screen.getByRole("table"));
  expect(screen.queryByText("当前范围的记录")).toBeNull();
  expect(screen.queryByText("等待你的核对")).toBeNull();
  expect(screen.queryByRole("progressbar")).toBeNull();
  expect(screen.getByLabelText("支付日期范围")).toHaveValue("scope");
  expect(screen.getByRole("button", { name: "下一页" })).toBeInTheDocument();
  expect(
    screen.getByRole("complementary", { name: "逐笔核对" }),
  ).toBeInTheDocument();
});
it("omits income from list filters but permits identifying it in the editor", async () => {
  render(<App />);
  await screen.findByLabelText("消费标题");
  expect(screen.getByLabelText("资金性质筛选")).not.toHaveTextContent("收入");
  expect(screen.getByLabelText("资金性质筛选")).not.toHaveTextContent(
    "内部转账",
  );
  expect(screen.getByLabelText("资金性质")).toHaveTextContent(
    "收入（保存后隐藏）",
  );
  expect(screen.getByLabelText("资金性质")).toHaveTextContent(
    "内部转账（保存后隐藏）",
  );
});
it.each(["income", "internal_transfer"] as const)(
  "moves to a remaining bill after saving hidden %s without reporting a failed save",
  async (kind) => {
    const user = userEvent.setup();
    vi.spyOn(api, "update").mockImplementation(async () => {
      vi.mocked(api.list).mockResolvedValue(
        listFor([{ ...bill, id: "remaining", description: "虚构剩余消费" }]),
      );
      return detailFor({
        ...bill,
        revision: 2,
        kind,
        direction: "inflow",
      });
    });
    render(<App />);
    await screen.findByLabelText("消费标题");
    await user.selectOptions(screen.getByLabelText("资金性质"), kind);
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    expect(
      await screen.findByRole("row", { name: /虚构剩余消费/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("row", { name: /虚构演出/ })).toBeNull();
    expect(screen.getByRole("status")).toHaveTextContent("修改已保存");
    expect(screen.queryByRole("alert")).toBeNull();
  },
);
it("keeps paging through bills in the records-first layout", async () => {
  const user = userEvent.setup();
  vi.mocked(api.list).mockImplementation(async (filters) => ({
    ...listFor(
      filters.page === 1
        ? [bill]
        : [{ ...bill, id: "page-two", description: "虚构下一页" }],
    ),
    page: filters.page,
    total: 51,
  }));
  render(<App />);
  await screen.findByLabelText("资金性质");
  await user.click(screen.getByRole("button", { name: "下一页" }));
  expect(
    await screen.findByRole("row", { name: /虚构下一页/ }),
  ).toBeInTheDocument();
  expect(api.list).toHaveBeenLastCalledWith(
    expect.objectContaining({ page: 2 }),
  );
  expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
  await user.click(screen.getByRole("button", { name: "上一页" }));
  await waitFor(() =>
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1 }),
    ),
  );
});
it("filters reviewed and unreviewed pending bills with independent counts and labels", async () => {
  const user = userEvent.setup();
  const checked = { ...bill, ai_reviewed: true };
  const unchecked = {
    ...bill,
    id: "fictional-unchecked",
    description: "虚构未检查",
  };
  const full = listFor([checked, unchecked]);
  vi.mocked(api.list).mockImplementation(async (filters) => {
    const items =
      filters.status === "ai_reviewed_pending"
        ? [checked]
        : filters.status === "ai_unreviewed_pending"
          ? [unchecked]
          : [checked, unchecked];
    return { ...full, items: listFor(items).items, total: items.length };
  });
  vi.mocked(api.detail).mockImplementation(async (id) =>
    detailFor(id === checked.id ? checked : unchecked),
  );
  render(<App />);
  await screen.findByLabelText("资金性质");
  const checkedButton = screen.getByRole("button", {
    name: "AI已检查待核对",
  });
  const uncheckedButton = screen.getByRole("button", {
    name: "AI未检查待核对",
  });
  expect(checkedButton).toHaveTextContent("1");
  expect(uncheckedButton).toHaveTextContent("1");
  await user.click(checkedButton);
  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /虚构未检查/ })).toBeNull(),
  );
  expect(api.list).toHaveBeenLastCalledWith(
    expect.objectContaining({ status: "ai_reviewed_pending", page: 1 }),
  );
  expect(
    screen.getByRole("complementary", { name: "逐笔核对" }),
  ).toHaveTextContent("AI已检查待核对");
  await user.click(uncheckedButton);
  expect(
    await screen.findByRole("row", { name: /虚构未检查/ }),
  ).toHaveTextContent("AI未检查待核对");
  expect(api.list).toHaveBeenLastCalledWith(
    expect.objectContaining({ status: "ai_unreviewed_pending", page: 1 }),
  );
});
it("keeps unsaved edits when switching AI review status is declined", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App />);
  await user.type(
    await screen.findByLabelText("给这笔记录留个备注"),
    "不要丢弃",
  );
  await user.click(screen.getByRole("button", { name: "AI已检查待核对" }));
  expect(confirm).toHaveBeenCalled();
  expect(api.list).toHaveBeenCalledTimes(1);
  expect(screen.getByLabelText("给这笔记录留个备注")).toHaveValue("不要丢弃");
});
it("combines category and AI review filters and resets pagination", async () => {
  const user = userEvent.setup();
  vi.mocked(api.list).mockImplementation(async (filters) => ({
    ...listFor(),
    page: filters.page,
    total: 101,
  }));
  render(<App />);
  await screen.findByLabelText("资金性质");
  await user.click(screen.getByRole("button", { name: "AI已检查待核对" }));
  await screen.findByLabelText("消费标题");
  await user.click(screen.getByRole("button", { name: "下一页" }));
  await screen.findByLabelText("消费标题");
  expect(api.list).toHaveBeenLastCalledWith(
    expect.objectContaining({ page: 2 }),
  );
  await user.selectOptions(screen.getByLabelText("分类筛选"), "unclassified");
  await waitFor(() =>
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({
        status: "ai_reviewed_pending",
        category: "unclassified",
        page: 1,
      }),
    ),
  );
  await screen.findByLabelText("消费标题");
  await user.selectOptions(screen.getByLabelText("来源平台"), "wechat");
  await waitFor(() =>
    expect(api.list).toHaveBeenLastCalledWith(
      expect.objectContaining({
        provider: "wechat",
        category: "unclassified",
        status: "ai_reviewed_pending",
        page: 1,
      }),
    ),
  );
});
it("keeps a title draft when a category filter change is declined", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App />);
  const title = await screen.findByLabelText("消费标题");
  await user.clear(title);
  await user.type(title, "尚未保存的标题");
  await user.selectOptions(screen.getByLabelText("分类筛选"), "unclassified");
  expect(confirm).toHaveBeenCalled();
  expect(screen.getByLabelText("分类筛选")).toHaveValue("all");
  expect(title).toHaveValue("尚未保存的标题");
  expect(api.list).toHaveBeenCalledTimes(1);
});
it("refreshes the row and detail title after saving without confirming", async () => {
  const user = userEvent.setup();
  vi.mocked(api.list).mockResolvedValue(listFor([bill]));
  vi.spyOn(api, "update").mockImplementation(async (_id, payload) => {
    const renamed = { ...bill, description: payload.description, revision: 2 };
    vi.mocked(api.list).mockResolvedValue(listFor([renamed]));
    return detailFor(renamed);
  });
  render(<App />);
  const title = await screen.findByLabelText("消费标题");
  await user.clear(title);
  await user.type(title, "虚构新活动标题");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(
    await screen.findByRole("row", { name: /虚构新活动标题/ }),
  ).toBeInTheDocument();
  expect(
    screen.getByRole("heading", { name: "虚构新活动标题" }),
  ).toBeInTheDocument();
  expect(api.update).toHaveBeenCalledWith(
    bill.id,
    expect.objectContaining({ description: "虚构新活动标题", confirm: false }),
    "fictional-token",
  );
});
it("requires discard approval before changing filters or leaving a dirty bill", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  render(<App />);
  await user.type(
    await screen.findByLabelText("给这笔记录留个备注"),
    "尚未保存",
  );
  await user.selectOptions(screen.getByLabelText("来源平台"), "alipay");
  expect(confirm).toHaveBeenCalled();
  expect(screen.getByLabelText("来源平台")).toHaveValue("all");
  expect(screen.getByLabelText("给这笔记录留个备注")).toHaveValue("尚未保存");
  await user.click(screen.getByRole("button", { name: /分类管理/ }));
  expect(screen.getByLabelText("给这笔记录留个备注")).toHaveValue("尚未保存");
});
it("ignores an older list response when a newer filter finishes first", async () => {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByLabelText("资金性质");
  let resolveOld: (value: BillList) => void = () => {
    throw new Error("deferred not initialized");
  };
  const old = new Promise<BillList>((resolve) => {
    resolveOld = resolve;
  });
  vi.mocked(api.list).mockImplementation((filters) =>
    filters.provider === "alipay"
      ? old
      : Promise.resolve(
          listFor([{ ...bill, id: "newer", description: "最新结果" }]),
        ),
  );
  await user.selectOptions(screen.getByLabelText("来源平台"), "alipay");
  await user.selectOptions(screen.getByLabelText("来源平台"), "wechat");
  expect(
    await screen.findByRole("row", { name: /最新结果/ }),
  ).toBeInTheDocument();
  await act(async () => {
    resolveOld(listFor([{ ...bill, description: "不应显示的旧结果" }]));
    await old;
  });
  expect(screen.getByLabelText("来源平台")).toHaveValue("wechat");
  expect(screen.queryByRole("row", { name: /不应显示的旧结果/ })).toBeNull();
});
it("confirms once and opens the next record without an unsaved warning", async () => {
  const user = userEvent.setup();
  const confirm = vi.spyOn(window, "confirm");
  vi.spyOn(api, "update").mockResolvedValue(
    detailFor({
      ...bill,
      revision: 2,
      kind: "expense",
      category_id: "culture",
      personal_base_cents: 1200,
      review_status: "user_confirmed",
      classification_locked: 1,
    }),
  );
  render(<App />);
  await screen.findByLabelText("资金性质");
  await user.selectOptions(screen.getByLabelText("资金性质"), "expense");
  await user.selectOptions(screen.getByLabelText("消费大类"), "culture");
  await user.click(screen.getByRole("button", { name: "确认并下一笔 →" }));
  await waitFor(() =>
    expect(
      screen.getByRole("heading", { name: "虚构第二笔" }),
    ).toBeInTheDocument(),
  );
  expect(api.update).toHaveBeenCalledTimes(1);
  expect(confirm).not.toHaveBeenCalled();
  expect(screen.getByRole("status")).toHaveTextContent("已确认");
});
it("retries initialization after a local service failure", async () => {
  vi.mocked(api.session).mockRejectedValueOnce(new Error("服务不可用"));
  const user = userEvent.setup();
  render(<App />);
  expect(await screen.findByRole("alert")).toHaveTextContent("服务不可用");
  await user.click(screen.getByRole("button", { name: /刷新/ }));
  expect(await screen.findByLabelText("资金性质")).toBeInTheDocument();
});
