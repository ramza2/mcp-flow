import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Check, AlertTriangle, X } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';
import { mockToolFactoryProjects } from '../../data/mock';

export default function FactoryBuildDetail() {
  const { buildId } = useParams();
  const navigate = useNavigate();
  const project = mockToolFactoryProjects.find(p => p.id === buildId) ?? mockToolFactoryProjects[1];

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Tool Factory', to: '/tool-factory' }, { label: project.project }]}
        title={project.project}
        description={`Source Type: ${project.sourceType}`}
      />
      <div className="p-6 max-w-3xl space-y-4">
        {/* Pipeline */}
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">Build Pipeline</h3>
          <div className="flex items-center gap-3">
            {[
              { label: 'Build', status: project.buildStatus },
              { label: 'Test', status: project.testStatus },
              { label: 'Review', status: project.reviewStatus },
              { label: 'Publish', status: project.publishStatus },
            ].map((stage, i) => (
              <div key={stage.label} className="flex items-center gap-2">
                <div className="text-center">
                  <p className="text-xs text-slate-400 mb-1">{stage.label}</p>
                  {stage.status ? <StatusBadge status={stage.status} size="sm" /> : <span className="text-xs text-slate-300">–</span>}
                </div>
                {i < 3 && <span className="text-slate-200">→</span>}
              </div>
            ))}
          </div>
        </div>

        {/* Generated Tools */}
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">생성된 Tool</h3>
          <div className="space-y-2">
            {[
              { name: 'get_finance_report', status: 'SUCCEEDED' },
              { name: 'export_report_pdf', status: 'SUCCEEDED' },
            ].map(t => (
              <div key={t.name} className="flex items-center gap-3 p-2.5 rounded-lg bg-slate-50">
                <Check size={13} className="text-green-500" />
                <span className="font-mono text-sm text-slate-700">{t.name}</span>
              </div>
            ))}
          </div>
          <p className="text-xs text-amber-600 mt-3">생성 성공이 Tool 활성화 성공을 의미하지 않습니다. MCP Server에 등록 후 활성화하세요.</p>
        </div>

        {/* Build Logs */}
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">Build Logs</h3>
          <div className="font-mono text-xs bg-slate-900 text-slate-300 rounded-lg p-4 space-y-1 overflow-auto max-h-40">
            <div className="text-slate-400">[2026-09-02 14:00:01] Build started: Finance Report Tools</div>
            <div>[2026-09-02 14:00:02] Parsing OpenAPI spec...</div>
            <div className="text-green-400">[2026-09-02 14:00:03] Extracted 2 endpoints</div>
            <div>[2026-09-02 14:00:04] Generating MCP Tool definitions...</div>
            <div className="text-green-400">[2026-09-02 14:00:05] Build SUCCEEDED: 2 tools generated</div>
          </div>
        </div>

        {/* Security Test */}
        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">Security Test</h3>
          <div className="space-y-2">
            <TestRow type="ok" label="입력 검증 테스트" />
            <TestRow type="ok" label="출력 크기 제한 테스트" />
            <TestRow type="ok" label="인젝션 방어 테스트" />
          </div>
        </div>
      </div>
    </div>
  );
}

function TestRow({ type, label }: { type: 'ok' | 'warn' | 'fail'; label: string }) {
  return (
    <div className="flex items-center gap-2 text-sm">
      {type === 'ok' && <Check size={13} className="text-green-500" />}
      {type === 'warn' && <AlertTriangle size={13} className="text-amber-500" />}
      {type === 'fail' && <X size={13} className="text-red-500" />}
      <span className="text-slate-700">{label}</span>
    </div>
  );
}
