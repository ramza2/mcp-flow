import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Calendar, Eye, Pencil, Plus, Rocket } from 'lucide-react';
import StatusBadge, { VersionBadge, RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import { EmptyState, InlineAlert } from '../../components/ui/EmptyState';
import { mockWorkflows, mockWorkflowFull } from '../../data/mock';

type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';

export default function WorkflowDetail() {
  const { workflowId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');

  const existingWorkflow = mockWorkflows.find(w => w.id === workflowId);
  const isNewLogicalWorkflow = !existingWorkflow && !!workflowId?.startsWith('wf-');
  const unknownWorkflowId = !existingWorkflow && !isNewLogicalWorkflow;

  const workflow = existingWorkflow ?? (isNewLogicalWorkflow
    ? {
      id: workflowId!,
      name: 'New Workflow',
      status: 'DRAFT' as const,
      publishedVersion: null as string | null,
      steps: 0,
      owner: 'admin',
      lastPublished: null as string | null,
      updatedAt: new Date().toISOString().slice(0, 10),
    }
    : null);

  const full = isNewLogicalWorkflow
    ? {
      description: '신규 Logical Workflow (아직 저장되지 않은 Mock Draft)',
      owner: 'admin',
      currentVersion: null as string | null,
      toolCount: 0,
      scheduleCount: 0,
      createdAt: new Date().toISOString().slice(0, 10),
      lastPublished: null as string | null,
      versions: [{
        version: 'v1',
        status: 'DRAFT' as const,
        steps: 0,
        changeSummary: 'Initial draft',
        validation: null as 'VALID' | 'INVALID' | 'WARNING' | null,
        createdBy: 'admin',
        createdAt: new Date().toISOString().slice(0, 10),
        publishedAt: null as string | null,
      }],
      tools: [] as typeof mockWorkflowFull['wf-001']['tools'],
      schedules: [] as typeof mockWorkflowFull['wf-001']['schedules'],
    }
    : (workflow ? (mockWorkflowFull[workflow.id] ?? null) : null);

  if (unknownWorkflowId || !workflow || !full) {
    return (
      <div className="p-6">
        <InlineAlert type="warning" message={`Workflow를 찾을 수 없습니다: ${workflowId ?? '(missing id)'}`} />
        <div className="mt-4">
          <Button variant="outline" onClick={() => navigate('/workflows')}>Workflows로 돌아가기</Button>
        </div>
      </div>
    );
  }

  const latest = full.versions[0];
  const latestStatus = (latest?.status ?? 'DRAFT') as VersionStatus;

  const handleLatestPrimary = () => {
    if (!latest) {
      navigate(`/workflows/${workflow.id}/versions/new/edit`);
      return;
    }
    if (latestStatus === 'DRAFT') {
      navigate(`/workflows/${workflow.id}/versions/${latest.version}/edit`);
      return;
    }
    navigate(`/workflows/${workflow.id}/versions/new/edit`);
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/workflows')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Workflows
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{workflow.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={workflow.status} />
              {full.currentVersion && <VersionBadge version={full.currentVersion} status="PUBLISHED" />}
              <span className="text-xs text-slate-400">{full.description}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              icon={latestStatus === 'DRAFT' ? <Pencil size={13} /> : <Plus size={13} />}
              onClick={handleLatestPrimary}
            >
              {latestStatus === 'DRAFT' ? 'Designer 열기' : 'Create New Draft'}
            </Button>
            <Button size="sm" icon={<Rocket size={13} />} disabled={latestStatus !== 'DRAFT'}>
              Publish
            </Button>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'versions', label: 'Versions', badge: full.versions.length },
            { id: 'tools', label: 'Tools', badge: full.tools.length },
            { id: 'schedules', label: 'Schedules', badge: full.schedules.length },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-3xl">
            <InfoCard title="기본 정보">
              <Row label="Owner">{full.owner}</Row>
              <Row label="Current" mono>{full.currentVersion ?? '—'}</Row>
              <Row label="Steps">{latest.steps}</Row>
              <Row label="Tools">{full.toolCount}</Row>
            </InfoCard>
            <InfoCard title="발행 상태">
              <Row label="Published" mono>{workflow.publishedVersion ?? '—'}</Row>
              <Row label="Last Published">{full.lastPublished ?? '—'}</Row>
              <Row label="Schedules">{full.scheduleCount}</Row>
              <Row label="Created">{full.createdAt}</Row>
            </InfoCard>
          </div>
        )}

        {tab === 'versions' && (
          <div className="space-y-3 max-w-4xl">
            <InlineAlert
              type="info"
              message="DRAFT Version만 Designer에서 편집할 수 있습니다. PUBLISHED는 View 또는 Create New Draft Version, DEPRECATED는 View만 가능합니다."
            />
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <DataTable
                columns={versionCols(navigate, workflow.id)}
                data={full.versions}
                rowKey={r => r.version}
                onRowClick={r => navigate(`/workflows/${workflow.id}/versions/${r.version}/edit`)}
              />
            </div>
          </div>
        )}

        {tab === 'tools' && (
          <div className="max-w-4xl bg-white rounded-xl border border-slate-200 overflow-hidden">
            <DataTable
              columns={toolCols}
              data={full.tools}
              rowKey={r => r.toolId}
              emptyMessage="이 Workflow에 연결된 Tool이 없습니다."
            />
          </div>
        )}

        {tab === 'schedules' && (
          <div className="max-w-3xl">
            {full.schedules.length === 0 ? (
              <div className="bg-white rounded-xl border border-slate-200">
                <EmptyState
                  icon={<Calendar size={22} />}
                  title="연결된 Schedule이 없습니다"
                  description="이 Workflow를 정기 실행하려면 Schedule을 등록하세요."
                  action={{ label: 'Schedule 등록', onClick: () => navigate('/schedules/new') }}
                />
              </div>
            ) : (
              <div className="space-y-2">
                {full.schedules.map(s => (
                  <div key={s.id} className="bg-white border border-slate-200 rounded-lg p-4 flex justify-between items-center">
                    <div>
                      <p className="text-sm font-medium text-slate-800">{s.name}</p>
                      <p className="text-xs text-slate-400">{s.schedule} · {s.timezone} · Next: {s.nextRun}</p>
                    </div>
                    <StatusBadge status={s.status} size="sm" />
                  </div>
                ))}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function versionCols(navigate: (to: string) => void, workflowId: string): Column<typeof mockWorkflowFull['wf-001']['versions'][0]>[] {
  return [
    { key: 'version', label: 'Version', render: r => <VersionBadge version={r.version} status={r.status} /> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'steps', label: 'Steps', render: r => <span className="text-sm text-slate-600">{r.steps}</span>, align: 'center' },
    { key: 'changeSummary', label: 'Change Summary', render: r => <span className="text-sm text-slate-700">{r.changeSummary}</span> },
    { key: 'createdBy', label: 'Author', render: r => <span className="text-xs text-slate-500">{r.createdBy}</span> },
    { key: 'createdAt', label: 'Created', render: r => <span className="text-xs text-slate-400">{r.createdAt}</span> },
    { key: 'actions', label: '', render: r => {
      const status = r.status as VersionStatus;
      return (
        <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
          {(status === 'PUBLISHED' || status === 'DEPRECATED') && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="View"
            >
              <Eye size={13} />
            </button>
          )}
          {status === 'DRAFT' && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="Edit"
            >
              <Pencil size={13} />
            </button>
          )}
          {status === 'PUBLISHED' && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/new/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="Create New Draft Version"
            >
              <Plus size={13} />
            </button>
          )}
        </div>
      );
    }},
  ];
}

const toolCols: Column<typeof mockWorkflowFull['wf-001']['tools'][0]>[] = [
  { key: 'step', label: 'Step', render: r => <span className="text-xs font-medium text-slate-500">{r.step}</span> },
  { key: 'toolName', label: 'Tool', render: r => <span className="font-medium text-slate-800">{r.toolName}</span> },
  { key: 'serverName', label: 'Server', render: r => <span className="text-xs text-slate-500">{r.serverName}</span> },
  { key: 'riskClass', label: 'Risk', render: r => <RiskBadge risk={r.riskClass} /> },
  { key: 'verification', label: 'Verification', render: r => <VerificationBadge status={r.verification} /> },
];

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-4">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
