import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryTestRunDetail from '@/screens/mcp/FactoryTestRunDetail';
import FactoryReviewDetail from '@/screens/mcp/FactoryReviewDetail';
import { renderWithRouter } from '../test-utils';

const JOB_ID = '11111111-1111-4111-8111-111111111111';
const PACKAGE_ID = '77777777-7777-4777-8777-777777777777';
const TEST_ID = '88888888-8888-4888-8888-888888888888';
const REVIEW_ID = '99999999-9999-4999-8999-999999999999';
const PUBLISH_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const SERVER_ID = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
const testRun = {
  id: TEST_ID, job_id: JOB_ID, generated_package_id: PACKAGE_ID,
  status: 'SUCCEEDED', result: 'PASS',
  evidence_hash: 'e'.repeat(64),
  security_policy_version: 'sec-v1', contract_policy_version: 'contract-v1',
  security_result: { passed: true, policy_version: 'sec-v1' },
  contract_result: { passed: true, policy_version: 'contract-v1' },
  error_code: null, error_message: null,
  created_at: '2026-11-13T00:00:00Z', finished_at: '2026-11-13T00:03:00Z',
};
const review = {
  id: REVIEW_ID, test_run_id: TEST_ID, generated_package_id: PACKAGE_ID,
  build_id: '55555555-5555-4555-8555-555555555555', job_id: JOB_ID,
  decision: 'APPROVE', comment: '검토 완료',
  test_evidence_hash: 'e'.repeat(64), review_evidence_hash: 'a'.repeat(64),
  reviewed_by: '22222222-2222-4222-8222-222222222222',
  reviewed_at: '2026-11-13T00:05:00Z', created_at: '2026-11-13T00:05:00Z',
};
const published = {
  id: PUBLISH_ID, review_id: REVIEW_ID, test_run_id: TEST_ID,
  generated_package_id: PACKAGE_ID, build_id: review.build_id, job_id: JOB_ID,
  mcp_server_id: SERVER_ID, mcp_tool_ids: ['cccccccc-cccc-4ccc-8ccc-cccccccccccc'],
  tool_count: 1, publish_evidence_hash: 'f'.repeat(64),
  requested_by: review.reviewed_by, created_at: '2026-11-13T00:08:00Z',
};
const notFound = { error: { code: 'NOT_FOUND', message: '아직 기록이 없습니다' } };

function respond(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

function runView() {
  return renderWithRouter(<FactoryTestRunDetail />, {
    path: '/tool-factory/test-runs/:testRunId',
    route: `/tool-factory/test-runs/${TEST_ID}`,
    routes: [
      { path: '/tool-factory/test-runs/:testRunId', element: <FactoryTestRunDetail /> },
      { path: '/tool-factory/reviews/:reviewId', element: <div>저장된 검토 상세 도착</div> },
    ],
  });
}

function reviewView() {
  return renderWithRouter(<FactoryReviewDetail />, {
    path: '/tool-factory/reviews/:reviewId',
    route: `/tool-factory/reviews/${REVIEW_ID}`,
  });
}

describe('Factory immutable Review and safe Publish/Import', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('loads PASS and missing Review with GET only; no decision is preselected', async () => {
    const mock = vi.fn((url: string, _opts: RequestInit) => Promise.resolve(
      respond(url.endsWith('/review') ? notFound : testRun, url.endsWith('/review') ? 404 : 200),
    ));
    vi.stubGlobal('fetch', mock);
    runView();

    expect(await screen.findByRole('heading', { name: 'Factory 검토·승인/반려' })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: '검토 결정 선택 필요' })).toBeDisabled();
    expect(screen.getByRole('radio', { name: /승인 —/ })).not.toBeChecked();
    expect(screen.getByRole('radio', { name: /반려 —/ })).not.toBeChecked();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
    expect(mock.mock.calls.map(([url]) => url)).toContain(`/api/v1/factory/test-runs/${TEST_ID}/review`);
  });

  it('requires explicit approve and confirmation; sends actual API payload with CSRF and key', async () => {
    const mock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST') return Promise.resolve(respond(review, 201));
      return Promise.resolve(respond(url.endsWith('/review') ? notFound : testRun, url.endsWith('/review') ? 404 : 200));
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    runView();

    await screen.findByRole('heading', { name: 'Factory 검토·승인/반려' });
    await user.click(await screen.findByRole('radio', { name: /승인 —/ }));
    expect(screen.getByRole('button', { name: '승인 결정 저장' })).toBeDisabled();
    await user.type(screen.getByLabelText(/검토 의견/), '검토 완료');
    await user.click(screen.getByRole('checkbox', { name: /변경 불가능한 기록/ }));
    await user.click(screen.getByRole('button', { name: '승인 결정 저장' }));
    expect(await screen.findByText('저장된 검토 상세 도착')).toBeInTheDocument();

    const writes = mock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toBe(`/api/v1/factory/test-runs/${TEST_ID}/review`);
    expect(JSON.parse(writes[0][1].body as string)).toEqual({ decision: 'APPROVE', comment: '검토 완료' });
    expect((writes[0][1].headers as Record<string, string>)['Idempotency-Key']).toMatch(/^factory-review-[0-9a-f]{32}$/);
    expect((writes[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('on ambiguous review failure preserves key on same decision/comment retry', async () => {
    let posts = 0;
    const mock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST') {
        posts += 1;
        return Promise.resolve(posts === 1
          ? respond({ error: { code: 'NETWORK_UNKNOWN', message: '일시적 오류', request_id: 'req-review' } }, 503)
          : respond({ ...review, decision: 'REJECT' }, 201));
      }
      return Promise.resolve(respond(url.endsWith('/review') ? notFound : testRun, url.endsWith('/review') ? 404 : 200));
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    runView();
    await screen.findByRole('heading', { name: 'Factory 검토·승인/반려' });
    await user.click(await screen.findByRole('radio', { name: /반려 —/ }));
    await user.click(screen.getByRole('checkbox', { name: /변경 불가능한 기록/ }));
    await user.click(screen.getByRole('button', { name: '반려 결정 저장' }));
    expect(await screen.findByText('일시적 오류')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-review')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '반려 결정 저장' }));
    expect(await screen.findByText('저장된 검토 상세 도착')).toBeInTheDocument();
    const writes = mock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(writes).toHaveLength(2);
    expect((writes[0][1].headers as Record<string, string>)['Idempotency-Key'])
      .toBe((writes[1][1].headers as Record<string, string>)['Idempotency-Key']);
  });

  it('existing Review cannot be redecided and only offers read-only details', async () => {
    const mock = vi.fn((url: string, _opts: RequestInit) => Promise.resolve(
      respond(url.endsWith('/review') ? review : testRun),
    ));
    vi.stubGlobal('fetch', mock);
    runView();
    expect(await screen.findByText(/이미 저장된 결정/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '승인 결정 저장' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '검토 결정 상세' })).toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('review detail does not create DRAFT resources without explicit publish confirmation', async () => {
    const mock = vi.fn((url: string, _opts: RequestInit) =>
      Promise.resolve(respond(url.endsWith('/publish') ? notFound : review, url.endsWith('/publish') ? 404 : 200)),
    );
    vi.stubGlobal('fetch', mock);
    reviewView();
    expect(await screen.findByText('저장된 검토 결정')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'DRAFT 서버·도구 게시 실행' })).toBeDisabled();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('publish imports approved Review as DRAFT server/INACTIVE tools with stable retry key', async () => {
    let writes = 0;
    const mock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST') {
        writes += 1;
        return Promise.resolve(writes === 1
          ? respond({ error: { code: 'SERVER_BUSY', message: '게시 응답 오류', request_id: 'req-publish' } }, 503)
          : respond(published, 201));
      }
      return Promise.resolve(respond(url.endsWith('/publish') ? notFound : review, url.endsWith('/publish') ? 404 : 200));
    });
    vi.stubGlobal('fetch', mock);
    const user = userEvent.setup();
    reviewView();
    await screen.findByText('저장된 검토 결정');

    await user.click(screen.getByRole('checkbox', { name: /실제 운영 DB에 DRAFT 서버/ }));
    await user.click(screen.getByRole('button', { name: 'DRAFT 서버·도구 게시 실행' }));
    expect(await screen.findByText('게시 응답 오류')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'DRAFT 서버·도구 게시 실행' }));
    expect(await screen.findByText(/DRAFT 서버 및 INACTIVE 도구가 등록되었으며/)).toBeInTheDocument();
    expect(screen.getByText(SERVER_ID)).toBeInTheDocument();

    const posts = mock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(posts).toHaveLength(2);
    expect(posts[0][0]).toBe(`/api/v1/factory/reviews/${REVIEW_ID}/publish`);
    const headers1 = posts[0][1].headers as Record<string, string>;
    const headers2 = posts[1][1].headers as Record<string, string>;
    expect(headers1['Idempotency-Key']).toMatch(/^factory-publish-[0-9a-f]{32}$/);
    expect(headers1['Idempotency-Key']).toBe(headers2['Idempotency-Key']);
    expect(headers1['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('reject decision never renders an enabled Publish action', async () => {
    const mock = vi.fn((url: string, _opts: RequestInit) => Promise.resolve(
      respond(url.endsWith('/publish') ? notFound : { ...review, decision: 'REJECT' }, url.endsWith('/publish') ? 404 : 200),
    ));
    vi.stubGlobal('fetch', mock);
    reviewView();
    expect(await screen.findByText(/반려된 검토 기록은 게시할 수 없습니다/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'DRAFT 서버·도구 게시 실행' })).not.toBeInTheDocument();
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('existing Publish is shown and Runtime Binding absence is GET-only', async () => {
    const mock = vi.fn((url: string, _opts: RequestInit) =>
      Promise.resolve(url.endsWith('/runtime-binding')
        ? respond(notFound, 404)
        : respond(url.endsWith('/publish') ? published : review)),
    );
    vi.stubGlobal('fetch', mock);
    reviewView();
    expect(await screen.findByText(PUBLISH_ID)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'DRAFT 서버·도구 게시 실행' })).not.toBeInTheDocument();
    await waitFor(() => expect(mock.mock.calls).toHaveLength(3));
    expect(mock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('non-PASS test never loads Review or exposes decision action', async () => {
    const mock = vi.fn((_url: string, _opts: RequestInit) => Promise.resolve(respond({ ...testRun, status: 'RUNNING', result: null })));
    vi.stubGlobal('fetch', mock);
    runView();
    expect(await screen.findByText('미확정')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Factory 검토·승인/반려' })).not.toBeInTheDocument();
    await waitFor(() => expect(mock.mock.calls).toHaveLength(1));
  });
});
