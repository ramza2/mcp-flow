import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';

/**
 * This is a non-operational walkthrough until Tool Factory API wiring is implemented.
 * Do not simulate uploads, build completion, security tests, or publish success.
 */
const FACTORY_STEPS = [
  { title: '소스 준비', description: 'OpenAPI 문서나 Python 코드를 준비합니다.' },
  { title: '소스 분석', description: '실제 Factory API를 연결한 이후 분석 결과를 표시할 단계입니다.' },
  { title: '도구 후보 검토', description: '분석된 후보와 위험 분류를 운영자가 확인할 단계입니다.' },
  { title: '빌드', description: '빌드 작업 실행 및 결과 조회가 연결되어야 합니다.' },
  { title: '보안·계약 테스트', description: '실제 검증 실행 기록이 있어야 통과 여부를 표시할 수 있습니다.' },
  { title: '검토', description: '테스트 증빙에 따른 운영자 검토가 필요합니다.' },
  { title: '게시·등록', description: '게시 후에도 별도의 MCP 서버 연결·도구 탐색·검증·활성화가 필요합니다.' },
] as const;

export default function ToolFactoryNew() {
  const navigate = useNavigate();

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: '도구 생성 (시연 화면)', to: '/tool-factory' }]}
        title="도구 생성 절차 안내"
        description="실제 생성·빌드·게시를 수행하지 않는 읽기 전용 화면입니다."
      />
      <div className="p-6 max-w-3xl space-y-4">
        <InlineAlert
          type="warning"
          message="현재 이 웹 화면은 실제 Tool Factory API와 연결되지 않았습니다. 소스 업로드·분석·빌드·보안 검사·게시는 실행되지 않으며, 성공 결과도 생성하지 않습니다."
        />
        <section className="rounded-xl border border-slate-200 bg-white p-5">
          <h2 className="text-sm font-semibold text-slate-800 mb-3">도구 생성 과정 (절차 예시)</h2>
          <ol className="space-y-3">
            {FACTORY_STEPS.map((step, index) => (
              <li key={step.title} className="flex items-start gap-3 text-sm">
                <span className="shrink-0 flex items-center justify-center w-6 h-6 rounded-full bg-slate-100 text-slate-700 text-xs font-semibold">
                  {index + 1}
                </span>
                <div>
                  <p className="font-medium text-slate-800">{step.title}</p>
                  <p className="text-slate-500 mt-0.5">{step.description}</p>
                </div>
              </li>
            ))}
          </ol>
        </section>
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" onClick={() => navigate('/tool-factory')}>
            시연 목록으로
          </Button>
          <Button onClick={() => navigate('/mcp/servers/new')}>
            실제 MCP 서버 등록
          </Button>
        </div>
      </div>
    </div>
  );
}
