import { describe, expect, it } from 'vitest';
import { parseWorkflowRunInputs } from '@/screens/build/workflowRunInputs';
import type { JsonValue } from '@/api/types';

const plan: Record<string, JsonValue> = {
  inputs: {
    title: { type: 'string', required: true, secret: false },
    credential: { type: 'string', required: false, secret: true },
    metadata: { type: 'object', required: false, secret: false },
  },
};

describe('workflow manual execution input validation', () => {
  it('accepts declared values and canonical secret references', () => {
    const value = { title: '일일 보고', credential: { kind: 'SECRET_REF', secret_id: '33333333-3333-4333-8333-333333333333' } };
    expect(parseWorkflowRunInputs(JSON.stringify(value), plan)).toEqual({ ok: true, value });
  });

  it('rejects non-object JSON, unknown keys and missing required input', () => {
    expect(parseWorkflowRunInputs('{', plan)).toMatchObject({ ok: false });
    expect(parseWorkflowRunInputs('[]', plan)).toMatchObject({ ok: false });
    expect(parseWorkflowRunInputs('{"title":"ok","unknown":1}', plan)).toMatchObject({ ok: false, error: expect.stringContaining('unknown') });
    expect(parseWorkflowRunInputs('{}', plan)).toMatchObject({ ok: false, error: expect.stringContaining('title') });
  });

  it('requires secret references instead of raw secret values', () => {
    expect(parseWorkflowRunInputs('{"title":"ok","credential":"secret"}', plan)).toMatchObject({ ok: false, error: expect.stringContaining('SECRET_REF') });
    expect(parseWorkflowRunInputs('{"title":"ok","credential":{"kind":"SECRET_REF","secret_id":"bad"}}', plan)).toMatchObject({ ok: false });
  });

  it('lets object-typed inputs use reference-shaped objects as backend permits', () => {
    const value = { title: 'ok', metadata: { kind: 'SECRET_REF', secret_id: '33333333-3333-4333-8333-333333333333' } };
    expect(parseWorkflowRunInputs(JSON.stringify(value), plan)).toEqual({ ok: true, value });
  });

  it('does not confuse prototype property names with declared inputs', () => {
    expect(parseWorkflowRunInputs('{"title":"ok","toString":"not-declared"}', plan)).toMatchObject({ ok: false });
  });
});
