import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import { listRunnableAgents, submitAgentRun } from '../agentRun';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Agent Run automatic default contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('reads server-selected default without guessing the first agent', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(
      jsonResponse({ items: [{ id: 'agent-a' }, { id: 'agent-b' }], default_agent_id: 'agent-b' }),
    ));
    const result = await listRunnableAgents();
    expect(result.default_agent_id).toBe('agent-b');
  });

  it('omits agent_id for automatic mode but preserves explicit selection', async () => {
    const fetchMock = vi.fn().mockImplementation(async () =>
      jsonResponse({ id: 'request-a', status: 'RECEIVED' }, 201),
    );
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('test-csrf');

    await submitAgentRun(null, '반려동물을 조회해 줘');
    let init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ request_text: '반려동물을 조회해 줘' });

    await submitAgentRun('agent-b', '날씨를 확인해 줘');
    init = fetchMock.mock.calls[1][1] as RequestInit;
    expect(JSON.parse(String(init.body))).toEqual({
      agent_id: 'agent-b',
      request_text: '날씨를 확인해 줘',
    });
  });
});
