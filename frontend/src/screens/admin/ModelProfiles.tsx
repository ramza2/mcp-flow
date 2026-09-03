import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import { useState } from 'react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { mockModelProfiles } from '../../data/mock';
import { TabBar } from '../../components/ui/Tabs';

export default function ModelProfiles() {
  const navigate = useNavigate();
  const [tab, setTab] = useState('llm');

  const llmProfiles = mockModelProfiles.filter(p => p.type === 'LLM');
  const embeddingProfiles = mockModelProfiles.filter(p => p.type === 'Embedding');

  const llmCols: Column<typeof mockModelProfiles[0]>[] = [
    { key: 'name', label: 'Name', render: r => <span className="font-medium text-slate-800">{r.name}</span> },
    { key: 'provider', label: 'Provider', render: r => <span className="text-sm text-slate-600">{r.provider}</span> },
    { key: 'model', label: 'Model', render: r => <span className="font-mono text-xs text-slate-500">{r.model}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'baseUrl', label: 'Base URL', render: r => <span className="font-mono text-xs text-slate-400">{r.baseUrl}</span> },
    { key: 'secret', label: 'Secret', render: r => <span className="text-xs text-slate-400">{r.secret} ••••••••</span> },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Model Profiles"
        description="LLM 및 Embedding Model Profile을 관리합니다."
        actions={<Button icon={<Plus size={14} />}>새 Profile</Button>}
        tabs={
          <TabBar
            tabs={[{ id: 'llm', label: 'LLM Profiles' }, { id: 'embedding', label: 'Embedding Profiles' }]}
            activeTab={tab}
            onChange={setTab}
          />
        }
      />
      <div className="p-6">
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          {tab === 'llm' && (
            <DataTable
              columns={llmCols}
              data={llmProfiles}
              rowKey={r => r.id}
              onRowClick={r => navigate(`/admin/model-profiles/${r.id}`)}
            />
          )}
          {tab === 'embedding' && (
            <DataTable
              columns={[
                ...llmCols.slice(0, 5),
                { key: 'dimension', label: 'Dimension', render: r => <span className="font-mono text-xs text-slate-500">{(r as Record<string, unknown>).dimension as number}</span> },
                { key: 'activeForToolSearch', label: 'Tool Search', render: r => (
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${(r as Record<string, unknown>).activeForToolSearch ? 'bg-green-50 text-green-700' : 'bg-slate-100 text-slate-500'}`}>
                    {(r as Record<string, unknown>).activeForToolSearch ? '활성' : '비활성'}
                  </span>
                )},
              ]}
              data={embeddingProfiles}
              rowKey={r => r.id}
              onRowClick={r => navigate(`/admin/model-profiles/${r.id}`)}
            />
          )}
        </div>
      </div>
    </div>
  );
}
