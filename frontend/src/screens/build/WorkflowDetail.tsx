import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Calendar, Eye, Pencil, Plus, Rocket } from 'lucide-react';
import StatusBadge, { VersionBadge, RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import { EmptyState, InlineAlert } from '../../components/ui/EmptyState';
import { mockWorkflows, mockWorkflowFull } from '../../data/mock';
import { labelWorkflowStatus, labelWorkflowVersionStatus } from '../../domain';

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
      name: '새 워크플로 (예시)',
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
      description: '새 워크플로 예시 (서버에 저장되지 않음)',
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
        changeSummary: '초기 초안',
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
        <InlineAlert type="warning" message={`워크플로를 찾을 수 없습니다: ${workflowId ?? '(ID 없음)'}`} />
        <div className="mt-4">
          <Button variant="outline" onClick={() => navigate('/workflows')}>워크플로 목록으로 돌아가기</Button>
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
          <ArrowLeft size={14} /> 워크플로 관리
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{workflow.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={workflow.status} label={labelWorkflowStatus(workflow.status)} />
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
              {latestStatus === 'DRAFT' ? '디자이너 열기' : '새 초안 예시 만들기'}
            </Button>
            <Button size="sm" icon={<Rocket size={13} />} disabled>
              게시 (미지원)
            </Button>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: '개요' },
            { id: 'versions', label: '버전', badge: full.versions.length },
            { id: 'tools', label: '연결 도구', badge: full.tools.length },
            { id: 'schedules', label: '예약 실행', badge: full.schedules.length },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        <div className="mb-4 max-w-4xl">
          <InlineAlert type="info" message="이 화면은 워크플로 예시 데이터입니다. 디자이너의 변경 사항은 저장되지 않고 게시·실행 기능도 아직 연결되어 있지 않습니다." />
        </div>
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-3xl">
            <InfoCard title="기본 정보">
              <Row label="소유자">{full.owner}</Row>
              <Row label="현재 버전" mono>{full.currentVersion ?? '—'}</Row>
              <Row label="단계 수">{latest.steps}</Row>
              <Row label="도구 수">{full.toolCount}</Row>
            </InfoCard>
            <InfoCard title="발행 상태">
              <Row label="게시 버전" mono>{workflow.publishedVersion ?? '—'}</Row>
              <Row label="최근 게시일">{full.lastPublished ?? '—'}</Row>
              <Row label="연결 예약 수">{full.scheduleCount}</Row>
              <Row label="생성일">{full.createdAt}</Row>
            </InfoCard>
          </div>
        )}

        {tab === 'versions' && (
          <div className="space-y-3 max-w-4xl">
            <InlineAlert
              type="info"
              message="초안(DRAFT) 버전만 편집할 수 있습니다. 게시됨(PUBLISHED) 버전은 읽기 전용이며 새 초안으로 변경해야 합니다. 이전 버전(DEPRECATED)은 조회만 가능합니다."
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
              emptyMessage="연결된 도구가 없습니다."
            />
          </div>
        )}

        {tab === 'schedules' && (
          <div className="max-w-3xl">
            {full.schedules.length === 0 ? (
              <div className="bg-white rounded-xl border border-slate-200">
                <EmptyState
                  icon={<Calendar size={22} />}
                  title="연결된 예약 실행이 없습니다"
                  description="실제 워크플로가 등록된 후 예약 실행에서 게시된 버전을 대상으로 선택할 수 있습니다."
                />
              </div>
            ) : (
              <div className="space-y-2">
                {full.schedules.map(s => (
                  <div key={s.id} className="bg-white border border-slate-200 rounded-lg p-4 flex justify-between items-center">
                    <div>
                      <p className="text-sm font-medium text-slate-800">{s.name}</p>
                      <p className="text-xs text-slate-400">{s.schedule} · {s.timezone} · 다음 실행(예시): {s.nextRun}</p>
                    </div>
                    <StatusBadge status={s.status} label={labelWorkflowStatus(s.status)} size="sm" />
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
    { key: 'version', label: '버전', render: r => <VersionBadge version={r.version} status={r.status} /> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} label={labelWorkflowVersionStatus(r.status)} size="sm" /> },
    { key: 'steps', label: '단계 수', render: r => <span className="text-sm text-slate-600">{r.steps}</span>, align: 'center' },
    { key: 'changeSummary', label: '변경 내용', render: r => <span className="text-sm text-slate-700">{r.changeSummary}</span> },
    { key: 'createdBy', label: '작성자', render: r => <span className="text-xs text-slate-500">{r.createdBy}</span> },
    { key: 'createdAt', label: '생성일', render: r => <span className="text-xs text-slate-400">{r.createdAt}</span> },
    { key: 'actions', label: '', render: r => {
      const status = r.status as VersionStatus;
      return (
        <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
          {(status === 'PUBLISHED' || status === 'DEPRECATED') && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="조회"
            >
              <Eye size={13} />
            </button>
          )}
          {status === 'DRAFT' && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="편집"
            >
              <Pencil size={13} />
            </button>
          )}
          {status === 'PUBLISHED' && (
            <button
              onClick={() => navigate(`/workflows/${workflowId}/versions/new/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="새 초안 예시 만들기"
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
  { key: 'step', label: '단계', render: r => <span className="text-xs font-medium text-slate-500">{r.step}</span> },
  { key: 'toolName', label: '도구', render: r => <span className="font-medium text-slate-800">{r.toolName}</span> },
  { key: 'serverName', label: 'MCP 서버', render: r => <span className="text-xs text-slate-500">{r.serverName}</span> },
  { key: 'riskClass', label: '위험도', render: r => <RiskBadge risk={r.riskClass} /> },
  { key: 'verification', label: '검증 상태', render: r => <VerificationBadge status={r.verification} /> },
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
