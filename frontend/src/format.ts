import type {
  Bill,
  Category,
  Direction,
  Kind,
  Payment,
  Provider,
} from "./models";

export const providers: Record<Provider, string> = {
  alipay: "支付宝",
  wechat: "微信",
  meituan: "美团",
  icbc: "工商银行",
  manual: "手工",
};
export const kinds: Record<Kind, string> = {
  pending: "性质待定",
  expense: "消费",
  income: "收入",
  refund: "退款",
  reimbursement: "垫付归还",
  internal_transfer: "内部转账",
  debt_repayment: "还款",
  investment: "投资理财",
};
export const directions: Record<Direction, string> = {
  outflow: "支出",
  inflow: "收入",
  none: "不计收支",
};
export const payments: Record<Payment, string> = {
  settled: "已完成",
  pending: "待确认",
  voided: "已关闭 / 作废",
};
export const reviews: Record<Bill["review_status"], string> = {
  pending: "待核对",
  auto_confirmed: "AI 已确认",
  user_confirmed: "本人已确认",
};
export const count = (value: number) =>
  new Intl.NumberFormat("zh-CN").format(value);
export const money = (cents: number) =>
  new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY" }).format(
    cents / 100,
  );
export function dateText(value: string, time = false): string {
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(time ? ({ hour: "2-digit", minute: "2-digit" } as const) : {}),
  }).format(new Date(value));
}
export function categorySelection(
  id: string | null,
  categories: Category[],
): { root: string; child: string } {
  const category = categories.find((item) => item.id === id);
  return {
    root: category?.parent_id || category?.id || "",
    child: category?.parent_id ? category.id : "",
  };
}
export function parseMoney(value: string): number | null {
  if (!value.trim()) return null;
  if (!/^\d+(?:\.\d{1,2})?$/.test(value.trim()))
    throw new Error("本人承担金额请填写非负数字，最多两位小数。");
  const [whole, fraction = ""] = value.trim().split(".");
  const amount = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  if (!Number.isSafeInteger(amount))
    throw new Error("金额过大，无法可靠保存。");
  return amount;
}
