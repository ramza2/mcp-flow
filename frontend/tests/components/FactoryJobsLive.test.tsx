import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryJobs from '@/screens/mcp/FactoryJobs';
import FactoryJobDetail from '@/screens/mcp/FactoryJobDetail';
import { renderWithRouter } from '../test-utils';

const JOB_ID = '11111111-1111-4111-8111-111111111111';

const job = {
  id: JOB_ID,
  job_type: 'OPENAPI_ANALYZE',
  status: 'SUCCEEDED',
  source_name: 'my-service.yaml',
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
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('Factory jobs — real, read-only API', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('loads real Factory jobs instead of mock projects without triggering writes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      items: [job], page: 1, page_size: 20, total: 1, has_next: false,
    }));
    vi.stubGlobal('fetch', fetchMock);
    renderWithRouter(<FactoryJobs />, { path: '/tool-factory/jobs', route: '/tool-factory/jobs' });

    expect(await screen.findByText('my-service.yaml')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Factory 실제 분석 작업' })).toBeInTheDocument();
    expect(screen.getByText(/작업 완료는 MCP 도구 생성이나 활성화 완료를 의미하지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByText('HR API Tools')).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/factory/jobs?page=1&page_size=20&sort=-created_at');
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ method: 'GET' }));
    expect(fetchMock.mock.calls.every(([, options]) => options?.method === 'GET')).toBe(true);
  });

  it('shows a real empty state rather than demo examples', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      items: [], page: 1, page_size: 20, total: 0, has_next: false,
    })));
    renderWithRouter(<FactoryJobs />, { path: '/tool-factory/jobs', route: '/tool-factory/jobs' });
    expect(await screen.findByText('등록된 Factory 분석 작업이 없습니다')).toBeInTheDocument();
    expect(screen.queryByText('Finance Report Tools')).not.toBeInTheDocument();
  });

  it('shows server error and request id with an explicit refresh action', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({
      error: { code: 'AUTH_FORBIDDEN', message: '조회 권한이 없습니다', request_id: 'req-factory' },
    }, 403)));
    renderWithRouter(<FactoryJobs />, { path: '/tool-factory/jobs', route: '/tool-factory/jobs' });
    expect(await screen.findByText('조회 권한이 없습니다')).toBeInTheDocument();
    expect(screen.getByText(/Request ID: req-factory/)).toBeInTheDocument();
    expect(screen.queryByText('HR API Tools')).not.toBeInTheDocument();
  });

  it('loads durable job detail with accurate candidate count and no fake success logs', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      ...job,
      candidate_count: 2,
      test_result_count: 0,
      analysis: { source_name: 'my-service.yaml' },
      analysis_artifact: null,
      test_results: [],
    }));
    vi.stubGlobal('fetch', fetchMock);
    renderWithRouter(<FactoryJobDetail />, {
      path: '/tool-factory/jobs/:jobId', route: `/tool-factory/jobs/${JOB_ID}`,
    });
    expect(await screen.findByText('my-service.yaml')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Factory 분석 작업 상세' })).toBeInTheDocument();
    expect(screen.getByText('저장된 후보 수')).toBeInTheDocument();
    expect(screen.getByText(/분석 작업 성공만으로 MCP 서버나 도구가 활성화되지 않습니다/)).toBeInTheDocument();
    expect(screen.queryByText(/Build started: Finance Report Tools/)).not.toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0][0])).toContain(`/api/v1/factory/jobs/${JOB_ID}`);
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({ method: 'GET' }));
  });
});
