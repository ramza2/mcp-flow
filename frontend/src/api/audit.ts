/**
 * Audit query API — docs/06 §18 / PR #57.
 * List omits before/after/change_set; detail returns sanitized snapshots only.
 */

import type { AuditActorType, AuditResult } from '../domain/types';
import { apiRequest } from './client';
import type { JsonValue } from './types';

export interface AuditEventListItemDto {
  event_id: string;
  occurred_at: string;
  actor_type: AuditActorType;
  actor_id: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  execution_id: string | null;
  result: AuditResult;
  request_id: string | null;
  trace_id: string | null;
  reason: string | null;
  integrity_hash: string;
}

export interface AuditEventDetailDto extends AuditEventListItemDto {
  before_data: Record<string, JsonValue> | null;
  after_data: Record<string, JsonValue> | null;
  change_set: Record<string, JsonValue> | null;
  source_ip_hash: string | null;
}

export interface AuditEventListDto {
  items: AuditEventListItemDto[];
  next_cursor: string | null;
}

export interface AuditEventListParams {
  cursor?: string;
  limit?: number;
  q?: string;
  actor_type?: AuditActorType | string;
  actor_id?: string;
  /** Exact action filter (API contract). Free-text search uses `q`. */
  action?: string;
  resource_type?: string;
  resource_id?: string;
  result?: AuditResult | string;
  request_id?: string;
  trace_id?: string;
  execution_id?: string;
  from?: string;
  to?: string;
  signal?: AbortSignal;
}

export function listAuditEvents(params: AuditEventListParams = {}) {
  return apiRequest<AuditEventListDto>('/audit/events', {
    query: {
      cursor: params.cursor,
      limit: params.limit ?? 50,
      q: params.q,
      actor_type: params.actor_type,
      actor_id: params.actor_id,
      action: params.action,
      resource_type: params.resource_type,
      resource_id: params.resource_id,
      result: params.result,
      request_id: params.request_id,
      trace_id: params.trace_id,
      execution_id: params.execution_id,
      from: params.from,
      to: params.to,
    },
    signal: params.signal,
  });
}

export function getAuditEvent(eventId: string, signal?: AbortSignal) {
  return apiRequest<AuditEventDetailDto>(`/audit/events/${eventId}`, { signal });
}
