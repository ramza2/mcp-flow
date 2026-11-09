import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Plus, GitBranch, Eye } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockWorkflows } from '../../data/mock';
import { WORKFLOW_STATUSES, labelWorkflowStatus } from '../../domain';

export default function Workflows() {
  const navigate = useNavigate();
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const filteredWorkflows = mockWorkflows.filter(workflow =>
    workflow.name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()) &&
    (!statusFilter || workflow.status === statusFilter),
  );

  /** Mock UX: create Logical Workflow + initial DRAFT Version (Designer falls back if id unknown). */
  const handleNewWorkflow = () => {
    const id = `wf-${Date.now().toString(36)}`;
    navigate(`/workflows/${id}/versions/v1/edit`);
  };

  const columns: Column<typeof mockWorkflows[0]>[] = [
    { key: 'name', label: '이름', render: r => (
      <div className="flex items-center gap-2">
        <span className="w-7 h-7 rounded-md bg-cyan-50 text-cyan-600 flex items-center justify-center shrink-0"><GitBranch size={14} /></span>
        <span className="font-medium text-slate-800">{r.name}</span>
      </div>
    )},
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} label={labelWorkflowStatus(r.status)} size="sm" /> },
    { key: 'publishedVersion', label: '게시 버전', render: r => r.publishedVersion
      ? <VersionBadge version={r.publishedVersion} status="PUBLISHED" />
      : <span className="text-xs text-slate-400">–</span> },
    { key: 'steps', label: '단계 수', render: r => <span className="text-sm text-slate-600">{r.steps}</span>, align: 'center' },
    { key: 'owner', label: '소유자', render: r => <span className="text-xs text-slate-500">{r.owner}</span> },
    { key: 'lastPublished', label: '최근 게시', render: r => <span className="text-xs text-slate-400">{r.lastPublished ?? '–'}</span> },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
    { key: 'actions', label: '', render: r => (
      <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
        <button onClick={() => navigate(`/workflows/${r.id}`)} className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50" title="상세"><Eye size={13} /></button>
      </div>
    )},
  ];

  return (
    <div>
      <PageHeader
        title="워크플로 관리"
        description="여러 도구와 실행 단계를 연결하는 흐름을 살펴봅니다."
        actions={<Button icon={<Plus size={14} />} onClick={handleNewWorkflow}>새 워크플로 예시</Button>}
      />
      <div className="p-6 space-y-4">
        <InlineAlert type="info" message="현재 워크플로 목록과 디자이너는 예시 데이터 기반 화면입니다. 새로 만든 워크플로와 변경 사항은 서버에 저장되지 않으며 실제 실행·게시되지 않습니다." />
        <FilterBar
          search searchPlaceholder="워크플로 이름 검색..." onSearch={setQuery} onFilter={(key, value) => { if (key === 'status') setStatusFilter(value); }}
          filters={[
            { key: 'status', label: '상태', options: WORKFLOW_STATUSES.map(v => ({
              value: v,
              label: labelWorkflowStatus(v),
            })) },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={filteredWorkflows}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/workflows/${r.id}`)}
            emptyMessage="조건에 맞는 워크플로 예시가 없습니다."
          />
        </div>
      </div>
    </div>
  );
}
