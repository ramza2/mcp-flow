import { afterEach, describe, expect, it, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import FactoryJobNew from '@/screens/mcp/FactoryJobNew';
import { renderWithRouter } from '../test-utils';

const JOB_ID = '11111111-1111-4111-8111-111111111111';

const job = {
  id: JOB_ID,
  job_type: 'OPENAPI_ANALYZE',
  status: 'QUEUED',
  source_name: 'pets.yaml',
  source_sha256: 'a'.repeat(64),
  source_format: 'YAML',
  analyzer_version: 'v1',
  operation_count: 0,
  server_count: 0,
  progress_current: 0,
  progress_total: 1,
  current_phase: 'ANALYZE',
  error_code: null,
  error_message: null,
  requested_by: '22222222-2222-4222-8222-222222222222',
  created_at: '2026-11-13T00:00:00Z',
  started_at: null,
  finished_at: null,
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const documentText = 'openapi: 3.0.3\ninfo:\n  title: Pets\n  version: 1.0.0\npaths: {}\n';

function renderForm() {
  return renderWithRouter(<FactoryJobNew />, {
    path: '/tool-factory/jobs/new',
    route: '/tool-factory/jobs/new',
    routes: [
      { path: '/tool-factory/jobs/new', element: <FactoryJobNew /> },
      { path: '/tool-factory/jobs/:jobId', element: <div>작업 상세 도착</div> },
    ],
  });
}

describe('Factory OpenAPI upload — real API / safe retry', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('rejects missing, invalid, empty and oversized files before making a request', async () => {
    const user = userEvent.setup({ applyAccept: false });
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    renderForm();

    expect(screen.getByRole('button', { name: '분석 작업 생성' })).toBeDisabled();
    const input = screen.getByLabelText('OpenAPI 문서') as HTMLInputElement;

    await user.upload(input, new File(['x'], 'script.py'));
    expect(screen.getByText(/\.json, \.yaml 또는 \.yml 파일만/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '분석 작업 생성' })).toBeDisabled();

    await user.upload(input, new File([], 'empty.json'));
    expect(screen.getByText('빈 파일은 업로드할 수 없습니다.')).toBeInTheDocument();

    await user.upload(input, new File(['x'.repeat(2 * 1024 * 1024 + 1)], 'large.yaml'));
    expect(screen.getByText(/파일 크기는 2MiB/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uploads real FormData with same-origin credentials and CSRF, then navigates to the created job', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(job, 201));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderForm();

    await user.upload(screen.getByLabelText('OpenAPI 문서'), new File([documentText], 'pets.yaml'));
    await user.click(screen.getByRole('button', { name: '분석 작업 생성' }));

    expect(await screen.findByText('작업 상세 도착')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/factory/jobs');
    expect(options.method).toBe('POST');
    expect(options.credentials).toBe('same-origin');
    expect(options.headers).toEqual(expect.objectContaining({
      'X-CSRF-Token': 'test-csrf-token',
      'Idempotency-Key': expect.stringMatching(/^factory-analyze-[0-9a-f]{32}$/),
    }));
    expect((options.headers as Record<string, string>)['Content-Type']).toBeUndefined();
    expect(options.body).toBeInstanceOf(FormData);
    const source = (options.body as FormData).get('source') as File;
    expect(source.name).toBe('pets.yaml');
    expect(source.size).toBe(new Blob([documentText]).size);
  });

  it('preserves the same idempotency key on retry after an ambiguous server failure', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        error: { code: 'INTERNAL', message: '서버 오류', request_id: 'req-factory-upload' },
      }, 503))
      .mockResolvedValueOnce(jsonResponse(job, 201));
    vi.stubGlobal('fetch', fetchMock);
    const user = userEvent.setup();
    renderForm();

    await user.upload(screen.getByLabelText('OpenAPI 문서'), new File([documentText], 'pets.yaml'));
    await user.click(screen.getByRole('button', { name: '분석 작업 생성' }));
    expect(await screen.findByText('서버 오류')).toBeInTheDocument();
    expect(screen.getByText('Request ID: req-factory-upload')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '분석 작업 생성' }));
    expect(await screen.findByText('작업 상세 도착')).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);

    const headers1 = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    const headers2 = fetchMock.mock.calls[1][1].headers as Record<string, string>;
    expect(headers2['Idempotency-Key']).toBe(headers1['Idempotency-Key']);
    expect(headers2['X-CSRF-Token']).toBe('test-csrf-token');
  });

  it('does not submit another request after a file change with invalid extension', async () => {
    const user = userEvent.setup({ applyAccept: false });
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      error: { code: 'AUTH_FORBIDDEN', message: '관리 권한 없음' },
    }, 403));
    vi.stubGlobal('fetch', fetchMock);
    renderForm();

    const input = screen.getByLabelText('OpenAPI 문서');
    await user.upload(input, new File([documentText], 'pets.yml'));
    await user.click(screen.getByRole('button', { name: '분석 작업 생성' }));
    expect(await screen.findByText('관리 권한 없음')).toBeInTheDocument();

    await user.upload(input, new File(['invalid'], 'not-source.txt'));
    expect(screen.getByRole('button', { name: '분석 작업 생성' })).toBeDisabled();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
  });
});
