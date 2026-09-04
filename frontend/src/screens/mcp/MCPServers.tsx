import { useNavigate } from 'react-router';
import { Plus, RefreshCw, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { mockMCPServers } from '../../data/mock';
import { labelDiscoveryMode, MCP_SERVER_STATUSES } from '../../domain';

export default function MCPServers() {
  const navigate = useNavigate();

  const columns: Column<typeof mockMCPServers[0]>[] = [
    { key: 'name', label: 'Name', render: r => <span className="font-medium text-slate-800">{r.name}</span> },
    { key: 'transport', label: 'Transport', render: r => <span className="text-xs font-mono bg-slate-100 px-2 py-0.5 rounded text-slate-600">{r.transport}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'protocol', label: 'Protocol', render: r => <span className="text-xs text-slate-600">{r.protocol}</span> },
    { key: 'version', label: 'Version', render: r => <span className="font-mono text-xs text-slate-500">{r.version}</span> },
    { key: 'discoveryMode', label: 'Discovery Mode', render: r => (
      <span className="text-xs text-slate-500">{labelDiscoveryMode(r.discoveryMode)}</span>
    )},
    { key: 'toolCount', label: 'Tools', render: r => <span className="text-sm text-slate-600">{r.toolCount}</span>, align: 'center' },
    { key: 'lastHealth', label: 'Last Health', render: r => <span className="text-xs text-slate-400">{r.lastHealth}</span> },
    { key: 'actions', label: '', render: r => (
      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <button className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
        <button className="p-1.5 rounded text-slate-400 hover:text-cyan-600 hover:bg-cyan-50" title="연결 테스트"><RefreshCw size={13} /></button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader
        title="MCP Servers"
        description="등록된 MCP Server와 Tool Discovery 상태를 관리합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/mcp/servers/new')}>Register MCP Server</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="서버 이름 검색..."
          filters={[
            { key: 'status', label: '상태', options: MCP_SERVER_STATUSES.map(v => ({ value: v, label: v })) },
            { key: 'transport', label: 'Transport', options: [
              { value: 'Streamable HTTP', label: 'Streamable HTTP' },
              { value: 'STDIO', label: 'STDIO' },
              { value: 'Legacy HTTP/SSE', label: 'Legacy HTTP/SSE' },
            ]},
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockMCPServers}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/mcp/servers/${r.id}`)}
          />
        </div>
      </div>
    </div>
  );
}
