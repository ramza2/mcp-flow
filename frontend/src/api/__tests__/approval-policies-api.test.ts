import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import {
  activateApprovalPolicy,
  buildApproverScope,
  createApprovalPolicy,
  deactivateApprovalPolicy,
  listApprovalPolicies,
  parseApproverRoleCodes,
  updateApprovalPolicy,
} from '../approvalPolicies';

function ok(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

const policyId = '11111111-1111-4111-8111-111111111111';

describe('ApprovalPolicy admin API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('lists with pagination, status, and search query params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      ok({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listApprovalPolicies({
      page: 2,
      page_size: 20,
      status: 'ACTIVE',
      q: '외부',
      sort: '-updated_at',
    });

    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/approval-policies?');
    expect(url).toContain('page=2');
    expect(url).toContain('page_size=20');
    expect(url).toContain('status=ACTIVE');
    expect(url).toContain('q=');
    expect(url).toContain('sort=-updated_at');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.credentials).toBe('same-origin');
  });

  it('creates with CSRF cookie session', async () => {
    setCachedCsrfTokenForTests('csrf-test-token');
    const fetchMock = vi.fn().mockResolvedValue(
      ok(
        {
          id: policyId,
          code: 'external-transfer',
          name: '외부 전송 승인',
          description: null,
          status: 'ACTIVE',
          decision_mode: 'ANY',
          required_approvals: 1,
          approver_scope: { role_codes: ['APPROVER'] },
          default_expiry_seconds: 3600,
          allow_self_approval: false,
          reject_comment_required: true,
          created_at: '2026-11-16T00:00:00Z',
          updated_at: '2026-11-16T00:00:00Z',
          lock_version: 1,
        },
        201,
      ),
    );
    vi.stubGlobal('fetch', fetchMock);

    await createApprovalPolicy({
      code: 'external-transfer',
      name: '외부 전송 승인',
      decision_mode: 'ANY',
      required_approvals: 1,
      approver_scope: { role_codes: ['APPROVER'] },
    });

    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/approval-policies');
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    expect((init.headers as Record<string, string>)['X-CSRF-Token']).toBe(
      'csrf-test-token',
    );
  });

  it('PATCH sends If-Match and body.lock_version', async () => {
    setCachedCsrfTokenForTests('csrf-test-token');
    const fetchMock = vi.fn().mockResolvedValue(
      ok({
        id: policyId,
        code: 'external-transfer',
        name: 'Renamed',
        description: null,
        status: 'ACTIVE',
        decision_mode: 'ANY',
        required_approvals: 1,
        approver_scope: null,
        default_expiry_seconds: 3600,
        allow_self_approval: false,
        reject_comment_required: false,
        created_at: '2026-11-16T00:00:00Z',
        updated_at: '2026-11-16T00:00:00Z',
        lock_version: 2,
      }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await updateApprovalPolicy(policyId, { name: 'Renamed' }, 7);

    expect(fetchMock.mock.calls[0][0]).toBe(`/api/v1/approval-policies/${policyId}`);
    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('PATCH');
    expect((init.headers as Record<string, string>)['If-Match']).toBe('7');
    expect(JSON.parse(String(init.body))).toEqual({ name: 'Renamed', lock_version: 7 });
  });

  it('activate and deactivate use canonical endpoints', async () => {
    setCachedCsrfTokenForTests('csrf-test-token');
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(ok({ id: policyId, status: 'ACTIVE', lock_version: 2 }))
      .mockResolvedValueOnce(ok({ id: policyId, status: 'INACTIVE', lock_version: 3 }));
    vi.stubGlobal('fetch', fetchMock);

    await activateApprovalPolicy(policyId);
    await deactivateApprovalPolicy(policyId);

    expect(fetchMock.mock.calls[0][0]).toBe(
      `/api/v1/approval-policies/${policyId}/activate`,
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      `/api/v1/approval-policies/${policyId}/deactivate`,
    );
  });

  it('parses and builds approver_scope without inventing keys', () => {
    expect(parseApproverRoleCodes(null)).toEqual([]);
    expect(parseApproverRoleCodes({})).toEqual([]);
    expect(parseApproverRoleCodes({ role_codes: [' A ', 'B'] })).toEqual(['A', 'B']);
    expect(buildApproverScope('')).toBeNull();
    expect(buildApproverScope('APPROVER, ADMIN')).toEqual({
      role_codes: ['APPROVER', 'ADMIN'],
    });
  });
});
