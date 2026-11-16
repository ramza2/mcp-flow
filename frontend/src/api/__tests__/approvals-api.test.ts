import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import { createApprovalDecision, getApproval, listApprovals } from '../approvals';

function ok(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const approvalId = '11111111-1111-4111-8111-111111111111';
const executionId = '22222222-2222-4222-8222-222222222222';

describe('Approval query API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('lists pending inbox with pagination and sort query params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listApprovals({ page: 2, page_size: 20, status: 'PENDING', sort: 'expires_at' });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/approvals?');
    expect(url).toContain('page=2');
    expect(url).toContain('page_size=20');
    expect(url).toContain('status=PENDING');
    expect(url).toContain('sort=expires_at');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method ?? 'GET').toBe('GET');
    expect(init.credentials).toBe('same-origin');
  });

  it('omits empty optional filters and defaults page when omitted', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listApprovals({ status: 'APPROVED' });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('page=1');
    expect(url).toContain('status=APPROVED');
    expect(url).not.toContain('execution_id=');
    expect(url).not.toContain('requested_by=');
  });

  it('loads detail by id without inventing fields', async () => {
    const body = {
      id: approvalId,
      status: 'PENDING',
      execution_id: executionId,
      step_execution_id: '33333333-3333-4333-8333-333333333333',
      requested_by: '44444444-4444-4444-8444-444444444444',
      decision_mode: 'ANY',
      required_approvals: 1,
      requested_at: '2026-11-16T00:00:00Z',
      expires_at: '2026-11-16T01:00:00Z',
      resolved_at: null,
      approve_count: 0,
      reject_count: 0,
      can_decide: true,
      safe_context: { tool_name: 'deletePet', risk_class: 'DESTRUCTIVE' },
      decisions: [],
    };
    const fetchMock = vi.fn().mockResolvedValue(ok(body));
    vi.stubGlobal('fetch', fetchMock);

    const detail = await getApproval(approvalId);
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/v1/approvals/${approvalId}`);
    expect(detail.status).toBe('PENDING');
    expect(detail.can_decide).toBe(true);
    expect(detail.safe_context).toEqual({
      tool_name: 'deletePet',
      risk_class: 'DESTRUCTIVE',
    });
    expect(detail).not.toHaveProperty('context_snapshot');
    expect(detail).not.toHaveProperty('context_hash');
  });

  it('POSTs decisions with CSRF and never sends context_hash', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok(
        {
          decision_id: '77777777-7777-4777-8777-777777777777',
          approval_request_id: approvalId,
          approval_status: 'APPROVED',
          execution_id: executionId,
          step_execution_id: '33333333-3333-4333-8333-333333333333',
          execution_status: 'WAITING_APPROVAL',
          step_status: 'WAITING_APPROVAL',
          decision: 'APPROVE',
          decided_by: '44444444-4444-4444-8444-444444444444',
          decided_at: '2026-11-16T00:30:00Z',
          resume_enqueued: true,
        },
        201,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await createApprovalDecision(approvalId, { decision: 'APPROVE' });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/approvals/${approvalId}/decisions`);
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('same-origin');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe('csrf-test');
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({ decision: 'APPROVE' });
    expect(body).not.toHaveProperty('context_hash');
  });

  it('includes reject comment when provided and still omits context_hash', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok(
        {
          decision_id: '88888888-8888-4888-8888-888888888888',
          approval_request_id: approvalId,
          approval_status: 'REJECTED',
          execution_id: executionId,
          step_execution_id: '33333333-3333-4333-8333-333333333333',
          execution_status: 'FAILED',
          step_status: 'FAILED',
          decision: 'REJECT',
          decided_by: '44444444-4444-4444-8444-444444444444',
          decided_at: '2026-11-16T00:30:00Z',
          resume_enqueued: false,
        },
        201,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await createApprovalDecision(approvalId, {
      decision: 'REJECT',
      comment: '정책상 거절',
    });
    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(body).toEqual({ decision: 'REJECT', comment: '정책상 거절' });
    expect(body).not.toHaveProperty('context_hash');
  });
});
