/** Form-based manual Workflow input UX. Backend remains authoritative. */
import type { JsonValue } from '../../api/types';
import { parseWorkflowRunInputs, planInputDefinitions, type ParsedWorkflowInputs } from './workflowRunInputs';

export type WorkflowInputFormValues = Record<string, string>;
export type WorkflowInputFormIncluded = Record<string, boolean>;

const INPUT_TYPES = new Set(['string', 'integer', 'number', 'boolean', 'object', 'array']);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function canUseWorkflowInputForm(plan: Record<string, JsonValue>): boolean {
  return Object.values(planInputDefinitions(plan)).every(def => INPUT_TYPES.has(def.type));
}

export function parseWorkflowInputForm(
  values: WorkflowInputFormValues,
  included: WorkflowInputFormIncluded,
  plan: Record<string, JsonValue>,
): ParsedWorkflowInputs {
  const definitions = planInputDefinitions(plan);
  const output: Record<string, JsonValue> = Object.create(null);
  for (const [name, definition] of Object.entries(definitions)) {
    if (!definition.required && !(Object.prototype.hasOwnProperty.call(included, name) && included[name] === true)) continue;
    const raw = Object.prototype.hasOwnProperty.call(values, name) ? (values[name] ?? '') : '';
    if (definition.required && raw.trim() === '') {
      return { ok: false, error: `필수 입력값이 없습니다: ${name}` };
    }
    if (definition.secret) {
      if (!UUID.test(raw.trim())) {
        return { ok: false, error: `${name}: 등록된 Secret의 UUID를 입력해 주세요.` };
      }
      output[name] = { kind: 'SECRET_REF', secret_id: raw.trim().toLowerCase() };
      continue;
    }
    switch (definition.type) {
      case 'string':
        output[name] = raw;
        break;
      case 'integer':
      case 'number': {
        if (raw.trim() === '') return { ok: false, error: `${name}: 숫자를 입력해 주세요.` };
        const num = Number(raw);
        if (!Number.isFinite(num) || (definition.type === 'integer' && !Number.isSafeInteger(num))) {
          return { ok: false, error: `${name}: 올바른 ${definition.type === 'integer' ? '정수' : '숫자'}를 입력해 주세요.` };
        }
        output[name] = num;
        break;
      }
      case 'boolean':
        if (raw !== 'true' && raw !== 'false') {
          return { ok: false, error: `${name}: 참 또는 거짓을 선택해 주세요.` };
        }
        output[name] = raw === 'true';
        break;
      case 'object':
      case 'array': {
        let obj: unknown;
        try { obj = JSON.parse(raw); } catch {
          return { ok: false, error: `${name}: 올바른 JSON을 입력해 주세요.` };
        }
        const shapeOk = definition.type === 'array'
          ? Array.isArray(obj)
          : obj !== null && typeof obj === 'object' && !Array.isArray(obj);
        if (!shapeOk) {
          return { ok: false, error: `${name}: JSON ${definition.type === 'array' ? '배열' : '객체'}가 필요합니다.` };
        }
        output[name] = obj as JsonValue;
        break;
      }
      default:
        return { ok: false, error: `${name}: 지원되지 않는 입력 타입입니다.` };
    }
  }
  // Reuse the existing declaration and SECRET_REF checks.
  return parseWorkflowRunInputs(JSON.stringify(output), plan);
}

/** Only called after JSON input has passed canonical client-side validation. */
export function workflowInputsToForm(
  inputs: Record<string, JsonValue>,
  plan: Record<string, JsonValue>,
): { values: WorkflowInputFormValues; included: WorkflowInputFormIncluded } {
  const values: WorkflowInputFormValues = Object.create(null);
  const included: WorkflowInputFormIncluded = Object.create(null);
  for (const [name, definition] of Object.entries(planInputDefinitions(plan))) {
    if (!Object.prototype.hasOwnProperty.call(inputs, name)) continue;
    const value = inputs[name];
    included[name] = true;
    if (definition.secret && value && typeof value === 'object' && !Array.isArray(value)
      && value.kind === 'SECRET_REF' && typeof value.secret_id === 'string') {
      values[name] = value.secret_id;
    } else if (typeof value === 'string') {
      values[name] = value;
    } else {
      values[name] = JSON.stringify(value, null, 2);
    }
  }
  return { values, included };
}
