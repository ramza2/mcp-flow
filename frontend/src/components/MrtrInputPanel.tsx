/**
 * Minimal MRTR WAITING_INPUT panel — docs/06 §15 / docs/07.
 * Fetches durable OPEN request; submits/rejects via API.
 * Never handles requestState or constructs inputResponses protocol payloads.
 */

import { useEffect, useState } from 'react';
import Button from './ui/Button';
import { InlineAlert } from './ui/EmptyState';
import {
  listInputRequests,
  rejectInputRequest,
  submitInputResponse,
  type MrtrInputRequestDto,
} from '../api/mrtr';
import { ApiError, isApiError } from '../api/client';

type PanelState = 'loading' | 'ready' | 'submitting' | 'rejecting' | 'done' | 'error';

function fieldEntries(inputRequests: Record<string, unknown>): Array<{
  key: string;
  label: string;
  type: string;
}> {
  return Object.entries(inputRequests).map(([key, raw]) => {
    const desc = raw && typeof raw === 'object' ? (raw as Record<string, unknown>) : {};
    const schema =
      desc.schema && typeof desc.schema === 'object'
        ? (desc.schema as Record<string, unknown>)
        : desc;
    const type = typeof schema.type === 'string' ? schema.type : 'string';
    const label =
      (typeof desc.message === 'string' && desc.message) ||
      (typeof desc.description === 'string' && desc.description) ||
      key;
    return { key, label, type };
  });
}

function defaultValue(type: string): unknown {
  if (type === 'boolean') return false;
  if (type === 'number' || type === 'integer') return 0;
  return '';
}

export default function MrtrInputPanel({
  executionId,
  onResolved,
}: {
  executionId: string;
  onResolved?: (status: string) => void;
}) {
  const [state, setState] = useState<PanelState>('loading');
  const [request, setRequest] = useState<MrtrInputRequestDto | null>(null);
  const [values, setValues] = useState<Record<string, unknown>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setState('loading');
    setError(null);
    listInputRequests(executionId, { status: 'OPEN', signal: controller.signal })
      .then((list) => {
        const open = list.items[0] ?? null;
        setRequest(open);
        if (open) {
          const next: Record<string, unknown> = {};
          for (const field of fieldEntries(open.input_requests)) {
            next[field.key] = defaultValue(field.type);
          }
          setValues(next);
          setState('ready');
        } else {
          setState('done');
        }
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        setError(isApiError(err) ? err.message : 'Failed to load pending input request.');
        setState('error');
      });
    return () => controller.abort();
  }, [executionId]);

  const submit = async () => {
    if (!request) return;
    setState('submitting');
    setError(null);
    try {
      const result = await submitInputResponse(executionId, request.id, values);
      setState('done');
      onResolved?.(result.execution_status);
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : 'Failed to submit response.';
      setError(message);
      setState('error');
    }
  };

  const reject = async () => {
    if (!request) return;
    setState('rejecting');
    setError(null);
    try {
      const result = await rejectInputRequest(executionId, request.id);
      setState('done');
      onResolved?.(result.execution_status);
    } catch (err: unknown) {
      const message =
        err instanceof ApiError
          ? `${err.code}: ${err.message}`
          : 'Failed to reject request.';
      setError(message);
      setState('error');
    }
  };

  if (state === 'loading') {
    return (
      <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4">
        <p className="text-sm text-amber-800">Loading pending MCP input request…</p>
      </div>
    );
  }

  if (!request && state === 'done') {
    return null;
  }

  const fields = request ? fieldEntries(request.input_requests) : [];

  return (
    <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-3">
      <p className="text-sm font-semibold text-amber-800">
        {state === 'done'
          ? 'MCP Tool input resolved'
          : 'MCP Tool requests information (Runtime WAITING_INPUT)'}
      </p>
      {request && (
        <div className="text-xs text-amber-700 space-y-0.5">
          <p>Round: {request.round_no}</p>
          <p className="font-mono text-[10px]">request id: {request.id}</p>
        </div>
      )}
      <p className="text-[10px] text-amber-500 font-mono">
        requestState is not user-visible / not editable
      </p>
      {state !== 'done' &&
        fields.map((field) => (
          <label key={field.key} className="block text-sm text-amber-900">
            <span className="font-medium">{field.label}</span>
            {field.type === 'boolean' ? (
              <input
                type="checkbox"
                className="ml-2"
                checked={Boolean(values[field.key])}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [field.key]: e.target.checked }))
                }
                disabled={state === 'submitting' || state === 'rejecting'}
              />
            ) : (
              <input
                className="mt-1 w-full rounded border border-amber-200 bg-white px-2 py-1 text-sm"
                value={String(values[field.key] ?? '')}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [field.key]: e.target.value }))
                }
                disabled={state === 'submitting' || state === 'rejecting'}
              />
            )}
          </label>
        ))}
      {error && <InlineAlert type="warning" message={error} />}
      {state !== 'done' && request && (
        <div className="flex gap-2">
          <Button
            size="sm"
            onClick={submit}
            loading={state === 'submitting'}
            disabled={state === 'rejecting'}
          >
            응답 후 Resume
          </Button>
          <Button
            size="sm"
            variant="outline"
            onClick={reject}
            loading={state === 'rejecting'}
            disabled={state === 'submitting'}
          >
            거부
          </Button>
        </div>
      )}
      {state === 'done' && (
        <InlineAlert type="info" message="응답/거부 처리됨 — Execution 상태를 확인하세요." />
      )}
    </div>
  );
}
