import { expect, it, vi } from "vitest";
import { api, APIError } from "../api";
import { bill, detailFor, listFor } from "./fixtures";

it("rejects successful HTTP responses that do not satisfy runtime types", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ csrf_token: 123, database: "fictional.sqlite3" }),
        ),
      ),
  );
  await expect(api.session()).rejects.toThrow("接口数据与界面类型不一致");
});
it("sends the explicit AI pending filter and validates AI review booleans", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(listFor())))
    .mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          ...detailFor(),
          bill: { ...bill, ai_reviewed: "yes" },
        }),
      ),
    );
  vi.stubGlobal("fetch", fetch);
  await api.list({
    status: "ai_reviewed_pending",
    period: "scope",
    provider: "all",
    kind: "all",
    q: "",
    page: 1,
  });
  expect(String(fetch.mock.calls[0]?.[0])).toContain(
    "status=ai_reviewed_pending",
  );
  await expect(api.detail(bill.id)).rejects.toThrow("接口数据与界面类型不一致");
});
it("rejects unsafe integer amounts instead of trusting JSON", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify(
            detailFor({ ...bill, amount_cents: Number.MAX_SAFE_INTEGER + 1 }),
          ),
        ),
      ),
  );
  await expect(api.detail(bill.id)).rejects.toThrow("接口数据与界面类型不一致");
});
it("includes the write token, revision and explicit confirmation flag", async () => {
  const fetch = vi
    .fn()
    .mockResolvedValue(new Response(JSON.stringify(detailFor())));
  vi.stubGlobal("fetch", fetch);
  const payload = {
    revision: 1,
    kind: bill.kind,
    direction: bill.direction,
    payment_status: bill.payment_status,
    category_id: null,
    personal_base_cents: null,
    usage_date: null,
    notes: "虚构备注",
    inclusion_status: bill.inclusion_status,
    exclusion_reason: null,
    confirm: false,
  };
  await api.update(bill.id, payload, "fictional-token");
  expect(fetch).toHaveBeenCalledWith(
    "/api/bills/fictional-first",
    expect.objectContaining({
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Accounting-CSRF": "fictional-token",
      },
      body: JSON.stringify(payload),
    }),
  );
});
it("preserves server conflict status for the review UI", async () => {
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ error: "记录已更新" }), { status: 409 }),
      ),
  );
  await expect(api.detail(bill.id)).rejects.toEqual(
    new APIError("记录已更新", 409),
  );
});
it("explains unavailable local service and malformed JSON", async () => {
  const fetch = vi
    .fn()
    .mockRejectedValueOnce(new TypeError("network"))
    .mockResolvedValueOnce(new Response("<html>not JSON</html>"));
  vi.stubGlobal("fetch", fetch);
  await expect(api.session()).rejects.toThrow("无法连接本地服务");
  await expect(api.session()).rejects.toThrow("无法识别的数据");
});
