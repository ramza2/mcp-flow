/**
 * ApprovalPolicy admin API — docs/06 §13.
 * Permission: ACTIVE + `approval.policy.manage` (not `approval.decide`).
 */

import type { ApprovalDecisionMode, ApprovalPolicyStatus } from '../domain/types';
import { apiRequest } from './client';
import type { PageDto } from './types';

export type ApprovalPolicySort =
  | 'name'
  | '-name'
  | 'updated_at'
  | '-updated_at'
  | 'created_at'
  | '-created_at'
  | 'status'
  | '-status'
  | 'code'
  | '-code';

/** Canonical approver_scope: null | {} | { role_codes: string[] }. */
export type ApprovalPolicyApproverScope =
  | null
  | Record<string, never>
  | { role_codes: string[] };

export interface ApprovalPolicyDto {
  id: string;
  code: string;
  name: string;
  description: string | null;
  status: ApprovalPolicyStatus;
  decision_mode: ApprovalDecisionMode;
  required_approvals: number;
  approver_scope: ApprovalPolicyApproverScope;
  default_expiry_seconds: number;
  allow_self_approval: boolean;
  reject_comment_required: boolean;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export interface ApprovalPolicyCreateRequest {
  code: string;
  name: string;
  description?: string | null;
  decision_mode?: ApprovalDecisionMode;
  required_approvals?: number;
  approver_scope?: ApprovalPolicyApproverScope;
  default_expiry_seconds?: number;
  allow_self_approval?: boolean;
  reject_comment_required?: boolean;
}

export type ApprovalPolicyUpdateRequest = Partial<
  Omit<ApprovalPolicyCreateRequest, 'code'>
>;

export interface ApprovalPolicyListParams {
  page?: number;
  page_size?: number;
  status?: ApprovalPolicyStatus | '';
  q?: string;
  sort?: ApprovalPolicySort;
  signal?: AbortSignal;
}

export function listApprovalPolicies(params: ApprovalPolicyListParams = {}) {
  return apiRequest<PageDto<ApprovalPolicyDto>>('/approval-policies', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      status: params.status || undefined,
      q: params.q,
      sort: params.sort,
    },
    signal: params.signal,
  });
}

export function getApprovalPolicy(policyId: string, signal?: AbortSignal) {
  return apiRequest<ApprovalPolicyDto>(`/approval-policies/${policyId}`, { signal });
}

export function createApprovalPolicy(body: ApprovalPolicyCreateRequest) {
  return apiRequest<ApprovalPolicyDto>('/approval-policies', {
    method: 'POST',
    body,
  });
}

export function updateApprovalPolicy(
  policyId: string,
  body: ApprovalPolicyUpdateRequest,
  lockVersion: number,
) {
  return apiRequest<ApprovalPolicyDto>(`/approval-policies/${policyId}`, {
    method: 'PATCH',
    body: { ...body, lock_version: lockVersion },
    headers: { 'If-Match': String(lockVersion) },
  });
}

export function activateApprovalPolicy(policyId: string) {
  return apiRequest<ApprovalPolicyDto>(`/approval-policies/${policyId}/activate`, {
    method: 'POST',
  });
}

export function deactivateApprovalPolicy(policyId: string) {
  return apiRequest<ApprovalPolicyDto>(`/approval-policies/${policyId}/deactivate`, {
    method: 'POST',
  });
}

export function parseApproverRoleCodes(
  scope: ApprovalPolicyApproverScope | undefined,
): string[] {
  if (!scope || !('role_codes' in scope) || !Array.isArray(scope.role_codes)) {
    return [];
  }
  return scope.role_codes.map((code) => String(code).trim()).filter(Boolean);
}

export function buildApproverScope(
  roleCodesText: string,
): ApprovalPolicyApproverScope {
  const codes = roleCodesText
    .split(/[,|\n]/)
    .map((part) => part.trim())
    .filter(Boolean);
  if (codes.length === 0) return null;
  return { role_codes: codes };
}
