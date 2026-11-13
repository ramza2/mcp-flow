/** Real Tool Factory read APIs — canonical /api/v1/factory contract. */

import { apiRequest } from './client';
import type { JsonValue } from './types';

export interface FactoryJobDto {
  id: string;
  job_type: string;
  status: string;
  source_name: string;
  source_sha256: string;
  source_format: 'JSON' | 'YAML' | null;
  analyzer_version: string;
  operation_count: number;
  server_count: number;
  progress_current: number;
  progress_total: number;
  current_phase: string | null;
  error_code: string | null;
  error_message: string | null;
  requested_by: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface FactoryArtifactSummaryDto {
  id: string;
  artifact_type: string;
  content_sha256: string;
  size_bytes: number;
  created_at: string;
}

export interface FactoryJobDetailDto extends FactoryJobDto {
  analysis: JsonValue | null;
  analysis_artifact: FactoryArtifactSummaryDto | null;
  test_result_count: number;
  candidate_count: number;
}

export interface FactoryJobPage {
  items: FactoryJobDto[];
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export function listFactoryJobs(
  params: { page?: number; page_size?: number; signal?: AbortSignal } = {},
) {
  return apiRequest<FactoryJobPage>('/factory/jobs', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      sort: '-created_at',
    },
    signal: params.signal,
  });
}

export function getFactoryJob(jobId: string, signal?: AbortSignal) {
  return apiRequest<FactoryJobDetailDto>(`/factory/jobs/${encodeURIComponent(jobId)}`, {
    signal,
  });
}
