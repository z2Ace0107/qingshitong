export interface FieldValueSource {
  type: string;
  value?: unknown;
}

export function initialFieldValue(field: FieldValueSource): unknown {
  if (field.value !== undefined && field.value !== null) return field.value;
  return field.type === "checkbox" ? false : "";
}
