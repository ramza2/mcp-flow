import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { mockToolFactoryProjects } from '../../data/mock';

function PipelineCell({ status }: { status: string | null }) {
  if (!status) return <span className="text-slate-300">–</span>;
  return <StatusBadge status={status} size="sm" />;
}

export default function ToolFactory() {
  const navigate = useNavigate();

  const columns: Column<typeof mockToolFactoryProjects[0]>[] = [
    { key: 'project', label: 'Project', render: r => <span className="font-medium text-slate-800">{r.project}</span> },
    { key: 'sourceType', label: 'Source Type', render: r => <span className="text-xs bg-slate-100 px-2 py-0.5 rounded text-slate-600">{r.sourceType}</span> },
    { key: 'buildStatus', label: 'Build', render: r => <PipelineCell status={r.buildStatus} /> },
    { key: 'testStatus', label: 'Test', render: r => <PipelineCell status={r.testStatus} /> },
    { key: 'reviewStatus', label: 'Review', render: r => <PipelineCell status={r.reviewStatus} /> },
    { key: 'publishStatus', label: 'Publish', render: r => <PipelineCell status={r.publishStatus} /> },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Tool Factory"
        description="OpenAPI 또는 Python 코드에서 MCP Tool을 자동 생성합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/tool-factory/new')}>새 Tool 생성</Button>}
      />
      <div className="p-6">
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockToolFactoryProjects}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/tool-factory/${r.id}`)}
          />
        </div>
      </div>
    </div>
  );
}
