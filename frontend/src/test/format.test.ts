import { describe, expect, it } from "vitest";
import { dateRange } from "../api";
import { categorySelection, dateText, parseMoney } from "../format";
import { categories } from "./fixtures";

describe("money and local dates", () => {
  it("converts decimal text to integer cents without floating point rounding", () => {
    expect(parseMoney("0.29")).toBe(29);
    expect(parseMoney(" 12.1 ")).toBe(1210);
    expect(parseMoney("0")).toBe(0);
    expect(parseMoney("")).toBeNull();
  });
  it.each(["-1", "1.001", "1e3", "NaN", "9007199254740992"])(
    "rejects unreliable amount %s",
    (value) => {
      expect(() => parseMoney(value)).toThrow();
    },
  );
  it("uses Shanghai date boundaries and real calendar month lengths", () => {
    expect(dateText("2026-01-31T17:00:00Z")).toContain("2026/02/01");
    expect(dateRange("02")).toEqual(["2026-02-01", "2026-02-28"]);
    expect(dateRange("scope")).toEqual(["2026-01-01", "2026-09-30"]);
    expect(() => dateRange("13")).toThrow();
  });
  it("resolves root and optional child categories", () => {
    expect(categorySelection("climbing", categories)).toEqual({
      root: "sport",
      child: "climbing",
    });
    expect(categorySelection("culture", categories)).toEqual({
      root: "culture",
      child: "",
    });
  });
});
