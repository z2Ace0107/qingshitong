import type { ChangeEvent } from "react";

type MaterialContent = Record<string, unknown>;

interface ActivityPlanFieldsProps {
  value: MaterialContent;
  onChange: (key: string, value: unknown) => void;
}

function textValue(value: unknown) {
  return value === null || value === undefined ? "" : String(value);
}

export function ActivityPlanFields({ value, onChange }: ActivityPlanFieldsProps) {
  const handleChange = (key: string) => (event: ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => {
    onChange(key, event.target.value);
  };

  return (
    <div className="material-fields activity-plan-fields">
      <label className="field-control full" htmlFor="activity-plan-purpose">
        <span>活动目的</span>
        <textarea id="activity-plan-purpose" rows={3} value={textValue(value.purpose)} onChange={handleChange("purpose")} placeholder="说明活动要解决什么问题或达到什么目标" />
      </label>
      <label className="field-control" htmlFor="activity-plan-audience">
        <span>活动对象</span>
        <input id="activity-plan-audience" value={textValue(value.audience)} onChange={handleChange("audience")} placeholder="例如：在校学生" />
      </label>
      <label className="field-control full" htmlFor="activity-plan-content">
        <span>活动内容</span>
        <textarea id="activity-plan-content" rows={4} value={textValue(value.content)} onChange={handleChange("content")} placeholder="说明活动的主要内容和安排" />
      </label>
      <label className="field-control full" htmlFor="activity-plan-schedule">
        <span>时间安排</span>
        <textarea id="activity-plan-schedule" rows={4} value={textValue(value.schedule)} onChange={handleChange("schedule")} placeholder="例如：14:00 签到；14:30 开始；16:30 结束" />
      </label>
    </div>
  );
}
