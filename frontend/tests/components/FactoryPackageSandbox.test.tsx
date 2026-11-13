import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryBuildRecord from '@/screens/mcp/FactoryBuildRecord';
import FactoryPackageDetail from '@/screens/mcp/FactoryPackageDetail';
import FactoryTestRunDetail from '@/screens/mcp/FactoryTestRunDetail';
import { renderWithRouter } from '../test-utils';

const BUILD_ID = '55555555-5555-4555-8555-555555555555';
const PACKAGE_ID = '77777777-7777-4777-8777-777777777777';
const TEST_ID = '88888888-8888-4888-8888-888888888888';
const JOB_ID = '11111111-1111-4111-8111-111111111111';
const CANDIDATE_ID = '33333333-3333-4333-8333-333333333333';

const build = {
  id: BUILD_ID, job_id: JOB_ID, candidate_ids: [CANDIDATE_ID], candidate_count: 1,
  candidates_snapshot: [{
    candidate_id: CANDIDATE_ID, operation_key: 'GET /pets',
    method: 'GET', path: '/pets', operation_id: 'listPets', summary: '동물 목록',
  }],
  analysis_artifact_id: '66666666-6666-4666-8666-666666666666',
  source_sha256: 'a'.repeat(64), analyzer_version: 'v1', generator_version: 'v1',
  candidates_snapshot_hash: 'b'.repeat(64), build_input_hash: 'c'.repeat(64),
  requested_by: '22222222-2222-4222-8222-222222222222',
  created_at: '2026-11-13T00:01:00Z',
};
const pkg = {
  id: PACKAGE_ID, artifact_type: 'GENERATED_PACKAGE', build_id: BUILD_ID, job_id: JOB_ID,
  package_content_sha256: 'f'.repeat(64), package_size_bytes: 300,
  generator_version: 'v1', package_schema_version: 'v1', tool_count: 1,
  tools: [{
    candidate_id: CANDIDATE_ID, operation_key: 'GET /pets',
    method: 'GET', path: '/pets', operation_id: 'listPets',
    summary: '동물 목록', deprecated: false,
  }],
  created_at: '2026-11-13T00:02:00Z',
};
const testRun = {
  id: TEST_ID, generated_package_id: PACKAGE_ID, build_id: BUILD_ID, job_id: JOB_ID,
  status: 'QUEUED', result: null, security_policy_version: 'sec-v1',
  contract_policy_version: 'contract-v1', security_result: null, contract_result: null,
  evidence_hash: null, error_code: null, error_message: null,
  created_at: '2026-11-13T00:03:00Z',
  queued_at: '2026-11-13T00:03:00Z', started_at: null, finished_at: null,
};
const packagePage = { items: [], build_id: BUILD_ID, page: 1, page_size: 20, total: 0, has_next: false };
const testPage = { items: [], generated_package_id: PACKAGE_ID, page: 1, page_size: 20, total: 0, has_next: false };

function respond(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}

function buildView() {
  return renderWithRouter(<FactoryBuildRecord />, {
    path: '/tool-factory/builds/:buildId',
    route: `/tool-factory/builds/${BUILD_ID}`,
    routes: [
      { path: '/tool-factory/builds/:buildId', element: <FactoryBuildRecord /> },
      { path: '/tool-factory/artifacts/:packageId', element: <div>패키지 상세 도착</div> },
    ],
  });
}
function packageView() {
  return renderWithRouter(<FactoryPackageDetail />, {
    path: '/tool-factory/artifacts/:packageId',
    route: `/tool-factory/artifacts/${PACKAGE_ID}`,
    routes: [
      { path: '/tool-factory/artifacts/:packageId', element: <FactoryPackageDetail /> },
      { path: '/tool-factory/test-runs/:testRunId', element: <div>테스트 상세 도착</div> },
    ],
  });
}

describe('Factory real package and sandbox test UX', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('loads Build and package list with GET only, without claiming packages or tests completed', async () => {
    const fetchMock = vi.fn((url: string, _opts: RequestInit) =>
      Promise.resolve(respond(url.includes('/artifacts') ? packagePage : build)));
    vi.stubGlobal('fetch', fetchMock);
    buildView();

    expect(await screen.findByText('생성된 패키지가 없습니다')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '패키지 생성' })).toBeEnabled();
    expect(screen.getByText(/아래에서 명시적으로 패키지를 생성할 수 있지만/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);
  });

  it('requires explicit click and reuses Idempotency-Key on retry for package generation', async () => {
    let posts = 0;
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST') {
        posts += 1;
        return Promise.resolve(posts === 1
          ? respond({ error: { code: 'SERVER_BUSY', message: '패키지 생성 일시 실패', request_id: 'req-pkg' } }, 503)
          : respond(pkg, 201));
      }
      return Promise.resolve(respond(url.includes('/artifacts') ? packagePage : build));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    buildView();

    await screen.findByText('생성된 패키지가 없습니다');
    await user.click(screen.getByRole('button', { name: '패키지 생성' }));
    expect(await screen.findByText('패키지 생성 일시 실패')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-pkg')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '패키지 생성' }));
    expect(await screen.findByText('패키지 상세 도착')).toBeInTheDocument();

    const calls = fetchMock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(calls).toHaveLength(2);
    expect(calls[0][0]).toBe(`/api/v1/factory/builds/${BUILD_ID}/generate`);
    const headers1 = calls[0][1].headers as Record<string, string>;
    const headers2 = calls[1][1].headers as Record<string, string>;
    expect(headers1['Idempotency-Key']).toMatch(/^factory-package-[0-9a-f]{32}$/);
    expect(headers2['Idempotency-Key']).toBe(headers1['Idempotency-Key']);
    expect(headers1['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('loads actual package and tests with GET only and explicitly launches a sandbox test', async () => {
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST') return Promise.resolve(respond(testRun, 201));
      return Promise.resolve(respond(url.endsWith('/tests?page=1&page_size=20&sort=-created_at') ? testPage : pkg));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    packageView();

    expect(await screen.findByText('등록된 테스트가 없습니다')).toBeInTheDocument();
    expect(screen.queryByText('PASS')).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([, opts]) => opts.method === 'GET')).toBe(true);

    await user.click(screen.getByRole('button', { name: '샌드박스 테스트 시작' }));
    expect(await screen.findByText('테스트 상세 도착')).toBeInTheDocument();
    const posts = fetchMock.mock.calls.filter(([, opts]) => opts.method === 'POST');
    expect(posts).toHaveLength(1);
    expect(posts[0][0]).toBe(`/api/v1/factory/artifacts/${PACKAGE_ID}/tests`);
    const headers = posts[0][1].headers as Record<string, string>;
    expect(headers['Idempotency-Key']).toMatch(/^factory-test-[0-9a-f]{32}$/);
    expect(headers['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('shows QUEUED TestRun as unconfirmed, not PASS, until the backend returns evidence', async () => {
    const fetchMock = vi.fn((_url: string, _opts: RequestInit) => Promise.resolve(respond(testRun)));
    vi.stubGlobal('fetch', fetchMock);
    renderWithRouter(<FactoryTestRunDetail />, {
      path: '/tool-factory/test-runs/:testRunId',
      route: `/tool-factory/test-runs/${TEST_ID}`,
    });

    expect(await screen.findByText('미확정')).toBeInTheDocument();
    expect(screen.getByText('아직 보안 검사 결과가 없습니다.')).toBeInTheDocument();
    expect(screen.getByText('아직 계약 검사 결과가 없습니다.')).toBeInTheDocument();
    expect(screen.queryByText(/테스트가 실제 PASS로 완료됐습니다/)).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/v1/factory/test-runs/${TEST_ID}`);
    expect(fetchMock.mock.calls[0][1].method).toBe('GET');
  });

  it('shows genuine PASS only after successful test status and evidence', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(respond({
      ...testRun,
      status: 'SUCCEEDED',
      result: 'PASS',
      security_result: { result: 'PASS' },
      contract_result: { result: 'PASS' },
      evidence_hash: 'e'.repeat(64),
      finished_at: '2026-11-13T00:05:00Z',
    })));
    renderWithRouter(<FactoryTestRunDetail />, {
      path: '/tool-factory/test-runs/:testRunId',
      route: `/tool-factory/test-runs/${TEST_ID}`,
    });
    expect(await screen.findByText(/테스트가 실제 PASS로 완료됐습니다/)).toBeInTheDocument();
    expect(screen.getByText('증빙 해시')).toBeInTheDocument();
    expect(screen.getByText(/검토·게시·활성화 기능은 후속 단계/)).toBeInTheDocument();
  });
});
