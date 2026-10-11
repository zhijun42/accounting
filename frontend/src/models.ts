import { z } from "zod";

const integer = z.number().int().safe();
const text = z.string().nullable();
export const kindSchema = z.enum([
  "pending",
  "expense",
  "income",
  "refund",
  "reimbursement",
  "internal_transfer",
  "debt_repayment",
  "investment",
]);
export const providerSchema = z.enum([
  "alipay",
  "wechat",
  "meituan",
  "icbc",
  "manual",
]);
export const directionSchema = z.enum(["outflow", "inflow", "none"]);
export const paymentSchema = z.enum(["settled", "pending", "voided"]);
export const inclusionSchema = z.enum(["included", "excluded"]);
export const billSchema = z.object({
  id: z.string(),
  kind: kindSchema,
  direction: directionSchema,
  payment_status: paymentSchema,
  amount_cents: integer.nonnegative(),
  currency: z.literal("CNY"),
  paid_at: z.iso.datetime(),
  usage_date: text,
  effective_usage_date: z.string(),
  description: text,
  counterparty: text,
  category_id: text,
  personal_base_cents: integer.nonnegative().nullable(),
  review_status: z.enum(["pending", "auto_confirmed", "user_confirmed"]),
  ai_reviewed: z.boolean(),
  classification_locked: z.union([z.literal(0), z.literal(1)]),
  inclusion_status: inclusionSchema,
  exclusion_reason: text,
  notes: text,
  revision: integer.positive(),
  source_provider: providerSchema.nullable(),
  original_category: text,
  original_status: text,
});
export const categorySchema = z.object({
  id: z.string(),
  parent_id: text,
  name: z.string(),
  parent_name: text,
});
export const auditSchema = z.object({
  id: integer,
  entity_table: z.string(),
  entity_id: z.string(),
  operation: z.enum(["insert", "update", "delete"]),
  reason: z.string(),
  created_at: z.iso.datetime(),
  actor_type: z.enum(["user", "ai", "importer", "system"]),
  before: z.record(z.string(), z.unknown()).nullable(),
  after: z.record(z.string(), z.unknown()).nullable(),
});
export const sourceSchema = z.object({
  id: z.string(),
  provider: providerSchema,
  source_filename: z.string(),
  transaction_id: text,
  payload: z.record(z.string(), z.unknown()),
  locator: z.record(z.string(), z.unknown()),
});
export const detailSchema = z.object({
  bill: billSchema,
  sources: z.array(sourceSchema),
  history: z.array(auditSchema),
  review_items: z.array(
    z.object({ id: z.string(), topic: z.string(), reason: z.string() }),
  ),
});
export const listSchema = z.object({
  items: z.array(
    billSchema.extend({ category_name: text, parent_category_name: text }),
  ),
  total: integer.nonnegative(),
  page: integer.positive(),
  page_size: integer.positive(),
  summary: z.object({
    total: integer.nonnegative(),
    pending: integer.nonnegative(),
    ai_reviewed_pending: integer.nonnegative(),
    ai_unreviewed_pending: integer.nonnegative(),
    confirmed: integer.nonnegative(),
    categorized: integer.nonnegative(),
  }),
});
export const sessionSchema = z.object({
  csrf_token: z.string().min(1),
  database: z.string(),
});
export const categoriesSchema = z.object({
  categories: z.array(categorySchema),
});
export const newCategorySchema = categoriesSchema.extend({ id: z.string() });
export const historySchema = z.object({ items: z.array(auditSchema) });

export type Kind = z.infer<typeof kindSchema>;
export type Provider = z.infer<typeof providerSchema>;
export type Direction = z.infer<typeof directionSchema>;
export type Payment = z.infer<typeof paymentSchema>;
export type Inclusion = z.infer<typeof inclusionSchema>;
export type Bill = z.infer<typeof billSchema>;
export type BillList = z.infer<typeof listSchema>;
export type Detail = z.infer<typeof detailSchema>;
export type Category = z.infer<typeof categorySchema>;
export type Audit = z.infer<typeof auditSchema>;
export type Source = z.infer<typeof sourceSchema>;
export type Status =
  "all" | "ai_reviewed_pending" | "ai_unreviewed_pending" | "confirmed";
export type View = "bills" | "categories" | "history";
export type Filters = {
  status: Status;
  period: string;
  provider: string;
  kind: string;
  q: string;
  page: number;
};
export type BillUpdate = Pick<
  Bill,
  | "revision"
  | "kind"
  | "direction"
  | "payment_status"
  | "category_id"
  | "personal_base_cents"
  | "usage_date"
  | "notes"
  | "inclusion_status"
  | "exclusion_reason"
> & { confirm: boolean };
