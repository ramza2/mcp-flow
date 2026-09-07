import { describe, expect, it } from 'vitest';
import { parseTagInput } from '../toolLifecycle';

describe('parseTagInput', () => {
  it('rejects a tag longer than 64 characters', () => {
    expect(parseTagInput('x'.repeat(65))).toEqual({
      ok: false,
      error: '각 태그는 최대 64자까지 입력할 수 있습니다.',
    });
  });

  it('rejects more than 32 unique tags', () => {
    const raw = Array.from({ length: 33 }, (_, i) => `tag-${i}`).join(',');
    expect(parseTagInput(raw)).toEqual({
      ok: false,
      error: '태그는 최대 32개까지 입력할 수 있습니다.',
    });
  });

  it('drops blanks and duplicates while preserving order', () => {
    expect(parseTagInput(' alpha , , beta, alpha\nbeta ')).toEqual({
      ok: true,
      tags: ['alpha', 'beta'],
    });
  });

  it('accepts exactly 32 unique tags and 64-char tags', () => {
    const tags = Array.from({ length: 32 }, (_, i) => `t${i}`);
    expect(parseTagInput(tags.join(','))).toEqual({ ok: true, tags });
    expect(parseTagInput('y'.repeat(64))).toEqual({ ok: true, tags: ['y'.repeat(64)] });
  });
});
