import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { EmptyState, InlineAlert } from '../../components/ui/EmptyState';
import { mockToolFactoryProjects } from '../../data/mock';

export default function FactoryBuildDetail() {
  const { buildId } = useParams();
  const navigate = useNavigate();
  const project = mockToolFactoryProjects.find(item => item.id === buildId);

  if (!project) {
    return (
      <div>
        <PageHeader title="예시 프로젝트를 찾을 수 없습니다" />
        <EmptyState
          title="요청한 예시 프로젝트가 없습니다"
          description="이 경로는 실제 Factory 작업 조회 API와 연결되지 않았습니다."
          action={{ label: '시연 목록으로', onClick: () => navigate('/tool-factory') }}
        />
      </div>
    );
  }

  const stages = [
    { label: '빌드 예시', status: project.buildStatus },
    { label: '테스트 예시', status: project.testStatus },
    { label: '검토 예시', status: project.reviewStatus },
    { label: '게시 예시', status: project.publishStatus },
  ];

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: '도구 생성 (시연 화면)', to: '/tool-factory' }, { label: project.project }]}
        title={`${project.project} (예시)`}
        description={`소스 유형: ${project.sourceType} · 고정된 샘플 데이터`}
      />
      <div className="p-6 max-w-3xl space-y-4">
        <InlineAlert
          type="warning"
          message="아래 상태는 실제 빌드·테스트·검토·게시 결과가 아닌 화면 예시입니다. 운영 작업 기록·보안 검증 증빙·생성된 MCP 도구를 조회하지 않습니다."
        />
        <section className="rounded-xl border border-slate-200 bg-white p-4">
          <h2 className="text-sm font-semibold text-slate-800 mb-4">예시 진행 상태</h2>
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            {stages.map(stage => (
              <div key={stage.label} className="rounded-lg border border-slate-100 p-3 space-y-2">
                <p className="text-xs text-slate-500">{stage.label}</p>
                {stage.status
                  ? <StatusBadge status={stage.status} size="sm" />
                  : <span className="text-xs text-slate-400">기록 없음 (예시)</span>}
              </div>
            ))}
          </div>
        </section>
        <p className="text-sm text-slate-600">
          실제 Factory 빌드 로그와 검증 결과는 연동이 완료되기 전까지 이 화면에 표시되지 않습니다.
          MCP 서버 연결은 서버 등록 화면에서 진행할 수 있습니다.
        </p>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => navigate('/tool-factory')}>시연 목록으로</Button>
          <Button onClick={() => navigate('/mcp/servers/new')}>실제 MCP 서버 등록</Button>
        </div>
      </div>
    </div>
  );
}
