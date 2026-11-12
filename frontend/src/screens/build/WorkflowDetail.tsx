import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Eye, Pencil, Play, Plus, Power, Rocket } from 'lucide-react';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import Dialog, { ConfirmDialog } from '../../components/ui/Dialog';
import DataTable, { Column } from '../../components/ui/DataTable';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { changeWorkflowStatus, createWorkflowExecution, createWorkflowVersion, getWorkflow, getWorkflowVersion, listWorkflowVersions, publishWorkflowVersion, type WorkflowDto, type WorkflowVersionDto } from '../../api/workflows';
import { isAbortError, isApiError } from '../../api/client';
import { formatTimestamp, labelWorkflowStatus, labelWorkflowVersionStatus, labelWorkflowVisibility } from '../../domain';
import type { WorkflowStatus } from '../../domain/types';
import { parseWorkflowRunInputs, planInputDefinitions } from './workflowRunInputs';
import { canUseWorkflowInputForm, parseWorkflowInputForm, workflowInputsToForm } from './workflowRunInputForm';
import WorkflowRunInputForm from './WorkflowRunInputForm';

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
  const [publishOpen, setPublishOpen] = useState(false);
  const [statusTarget, setStatusTarget] = useState<WorkflowStatus | null>(null);
  const [runOpen, setRunOpen] = useState(false);
  const [runInputs, setRunInputs] = useState('{}');
  const [runMode, setRunMode] = useState<'form' | 'json'>('form');
  const [runFormValues, setRunFormValues] = useState<Record<string, string>>({});
  const [runFormIncluded, setRunFormIncluded] = useState<Record<string, boolean>>({});
  const [runKey, setRunKey] = useState('');
  const [runInputError, setRunInputError] = useState<string | null>(null);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [actionSuccess, setActionSuccess] = useState<string | null>(null);

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
  const canPublish = Boolean(draft && draft.validation_status === 'VALID'
    && draft.validation_report?.valid === true
    && draft.validation_report.content_hash === draft.content_hash);
  const hasPublishedCurrent = Boolean(current && current.status === 'PUBLISHED' && current.validation_status === 'VALID');
  const canActivate = Boolean(hasPublishedCurrent && (workflow?.status === 'DRAFT' || workflow?.status === 'INACTIVE'));
  const canRun = Boolean(workflow?.status === 'ACTIVE' && hasPublishedCurrent);
  const inputDefinitions = current ? planInputDefinitions(current.plan_definition) : {};
  const runFormSupported = current ? canUseWorkflowInputForm(current.plan_definition) : false;

  const handleCreateOrEdit = async () => {
    if (!workflow || busy) return;
    if (draft) {
      navigate(`/workflows/${workflow.id}/versions/${draft.id}/edit`);
      return;
    }
    setBusy(true);
    setActionError(null);
    setActionSuccess(null);
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

  const handlePublish = async () => {
    if (!workflow || !draft || !canPublish || busy) return;
    setBusy(true);
    setActionError(null);
    setActionSuccess(null);
    try {
      // The backend rechecks the Plan and dependencies atomically at publish.
      // This preflight also avoids publishing stale browser state by accident.
      const [latestVersion, latestWorkflow] = await Promise.all([
        getWorkflowVersion(workflow.id, draft.id), getWorkflow(workflow.id),
      ]);
      if (latestWorkflow.current_version_id !== workflow.current_version_id
        || latestVersion.status !== 'DRAFT'
        || latestVersion.content_hash !== draft.content_hash
        || latestVersion.validation_status !== 'VALID'
        || latestVersion.validation_report?.valid !== true
        || latestVersion.validation_report.content_hash !== latestVersion.content_hash) {
        setActionError('게시 상태 또는 검증 결과가 변경되었습니다. 최신 버전을 다시 확인해 주세요.');
        load();
        return;
      }
      await publishWorkflowVersion(workflow.id, draft.id);
      setActionSuccess(`v${draft.version_no} 게시가 완료되었습니다. 실행하려면 별도 활성화 및 실행 절차가 필요합니다.`);
      load();
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setActionError(apiErr?.message ?? '게시하지 못했습니다. 서버 검증 상태를 확인해 주세요.');
      load();
    } finally {
      setPublishOpen(false);
      setBusy(false);
    }
  };

  const handleStatusChange = async () => {
    if (!workflow || !statusTarget || busy) return;
    setBusy(true);
    setActionError(null);
    setActionSuccess(null);
    try {
      const latest = await getWorkflow(workflow.id);
      if (latest.lock_version !== workflow.lock_version || latest.status !== workflow.status
        || latest.current_version_id !== workflow.current_version_id) {
        setActionError('워크플로 상태가 다른 곳에서 변경되었습니다. 최신 상태를 확인해 주세요.');
        load();
        return;
      }
      await changeWorkflowStatus(workflow.id, statusTarget, workflow.lock_version);
      setActionSuccess(statusTarget === 'ACTIVE' ? '워크플로가 활성화되었습니다.' : '워크플로가 비활성화되었습니다.');
      load();
    } catch (err) {
      setActionError(isApiError(err) ? err.message : '워크플로 상태를 변경하지 못했습니다.');
      load();
    } finally {
      setStatusTarget(null);
      setBusy(false);
    }
  };

  const switchRunMode = () => {
    if (!current || busy) return;
    if (runMode === 'form') {
      const parsed = parseWorkflowInputForm(runFormValues, runFormIncluded, current.plan_definition);
      if (!parsed.ok) { setRunInputError(parsed.error); return; }
      setRunInputs(JSON.stringify(parsed.value, null, 2));
      setRunMode('json');
    } else {
      const parsed = parseWorkflowRunInputs(runInputs, current.plan_definition);
      if (!parsed.ok) { setRunInputError(parsed.error); return; }
      const form = workflowInputsToForm(parsed.value, current.plan_definition);
      setRunFormValues(form.values);
      setRunFormIncluded(form.included);
      setRunMode('form');
    }
    setRunInputError(null);
  };

  const handleRun = async () => {
    if (!workflow || !current || !canRun || !runKey || busy) return;
    const parsed = runMode === 'form'
      ? parseWorkflowInputForm(runFormValues, runFormIncluded, current.plan_definition)
      : parseWorkflowRunInputs(runInputs, current.plan_definition);
    if (!parsed.ok) {
      setRunInputError(parsed.error);
      return;
    }
    setBusy(true);
    setRunInputError(null);
    setActionError(null);
    try {
      // Recheck the exact immutable version and Workflow activation before any execution write.
      const [latestWorkflow, latestVersion] = await Promise.all([
        getWorkflow(workflow.id), getWorkflowVersion(workflow.id, current.id),
      ]);
      if (latestWorkflow.lock_version !== workflow.lock_version
        || latestWorkflow.status !== 'ACTIVE'
        || latestWorkflow.current_version_id !== current.id
        || latestVersion.status !== 'PUBLISHED'
        || latestVersion.validation_status !== 'VALID'
        || latestVersion.content_hash !== current.content_hash) {
        setActionError('워크플로 또는 게시 버전 상태가 변경되었습니다. 다시 확인해 주세요.');
        setRunOpen(false);
        load();
        return;
      }
      const result = await createWorkflowExecution(workflow.id, current.id, parsed.value, runKey);
      setRunOpen(false);
      navigate(`/executions/${result.id}`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setRunInputError(apiErr?.status === 403
        ? '실행 권한이 없습니다. workflow.execute + WORKFLOW 권한 및 도구별 mcp.tool.execute + MCP_TOOL 권한을 확인하세요.'
        : apiErr?.message ?? '실행 요청에 실패했습니다. 동일한 요청 키로 재시도할 수 있습니다.');
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
          <div className="flex gap-2">
            <Button size="sm" variant="outline" icon={draft ? <Pencil size={13} /> : <Plus size={13} />} loading={busy} onClick={() => { void handleCreateOrEdit(); }}>
              {draft ? '초안 편집' : '새 초안 버전 생성'}
            </Button>
            <Button size="sm" icon={<Rocket size={13} />} disabled={!canPublish || busy} title={canPublish ? '검증된 초안 게시' : '게시하려면 초안을 저장하고 서버 검증을 통과해야 합니다.'} onClick={() => setPublishOpen(true)}>
              게시
            </Button>
            {(workflow.status === 'ACTIVE' || workflow.status === 'DRAFT' || workflow.status === 'INACTIVE') && (
              <Button size="sm" variant="outline" icon={<Power size={13} />} disabled={busy || (workflow.status !== 'ACTIVE' && !canActivate)}
                title={workflow.status === 'ACTIVE' ? '실행을 중지하고 비활성화' : canActivate ? '게시 버전 활성화' : '검증된 게시 버전이 있어야 활성화할 수 있습니다.'}
                onClick={() => setStatusTarget(workflow.status === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE')}>
                {workflow.status === 'ACTIVE' ? '비활성화' : '활성화'}
              </Button>
            )}
            <Button size="sm" icon={<Play size={13} />} disabled={!canRun || busy}
              title={canRun ? '현재 게시 버전 수동 실행' : 'ACTIVE 워크플로의 검증된 게시 버전만 실행할 수 있습니다.'}
              onClick={() => {
                setRunInputs('{}');
                setRunFormValues({});
                setRunFormIncluded({});
                setRunMode(runFormSupported ? 'form' : 'json');
                setRunInputError(null);
                setRunKey(crypto.randomUUID());
                setRunOpen(true);
              }}>
              수동 실행
            </Button>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar tabs={[{ id: 'overview', label: '개요' }, { id: 'versions', label: '버전', badge: versions.length }]} activeTab={tab} onChange={setTab} />
      </div>

      <div className="p-6 space-y-4">
        {actionError && <InlineAlert type="error" message={actionError} />}
        {actionSuccess && <div role="status" className="rounded-md border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm text-emerald-800">{actionSuccess}</div>}
        <InlineAlert type="info" message="초안은 저장 후 서버 검증을 통과해야 게시할 수 있습니다. 게시된 버전은 읽기 전용이며, 게시만으로 활성화되거나 실행되지 않습니다. 수동 실행은 활성 워크플로의 현재 게시 버전을 사용하며 서버 권한·정책을 다시 검증합니다." />
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
      <ConfirmDialog
        open={publishOpen && canPublish}
        onClose={() => { if (!busy) setPublishOpen(false); }}
        onConfirm={() => { void handlePublish(); }}
        loading={busy}
        title="워크플로 버전 게시"
        confirmLabel="게시 확정"
        description={`v${draft?.version_no ?? '—'} 초안을 게시하시겠습니까? 게시 후에는 수정할 수 없으며${current ? ` 현재 게시된 v${current.version_no}은 이전 버전으로 전환됩니다.` : ' 현재 게시 버전으로 지정됩니다.'} 게시만으로 활성화되거나 실행되지는 않습니다.`}
      />
      <ConfirmDialog
        open={statusTarget !== null}
        onClose={() => { if (!busy) setStatusTarget(null); }}
        onConfirm={() => { void handleStatusChange(); }}
        loading={busy}
        title={statusTarget === 'ACTIVE' ? '워크플로 활성화' : '워크플로 비활성화'}
        confirmLabel={statusTarget === 'ACTIVE' ? '활성화 확정' : '비활성화 확정'}
        description={statusTarget === 'ACTIVE'
          ? '현재 게시된 검증 버전을 활성화하시겠습니까? 활성화 자체로 실행되지는 않습니다.'
          : '워크플로를 비활성화하시겠습니까? 이후 새 수동·예약 실행이 제한됩니다. 이미 시작한 실행은 이 작업으로 취소되지 않습니다.'}
      />
      <Dialog open={runOpen} onClose={() => { if (!busy) setRunOpen(false); }}
        title="워크플로 수동 실행" description={`현재 게시 버전 v${current?.version_no ?? '—'}을 실행합니다.`}
        footer={<>
          <Button variant="outline" disabled={busy} onClick={() => setRunOpen(false)}>취소</Button>
          <Button loading={busy} onClick={() => { void handleRun(); }}>실행 확정</Button>
        </>}>
        <div className="space-y-3">
          <p className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-md p-2">
            실행 시 연결된 MCP 도구가 외부 시스템에 실제 변경을 수행할 수 있습니다. 입력값과 실행 대상을 확인하세요.
          </p>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-xs text-slate-600">현재 게시 버전 입력 항목 ({Object.keys(inputDefinitions).length}개)</p>
            {runFormSupported && (
              <button type="button" className="text-xs text-indigo-600 underline hover:text-indigo-800"
                disabled={busy} onClick={switchRunMode}>
                {runMode === 'form' ? '고급 JSON 입력' : '폼 입력으로 돌아가기'}
              </button>
            )}
          </div>
          {!runFormSupported && <InlineAlert type="info" message="지원하지 않는 입력 유형이 있어 고급 JSON 입력을 사용합니다. 서버에서 최종 검증합니다." />}
          {runMode === 'form' && runFormSupported ? (
            <WorkflowRunInputForm definitions={inputDefinitions} values={runFormValues}
              included={runFormIncluded} busy={busy}
              onValueChange={(name, value) => {
                setRunFormValues(previous => ({ ...previous, [name]: value }));
                setRunInputError(null);
              }}
              onIncludeChange={(name, enabled) => {
                setRunFormIncluded(previous => ({ ...previous, [name]: enabled }));
                setRunInputError(null);
              }} />
          ) : (
            <>
              <label htmlFor="workflow-run-inputs" className="block text-xs font-medium text-slate-700">입력값 JSON 객체</label>
              <textarea id="workflow-run-inputs" aria-label="입력값 JSON 객체" value={runInputs}
                onChange={event => { setRunInputs(event.target.value); setRunInputError(null); }}
                disabled={busy} spellCheck={false} rows={6}
                className="w-full rounded-md border border-slate-300 p-2 text-xs font-mono focus:ring-2 focus:ring-indigo-500" />
              <p className="text-xs text-slate-500">비밀 입력은 원문 대신 등록된 Secret UUID를 담은 SECRET_REF 객체로 전달해야 합니다.</p>
            </>
          )}
          {runInputError && <InlineAlert type="error" message={runInputError} />}
        </div>
      </Dialog>
    </div>
  );
}
