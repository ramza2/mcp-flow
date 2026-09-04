import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Eye, Pencil, Plus, Rocket } from 'lucide-react';
import StatusBadge, { VersionBadge, RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockAgents, mockAgentFull, mockTools } from '../../data/mock';

type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';

export default function AgentDetail() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');

  const existingAgent = mockAgents.find(a => a.id === agentId);
  const isNewLogicalAgent = !existingAgent && !!agentId?.startsWith('agt-');
  const unknownAgentId = !existingAgent && !isNewLogicalAgent;

  const agent = existingAgent ?? (isNewLogicalAgent
    ? {
      id: agentId!,
      name: 'New Agent',
      status: 'DRAFT' as const,
      publishedVersion: null as string | null,
      allowedTools: 0,
      modelProfile: 'Claude 3.5 Sonnet',
      owner: 'admin',
      updatedAt: new Date().toISOString().slice(0, 10),
      versions: [{ version: 'v1', status: 'DRAFT' as const, createdAt: new Date().toISOString().slice(0, 10), author: 'admin' }],
    }
    : null);

  const full = isNewLogicalAgent
    ? {
      description: '신규 Logical Agent (아직 저장되지 않은 Mock Draft)',
      purpose: '',
      visibility: 'INTERNAL',
      createdAt: new Date().toISOString().slice(0, 10),
      currentVersion: null as string | null,
      allowedToolIds: [] as string[],
      instructions: '',
      versions: [{
        version: 'v1',
        status: 'DRAFT' as const,
        changeSummary: 'Initial draft',
        validation: null as 'VALID' | 'INVALID' | 'WARNING' | null,
        createdBy: 'admin',
        createdAt: new Date().toISOString().slice(0, 10),
        publishedAt: null as string | null,
      }],
    }
    : (agent ? (mockAgentFull[agent.id] ?? null) : null);

  if (unknownAgentId || !agent || !full) {
    return (
      <div className="p-6">
        <InlineAlert type="warning" message={`Agent를 찾을 수 없습니다: ${agentId ?? '(missing id)'}`} />
        <div className="mt-4">
          <Button variant="outline" onClick={() => navigate('/agents')}>Agents로 돌아가기</Button>
        </div>
      </div>
    );
  }

  const allowedTools = mockTools.filter(t => full.allowedToolIds.includes(t.id));
  const latest = full.versions[0];
  const latestStatus = (latest?.status ?? 'DRAFT') as VersionStatus;

  const handleLatestPrimary = () => {
    if (!latest) {
      navigate(`/agents/${agent.id}/versions/new/edit`);
      return;
    }
    if (latestStatus === 'DRAFT') {
      navigate(`/agents/${agent.id}/versions/${latest.version}/edit`);
      return;
    }
    // PUBLISHED (or unexpected) → Create New Draft Version
    navigate(`/agents/${agent.id}/versions/new/edit`);
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/agents')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Agents
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{agent.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={agent.status} />
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
              {latestStatus === 'DRAFT' ? '최신 버전 편집' : 'Create New Draft'}
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
            { id: 'tools', label: 'Allowed Tools', badge: allowedTools.length },
            { id: 'instructions', label: 'Instructions' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-3xl">
            <InfoCard title="기본 정보">
              <Row label="Purpose">{full.purpose}</Row>
              <Row label="Visibility">{full.visibility}</Row>
              <Row label="Owner">{agent.owner}</Row>
              <Row label="Model Profile">{agent.modelProfile}</Row>
            </InfoCard>
            <InfoCard title="버전 상태">
              <Row label="Published" mono>{agent.publishedVersion ?? '—'}</Row>
              <Row label="Current" mono>{full.currentVersion ?? '—'}</Row>
              <Row label="Created">{full.createdAt}</Row>
              <Row label="Updated">{agent.updatedAt}</Row>
            </InfoCard>
          </div>
        )}

        {tab === 'versions' && (
          <div className="space-y-3 max-w-4xl">
            <InlineAlert
              type="info"
              message="편집은 DRAFT Version에만 적용됩니다. PUBLISHED는 View 또는 Create New Draft Version만 가능하며, DEPRECATED는 View만 가능합니다."
            />
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <DataTable
                columns={versionCols(navigate, agent.id)}
                data={full.versions}
                rowKey={r => r.version}
                onRowClick={r => navigate(`/agents/${agent.id}/versions/${r.version}/edit`)}
              />
            </div>
          </div>
        )}

        {tab === 'tools' && (
          <div className="max-w-4xl bg-white rounded-xl border border-slate-200 overflow-hidden">
            <DataTable
              columns={toolCols}
              data={allowedTools}
              rowKey={r => r.id}
              emptyMessage="허용된 Tool이 없습니다."
            />
          </div>
        )}

        {tab === 'instructions' && (
          <div className="max-w-3xl space-y-3">
            <InlineAlert type="info" message="Instructions는 Agent가 Tool을 선택하고 실행 계획을 세울 때 참조하는 시스템 프롬프트입니다." />
            <pre className="bg-white border border-slate-200 rounded-xl p-4 text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {full.instructions}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}

function versionCols(navigate: (to: string) => void, agentId: string): Column<typeof mockAgentFull['agt-001']['versions'][0]>[] {
  return [
    { key: 'version', label: 'Version', render: r => <VersionBadge version={r.version} status={r.status} /> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'changeSummary', label: 'Change Summary', render: r => <span className="text-sm text-slate-700">{r.changeSummary}</span> },
    { key: 'validation', label: 'Validation', render: r => r.validation ? <StatusBadge status={r.validation} size="sm" /> : <span className="text-xs text-slate-400">–</span> },
    { key: 'createdBy', label: 'Author', render: r => <span className="text-xs text-slate-500">{r.createdBy}</span> },
    { key: 'createdAt', label: 'Created', render: r => <span className="text-xs text-slate-400">{r.createdAt}</span> },
    { key: 'actions', label: '', render: r => {
      const status = r.status as VersionStatus;
      return (
        <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
          {(status === 'PUBLISHED' || status === 'DEPRECATED') && (
            <button
              onClick={() => navigate(`/agents/${agentId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="View"
            >
              <Eye size={13} />
            </button>
          )}
          {status === 'DRAFT' && (
            <button
              onClick={() => navigate(`/agents/${agentId}/versions/${r.version}/edit`)}
              className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
              title="Edit"
            >
              <Pencil size={13} />
            </button>
          )}
          {status === 'PUBLISHED' && (
            <button
              onClick={() => navigate(`/agents/${agentId}/versions/new/edit`)}
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

const toolCols: Column<typeof mockTools[0]>[] = [
  { key: 'displayName', label: 'Display Name', render: r => <span className="font-medium text-slate-800">{r.displayName}</span> },
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
