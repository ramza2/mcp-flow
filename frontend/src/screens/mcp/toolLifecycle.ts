/** Shared MCP Tool lifecycle UI helpers (presentation only). */

import { isApiError } from '../../api/client';
import type { JsonValue, MCPToolDto } from '../../api/types';
import type { MCPToolStatus } from '../../domain/types';

export type FeedbackError = { message: string; requestId?: string; code?: string };

export function toFeedbackError(err: unknown, fallback: string): FeedbackError {
  if (isApiError(err)) {
    return {
      message: err.message,
      requestId: err.requestId ?? undefined,
      code: err.code,
    };
  }
  return { message: fallback };
}

export function stringTags(tags: JsonValue[] | null | undefined): string[] {
  if (!tags) return [];
  return tags.filter((t): t is string => typeof t === 'string');
}

export function normalizeTagInput(raw: string): string[] {
  const cleaned: string[] = [];
  const seen = new Set<string>();
  for (const part of raw.split(/[,\n]/)) {
    const value = part.trim();
    if (!value || value.length > 64 || seen.has(value)) continue;
    seen.add(value);
    cleaned.push(value);
    if (cleaned.length >= 32) break;
  }
  return cleaned;
}

export type ToolLifecycleAction = 'activate' | 'deactivate' | null;

export function toolLifecycleAction(status: MCPToolStatus | string): ToolLifecycleAction {
  if (status === 'DISCOVERED' || status === 'INACTIVE') return 'activate';
  if (status === 'ACTIVE') return 'deactivate';
  return null;
}

export function isLifecycleActionDisabled(status: MCPToolStatus | string): boolean {
  return status === 'MISSING' || status === 'BLOCKED';
}

export function isVersionConflict(err: unknown): boolean {
  return isApiError(err) && err.code === 'RESOURCE_VERSION_CONFLICT';
}

export function isResourceConflict(err: unknown): boolean {
  return isApiError(err) && err.code === 'RESOURCE_CONFLICT';
}

export function mergeToolRow(items: MCPToolDto[], updated: MCPToolDto): MCPToolDto[] {
  return items.map(item => (item.id === updated.id ? updated : item));
}
