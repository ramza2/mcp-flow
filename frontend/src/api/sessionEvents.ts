/**
 * Session-invalidation pub/sub for the shared API client.
 * Keep React Router out of the API layer.
 */

type SessionInvalidListener = () => void;

const listeners = new Set<SessionInvalidListener>();

export function onSessionInvalid(listener: SessionInvalidListener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function notifySessionInvalid(): void {
  for (const listener of [...listeners]) {
    listener();
  }
}
