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

/** The backend accepts only OpenAPI .json/.yaml/.yml files up to 2 MiB. */
export const FACTORY_MAX_SOURCE_BYTES = 2 * 1024 * 1024;

export function createFactoryAnalyzeJob(source: File, idempotencyKey: string, signal?: AbortSignal) {
  const form = new FormData();
  form.append('source', source, source.name);
  return apiRequest<FactoryJobDto>('/factory/jobs', {
    method: 'POST',
    body: form,
    headers: { 'Idempotency-Key': idempotencyKey },
    signal,
  });
}

/** The Backend supports up to MAX_OPERATIONS=2000 candidate IDs in one Build. */
export const FACTORY_MAX_BUILD_CANDIDATES = 2000;

export interface FactoryCandidateDto {
  id: string;
  job_id: string;
  analysis_artifact_id: string;
  source_sha256: string;
  analyzer_version: string;
  operation_key: string;
  method: string;
  path: string;
  operation_id: string | null;
  summary: string | null;
  description: string | null;
  tags: string[];
  deprecated: boolean;
  created_at: string;
}

export interface FactoryCandidatePage {
  items: FactoryCandidateDto[];
  job_id: string;
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export interface FactoryCandidateMaterializeResult {
  job_id: string;
  created_count: number;
  total_count: number;
}

export interface FactoryBuildDto {
  id: string;
  job_id: string;
  candidate_ids: string[];
  candidate_count: number;
  candidates_snapshot: Array<{
    candidate_id: string;
    operation_key: string;
    method: string;
    path: string;
    operation_id: string | null;
    summary: string | null;
  }>;
  analysis_artifact_id: string;
  source_sha256: string;
  analyzer_version: string;
  generator_version: string;
  candidates_snapshot_hash: string;
  build_input_hash: string;
  requested_by: string;
  created_at: string;
}

export function listFactoryCandidates(
  jobId: string,
  params: { page?: number; page_size?: number; signal?: AbortSignal } = {},
) {
  return apiRequest<FactoryCandidatePage>(
    `/factory/jobs/${encodeURIComponent(jobId)}/candidates`,
    {
      query: {
        page: params.page ?? 1,
        page_size: params.page_size ?? 20,
        sort: 'operation_key',
      },
      signal: params.signal,
    },
  );
}

/** Writes durable Candidate records; must be invoked only after user confirmation. */
export function materializeFactoryCandidates(jobId: string) {
  return apiRequest<FactoryCandidateMaterializeResult>(
    `/factory/jobs/${encodeURIComponent(jobId)}/candidates/materialize`,
    { method: 'POST' },
  );
}

/** Creates an immutable candidate selection/snapshot. Package generation is separate. */
export function createFactoryBuild(jobId: string, candidateIds: string[], idempotencyKey: string) {
  return apiRequest<FactoryBuildDto>(`/factory/jobs/${encodeURIComponent(jobId)}/builds`, {
    method: 'POST',
    body: { candidate_ids: candidateIds },
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export function getFactoryBuild(buildId: string, signal?: AbortSignal) {
  return apiRequest<FactoryBuildDto>(`/factory/builds/${encodeURIComponent(buildId)}`, { signal });
}
