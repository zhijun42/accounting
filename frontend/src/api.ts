import { z } from "zod";
import {
  categoriesSchema,
  detailSchema,
  historySchema,
  listSchema,
  newCategorySchema,
  sessionSchema,
} from "./models";
import type { BillUpdate, Filters } from "./models";

export class APIError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : "请求未完成，请重新加载检查。";
}

async function request<T>(
  path: string,
  schema: z.ZodType<T>,
  token?: string,
  payload?: unknown,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(
      path,
      payload === undefined
        ? { cache: "no-store" }
        : {
            method: "POST",
            cache: "no-store",
            headers: {
              "Content-Type": "application/json",
              "X-Accounting-CSRF": token ?? "",
            },
            body: JSON.stringify(payload),
          },
    );
  } catch {
    throw new Error("无法连接本地服务。请检查服务是否仍在运行，再重试。");
  }
  let data: unknown;
  try {
    data = await response.json();
  } catch {
    throw new APIError(
      "本地服务返回了无法识别的数据，请重新加载检查。",
      response.status,
    );
  }
  if (!response.ok) {
    const failure = z.object({ error: z.string() }).safeParse(data);
    throw new APIError(
      failure.success ? failure.data.error : "请求失败，请重新加载检查。",
      response.status,
    );
  }
  const result = schema.safeParse(data);
  if (!result.success)
    throw new Error(
      "接口数据与界面类型不一致。请重新加载检查服务版本和保存结果。",
    );
  return result.data;
}

export function dateRange(period: string): [string, string] {
  if (period === "scope") return ["2026-01-01", "2026-09-30"];
  if (period === "all") return ["2026-01-01", "2026-12-31"];
  if (!/^(0[1-9]|1[0-2])$/.test(period)) throw new Error("支付月份不合法");
  const last = new Date(Date.UTC(2026, Number(period), 0)).getUTCDate();
  return [`2026-${period}-01`, `2026-${period}-${last}`];
}

export const api = {
  session: () => request("/api/session", sessionSchema),
  categories: () => request("/api/categories", categoriesSchema),
  history: () => request("/api/history", historySchema),
  detail: (id: string) =>
    request("/api/bills/" + encodeURIComponent(id), detailSchema),
  list: (filters: Filters) => {
    const [start, end] = dateRange(filters.period);
    const query = new URLSearchParams({
      start,
      end,
      status: filters.status,
      provider: filters.provider,
      kind: filters.kind,
      q: filters.q,
      page: String(filters.page),
    });
    return request("/api/bills?" + query, listSchema);
  },
  update: (id: string, payload: BillUpdate, token: string) =>
    request(
      "/api/bills/" + encodeURIComponent(id),
      detailSchema,
      token,
      payload,
    ),
  createCategory: (name: string, parent: string, token: string) =>
    request("/api/categories", newCategorySchema, token, {
      name,
      parent_id: parent || null,
    }),
};
