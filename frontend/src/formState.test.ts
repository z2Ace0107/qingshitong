import { describe, expect, it } from "vitest";
import { initialFieldValue } from "./formState";

describe("initialFieldValue", () => {
  it("represents an unanswered required checkbox as an explicit no", () => {
    expect(initialFieldValue({ type: "checkbox" })).toBe(false);
    expect(initialFieldValue({ type: "checkbox", value: true })).toBe(true);
    expect(initialFieldValue({ type: "text" })).toBe("");
  });
});
