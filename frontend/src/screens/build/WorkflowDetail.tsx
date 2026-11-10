import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Eye, Pencil, Plus } from 'lucide-react';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { createWorkflowVersion, getWorkflow, listWorkflowVersions, type WorkflowDto, type WorkflowVersionDto } from '../../api/workflows';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp, labelWorkflowStatus, labelWorkflowVersionStatus, labelWorkflowVisibility } from '../../domain';

/** Initial server-side DRAFT is intentionally incomplete and must be edited + validated. */
export function initialWorkflowPlan(workflow: WorkflowDto) {
  return {
    schema_version: '1.0',
    goal: workflow.name,
    source: { type: 'WORKFLOW', workflow_id: workflow.id },
    inputs: {},
    limits: { max_steps: 20, max_duration_seconds: 300, max_parallelism: 4, max_loop_iterations: 50 },
    steps: [],
    completion: { success_policy: 'ALL_REQUIRED', response_step_ids: [] },
  };
}

export default function WorkflowDetail() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [workflow, setWorkflow] = useState<WorkflowDto | null>(null);
  const [versions, setVersions] = useState<WorkflowVersionDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!workflowId) return () => {};
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([getWorkflow(workflowId, controller.signal), listWorkflowVersions(workflowId, controller.signal)])
      .then(([workflowResult, versionPage]) => {
        if (cancelled) return;
        setWorkflow(workflowResult);
        setVersions(versionPage.items);
      }).catch(err => {
        if (cancelled || isAbortError(err)) return;
        const apiErr = isApiError(err) ? err : null;
        setError({ message: apiErr?.message ?? '워크플로 정보를 불러오지 못했습니다.', requestId: apiErr?.requestId ?? undefined });
        setWorkflow(null);
        setVersions([]);
      }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [workflowId]);

  useEffect(() => load(), [load]);

  const draft = versions.find(version => version.status === 'DRAFT');
  const current = versions.find(version => version.id === workflow?.current_version_id);

  const handleCreateOrEdit = async () => {
    if (!workflow || busy) return;
    if (draft) {
      navigate(`/workflows/${workflow.id}/versions/${draft.id}/edit`);
      return;
    }
    setBusy(true);
    setActionError(null);
    try {
      const created = await createWorkflowVersion(workflow.id, current
        ? { source_version_id: current.id, change_summary: '게시 버전에서 새 초안 생성' }
        : { plan_definition: initialWorkflowPlan(workflow), change_summary: '초기 초안' });
      navigate(`/workflows/${workflow.id}/versions/${created.id}/edit`);
    } catch (err) {
      setActionError(isApiError(err) ? err.message : '초안 버전 생성에 실패했습니다.');
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <div className="p-6"><LoadingSkeleton rows={7} /></div>;
  if (error || !workflow) return (
    <div className="p-6 space-y-4">
      <ErrorState message={error?.message ?? '워크플로를 찾을 수 없습니다.'} requestId={error?.requestId} onRetry={() => { load(); }} />
      <Button variant="outline" onClick={() => navigate('/workflows')}>목록으로 돌아가기</Button>
    </div>
  );

  const columns: Column<WorkflowVersionDto>[] = [
    { key: 'version', label: '버전', render: row => <VersionBadge version={`v${row.version_no}`} status={row.status} /> },
    { key: 'status', label: '상태', render: row => <StatusBadge status={row.status} label={labelWorkflowVersionStatus(row.status)} size="sm" /> },
    { key: 'validation', label: '검증', render: row => <StatusBadge status={row.validation_status} size="sm" /> },
    { key: 'steps', label: '단계 수', align: 'center', render: row => <span className="text-sm text-slate-600">{Array.isArray(row.plan_definition.steps) ? row.plan_definition.steps.length : '—'}</span> },
    { key: 'summary', label: '변경 내용', render: row => <span className="text-sm text-slate-600">{row.change_summary ?? '—'}</span> },
    { key: 'created', label: '생성일', render: row => <span className="text-xs text-slate-400">{formatTimestamp(row.created_at)}</span> },
    { key: 'actions', label: '', render: row => (
      <button
        title={row.status === 'DRAFT' ? '초안 편집' : '실행 계획 조회'}
        onClick={event => { event.stopPropagation(); navigate(`/workflows/${workflow.id}/versions/${row.id}/edit`); }}
        className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
      >
        {row.status === 'DRAFT' ? <Pencil size={13} /> : <Eye size={13} />}
      </button>
    ) },
  ];

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/workflows')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"><ArrowLeft size={14} /> 워크플로 관리</button>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{workflow.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={workflow.status} label={labelWorkflowStatus(workflow.status)} />
              {current && <VersionBadge version={`v${current.version_no}`} status="PUBLISHED" />}
              <span className="text-xs text-slate-400">{workflow.description}</span>
            </div>
          </div>
          <Button size="sm" icon={draft ? <Pencil size={13} /> : <Plus size={13} />} loading={busy} onClick={() => { void handleCreateOrEdit(); }}>
            {draft ? '초안 편집' : '새 초안 버전 생성'}
          </Button>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar tabs={[{ id: 'overview', label: '개요' }, { id: 'versions', label: '버전', badge: versions.length }]} activeTab={tab} onChange={setTab} />
      </div>

      <div className="p-6 space-y-4">
        {actionError && <InlineAlert type="error" message={actionError} />}
        <InlineAlert type="info" message="실제 서버에 등록된 워크플로입니다. 초안은 저장 후 별도로 검증해야 합니다. 단계 흐름의 시각 편집과 고급 JSON 편집을 지원하며, 게시·실행 UI는 후속 개발 대상입니다." />
        {tab === 'overview' && (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4 max-w-3xl">
            <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-2 text-sm">
              <h2 className="font-semibold text-slate-800 mb-3">기본 정보</h2>
              <p>공개 범위: <span title={workflow.visibility}>{labelWorkflowVisibility(workflow.visibility)}</span></p>
              <p>소유자 ID: <span className="font-mono text-xs">{workflow.owner_id ?? '—'}</span></p>
              <p>생성일: {formatTimestamp(workflow.created_at)}</p>
              <p>수정일: {formatTimestamp(workflow.updated_at)}</p>
            </div>
            <div className="bg-white border border-slate-200 rounded-xl p-4 space-y-2 text-sm">
              <h2 className="font-semibold text-slate-800 mb-3">버전 정보</h2>
              <p>현재 게시 버전: {current ? `v${current.version_no}` : '없음'}</p>
              <p>전체 버전 수: {versions.length}</p>
              <p>편집 가능한 초안: {draft ? `v${draft.version_no}` : '없음'}</p>
            </div>
          </div>
        )}
        {tab === 'versions' && (
          <div className="max-w-5xl space-y-3">
            <InlineAlert type="info" message="초안(DRAFT)만 수정 가능합니다. 게시됨(PUBLISHED)과 이전 버전(DEPRECATED)의 실행 계획은 읽기 전용입니다." />
            <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
              {versions.length ? (
                <DataTable columns={columns} data={versions} rowKey={row => row.id} onRowClick={row => navigate(`/workflows/${workflow.id}/versions/${row.id}/edit`)} />
              ) : <EmptyState title="아직 버전이 없습니다" description="새 초안 버전을 생성하여 실행 계획을 작성하세요." />}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
