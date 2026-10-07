import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  EXECUTION_EVENT_TYPES,
  executionEventsUrl,
  openExecutionEventsStream,
  parseSseEventId,
} from '@/api/executionEvents';
import { FakeEventSource, stubFakeEventSource } from '../helpers/fakeEventSource';

describe('executionEvents helpers', () => {
  afterEach(() => {
    FakeEventSource.reset();
    vi.unstubAllGlobals();
  });

  it('builds same-origin events URL', () => {
    expect(executionEventsUrl('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa')).toBe(
      '/api/v1/executions/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events',
    );
  });

  it('parseSseEventId accepts decimal bigint strings only', () => {
    expect(parseSseEventId('10293')).toBe(10293n);
    expect(parseSseEventId('0')).toBe(0n);
    expect(parseSseEventId('')).toBeNull();
    expect(parseSseEventId('  ')).toBeNull();
    expect(parseSseEventId(null)).toBeNull();
    expect(parseSseEventId('1.5')).toBeNull();
    expect(parseSseEventId('-1')).toBeNull();
    expect(parseSseEventId('abc')).toBeNull();
  });

  it('registers named catalog listeners and ignores onmessage-only delivery', () => {
    stubFakeEventSource();
    const onEvent = vi.fn();
    const handle = openExecutionEventsStream({
      executionId: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      onEvent,
      eventSourceFactory: FakeEventSource as unknown as typeof EventSource,
    });
    const source = FakeEventSource.instances[0];
    expect(source.url).toBe(
      '/api/v1/executions/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/events',
    );
    for (const type of EXECUTION_EVENT_TYPES) {
      expect(source.hasListener(type)).toBe(true);
    }

    const envelope = {
      event_id: 'eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee',
      execution_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      step_execution_id: null,
      event_type: 'execution.started',
      payload: { status: 'RUNNING' },
      payload_version: 1,
      occurred_at: '2026-10-07T12:00:00Z',
    };

    source.emitMessage(envelope, '1');
    expect(onEvent).not.toHaveBeenCalled();

    source.emitNamed('execution.started', envelope, '2');
    expect(onEvent).toHaveBeenCalledTimes(1);
    expect(onEvent.mock.calls[0][0].sseId).toBe('2');
    expect(onEvent.mock.calls[0][0].envelope.event_type).toBe('execution.started');

    handle.close();
    expect(source.isClosed).toBe(true);
  });
});
