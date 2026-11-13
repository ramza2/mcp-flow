import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryJobCandidates from '@/screens/mcp/FactoryJobCandidates';
import FactoryBuildRecord from '@/screens/mcp/FactoryBuildRecord';
import { renderWithRouter } from '../test-utils';

const JOB_ID = '11111111-1111-4111-8111-111111111111';
const CANDIDATE_ID = '33333333-3333-4333-8333-333333333333';
const CANDIDATE_ID_2 = '44444444-4444-4444-8444-444444444444';
const BUILD_ID = '55555555-5555-4555-8555-555555555555';

const job = {
  id: JOB_ID,
  job_type: 'OPENAPI_ANALYZE',
  status: 'SUCCEEDED',
  source_name: 'pets.yaml',
  source_sha256: 'a'.repeat(64),
  source_format: 'YAML',
  analyzer_version: 'v1',
  operation_count: 2,
  server_count: 1,
  progress_current: 2,
  progress_total: 2,
  current_phase: null,
  error_code: null,
  error_message: null,
  requested_by: '22222222-2222-4222-8222-222222222222',
  created_at: '2026-11-13T00:00:00Z',
  started_at: '2026-11-13T00:01:00Z',
  finished_at: '2026-11-13T00:02:00Z',
  candidate_count: 2,
  test_result_count: 0,
  analysis: null,
  analysis_artifact: null,
  test_results: [],
};
const candidate = {
  id: CANDIDATE_ID,
  job_id: JOB_ID,
  analysis_artifact_id: '66666666-6666-4666-8666-666666666666',
  source_sha256: 'a'.repeat(64),
  analyzer_version: 'v1',
  operation_key: 'GET /pets',
  method: 'GET',
  path: '/pets',
  operation_id: 'listPets',
  summary: '동물 목록',
  description: null,
  tags: [],
  deprecated: false,
  created_at: '2026-11-13T00:00:00Z',
};
const build = {
  id: BUILD_ID,
  job_id: JOB_ID,
  candidate_ids: [CANDIDATE_ID],
  candidate_count: 1,
  candidates_snapshot: [{
    candidate_id: CANDIDATE_ID, operation_key: 'GET /pets',
    method: 'GET', path: '/pets', operation_id: 'listPets', summary: '동물 목록',
  }],
  analysis_artifact_id: candidate.analysis_artifact_id,
  source_sha256: 'a'.repeat(64),
  analyzer_version: 'v1',
  generator_version: 'v1',
  candidates_snapshot_hash: 'b'.repeat(64),
  build_input_hash: 'c'.repeat(64),
  requested_by: job.requested_by,
  created_at: '2026-11-13T00:03:00Z',
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status, headers: { 'Content-Type': 'application/json' },
  });
}
function candidatePage(items: unknown[], page = 1, total = items.length) {
  return { items, job_id: JOB_ID, page, page_size: 20, total, has_next: page * 20 < total };
}
function renderCandidates() {
  return renderWithRouter(<FactoryJobCandidates />, {
    route: `/tool-factory/jobs/${JOB_ID}/candidates`,
    path: '/tool-factory/jobs/:jobId/candidates',
    routes: [
      { path: '/tool-factory/jobs/:jobId/candidates', element: <FactoryJobCandidates /> },
      { path: '/tool-factory/builds/:buildId', element: <div>실제 Build 기록으로 이동</div> },
    ],
  });
}

describe('Factory candidate review and Build snapshot', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('GET-only loading never materializes, builds, packages or publishes', async () => {
    const fetchMock = vi.fn((url: string, _opts: RequestInit) => Promise.resolve(
      jsonResponse(url.includes('/candidates') ? candidatePage([candidate]) : job),
    ));
    vi.stubGlobal('fetch', fetchMock);
    renderCandidates();

    expect(await screen.findByText('동물 목록')).toBeInTheDocument();
    expect(screen.getByText(/후보 목록 조회는 읽기 전용입니다/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Build 정의 생성 (0개)' })).toBeDisabled();
    expect(fetchMock.mock.calls.every(([, opts]) => opts?.method === 'GET')).toBe(true);
  });

  it('requires a deliberate materialize click on successful analyses and refreshes persisted results', async () => {
    let materialized = false;
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST' && url.includes('/materialize')) {
        materialized = true;
        return Promise.resolve(jsonResponse({ job_id: JOB_ID, created_count: 1, total_count: 1 }));
      }
      if (url.includes('/candidates')) {
        return Promise.resolve(jsonResponse(candidatePage(materialized ? [candidate] : [])));
      }
      return Promise.resolve(jsonResponse({ ...job, candidate_count: materialized ? 1 : 0 }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderCandidates();

    expect(await screen.findByText('저장된 후보 도구가 없습니다')).toBeInTheDocument();
    expect(fetchMock.mock.calls.every(([, opts]) => opts?.method === 'GET')).toBe(true);

    await user.click(screen.getByRole('button', { name: '후보 DB 저장' }));
    expect(await screen.findByText('동물 목록')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('신규 1개, 총 1개');
    const writes = fetchMock.mock.calls.filter(([, opts]) => opts?.method === 'POST');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toContain(`/api/v1/factory/jobs/${JOB_ID}/candidates/materialize`);
    expect((writes[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('selects a real persisted candidate and creates only an immutable Build definition', async () => {
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST' && url.includes('/builds')) return Promise.resolve(jsonResponse(build, 201));
      return Promise.resolve(jsonResponse(url.includes('/candidates') ? candidatePage([candidate]) : job));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderCandidates();

    await screen.findByText('동물 목록');
    await user.click(screen.getByRole('checkbox', { name: '후보 선택: GET /pets' }));
    await user.click(screen.getByRole('button', { name: 'Build 정의 생성 (1개)' }));
    expect(await screen.findByText('실제 Build 기록으로 이동')).toBeInTheDocument();

    const writes = fetchMock.mock.calls.filter(([, opts]) => opts?.method === 'POST');
    expect(writes).toHaveLength(1);
    expect(writes[0][0]).toContain(`/api/v1/factory/jobs/${JOB_ID}/builds`);
    expect(JSON.parse(writes[0][1].body as string)).toEqual({ candidate_ids: [CANDIDATE_ID] });
    expect((writes[0][1].headers as Record<string, string>)['Idempotency-Key']).toMatch(/^factory-build-[0-9a-f]{32}$/);
    expect((writes[0][1].headers as Record<string, string>)['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('reuses the same idempotency key when retrying an ambiguous Build failure', async () => {
    let writes = 0;
    const fetchMock = vi.fn((url: string, opts: RequestInit) => {
      if (opts.method === 'POST' && url.includes('/builds')) {
        writes += 1;
        return Promise.resolve(writes === 1
          ? jsonResponse({ error: { code: 'INTERNAL', message: '일시적 오류', request_id: 'req-build' } }, 503)
          : jsonResponse(build, 201));
      }
      return Promise.resolve(jsonResponse(url.includes('/candidates') ? candidatePage([candidate]) : job));
    });
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderCandidates();

    await screen.findByText('동물 목록');
    await user.click(screen.getByRole('checkbox', { name: '후보 선택: GET /pets' }));
    await user.click(screen.getByRole('button', { name: 'Build 정의 생성 (1개)' }));
    expect(await screen.findByText('일시적 오류')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-build')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Build 정의 생성 (1개)' }));
    expect(await screen.findByText('실제 Build 기록으로 이동')).toBeInTheDocument();
    const posts = fetchMock.mock.calls.filter(([, opts]) => opts?.method === 'POST');
    expect(posts).toHaveLength(2);
    expect((posts[0][1].headers as Record<string, string>)['Idempotency-Key']).toBe((posts[1][1].headers as Record<string, string>)['Idempotency-Key']);
  });

  it('disables both write actions for incomplete analysis and shows real status', async () => {
    vi.stubGlobal('fetch', vi.fn((url: string, _opts: RequestInit) =>
      Promise.resolve(jsonResponse(url.includes('/candidates')
        ? candidatePage([])
        : { ...job, status: 'RUNNING' })),
    ));
    renderCandidates();
    await screen.findByText('저장된 후보 도구가 없습니다');
    expect(screen.getByRole('button', { name: '후보 DB 저장' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Build 정의 생성 (0개)' })).toBeDisabled();
  });

  it('reads actual Build snapshots without pretending package, tests or publishing succeeded', async () => {
    const fetchMock = vi.fn((url: string, _opts: RequestInit) => Promise.resolve(
      jsonResponse(url.includes('/artifacts')
        ? { items: [], build_id: BUILD_ID, page: 1, page_size: 20, total: 0, has_next: false }
        : build),
    ));
    vi.stubGlobal('fetch', fetchMock);
    renderWithRouter(<FactoryBuildRecord />, {
      route: `/tool-factory/builds/${BUILD_ID}`,
      path: '/tool-factory/builds/:buildId',
    });
    expect(await screen.findByText('동물 목록')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Factory Build 정의 상세' })).toBeInTheDocument();
    expect(screen.getByText(/패키지와 구분됩니다.*보안·계약 테스트/)).toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0][0]).toContain(`/api/v1/factory/builds/${BUILD_ID}`);
    expect(fetchMock.mock.calls[0][1].method).toBe('GET');
  });
});
