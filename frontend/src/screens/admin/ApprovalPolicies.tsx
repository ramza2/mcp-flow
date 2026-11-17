import { useCallback, useEffect, useMemo, useState } from 'react';
import { Plus, RefreshCw, Search } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import {
  EmptyState,
  ErrorState,
  InlineAlert,
  LoadingSkeleton,
  PermissionDenied,
} from '../../components/ui/EmptyState';
import {
  activateApprovalPolicy,
  buildApproverScope,
  createApprovalPolicy,
  deactivateApprovalPolicy,
  listApprovalPolicies,
  parseApproverRoleCodes,
  updateApprovalPolicy,
  type ApprovalPolicyDto,
} from '../../api/approvalPolicies';
import { isAbortError, isApiError } from '../../api/client';
import {
  APPROVAL_DECISION_MODES,
  APPROVAL_POLICY_STATUSES,
  labelApprovalDecisionMode,
  labelApprovalPolicyStatus,
  type ApprovalDecisionMode,
  type ApprovalPolicyStatus,
} from '../../domain';

const PAGE_SIZE = 50;

interface PolicyFormState {
  code: string;
  name: string;
  description: string;
  decision_mode: ApprovalDecisionMode;
  required_approvals: number;
  role_codes_text: string;
  default_expiry_seconds: number;
  allow_self_approval: boolean;
  reject_comment_required: boolean;
}

function emptyForm(): PolicyFormState {
  return {
    code: '',
    name: '',
    description: '',
    decision_mode: 'ANY',
    required_approvals: 1,
    role_codes_text: '',
    default_expiry_seconds: 3600,
    allow_self_approval: false,
    reject_comment_required: false,
  };
}

function formFromPolicy(policy: ApprovalPolicyDto): PolicyFormState {
  return {
    code: policy.code,
    name: policy.name,
    description: policy.description ?? '',
    decision_mode: policy.decision_mode,
    required_approvals: policy.required_approvals,
    role_codes_text: parseApproverRoleCodes(policy.approver_scope).join(', '),
    default_expiry_seconds: policy.default_expiry_seconds,
    allow_self_approval: policy.allow_self_approval,
    reject_comment_required: policy.reject_comment_required,
  };
}

function formatApiError(err: unknown, fallback: string): string {
  if (!isApiError(err)) return fallback;
  const suffix = err.requestId ? ` (Request ID: ${err.requestId})` : '';
  if (err.code === 'RESOURCE_VERSION_CONFLICT') {
    return `다른 사용자가 이미 수정했습니다. 목록을 새로고침한 뒤 다시 시도하세요.${suffix}`;
  }
  if (err.status === 403) {
    return `권한이 없습니다. approval.policy.manage 권한이 필요합니다.${suffix}`;
  }
  return `${err.message}${suffix}`;
}

export default function ApprovalPolicies() {
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<ApprovalPolicyStatus | ''>('');
  const [reload, setReload] = useState(0);
  const [items, setItems] = useState<ApprovalPolicyDto[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [forbidden, setForbidden] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [editorOpen, setEditorOpen] = useState(false);
  const [editorMode, setEditorMode] = useState<'create' | 'edit'>('create');
  const [form, setForm] = useState<PolicyFormState>(emptyForm);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setQuery(searchInput.trim());
    }, 300);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const load = useCallback(() => {
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setForbidden(false);

    listApprovalPolicies({
      page: 1,
      page_size: PAGE_SIZE,
      q: query || undefined,
      status: statusFilter || undefined,
      sort: '-updated_at',
      signal: controller.signal,
    })
      .then((data) => {
        if (cancelled) return;
        setItems(data.items);
        setSelectedId((current) => {
          if (current && data.items.some((item) => item.id === current)) return current;
          return data.items[0]?.id ?? null;
        });
      })
      .catch((err: unknown) => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 403) {
          setForbidden(true);
          setItems([]);
          setSelectedId(null);
          return;
        }
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? '승인 정책 목록을 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setItems([]);
        setSelectedId(null);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [query, statusFilter, reload]);

  useEffect(() => load(), [load]);

  const selected = useMemo(
    () => items.find((item) => item.id === selectedId) ?? null,
    [items, selectedId],
  );

  function openCreate() {
    setEditorMode('create');
    setForm(emptyForm());
    setFormError(null);
    setEditorOpen(true);
  }

  function openEdit(policy: ApprovalPolicyDto) {
    setEditorMode('edit');
    setForm(formFromPolicy(policy));
    setFormError(null);
    setEditorOpen(true);
  }

  function validateForm(): string | null {
    if (!form.name.trim()) return '이름을 입력하세요.';
    if (editorMode === 'create' && !form.code.trim()) return '코드를 입력하세요.';
    if (form.required_approvals < 1) return '필요 승인 수는 1 이상이어야 합니다.';
    if (form.decision_mode === 'ANY' && form.required_approvals !== 1) {
      return '결정 방식이 ANY이면 필요 승인 수는 1이어야 합니다.';
    }
    if (form.default_expiry_seconds <= 0) return '만료 시간은 1초 이상이어야 합니다.';
    return null;
  }

  async function saveForm() {
    const validation = validateForm();
    if (validation) {
      setFormError(validation);
      return;
    }
    setBusy(true);
    setFormError(null);
    setActionError(null);
    setFeedback(null);
    const scope = buildApproverScope(form.role_codes_text);
    try {
      if (editorMode === 'create') {
        const created = await createApprovalPolicy({
          code: form.code.trim(),
          name: form.name.trim(),
          description: form.description.trim() || null,
          decision_mode: form.decision_mode,
          required_approvals: form.required_approvals,
          approver_scope: scope,
          default_expiry_seconds: form.default_expiry_seconds,
          allow_self_approval: form.allow_self_approval,
          reject_comment_required: form.reject_comment_required,
        });
        setEditorOpen(false);
        setFeedback('승인 정책을 만들었습니다.');
        setSelectedId(created.id);
        setReload((n) => n + 1);
      } else if (selected) {
        const updated = await updateApprovalPolicy(
          selected.id,
          {
            name: form.name.trim(),
            description: form.description.trim() || null,
            decision_mode: form.decision_mode,
            required_approvals: form.required_approvals,
            approver_scope: scope,
            default_expiry_seconds: form.default_expiry_seconds,
            allow_self_approval: form.allow_self_approval,
            reject_comment_required: form.reject_comment_required,
          },
          selected.lock_version,
        );
        setEditorOpen(false);
        setFeedback('승인 정책을 저장했습니다.');
        setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      }
    } catch (err) {
      setFormError(formatApiError(err, '승인 정책을 저장하지 못했습니다.'));
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus(policy: ApprovalPolicyDto) {
    if (busy) return;
    setBusy(true);
    setActionError(null);
    setFeedback(null);
    try {
      const updated =
        policy.status === 'ACTIVE'
          ? await deactivateApprovalPolicy(policy.id)
          : await activateApprovalPolicy(policy.id);
      setItems((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      setFeedback(
        updated.status === 'ACTIVE'
          ? '승인 정책을 활성화했습니다.'
          : '승인 정책을 비활성화했습니다.',
      );
    } catch (err) {
      setActionError(formatApiError(err, '승인 정책 상태를 변경하지 못했습니다.'));
      if (isApiError(err) && err.code === 'RESOURCE_VERSION_CONFLICT') {
        setReload((n) => n + 1);
      }
    } finally {
      setBusy(false);
    }
  }

  const roleCodes = selected ? parseApproverRoleCodes(selected.approver_scope) : [];

  return (
    <div>
      <PageHeader
        title="승인 정책"
        description="승인 정책을 구성하고 Tool 및 Workflow에 적용합니다."
        actions={
          <div className="flex items-center gap-2">
            <Button
              icon={<RefreshCw size={14} />}
              size="sm"
              variant="outline"
              onClick={() => setReload((n) => n + 1)}
              disabled={loading}
            >
              새로고침
            </Button>
            <Button icon={<Plus size={14} />} size="sm" onClick={openCreate} disabled={forbidden}>
              새 정책
            </Button>
          </div>
        }
      />

      <div className="border-b border-slate-200 bg-white px-4 py-3 flex flex-wrap gap-3 items-center">
        <div className="relative flex-1 min-w-[200px] max-w-md">
          <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
            placeholder="이름·코드 검색"
            className="w-full pl-8 pr-3 py-1.5 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-200"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(event) => setStatusFilter(event.target.value as ApprovalPolicyStatus | '')}
          className="text-sm border border-slate-200 rounded-lg px-2 py-1.5 bg-white"
        >
          <option value="">전체 상태</option>
          {APPROVAL_POLICY_STATUSES.map((status) => (
            <option key={status} value={status}>
              {labelApprovalPolicyStatus(status)}
            </option>
          ))}
        </select>
      </div>

      {feedback && (
        <div className="px-4 pt-3">
          <InlineAlert type="info" message={feedback} />
        </div>
      )}
      {actionError && (
        <div className="px-4 pt-3">
          <InlineAlert type="error" message={actionError} />
        </div>
      )}

      <div className="flex h-[calc(100vh-220px)]">
        <div className="w-72 border-r border-slate-200 bg-white overflow-y-auto">
          {loading && <LoadingSkeleton rows={6} />}
          {!loading && forbidden && <PermissionDenied />}
          {!loading && !forbidden && error && (
            <ErrorState
              message={error.message}
              requestId={error.requestId}
              onRetry={() => setReload((n) => n + 1)}
            />
          )}
          {!loading && !forbidden && !error && items.length === 0 && (
            <EmptyState
              title="승인 정책이 없습니다"
              description="새 정책을 만들어 Tool Policy 또는 Workflow 승인 단계에 연결하세요."
              action={{ label: '새 정책', onClick: openCreate, icon: <Plus size={14} /> }}
            />
          )}
          {!loading && !forbidden && !error &&
            items.map((policy) => (
              <button
                key={policy.id}
                type="button"
                onClick={() => setSelectedId(policy.id)}
                className={`w-full text-left px-4 py-3 border-b border-slate-100 hover:bg-slate-50 transition-colors
                  ${selectedId === policy.id ? 'bg-indigo-50' : ''}`}
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-sm font-medium text-slate-800 min-w-0 flex-1 break-words">
                    {policy.name}
                  </span>
                  <span title={policy.status} className="shrink-0">
                    <StatusBadge
                      status={policy.status}
                      label={labelApprovalPolicyStatus(policy.status)}
                      size="sm"
                    />
                  </span>
                </div>
                <p className="text-xs text-slate-400 mt-0.5 truncate" title={policy.code}>
                  {policy.code} · {labelApprovalDecisionMode(policy.decision_mode)} ·{' '}
                  {policy.required_approvals}명 필요
                </p>
              </button>
            ))}
        </div>

        <div className="flex-1 p-6 overflow-y-auto">
          {!loading && !forbidden && !error && !selected && items.length > 0 && (
            <EmptyState title="정책을 선택하세요" description="왼쪽 목록에서 승인 정책을 선택합니다." />
          )}
          {selected && (
            <div className="max-w-lg space-y-4">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <h2 className="text-base font-semibold text-slate-900 min-w-0 flex-1 break-words">
                  {selected.name}
                </h2>
                <div className="flex flex-wrap items-center gap-2 shrink-0 max-w-full">
                  <Button size="sm" variant="outline" onClick={() => openEdit(selected)} disabled={busy}>
                    편집
                  </Button>
                  <Button
                    size="sm"
                    variant={selected.status === 'ACTIVE' ? 'outline' : 'primary'}
                    onClick={() => void toggleStatus(selected)}
                    loading={busy}
                  >
                    {selected.status === 'ACTIVE' ? '비활성화' : '활성화'}
                  </Button>
                </div>
              </div>
              <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
                <Row label="코드">
                  <span className="font-mono text-xs text-slate-600">{selected.code}</span>
                </Row>
                <Row label="상태">
                  <span title={selected.status}>
                    <StatusBadge
                      status={selected.status}
                      label={labelApprovalPolicyStatus(selected.status)}
                      size="sm"
                    />
                  </span>
                </Row>
                <Row label="결정 방식">
                  <span title={selected.decision_mode}>
                    {labelApprovalDecisionMode(selected.decision_mode)}
                  </span>
                </Row>
                <Row label="필요 승인 수">{selected.required_approvals}명</Row>
                <Row label="승인자 역할">
                  {roleCodes.length > 0 ? roleCodes.join(', ') : '제한 없음 (decide 권한)'}
                </Row>
                <Row label="만료 시간">
                  {Math.round(selected.default_expiry_seconds / 60)}분
                  <span className="text-slate-400 text-xs ml-1">
                    ({selected.default_expiry_seconds}초)
                  </span>
                </Row>
                <Row label="자기 승인">{selected.allow_self_approval ? '허용' : '불허'}</Row>
                <Row label="거절 코멘트">
                  {selected.reject_comment_required ? '필수' : '선택'}
                </Row>
                {selected.description && (
                  <Row label="설명">
                    <span className="whitespace-pre-wrap">{selected.description}</span>
                  </Row>
                )}
                <Row label="버전">
                  <span className="font-mono text-xs text-slate-500">{selected.lock_version}</span>
                </Row>
              </div>
              <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 text-sm text-blue-700">
                이 정책은 Tool Policy 또는 Workflow Approval 설정에서 선택할 수 있습니다.
                비활성화는 ToolPolicy·대기 중 승인 요청·초안/게시 Workflow 참조가 있으면
                서버에서 거부됩니다.
              </div>
            </div>
          )}
        </div>
      </div>

      <Dialog
        open={editorOpen}
        onClose={() => !busy && setEditorOpen(false)}
        title={editorMode === 'create' ? '새 승인 정책' : '승인 정책 편집'}
        description="결정 방식·승인자 역할·만료 시간을 설정합니다."
        size="md"
        footer={
          <>
            <Button variant="outline" onClick={() => setEditorOpen(false)} disabled={busy}>
              취소
            </Button>
            <Button onClick={() => void saveForm()} loading={busy}>
              {editorMode === 'create' ? '만들기' : '저장'}
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm">
          {formError && <InlineAlert type="error" message={formError} />}
          {editorMode === 'create' && (
            <Field label="코드">
              <input
                value={form.code}
                onChange={(event) => setForm((prev) => ({ ...prev, code: event.target.value }))}
                className="w-full border border-slate-200 rounded-lg px-3 py-1.5 font-mono text-xs"
                placeholder="external-transfer"
              />
            </Field>
          )}
          <Field label="이름">
            <input
              value={form.name}
              onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))}
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5"
              placeholder="외부 전송 승인"
            />
          </Field>
          <Field label="설명">
            <textarea
              value={form.description}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, description: event.target.value }))
              }
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5 min-h-[64px]"
            />
          </Field>
          <Field label="결정 방식">
            <select
              value={form.decision_mode}
              onChange={(event) => {
                const mode = event.target.value as ApprovalDecisionMode;
                setForm((prev) => ({
                  ...prev,
                  decision_mode: mode,
                  required_approvals: mode === 'ANY' ? 1 : prev.required_approvals,
                }));
              }}
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5 bg-white"
            >
              {APPROVAL_DECISION_MODES.map((mode) => (
                <option key={mode} value={mode}>
                  {labelApprovalDecisionMode(mode)} ({mode})
                </option>
              ))}
            </select>
          </Field>
          <Field label="필요 승인 수">
            <input
              type="number"
              min={1}
              value={form.required_approvals}
              disabled={form.decision_mode === 'ANY'}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  required_approvals: Math.max(1, Number(event.target.value) || 1),
                }))
              }
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5 disabled:bg-slate-50"
            />
          </Field>
          <Field label="승인자 역할 코드">
            <input
              value={form.role_codes_text}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, role_codes_text: event.target.value }))
              }
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5 font-mono text-xs"
              placeholder="APPROVER, ADMIN (비우면 제한 없음)"
            />
            <p className="text-xs text-slate-400 mt-1">
              쉼표로 구분합니다. 비우면 null 범위(approval.decide만)입니다.
            </p>
          </Field>
          <Field label="만료(초)">
            <input
              type="number"
              min={1}
              value={form.default_expiry_seconds}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  default_expiry_seconds: Math.max(1, Number(event.target.value) || 1),
                }))
              }
              className="w-full border border-slate-200 rounded-lg px-3 py-1.5"
            />
          </Field>
          <label className="flex items-center gap-2 text-slate-700">
            <input
              type="checkbox"
              checked={form.allow_self_approval}
              onChange={(event) =>
                setForm((prev) => ({ ...prev, allow_self_approval: event.target.checked }))
              }
            />
            자기 승인 허용
          </label>
          <label className="flex items-center gap-2 text-slate-700">
            <input
              type="checkbox"
              checked={form.reject_comment_required}
              onChange={(event) =>
                setForm((prev) => ({
                  ...prev,
                  reject_comment_required: event.target.checked,
                }))
              }
            />
            거절 시 코멘트 필수
          </label>
        </div>
      </Dialog>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-4">
      <span className="text-slate-400 w-36 shrink-0 text-xs pt-0.5">{label}</span>
      <span className="text-slate-700">{children}</span>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1">
      <span className="text-xs font-medium text-slate-600">{label}</span>
      {children}
    </label>
  );
}
