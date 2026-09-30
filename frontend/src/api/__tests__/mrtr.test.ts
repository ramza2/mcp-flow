import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import {
  listInputRequests,
  rejectInputRequest,
  submitInputResponse,
} from '../mrtr';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('MRTR API client boundary', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('listInputRequests never surfaces requestState in typed DTO usage', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        items: [
          {
            id: '11111111-1111-4111-8111-111111111111',
            status: 'OPEN',
            source: 'MCP_MRTR',
            execution_id: '22222222-2222-4222-8222-222222222222',
            step_execution_id: '33333333-3333-4333-8333-333333333333',
            round_no: 1,
            input_requests: {
              city: { type: 'string', description: 'City' },
            },
            expires_at: '2026-09-02T15:05:00Z',
            requested_at: '2026-09-02T15:00:00Z',
            answered_at: null,
          },
        ],
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await listInputRequests('22222222-2222-4222-8222-222222222222', {
      status: 'OPEN',
    });
    expect(result.items).toHaveLength(1);
    expect(result.items[0]).not.toHaveProperty('requestState');
    expect(result.items[0]).not.toHaveProperty('request_state');
    expect(JSON.stringify(result)).not.toMatch(/requestState/);
  });

  it('submitInputResponse posts only responses map (never requestState)', async () => {
    setCachedCsrfTokenForTests('test-csrf');
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          input_request_id: '11111111-1111-4111-8111-111111111111',
          execution_id: '22222222-2222-4222-8222-222222222222',
          status: 'ANSWERED',
          resume_enqueued: true,
          execution_status: 'WAITING_INPUT',
          step_status: 'WAITING_INPUT',
        },
        201,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await submitInputResponse(
      '22222222-2222-4222-8222-222222222222',
      '11111111-1111-4111-8111-111111111111',
      { city: 'Seoul' },
    );

    expect(fetchMock).toHaveBeenCalled();
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({ responses: { city: 'Seoul' } });
    expect(body).not.toHaveProperty('requestState');
    expect(body).not.toHaveProperty('inputResponses');
  });

  it('rejectInputRequest posts without requestState', async () => {
    setCachedCsrfTokenForTests('test-csrf');
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        input_request_id: '11111111-1111-4111-8111-111111111111',
        execution_id: '22222222-2222-4222-8222-222222222222',
        status: 'REJECTED',
        execution_status: 'FAILED',
        step_status: 'FAILED',
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    const result = await rejectInputRequest(
      '22222222-2222-4222-8222-222222222222',
      '11111111-1111-4111-8111-111111111111',
    );
    expect(result.status).toBe('REJECTED');
    expect(result).not.toHaveProperty('requestState');
  });
});
