import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ActivityPlanFields } from "./materials";

describe("ActivityPlanFields", () => {
  it("renders the four structured activity-plan fields and reports edits", () => {
    const onChange = vi.fn();

    render(<ActivityPlanFields value={{}} onChange={onChange} />);

    expect(screen.getByLabelText("活动目的")).toBeTruthy();
    expect(screen.getByLabelText("活动对象")).toBeTruthy();
    expect(screen.getByLabelText("活动内容")).toBeTruthy();
    expect(screen.getByLabelText("时间安排")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("活动目的"), { target: { value: "迎新交流" } });
    expect(onChange).toHaveBeenCalledWith("purpose", "迎新交流");
  });
});
