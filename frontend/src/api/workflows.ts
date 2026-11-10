/** Live Workflow registry and version APIs (docs/06 §12). */
import { apiRequest } from './client';
import type { JsonValue, PageDto } from './types';
import type { WorkflowStatus, WorkflowVersionStatus, WorkflowVersionValidationStatus, WorkflowVisibility } from '../domain/types';

export interface WorkflowDto {
  id: string;
  code: string;
  name: string;
  description: string | null;
  status: WorkflowStatus;
  current_version_id: string | null;
  owner_id: string | null;
  visibility: WorkflowVisibility;
  created_at: string;
  created_by: string | null;
  updated_at: string;
  updated_by: string | null;
  lock_version: number;
}

export interface WorkflowVersionDto {
  id: string;
  workflow_id: string;
  version_no: number;
  status: WorkflowVersionStatus;
  plan_schema_version: string;
  plan_definition: Record<string, JsonValue>;
  input_schema: Record<string, JsonValue>;
  output_schema: Record<string, JsonValue>;
  policy_defaults: Record<string, JsonValue>;
  validation_status: WorkflowVersionValidationStatus;
  validation_report: Record<string, JsonValue> | null;
  content_hash: string;
  change_summary: string | null;
  published_at: string | null;
  published_by: string | null;
  deprecated_at: string | null;
  deprecated_by: string | null;
  created_at: string;
  created_by: string | null;
}

export type WorkflowTargetDto = WorkflowDto;
export type WorkflowVersionTargetDto = WorkflowVersionDto;

export interface WorkflowListParams {
  page?: number;
  page_size?: number;
  status?: WorkflowStatus;
  q?: string;
  sort?: string;
  signal?: AbortSignal;
}

export function listWorkflows({ page = 1, page_size = 20, status, q, sort = '-updated_at', signal }: WorkflowListParams = {}) {
  return apiRequest<PageDto<WorkflowDto>>('/workflows', {
    query: { page, page_size, status, q, sort }, signal,
  });
}

export function getWorkflow(workflowId: string, signal?: AbortSignal) {
  return apiRequest<WorkflowDto>(`/workflows/${workflowId}`, { signal });
}

/** Optimistic concurrency is enforced by the server's Workflow.lock_version. */
export function changeWorkflowStatus(workflowId: string, status: WorkflowStatus, lockVersion: number) {
  return apiRequest<WorkflowDto>(`/workflows/${workflowId}`, {
    method: 'PATCH', body: { status, lock_version: lockVersion },
  });
}

export function createWorkflow(body: { name: string; description?: string | null; visibility: WorkflowVisibility }) {
  return apiRequest<WorkflowDto>('/workflows', { method: 'POST', body });
}

export function listWorkflowVersions(workflowId: string, signal?: AbortSignal) {
  return apiRequest<PageDto<WorkflowVersionDto>>(`/workflows/${workflowId}/versions`, {
    query: { page: 1, page_size: 100, sort: '-version_no' }, signal,
  });
}

export function getWorkflowVersion(workflowId: string, versionId: string, signal?: AbortSignal) {
  return apiRequest<WorkflowVersionDto>(`/workflows/${workflowId}/versions/${versionId}`, { signal });
}

export function createWorkflowVersion(workflowId: string, body: {
  plan_definition?: Record<string, JsonValue>;
  source_version_id?: string;
  change_summary?: string;
}) {
  return apiRequest<WorkflowVersionDto>(`/workflows/${workflowId}/versions`, { method: 'POST', body });
}

export function saveWorkflowPlan(workflowId: string, versionId: string, plan: Record<string, JsonValue>) {
  return apiRequest<WorkflowVersionDto>(`/workflows/${workflowId}/versions/${versionId}/plan`, {
    method: 'PUT', body: { plan_definition: plan },
  });
}

export function validateWorkflowVersion(workflowId: string, versionId: string) {
  return apiRequest<Pick<WorkflowVersionDto, 'id' | 'workflow_id' | 'version_no' | 'status' | 'validation_status' | 'validation_report' | 'content_hash'>>(
    `/workflows/${workflowId}/versions/${versionId}/validate`, { method: 'POST' },
  );
}

export function publishWorkflowVersion(workflowId: string, versionId: string) {
  return apiRequest<WorkflowVersionDto>(
    `/workflows/${workflowId}/versions/${versionId}/publish`, { method: 'POST' },
  );
}

export interface WorkflowExecutionCreateResult {
  id: string;
  status: string;
  source_type: 'WORKFLOW_VERSION';
  trigger_type: string;
  workflow_version_id: string;
  plan_hash: string;
  requested_at: string;
  step_count: number;
}

/** Explicitly pinned published version; a stable key must be reused on uncertain retries. */
export function createWorkflowExecution(
  workflowId: string,
  versionId: string,
  inputs: Record<string, JsonValue>,
  idempotencyKey: string,
) {
  return apiRequest<WorkflowExecutionCreateResult>(`/workflows/${workflowId}/versions/${versionId}/executions`, {
    method: 'POST', body: { inputs }, headers: { 'Idempotency-Key': idempotencyKey },
  });
}

export function listWorkflowTargets(signal?: AbortSignal) {
  return listWorkflows({ page: 1, page_size: 100, signal });
}

export function listWorkflowTargetVersions(workflowId: string, signal?: AbortSignal) {
  return listWorkflowVersions(workflowId, signal);
}
