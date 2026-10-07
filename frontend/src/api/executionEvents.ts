/**
 * Execution Events SSE client — docs/06 §16 / docs/07 §23.
 * Native EventSource only (same-origin Cookie Session; GET → no CSRF).
 */

import { API_V1_PREFIX } from './clientCore';
import type { JsonValue } from './types';

/** Canonical SSE event catalog (docs/06 §16). */
export const EXECUTION_EVENT_TYPES = [
  'execution.created',
  'execution.queued',
  'execution.started',
  'execution.waiting_input',
  'execution.waiting_approval',
  'execution.cancel_requested',
  'execution.succeeded',
  'execution.partially_succeeded',
  'execution.failed',
  'execution.cancelled',
  'execution.timed_out',
  'execution.step.ready',
  'execution.step.started',
  'execution.step.progress',
  'execution.step.waiting_input',
  'execution.step.waiting_approval',
  'execution.step.retrying',
  'execution.step.succeeded',
  'execution.step.failed',
  'execution.step.skipped',
  'approval.requested',
  'approval.decided',
  'artifact.created',
] as const;

export type ExecutionEventType = (typeof EXECUTION_EVENT_TYPES)[number];

export interface ExecutionEventEnvelope {
  event_id: string;
  execution_id: string;
  step_execution_id: string | null;
  event_type: string;
  payload: Record<string, JsonValue>;
  payload_version: number;
  occurred_at: string;
}

/** Accepted SSE message: wire envelope + durable bigint id as decimal string. */
export interface ExecutionEventMessage {
  /** Durable `execution_events.id` as decimal string (never JS number). */
  sseId: string;
  envelope: ExecutionEventEnvelope;
}

export type SseConnectionState =
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'polling'
  | 'disconnected'
  | 'unavailable';

export const SSE_MAX_CONSECUTIVE_ERRORS = 3;
/** Sustained browser `offline` grace before active executions fall back to REST polling. */
export const SSE_OFFLINE_FALLBACK_GRACE_MS = 5000;
export const MAX_TIMELINE_EVENTS = 200;
export const SNAPSHOT_REFRESH_DEBOUNCE_MS = 150;

export function executionEventsUrl(executionId: string): string {
  return `${API_V1_PREFIX}/executions/${executionId}/events`;
}

/** Parse SSE `lastEventId` as non-negative decimal bigint. Malformed/blank → null. */
export function parseSseEventId(raw: string | null | undefined): bigint | null {
  if (raw == null) return null;
  const value = raw.trim();
  if (!value || !/^\d+$/.test(value)) return null;
  try {
    return BigInt(value);
  } catch {
    return null;
  }
}

function parseEnvelope(data: string): ExecutionEventEnvelope | null {
  try {
    const parsed: unknown = JSON.parse(data);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
      return null;
    }
    const obj = parsed as Record<string, unknown>;
    if (typeof obj.event_id !== 'string' || typeof obj.execution_id !== 'string') {
      return null;
    }
    if (typeof obj.event_type !== 'string' || typeof obj.occurred_at !== 'string') {
      return null;
    }
    if (typeof obj.payload_version !== 'number') return null;
    if (
      typeof obj.payload !== 'object' ||
      obj.payload === null ||
      Array.isArray(obj.payload)
    ) {
      return null;
    }
    const step =
      obj.step_execution_id === null || typeof obj.step_execution_id === 'string'
        ? obj.step_execution_id
        : null;
    return {
      event_id: obj.event_id,
      execution_id: obj.execution_id,
      step_execution_id: step,
      event_type: obj.event_type,
      payload: obj.payload as Record<string, JsonValue>,
      payload_version: obj.payload_version,
      occurred_at: obj.occurred_at,
    };
  } catch {
    return null;
  }
}

export type EventSourceConstructor = new (url: string) => EventSource;

export function isEventSourceAvailable(
  factory?: EventSourceConstructor | null,
): boolean {
  if (factory === null) return false;
  if (factory !== undefined) return typeof factory === 'function';
  return typeof EventSource !== 'undefined';
}

export interface OpenExecutionEventsOptions {
  executionId: string;
  onEvent: (message: ExecutionEventMessage) => void;
  onOpen?: () => void;
  onError?: () => void;
  /** Injected for tests; default is global EventSource. */
  eventSourceFactory?: EventSourceConstructor | null;
}

export interface ExecutionEventsStreamHandle {
  close: () => void;
}

/**
 * Open a same-origin EventSource and register named catalog listeners.
 * Does not use `onmessage` — backend emits custom `event:` names.
 */
export function openExecutionEventsStream(
  options: OpenExecutionEventsOptions,
): ExecutionEventsStreamHandle {
  const Factory =
    options.eventSourceFactory === undefined
      ? typeof EventSource !== 'undefined'
        ? EventSource
        : null
      : options.eventSourceFactory;

  if (!Factory) {
    return { close: () => undefined };
  }

  const url = executionEventsUrl(options.executionId);
  const source = new Factory(url);

  const handler = (ev: Event) => {
    const messageEvent = ev as MessageEvent<string>;
    const sseId = messageEvent.lastEventId ?? '';
    const envelope = parseEnvelope(
      typeof messageEvent.data === 'string' ? messageEvent.data : '',
    );
    if (!envelope) return;
    options.onEvent({ sseId, envelope });
  };

  for (const eventType of EXECUTION_EVENT_TYPES) {
    source.addEventListener(eventType, handler);
  }

  source.onopen = () => {
    options.onOpen?.();
  };
  source.onerror = () => {
    options.onError?.();
  };

  return {
    close: () => {
      source.onopen = null;
      source.onerror = null;
      for (const eventType of EXECUTION_EVENT_TYPES) {
        source.removeEventListener(eventType, handler);
      }
      source.close();
    },
  };
}
