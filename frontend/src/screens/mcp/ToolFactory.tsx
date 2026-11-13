import { useNavigate } from 'react-router';
import { Info } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockToolFactoryProjects } from '../../data/mock';

function PipelineCell({ status }: { status: string | null }) {
  if (!status) return <span className="text-slate-300">–</span>;
  return <StatusBadge status={status} size="sm" />;
}

export default function ToolFactory() {
  const navigate = useNavigate();

  const columns: Column<typeof mockToolFactoryProjects[0]>[] = [
    { key: 'project', label: '예시 프로젝트', render: r => <span className="font-medium text-slate-800">{r.project}</span> },
    { key: 'sourceType', label: '소스 유형', render: r => <span className="text-xs bg-slate-100 px-2 py-0.5 rounded text-slate-600">{r.sourceType}</span> },
    { key: 'buildStatus', label: '빌드 예시', render: r => <PipelineCell status={r.buildStatus} /> },
    { key: 'testStatus', label: '테스트 예시', render: r => <PipelineCell status={r.testStatus} /> },
    { key: 'reviewStatus', label: '검토 예시', render: r => <PipelineCell status={r.reviewStatus} /> },
    { key: 'publishStatus', label: '게시 예시', render: r => <PipelineCell status={r.publishStatus} /> },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="도구 생성 (시연 화면)"
        description="실제 Tool Factory 작업 목록이 아닌 화면 구성 예시입니다."
        actions={<Button variant="outline" icon={<Info size={14} />} onClick={() => navigate('/tool-factory/new')}>시연 단계 안내</Button>}
      />
      <div className="p-6 space-y-4">
        <InlineAlert
          type="warning"
          message="이 목록은 고정된 예시 데이터입니다. 실제 도구 생성·빌드·테스트·검토·게시 이력을 조회하지 않으며, 표시된 성공 상태도 운영 결과가 아닙니다."
        />
        <p className="text-sm text-slate-600">
          실제 MCP 서버 연동은 MCP 서버 등록 화면에서 시작하세요.
          도구 생성 화면의 실제 Factory API 연동은 별도 구현이 필요합니다.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => navigate('/mcp/servers/new')}>실제 MCP 서버 등록</Button>
        </div>
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
