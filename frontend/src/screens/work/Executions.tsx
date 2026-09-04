import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { mockExecutions } from '../../data/mock';
import { EXECUTION_SOURCE_TYPES, labelExecutionSource } from '../../domain';

export default function Executions() {
  const navigate = useNavigate();
  const [q, setQ] = useState('');

  const filtered = mockExecutions.filter(e =>
    !q || e.name.includes(q) || e.id.includes(q)
  );

  const columns: Column<typeof mockExecutions[0]>[] = [
    { key: 'id', label: 'Execution ID', render: r => <span className="font-mono text-xs text-slate-500">{r.id}</span> },
    { key: 'name', label: '요청 / 이름', render: r => <span className="font-medium text-slate-800">{r.name}</span> },
    { key: 'sourceType', label: 'Source', render: r => (
      <span className="text-xs bg-slate-100 px-2 py-0.5 rounded text-slate-600">
        {labelExecutionSource(r.sourceType)}
      </span>
    )},
    { key: 'user', label: '사용자', render: r => <span className="text-slate-600 font-mono text-xs">{r.user}</span> },
    { key: 'agent', label: 'Agent / Workflow', render: r => <span className="text-sm text-slate-600">{r.agent ?? r.workflow ?? '–'}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'steps', label: 'Steps', render: r => <span className="text-sm text-slate-500">{r.stepCount} / {r.totalSteps}</span>, align: 'center' },
    { key: 'duration', label: 'Duration', render: r => <span className="font-mono text-xs text-slate-500">{r.duration}</span> },
    { key: 'startedAt', label: '시작', render: r => <span className="text-xs text-slate-500">{r.startedAt}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Executions"
        description="모든 실행 기록을 조회하고 상태를 모니터링합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/run')}>새 실행</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="Execution ID, 이름 검색..."
          onSearch={setQ}
          filters={[
            { key: 'status', label: '상태', options: [
              { value: 'RUNNING', label: 'Running' },
              { value: 'SUCCEEDED', label: 'Succeeded' },
              { value: 'FAILED', label: 'Failed' },
              { value: 'WAITING_APPROVAL', label: 'Waiting Approval' },
            ]},
            { key: 'sourceType', label: 'Source', options: EXECUTION_SOURCE_TYPES.map(v => ({
              value: v,
              label: labelExecutionSource(v),
            })) },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={filtered}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/executions/${r.id}`)}
            emptyMessage="조건에 맞는 Execution이 없습니다."
          />
        </div>
      </div>
    </div>
  );
}
