import type { JsonValue } from '../../api/types';

type InputDefinition = { type: string; required: boolean; secret: boolean };

export function planInputDefinitions(plan: Record<string, JsonValue>): Record<string, InputDefinition> {
  const raw = plan.inputs;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return {};
  const result: Record<string, InputDefinition> = Object.create(null);
  for (const [name, spec] of Object.entries(raw)) {
    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) continue;
    result[name] = {
      type: typeof spec.type === 'string' ? spec.type : 'unknown',
      required: spec.required === true,
      secret: spec.secret === true,
    };
  }
  return result;
}

export type ParsedWorkflowInputs =
  | { ok: true; value: Record<string, JsonValue> }
  | { ok: false; error: string };

/** UX checks only: Backend remains the authority for Plan input validation. */
export function parseWorkflowRunInputs(
  text: string,
  plan: Record<string, JsonValue>,
): ParsedWorkflowInputs {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { ok: false, error: '입력값 JSON 형식이 올바르지 않습니다.' };
  }
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return { ok: false, error: '입력값은 JSON 객체여야 합니다.' };
  }

  const inputs = value as Record<string, JsonValue>;
  const definitions = planInputDefinitions(plan);
  for (const key of Object.keys(inputs)) {
    if (!Object.prototype.hasOwnProperty.call(definitions, key)) {
      return { ok: false, error: `선언되지 않은 입력 항목입니다: ${key}` };
    }
  }
  for (const [key, definition] of Object.entries(definitions)) {
    if (definition.required && !Object.prototype.hasOwnProperty.call(inputs, key)) {
      return { ok: false, error: `필수 입력값이 없습니다: ${key}` };
    }
    if (!Object.prototype.hasOwnProperty.call(inputs, key)) continue;
    const entry = inputs[key];
    if (definition.secret) {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)
        || Object.keys(entry).length !== 2 || entry.kind !== 'SECRET_REF'
        || typeof entry.secret_id !== 'string'
        || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(entry.secret_id)) {
        return { ok: false, error: `${key}: 비밀값 대신 {"kind":"SECRET_REF","secret_id":"UUID"} 참조를 사용하세요.` };
      }
    } else if (definition.type !== 'object' && entry && typeof entry === 'object' && !Array.isArray(entry)
      && entry.kind === 'SECRET_REF') {
      return { ok: false, error: `${key}: 일반 입력에는 SECRET_REF를 사용할 수 없습니다.` };
    }
  }
  return { ok: true, value: inputs };
}
