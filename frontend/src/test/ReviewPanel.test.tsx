import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, it, vi } from "vitest";
import { APIError } from "../api";
import { ReviewPanel } from "../components/ReviewPanel";
import { bill, categories, detailFor } from "./fixtures";

function props() {
  return {
    detail: detailFor(),
    categories,
    open: true,
    saving: false,
    dirty: false,
    assigned: null,
    onDirty: vi.fn(),
    onSave: vi.fn().mockResolvedValue(undefined),
    onReload: vi.fn(),
    onClose: vi.fn(),
    onAddCategory: vi.fn(),
  };
}
it("shows unresolved AI questions without presenting them as confirmation", () => {
  const input = props();
  render(<ReviewPanel {...input} />);
  expect(
    screen.getByRole("region", { name: "需要本人核对" }),
  ).toHaveTextContent("虚构分类待核对");
  expect(screen.getByLabelText("资金性质")).toHaveValue("pending");
});
it("saves user date, child category and integer share without confirming a draft", async () => {
  const user = userEvent.setup();
  const input = props();
  render(<ReviewPanel {...input} />);
  await user.selectOptions(screen.getByLabelText("资金性质"), "expense");
  expect(screen.getByLabelText("本人承担（元）")).toHaveValue("12.00");
  await user.selectOptions(screen.getByLabelText("消费大类"), "sport");
  await user.selectOptions(screen.getByLabelText("消费小类"), "climbing");
  await user.type(screen.getByLabelText("使用日期"), "2026-10-15");
  await user.clear(screen.getByLabelText("本人承担（元）"));
  await user.type(screen.getByLabelText("本人承担（元）"), "0.29");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(input.onSave).toHaveBeenCalledWith(
    expect.objectContaining({
      revision: 1,
      kind: "expense",
      category_id: "climbing",
      usage_date: "2026-10-15",
      personal_base_cents: 29,
      confirm: false,
    }),
  );
  expect(document.querySelector("script")).toBeNull();
});
it("adding a category preserves unsaved notes and date", async () => {
  const user = userEvent.setup();
  const input = props();
  const { rerender } = render(<ReviewPanel {...input} />);
  await user.selectOptions(screen.getByLabelText("资金性质"), "expense");
  await user.type(
    screen.getByLabelText("给这笔记录留个备注"),
    "还没保存的备注",
  );
  await user.type(screen.getByLabelText("使用日期"), "2026-10-15");
  rerender(<ReviewPanel {...input} assigned="climbing" />);
  expect(screen.getByLabelText("给这笔记录留个备注")).toHaveValue(
    "还没保存的备注",
  );
  expect(screen.getByLabelText("使用日期")).toHaveValue("2026-10-15");
  expect(screen.getByLabelText("消费小类")).toHaveValue("climbing");
});
it("keeps inputs and offers refresh after a revision conflict", async () => {
  const user = userEvent.setup();
  const input = props();
  input.onSave.mockRejectedValue(new APIError("记录已被修改", 409));
  render(<ReviewPanel {...input} />);
  await user.type(screen.getByLabelText("给这笔记录留个备注"), "不能被覆盖");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("当前输入已保留");
  expect(screen.getByLabelText("给这笔记录留个备注")).toHaveValue("不能被覆盖");
});
it("clears consumption-only fields when confirming a refund", async () => {
  const user = userEvent.setup();
  const input = props();
  render(
    <ReviewPanel
      {...input}
      detail={detailFor({
        ...bill,
        kind: "expense",
        category_id: "climbing",
        personal_base_cents: 1200,
        usage_date: "2026-10-15",
      })}
    />,
  );
  await user.selectOptions(screen.getByLabelText("资金性质"), "refund");
  await user.click(screen.getByRole("button", { name: "确认并下一笔 →" }));
  expect(input.onSave).toHaveBeenCalledWith(
    expect.objectContaining({
      kind: "refund",
      direction: "inflow",
      category_id: null,
      personal_base_cents: null,
      usage_date: null,
      confirm: true,
    }),
  );
});
it("blocks malformed money and disables all edits during a save", async () => {
  const user = userEvent.setup();
  const input = props();
  const { rerender } = render(<ReviewPanel {...input} />);
  await user.selectOptions(screen.getByLabelText("资金性质"), "expense");
  await user.clear(screen.getByLabelText("本人承担（元）"));
  await user.type(screen.getByLabelText("本人承担（元）"), "1.001");
  await user.click(screen.getByRole("button", { name: "保存修改" }));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent("最多两位小数"),
  );
  expect(input.onSave).not.toHaveBeenCalled();
  rerender(<ReviewPanel {...input} saving />);
  expect(screen.getByLabelText("给这笔记录留个备注")).toBeDisabled();
  expect(screen.getByRole("button", { name: "确认并下一笔 →" })).toBeDisabled();
});
