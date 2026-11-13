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

/** Persisted declarative Tool package (not a published or active MCP Tool). */
export interface FactoryPackageDto {
  id: string;
  artifact_type: 'GENERATED_PACKAGE';
  build_id: string;
  job_id: string;
  package_content_sha256: string;
  package_size_bytes: number;
  generator_version: string;
  package_schema_version: string;
  tool_count: number;
  tools: Array<{
    candidate_id: string;
    operation_key: string;
    method: string;
    path: string;
    operation_id: string | null;
    summary: string | null;
    deprecated: boolean;
  }>;
  created_at: string;
}

export interface FactoryPackagePage {
  items: FactoryPackageDto[];
  build_id: string;
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export interface FactoryTestRunDto {
  id: string;
  generated_package_id: string;
  build_id: string;
  job_id: string;
  status: string;
  result: 'PASS' | 'FAIL' | null;
  security_policy_version: string;
  contract_policy_version: string;
  security_result: JsonValue | null;
  contract_result: JsonValue | null;
  evidence_hash: string | null;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  queued_at: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface FactoryTestRunPage {
  items: FactoryTestRunDto[];
  generated_package_id: string;
  page: number;
  page_size: number;
  total: number;
  has_next: boolean;
}

export function listFactoryPackages(buildId: string, signal?: AbortSignal) {
  return apiRequest<FactoryPackagePage>(`/factory/builds/${encodeURIComponent(buildId)}/artifacts`, {
    query: { page: 1, page_size: 20, sort: '-created_at' },
    signal,
  });
}

export function generateFactoryPackage(buildId: string, idempotencyKey: string) {
  return apiRequest<FactoryPackageDto>(`/factory/builds/${encodeURIComponent(buildId)}/generate`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export function getFactoryPackage(packageId: string, signal?: AbortSignal) {
  return apiRequest<FactoryPackageDto>(`/factory/artifacts/${encodeURIComponent(packageId)}`, {
    signal,
  });
}

export function listFactoryPackageTests(packageId: string, signal?: AbortSignal) {
  return apiRequest<FactoryTestRunPage>(`/factory/artifacts/${encodeURIComponent(packageId)}/tests`, {
    query: { page: 1, page_size: 20, sort: '-created_at' },
    signal,
  });
}

/** Enqueues a real asynchronous sandbox test; never synthesizes a PASS outcome. */
export function startFactoryPackageTest(packageId: string, idempotencyKey: string) {
  return apiRequest<FactoryTestRunDto>(`/factory/artifacts/${encodeURIComponent(packageId)}/tests`, {
    method: 'POST',
    headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export function getFactoryTestRun(testRunId: string, signal?: AbortSignal) {
  return apiRequest<FactoryTestRunDto>(`/factory/test-runs/${encodeURIComponent(testRunId)}`, {
    signal,
  });
}
