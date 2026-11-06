/** Minimal Workflow registry reads for explicitly pinned Schedule targets. */
import { apiRequest } from './client';
import type { PageDto } from './types';

export interface WorkflowTargetDto {
  id: string;
  name: string;
  status: string;
  current_version_id: string | null;
}

export interface WorkflowVersionTargetDto {
  id: string;
  workflow_id: string;
  version_no: number;
  status: string;
  validation_status: string;
}

export function listWorkflowTargets(signal?: AbortSignal) {
  return apiRequest<PageDto<WorkflowTargetDto>>('/workflows', {
    query: { page: 1, page_size: 100 }, signal,
  });
}

export function listWorkflowTargetVersions(workflowId: string, signal?: AbortSignal) {
  return apiRequest<PageDto<WorkflowVersionTargetDto>>(`/workflows/${workflowId}/versions`, {
    query: { page: 1, page_size: 100, sort: '-version_no' }, signal,
  });
}
