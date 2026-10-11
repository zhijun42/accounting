import type { Bill, BillList, Category, Detail } from "../models";

export const categories: Category[] = [
  { id: "culture", name: "文化休闲", parent_id: null, parent_name: null },
  { id: "sport", name: "运动户外", parent_id: null, parent_name: null },
  { id: "climbing", name: "攀岩", parent_id: "sport", parent_name: "运动户外" },
];
export const bill: Bill = {
  id: "fictional-first",
  kind: "pending",
  direction: "outflow",
  payment_status: "settled",
  amount_cents: 1200,
  currency: "CNY",
  paid_at: "2026-08-01T04:00:00Z",
  usage_date: null,
  effective_usage_date: "2026-08-01",
  description: "虚构演出 <script>alert(1)</script>",
  counterparty: "虚构商户",
  category_id: null,
  personal_base_cents: null,
  review_status: "pending",
  ai_reviewed: false,
  classification_locked: 0,
  inclusion_status: "included",
  exclusion_reason: null,
  notes: null,
  revision: 1,
  source_provider: "meituan",
  original_category: "餐饮美食",
  original_status: "交易成功",
};
export function detailFor(value = bill): Detail {
  return {
    bill: value,
    sources: [],
    history: [],
    review_items: [
      { id: "fictional-review", topic: "category", reason: "虚构分类待核对" },
    ],
  };
}
export function listFor(
  items = [
    bill,
    { ...bill, id: "fictional-second", description: "虚构第二笔" },
  ],
): BillList {
  return {
    items: items.map((item) => ({
      ...item,
      category_name: null,
      parent_category_name: null,
    })),
    total: items.length,
    page: 1,
    page_size: 50,
    summary: {
      total: items.length,
      pending: items.filter((item) => item.review_status === "pending").length,
      ai_reviewed_pending: items.filter(
        (item) => item.review_status === "pending" && item.ai_reviewed,
      ).length,
      ai_unreviewed_pending: items.filter(
        (item) => item.review_status === "pending" && !item.ai_reviewed,
      ).length,
      confirmed: items.filter((item) => item.review_status !== "pending")
        .length,
      categorized: items.filter((item) => item.category_id !== null).length,
    },
  };
}
