import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Play } from 'lucide-react';
import StatusBadge, { RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import { mockTools } from '../../data/mock';
import { InlineAlert } from '../../components/ui/EmptyState';

export default function MCPToolDetail() {
  const { toolId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [testRunning, setTestRunning] = useState(false);
  const [testResult, setTestResult] = useState<string | null>(null);

  const tool = mockTools.find(t => t.id === toolId) ?? mockTools[0];
  const isHighRisk = tool.riskClass === 'DESTRUCTIVE' || tool.riskClass === 'NON_IDEMPOTENT_WRITE';

  const runTest = async () => {
    setTestRunning(true);
    await delay(1800);
    setTestRunning(false);
    setTestResult(JSON.stringify({ temperature: 23.5, weather: "맑음", humidity: 60, city: "Seoul" }, null, 2));
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/mcp/tools')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> MCP Tools
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{tool.displayName}</h1>
            <p className="text-xs font-mono text-slate-400 mt-0.5">{tool.sourceName}</p>
            <div className="flex items-center gap-3 mt-2">
              <StatusBadge status={tool.status} />
              <RiskBadge risk={tool.riskClass} />
              <VerificationBadge status={tool.verification} />
              <span className="text-xs text-slate-400">{tool.serverName}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'schema', label: 'Input Schema' },
            { id: 'output', label: 'Output Schema' },
            { id: 'policy', label: 'Policy' },
            { id: 'verification', label: 'Verification' },
            { id: 'test', label: 'Test Call' },
            { id: 'usedby', label: 'Used By' },
            { id: 'versions', label: 'Versions' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6 max-w-3xl">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4">
            <InfoCard title="Tool 정보">
              <Row label="Source Name" mono>{tool.sourceName}</Row>
              <Row label="Server">{tool.serverName}</Row>
              <Row label="Version" mono>{tool.currentVersion}</Row>
              <Row label="Validation">{tool.validation}</Row>
              <Row label="Capability" mono>{tool.capability}</Row>
              <Row label="Used By">{tool.usedBy}개 Agent/Workflow</Row>
            </InfoCard>
            <InfoCard title="Risk 설명">
              <RiskBadge risk={tool.riskClass} />
              <p className="text-xs text-slate-600 mt-2">
                {tool.riskClass === 'READ_ONLY' && '외부 데이터를 변경하지 않습니다. 안전하게 반복 실행할 수 있습니다.'}
                {tool.riskClass === 'IDEMPOTENT_WRITE' && '동일 요청의 중복 효과가 제어됩니다.'}
                {tool.riskClass === 'NON_IDEMPOTENT_WRITE' && '중복 실행 시 추가 작업이 발생할 수 있습니다.'}
                {tool.riskClass === 'DESTRUCTIVE' && '삭제 또는 복구하기 어려운 작업이 포함됩니다.'}
                {tool.riskClass === 'UNKNOWN' && '이 Tool의 부작용 특성이 충분히 검증되지 않았습니다.'}
              </p>
            </InfoCard>
          </div>
        )}

        {tab === 'schema' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Input Schema</h3>
            <pre className="text-xs font-mono bg-slate-50 rounded-lg p-4 text-slate-700 overflow-auto">
{`{
  "type": "object",
  "properties": {
    "city": {
      "type": "string",
      "description": "도시 이름",
      "required": true
    },
    "units": {
      "type": "string",
      "enum": ["celsius", "fahrenheit"],
      "default": "celsius"
    }
  }
}`}
            </pre>
          </div>
        )}

        {tab === 'output' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">Output Schema</h3>
            <pre className="text-xs font-mono bg-slate-50 rounded-lg p-4 text-slate-700 overflow-auto">
{`{
  "type": "object",
  "properties": {
    "temperature": { "type": "number" },
    "weather": { "type": "string" },
    "humidity": { "type": "number" },
    "city": { "type": "string" }
  }
}`}
            </pre>
          </div>
        )}

        {tab === 'policy' && (
          <InfoCard title="Tool Policy">
            <Row label="Risk Class"><RiskBadge risk={tool.riskClass} /></Row>
            <Row label="User Confirmation">{tool.riskClass !== 'READ_ONLY' ? '필요' : '불필요'}</Row>
            <Row label="Approval Policy">{tool.riskClass === 'NON_IDEMPOTENT_WRITE' ? 'Standard Email Approval' : '–'}</Row>
            <Row label="Timeout">30초</Row>
            <Row label="Max Attempts">3회</Row>
            <Row label="Result Size Limit">1MB</Row>
            <Row label="Auto Select">허용됨</Row>
          </InfoCard>
        )}

        {tab === 'verification' && (
          <div className="space-y-4">
            <InlineAlert
              type="info"
              message="Verification은 ToolVersion 단위입니다. Logical Tool이 아니라 특정 ToolVersion에 귀속됩니다."
            />
            <InfoCard title={`Verification — ToolVersion ${tool.currentVersion}`}>
              <Row label="Verification Status"><VerificationBadge status={tool.verification} /></Row>
              <Row label="ToolVersion" mono>{tool.currentVersion}</Row>
              <Row label="Verified At">2026-08-15 14:00</Row>
              <Row label="Verified By">admin</Row>
              <Row label="Test Execution" mono>EXE-VERIFY-20260815-001</Row>
              <Row label="Criteria Version" mono>v1.2</Row>
              <Row label="Evidence">schema_match · sample_call_ok · risk_review</Row>
              <Row label="Expires At">2026-11-15 14:00</Row>
            </InfoCard>
            {tool.verification === 'EXPIRED' && (
              <InlineAlert type="warning" message="이 ToolVersion의 Verification이 만료되었습니다. 재검증을 실행하거나 관리자에게 문의하세요." />
            )}
          </div>
        )}

        {tab === 'test' && (
          <div className="space-y-4">
            {isHighRisk && (
              <InlineAlert type="warning" message={`이 Tool은 ${tool.riskClass} 등급입니다. Test Call 실행 시 실제 Side Effect가 발생할 수 있습니다.`} />
            )}
            <InfoCard title="Test Call">
              <div className="space-y-3">
                <div>
                  <label className="text-xs font-medium text-slate-600 block mb-1">city</label>
                  <input defaultValue="Seoul" className="w-full h-8 px-2.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500" />
                </div>
                <div>
                  <label className="text-xs font-medium text-slate-600 block mb-1">units</label>
                  <select className="w-full h-8 px-2 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 bg-white">
                    <option>celsius</option>
                    <option>fahrenheit</option>
                  </select>
                </div>
                <Button icon={<Play size={13} />} loading={testRunning} onClick={runTest} size="sm">Test 실행</Button>
              </div>
              {testResult && (
                <div className="mt-4">
                  <p className="text-xs font-semibold text-slate-500 mb-2">Result</p>
                  <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700 overflow-auto">{testResult}</pre>
                </div>
              )}
            </InfoCard>
          </div>
        )}

        {tab === 'usedby' && (
          <InfoCard title="사용 중인 Agent / Workflow">
            {['General Work Assistant (v2)', 'Research Assistant (v1)'].slice(0, tool.usedBy).map((a, i) => (
              <div key={i} className="py-1.5 text-sm text-slate-700">{a}</div>
            ))}
            {tool.usedBy === 0 && <p className="text-sm text-slate-400">사용 중인 Agent/Workflow가 없습니다.</p>}
          </InfoCard>
        )}

        {tab === 'versions' && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            {[
              { version: tool.currentVersion, status: 'CURRENT', validation: tool.validation, verification: tool.verification, at: tool.updatedAt },
              { version: 'v0.9.0', status: 'DEPRECATED', validation: 'VALID', verification: 'EXPIRED', at: '2026-07-01' },
            ].map((v, i) => (
              <div key={i} className="flex items-center justify-between px-4 py-3 border-b last:border-0">
                <div>
                  <p className="text-sm font-mono font-medium text-slate-800">{v.version}</p>
                  <p className="text-xs text-slate-400">{v.at}</p>
                </div>
                <div className="flex items-center gap-2">
                  <StatusBadge status={v.status === 'CURRENT' ? 'ACTIVE' : 'DEPRECATED'} size="sm" />
                  <span className="text-xs text-slate-500">{v.validation}</span>
                  <VerificationBadge status={v.verification} />
                </div>
              </div>
            ))}
          </div>
        )}

        {tab === 'audit' && (
          <div className="bg-white border border-slate-200 rounded-xl overflow-hidden">
            {[
              { time: '2026-09-01 10:00', actor: 'admin', action: 'tool.policy.update', result: 'SUCCESS' },
              { time: '2026-08-15 14:00', actor: 'admin', action: 'tool.version.verify', result: 'SUCCESS' },
              { time: '2026-08-10 09:30', actor: 'system', action: 'tool.discover', result: 'SUCCESS' },
            ].map((log, i) => (
              <div key={i} className="flex gap-4 px-4 py-3 border-b last:border-0 text-sm">
                <span className="font-mono text-xs text-slate-400 w-36 shrink-0">{log.time}</span>
                <span className="text-slate-600 w-16 shrink-0">{log.actor}</span>
                <span className="font-mono text-xs text-indigo-600">{log.action}</span>
                <span className={`ml-auto text-xs ${log.result === 'SUCCESS' ? 'text-green-600' : 'text-red-600'}`}>{log.result}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

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
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-36 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
