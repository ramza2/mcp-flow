/** MCP Tool Policy tab — lazy-loaded GET/PUT against backend policy API. */

import { useCallback, useEffect, useRef, useState } from 'react';
import { getMCPToolPolicy, putMCPToolPolicy } from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { JsonValue, MCPToolPolicyDto, MCPToolPolicyPutRequest } from '../../api/types';
import { RISK_CLASSES, type RiskClass } from '../../domain/types';
import { labelRiskClass, shortenId } from '../../domain';
import { RiskBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import { EmptyState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import JsonViewer from '../../components/ui/JsonViewer';
import {
  isResourceConflict,
  isVersionConflict,
  toFeedbackError,
  type FeedbackError,
} from './toolLifecycle';

const FORM_INITIAL: MCPToolPolicyPutRequest = {
  risk_class: 'UNKNOWN',
  requires_confirmation: false,
  requires_approval: false,
  approval_policy_id: null,
  timeout_ms: 30000,
  max_attempts: 1,
  max_result_bytes: 1048576,
  allow_auto_select: true,
  backoff_policy: null,
  data_classification: null,
  policy_metadata: null,
};

interface Props {
  toolId: string;
  active: boolean;
}

function parseJsonObject(raw: string, label: string): { ok: true; value: Record<string, JsonValue> | null } | { ok: false; error: string } {
  const trimmed = raw.trim();
  if (!trimmed) return { ok: true, value: null };
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (parsed === null) return { ok: true, value: null };
    if (typeof parsed !== 'object' || Array.isArray(parsed)) {
      return { ok: false, error: `${label} must be a JSON object or empty.` };
    }
    return { ok: true, value: parsed as Record<string, JsonValue> };
  } catch {
    return { ok: false, error: `${label} is not valid JSON.` };
  }
}

export default function ToolPolicyTab({ toolId, active }: Props) {
  const mountedRef = useRef(true);
  const requestRef = useRef<AbortController | null>(null);
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [policy, setPolicy] = useState<MCPToolPolicyDto | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [mutationError, setMutationError] = useState<FeedbackError | null>(null);
  const [form, setForm] = useState<MCPToolPolicyPutRequest>(FORM_INITIAL);
  const [backoffText, setBackoffText] = useState('');
  const [metadataText, setMetadataText] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      requestRef.current?.abort();
    };
  }, []);

  const loadPolicy = useCallback(() => {
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setLoading(true);
    setError(null);
    setMutationError(null);

    getMCPToolPolicy(toolId, controller.signal)
      .then(data => {
        if (requestRef.current !== controller || !mountedRef.current) return;
        setPolicy(data);
        setMissing(false);
        setLoaded(true);
      })
      .catch(err => {
        if (isAbortError(err) || controller.signal.aborted || requestRef.current !== controller) {
          return;
        }
        if (!mountedRef.current) return;
        if (isApiError(err) && err.status === 404) {
          setPolicy(null);
          setMissing(true);
          setLoaded(true);
          return;
        }
        setError(toFeedbackError(err, 'Tool Policy를 불러오지 못했습니다.'));
        setLoaded(true);
      })
      .finally(() => {
        if (requestRef.current === controller && mountedRef.current) {
          setLoading(false);
        }
      });
  }, [toolId]);

  useEffect(() => {
    if (!active) return;
    if (loaded) return;
    loadPolicy();
  }, [active, loaded, loadPolicy]);

  useEffect(() => {
    setLoaded(false);
    setPolicy(null);
    setMissing(false);
    setError(null);
  }, [toolId]);

  const openCreate = () => {
    setForm({ ...FORM_INITIAL });
    setBackoffText('');
    setMetadataText('');
    setFormError(null);
    setMutationError(null);
    setEditorOpen(true);
  };

  const openEdit = () => {
    if (!policy) return;
    setForm({
      risk_class: policy.risk_class,
      requires_confirmation: policy.requires_confirmation,
      requires_approval: policy.requires_approval,
      approval_policy_id: policy.approval_policy_id,
      timeout_ms: policy.timeout_ms,
      max_attempts: policy.max_attempts,
      max_result_bytes: policy.max_result_bytes,
      allow_auto_select: policy.allow_auto_select,
      backoff_policy: policy.backoff_policy,
      data_classification: policy.data_classification,
      policy_metadata: policy.policy_metadata,
    });
    setBackoffText(policy.backoff_policy ? JSON.stringify(policy.backoff_policy, null, 2) : '');
    setMetadataText(policy.policy_metadata ? JSON.stringify(policy.policy_metadata, null, 2) : '');
    setFormError(null);
    setMutationError(null);
    setEditorOpen(true);
  };

  const approvalBlocked =
    form.requires_approval &&
    !form.approval_policy_id &&
    !(policy?.requires_approval && policy.approval_policy_id);

  const handleSave = async () => {
    setFormError(null);
    setMutationError(null);

    if (!(form.timeout_ms > 0)) {
      setFormError('timeout_ms must be > 0.');
      return;
    }
    if (!(form.max_attempts >= 1)) {
      setFormError('max_attempts must be >= 1.');
      return;
    }
    if (!(form.max_result_bytes > 0)) {
      setFormError('max_result_bytes must be > 0.');
      return;
    }

    const backoff = parseJsonObject(backoffText, 'backoff_policy');
    if (!backoff.ok) {
      setFormError(backoff.error);
      return;
    }
    const metadata = parseJsonObject(metadataText, 'policy_metadata');
    if (!metadata.ok) {
      setFormError(metadata.error);
      return;
    }

    if (approvalBlocked) {
      setFormError(
        '현재 ApprovalPolicy 관리 API가 제공되지 않아 새 승인정책 연결은 아직 지원하지 않습니다.',
      );
      return;
    }

    const body: MCPToolPolicyPutRequest = {
      risk_class: form.risk_class,
      requires_confirmation: form.requires_confirmation,
      requires_approval: form.requires_approval,
      approval_policy_id: form.requires_approval
        ? (form.approval_policy_id ?? policy?.approval_policy_id ?? null)
        : null,
      timeout_ms: form.timeout_ms,
      max_attempts: form.max_attempts,
      max_result_bytes: form.max_result_bytes,
      allow_auto_select: form.allow_auto_select,
      backoff_policy: backoff.value,
      data_classification: form.data_classification?.trim() || null,
      policy_metadata: metadata.value,
    };

    setSaving(true);
    try {
      const updated = await putMCPToolPolicy(toolId, body, {
        lockVersion: policy?.lock_version,
      });
      if (!mountedRef.current) return;
      setPolicy(updated);
      setMissing(false);
      setEditorOpen(false);
    } catch (err) {
      if (!mountedRef.current) return;
      if (isVersionConflict(err) || isResourceConflict(err)) {
        const message = isResourceConflict(err)
          ? '다른 작업에서 Policy가 생성되었습니다. 최신 Policy를 다시 불러옵니다.'
          : '다른 작업으로 Policy가 변경되었습니다. 최신 상태를 다시 불러옵니다.';
        setMutationError({
          ...toFeedbackError(err, message),
          message: `${message} (${isApiError(err) ? err.message : 'conflict'})`,
        });
        setEditorOpen(false);
        loadPolicy();
        return;
      }
      setMutationError(toFeedbackError(err, 'Policy 저장에 실패했습니다.'));
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  };

  if (!active) return null;

  if (loading && !loaded) {
    return <LoadingSkeleton rows={4} />;
  }

  if (error) {
    return (
      <div className="space-y-2">
        <InlineAlert type="error" message={error.message} />
        {error.requestId && (
          <p className="font-mono text-xs text-slate-400">Request ID: {error.requestId}</p>
        )}
        <Button variant="outline" size="sm" onClick={loadPolicy}>
          다시 시도
        </Button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {mutationError && (
        <div className="space-y-1">
          <InlineAlert type="error" message={mutationError.message} />
          {mutationError.requestId && (
            <p className="font-mono text-xs text-slate-400">Request ID: {mutationError.requestId}</p>
          )}
        </div>
      )}

      {missing && !policy && (
        <EmptyState
          title="아직 Tool Policy가 설정되지 않았습니다."
          description="운영자가 별도로 Policy를 저장합니다. Discovery가 자동 생성하지 않습니다."
          action={{ label: 'Create Policy', onClick: openCreate }}
        />
      )}

      {policy && (
        <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-semibold text-slate-800">Tool Policy</h3>
            <Button variant="outline" size="sm" onClick={openEdit}>
              Edit Policy
            </Button>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <Field label="Risk class">
              <RiskBadge risk={policy.risk_class} />
            </Field>
            <Field label="Allow auto-select">{policy.allow_auto_select ? 'Yes' : 'No'}</Field>
            <Field label="Requires confirmation">{policy.requires_confirmation ? 'Yes' : 'No'}</Field>
            <Field label="Requires approval">{policy.requires_approval ? 'Yes' : 'No'}</Field>
            <Field label="Approval Policy Ref">
              {policy.approval_policy_id ? (
                <span className="font-mono text-xs">{shortenId(policy.approval_policy_id)}</span>
              ) : (
                '—'
              )}
            </Field>
            <Field label="Timeout (ms)">{policy.timeout_ms}</Field>
            <Field label="Max attempts">{policy.max_attempts}</Field>
            <Field label="Max result bytes">{policy.max_result_bytes}</Field>
            <Field label="Data classification">{policy.data_classification ?? '—'}</Field>
            <Field label="lock_version">{policy.lock_version}</Field>
          </div>
          {policy.backoff_policy && (
            <div>
              <p className="text-xs text-slate-400 mb-1">backoff_policy</p>
              <JsonViewer value={policy.backoff_policy} />
            </div>
          )}
          {policy.policy_metadata && (
            <div>
              <p className="text-xs text-slate-400 mb-1">policy_metadata</p>
              <JsonViewer value={policy.policy_metadata} />
            </div>
          )}
        </div>
      )}

      <Dialog
        open={editorOpen}
        onClose={() => !saving && setEditorOpen(false)}
        title={policy ? 'Edit Tool Policy' : 'Create Tool Policy'}
        description="Frontend form initial values are UX defaults only — not Domain auto-policy."
        size="lg"
        footer={
          <>
            <Button variant="outline" onClick={() => setEditorOpen(false)} disabled={saving}>
              취소
            </Button>
            <Button
              variant="primary"
              loading={saving}
              disabled={approvalBlocked}
              onClick={() => void handleSave()}
            >
              Save
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm">
          {formError && <InlineAlert type="error" message={formError} />}
          {mutationError && editorOpen && (
            <InlineAlert type="error" message={mutationError.message} />
          )}

          <label className="block space-y-1">
            <span className="text-xs text-slate-500">risk_class</span>
            <select
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={form.risk_class}
              onChange={e =>
                setForm(prev => ({ ...prev, risk_class: e.target.value as RiskClass }))
              }
            >
              {RISK_CLASSES.map(value => (
                <option key={value} value={value}>
                  {labelRiskClass(value)}
                </option>
              ))}
            </select>
          </label>

          <div className="grid grid-cols-2 gap-3">
            <NumberField
              label="timeout_ms"
              value={form.timeout_ms}
              onChange={n => setForm(prev => ({ ...prev, timeout_ms: n }))}
            />
            <NumberField
              label="max_attempts"
              value={form.max_attempts}
              onChange={n => setForm(prev => ({ ...prev, max_attempts: n }))}
            />
            <NumberField
              label="max_result_bytes"
              value={form.max_result_bytes}
              onChange={n => setForm(prev => ({ ...prev, max_result_bytes: n }))}
            />
            <label className="block space-y-1">
              <span className="text-xs text-slate-500">data_classification</span>
              <input
                className="w-full border border-slate-300 rounded-md px-2 py-1.5"
                value={form.data_classification ?? ''}
                onChange={e =>
                  setForm(prev => ({ ...prev, data_classification: e.target.value || null }))
                }
              />
            </label>
          </div>

          <div className="flex flex-wrap gap-4">
            <label className="inline-flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={form.requires_confirmation}
                onChange={e =>
                  setForm(prev => ({ ...prev, requires_confirmation: e.target.checked }))
                }
              />
              requires_confirmation
            </label>
            <label className="inline-flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={form.allow_auto_select}
                onChange={e =>
                  setForm(prev => ({ ...prev, allow_auto_select: e.target.checked }))
                }
              />
              allow_auto_select
            </label>
            <label className="inline-flex items-center gap-2 text-xs text-slate-600">
              <input
                type="checkbox"
                checked={form.requires_approval}
                onChange={e => {
                  const on = e.target.checked;
                  setForm(prev => ({
                    ...prev,
                    requires_approval: on,
                    approval_policy_id: on
                      ? (prev.approval_policy_id ?? policy?.approval_policy_id ?? null)
                      : null,
                  }));
                }}
              />
              requires_approval
            </label>
          </div>

          {form.requires_approval && (
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 space-y-1">
              {form.approval_policy_id || policy?.approval_policy_id ? (
                <p>
                  Approval required. Existing Approval Policy Ref:{' '}
                  <span className="font-mono">
                    {shortenId(form.approval_policy_id ?? policy?.approval_policy_id)}
                  </span>
                  . 다른 ApprovalPolicy로 변경하는 picker는 아직 없습니다.
                </p>
              ) : (
                <p>
                  현재 ApprovalPolicy 관리 API가 제공되지 않아 새 승인정책 연결은 아직 지원하지
                  않습니다.
                </p>
              )}
            </div>
          )}

          <label className="block space-y-1">
            <span className="text-xs text-slate-500">backoff_policy (JSON object)</span>
            <textarea
              className="w-full border border-slate-300 rounded-md px-2 py-1.5 font-mono text-xs min-h-[72px]"
              value={backoffText}
              onChange={e => setBackoffText(e.target.value)}
              placeholder="{}"
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">policy_metadata (JSON object)</span>
            <textarea
              className="w-full border border-slate-300 rounded-md px-2 py-1.5 font-mono text-xs min-h-[72px]"
              value={metadataText}
              onChange={e => setMetadataText(e.target.value)}
              placeholder="{}"
            />
          </label>
        </div>
      </Dialog>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-slate-400">{label}</p>
      <div className="text-slate-700 mt-0.5">{children}</div>
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: number;
  onChange: (n: number) => void;
}) {
  return (
    <label className="block space-y-1">
      <span className="text-xs text-slate-500">{label}</span>
      <input
        type="number"
        className="w-full border border-slate-300 rounded-md px-2 py-1.5"
        value={Number.isFinite(value) ? value : ''}
        onChange={e => onChange(Number(e.target.value))}
      />
    </label>
  );
}
