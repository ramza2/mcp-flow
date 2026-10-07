/**
 * Minimal EventSource fake for ExecutionDetail SSE tests.
 * Supports named addEventListener (not only onmessage).
 */

import { vi } from 'vitest';

type Listener = (event: MessageEvent<string> | Event) => void;

export class FakeEventSource {
  static instances: FakeEventSource[] = [];
  static reset() {
    for (const instance of FakeEventSource.instances) {
      instance.close();
    }
    FakeEventSource.instances = [];
  }

  readonly url: string;
  readyState = 0;
  onopen: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent<string>) => void) | null = null;
  private listeners = new Map<string, Set<Listener>>();
  private closed = false;

  constructor(url: string) {
    this.url = url;
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const fn =
      typeof listener === 'function'
        ? (listener as Listener)
        : (event: Event) => listener.handleEvent(event);
    const set = this.listeners.get(type) ?? new Set();
    set.add(fn);
    this.listeners.set(type, set);
  }

  removeEventListener(
    type: string,
    listener: EventListenerOrEventListenerObject,
  ): void {
    const set = this.listeners.get(type);
    if (!set) return;
    const fn =
      typeof listener === 'function'
        ? (listener as Listener)
        : (event: Event) => listener.handleEvent(event);
    set.delete(fn);
  }

  close(): void {
    this.closed = true;
    this.readyState = 2;
  }

  get isClosed(): boolean {
    return this.closed;
  }

  hasListener(type: string): boolean {
    return (this.listeners.get(type)?.size ?? 0) > 0;
  }

  emitOpen(): void {
    if (this.closed) return;
    this.readyState = 1;
    this.onopen?.(new Event('open'));
  }

  emitError(): void {
    if (this.closed) return;
    this.onerror?.(new Event('error'));
  }

  /** Emit a custom named SSE event (backend wire). */
  emitNamed(eventType: string, data: unknown, lastEventId: string): void {
    if (this.closed) return;
    const event = {
      type: eventType,
      data: JSON.stringify(data),
      lastEventId,
    } as MessageEvent<string>;
    const set = this.listeners.get(eventType);
    if (set) {
      for (const listener of set) listener(event);
    }
    // Intentionally do NOT call onmessage — tests assert named listeners.
  }

  /** Would fire only if production code wrongly used onmessage. */
  emitMessage(data: unknown, lastEventId: string): void {
    if (this.closed) return;
    const event = {
      type: 'message',
      data: JSON.stringify(data),
      lastEventId,
    } as MessageEvent<string>;
    this.onmessage?.(event);
  }
}

export function stubFakeEventSource(): typeof FakeEventSource {
  FakeEventSource.reset();
  vi.stubGlobal('EventSource', FakeEventSource);
  return FakeEventSource;
}
