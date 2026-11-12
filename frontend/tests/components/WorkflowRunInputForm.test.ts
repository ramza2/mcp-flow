import { describe, expect, it } from 'vitest';
import type { JsonValue } from '@/api/types';
import {
  canUseWorkflowInputForm, parseWorkflowInputForm, workflowInputsToForm,
} from '@/screens/build/workflowRunInputForm';

const plan: Record<string, JsonValue> = { inputs: {
  title: { type: 'string', required: true, secret: false },
  count: { type: 'integer', required: true, secret: false },
  ratio: { type: 'number', required: true, secret: false },
  dry_run: { type: 'boolean', required: true, secret: false },
  metadata: { type: 'object', required: false, secret: false },
  items: { type: 'array', required: false, secret: false },
  credential: { type: 'string', required: false, secret: true },
} };
const secretId = '33333333-3333-4333-8333-333333333333';
const requiredValues = { title: '검증', count: '0', ratio: '1.5', dry_run: 'false' };

describe('workflow manual run schema-driven form', () => {
  it('supports canonical Plan input types without changing backend contract', () => {
    expect(canUseWorkflowInputForm(plan)).toBe(true);
    expect(canUseWorkflowInputForm({ inputs: { unsupported: { type: 'file', required: false } } })).toBe(false);
  });

  it('converts string/integer/number/boolean and omits unchecked optional keys', () => {
    const parsed = parseWorkflowInputForm(requiredValues, {}, plan);
    expect(parsed).toEqual({
      ok: true, value: { title: '검증', count: 0, ratio: 1.5, dry_run: false },
    });
  });

  it('keeps explicit optional false/empty string when enabled', () => {
    const optPlan: Record<string, JsonValue> = {
      inputs: { optional_string: { type: 'string', required: false }, optional_bool: { type: 'boolean', required: false } },
    };
    expect(parseWorkflowInputForm(
      { optional_string: '', optional_bool: 'false' },
      { optional_string: true, optional_bool: true }, optPlan,
    )).toEqual({ ok: true, value: { optional_string: '', optional_bool: false } });
    expect(parseWorkflowInputForm({ optional_bool: 'false' }, {}, optPlan)).toEqual({ ok: true, value: {} });
  });

  it('parses object/array and canonical SECRET_REF without exposing secret content', () => {
    const parsed = parseWorkflowInputForm(
      { ...requiredValues, metadata: '{"team":"A"}', items: '[1,2]', credential: secretId },
      { metadata: true, items: true, credential: true },
      plan,
    );
    expect(parsed).toEqual({ ok: true, value: {
      title: '검증', count: 0, ratio: 1.5, dry_run: false,
      metadata: { team: 'A' }, items: [1, 2],
      credential: { kind: 'SECRET_REF', secret_id: secretId },
    } });
  });

  it('blocks missing required, invalid number, boolean, malformed JSON, and raw secret', () => {
    const assertError = (values: Record<string, string>, included: Record<string, boolean>, message: string) => {
      const result = parseWorkflowInputForm(values, included, plan);
      expect(result).toMatchObject({ ok: false, error: expect.stringContaining(message) });
    };
    assertError({ ...requiredValues, title: '' }, {}, 'title');
    assertError({ ...requiredValues, count: '1.2' }, {}, 'count');
    assertError({ ...requiredValues, count: '9007199254740993' }, {}, 'count');
    assertError({ ...requiredValues, ratio: 'Infinity' }, {}, 'ratio');
    assertError({ ...requiredValues, dry_run: '' }, {}, 'dry_run');
    assertError({ ...requiredValues, metadata: '{' }, { metadata: true }, 'metadata');
    assertError({ ...requiredValues, items: '{}' }, { items: true }, 'items');
    assertError({ ...requiredValues, credential: 'plaintext-password' }, { credential: true }, 'credential');
    assertError({ ...requiredValues, credential: '' }, { credential: true }, 'credential');
  });

  it('round trips validated JSON values and optional selected keys', () => {
    const inputs = {
      title: '보고서', count: 7, ratio: 0, dry_run: false,
      items: ['a'], credential: { kind: 'SECRET_REF', secret_id: secretId },
    };
    const form = workflowInputsToForm(inputs, plan);
    expect(form.included).toMatchObject({ items: true, credential: true });
    expect(parseWorkflowInputForm(form.values, form.included, plan)).toEqual({ ok: true, value: inputs });
  });

  it('uses an empty object for workflows with no declared inputs', () => {
    expect(parseWorkflowInputForm({}, {}, { inputs: {} })).toEqual({ ok: true, value: {} });
  });

  it('never includes inherited object properties as input values', () => {
    const values = Object.assign(Object.create(null), requiredValues);
    const included = Object.create({ metadata: true });
    expect(parseWorkflowInputForm(values, included, plan)).toEqual({
      ok: true, value: { title: '검증', count: 0, ratio: 1.5, dry_run: false },
    });
  });
});
