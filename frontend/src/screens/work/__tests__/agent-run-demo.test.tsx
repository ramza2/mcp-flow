// @vitest-environment jsdom

import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import AgentRun from '../AgentRun';
import {
  DEMO_FINISHED,
  DEMO_REQUEST,
  REAL_EXECUTIONS_PATH,
  isDemoRequest,
} from '../agentRunDemo';

describe('Agent Run is an explicitly simulated-only screen', () => {
  beforeEach(() => {
    Element.prototype.scrollIntoView = vi.fn();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('does not simulate an arbitrary Korean user request or call an API', () => {
    const mockedFetch = vi.fn();
    vi.stubGlobal('fetch', mockedFetch);

    render(<MemoryRouter><AgentRun /></MemoryRouter>);
    expect(screen.getByText(/데모 화면 — 실제 실행 아님/)).toBeTruthy();

    fireEvent.change(screen.getByPlaceholderText(/고정 예시 문장만/), {
      target: { value: '프로젝트 아틀라스의 2026년 9월 2일자 검토 문서를 찾아줘' },
    });
    fireEvent.click(screen.getByRole('button', { name: '데모 실행' }));

    expect(screen.getByText(/입력하신 요청은 분석하거나 실행하지 않았습니다/)).toBeTruthy();
    expect(screen.queryByRole('button', { name: /Execution 상세 보기/ })).toBeNull();
    expect(screen.queryByText(/개발팀 3명에게 이메일을 발송했습니다/)).toBeNull();
    expect(mockedFetch).not.toHaveBeenCalled();
  });

  it('accepts only the explicit demonstration sample', () => {
    expect(isDemoRequest(DEMO_REQUEST)).toBe(true);
    expect(isDemoRequest(`  ${DEMO_REQUEST}  `)).toBe(true);
    expect(isDemoRequest('프로젝트 아틀라스 문서를 검색해줘')).toBe(false);
    expect(isDemoRequest('메일 발송해줘')).toBe(false);
  });

  it('never labels the sample as a delivered email or navigates to a fake detail', () => {
    expect(DEMO_FINISHED).toContain('이메일을 발송하지 않았으며');
    expect(DEMO_FINISHED).toContain('실제 Execution 기록도 없습니다');
    expect(REAL_EXECUTIONS_PATH).toBe('/executions');
  });
});
