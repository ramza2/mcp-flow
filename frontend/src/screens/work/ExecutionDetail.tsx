import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, X, CheckCircle2, Loader2, Circle, AlertTriangle, Clock, ChevronRight } from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import { mockExecutions } from '../../data/mock';

const STEP_DATA = [
  { id: 's1', name: '주간 데이터 조회', type: 'Tool', status: 'SUCCEEDED', tool: 'Search Documents', version: 'v2.1.0', attempts: 1, started: '14:30:01', ended: '14:30:08', duration: '7s', inputs: { query: '주간 보고 데이터 2026-09-02', limit: 10 }, outputs: { count: 8, documents: ['doc-001', 'doc-002'] }, error: null },
  { id: 's2', name: '보고서 파일 생성', type: 'Tool', status: 'SUCCEEDED', tool: 'Generate Report', version: 'v1.0.3', attempts: 1, started: '14:30:09', ended: '14:30:35', duration: '26s', inputs: { template: 'weekly_summary', data: '...', format: 'PDF' }, outputs: { fileId: 'file-abc123', filename: 'report_2026-09-02.pdf' }, error: null },
  { id: 's3', name: '이메일 발송 승인 대기', type: 'Approval', status: 'WAITING_APPROVAL', tool: null, version: null, attempts: 0, started: '14:30:36', ended: null, duration: '–', inputs: null, outputs: null, error: null },
  { id: 's4', name: '이메일 발송', type: 'Tool', status: 'PENDING', tool: 'Send Email', version: 'v3.0.1', attempts: 0, started: null, ended: null, duration: '–', inputs: null, outputs: null, error: null },
];

export default function ExecutionDetail() {
  const { executionId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [selectedStep, setSelectedStep] = useState<typeof STEP_DATA[0] | null>(null);

  const execution = mockExecutions.find(e => e.id === executionId) ?? mockExecutions[0];

  return (
    <div>
      {/* Header */}
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/executions')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Executions
        </button>
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h1 className="text-lg font-semibold text-slate-900 font-mono">{execution.id}</h1>
              <StatusBadge status={execution.status} />
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
              <span>Source: <span className="text-slate-700">{execution.source}</span></span>
              <span>Agent: <span className="text-slate-700">{execution.agent ?? execution.workflow ?? '–'}</span></span>
              <span>시작자: <span className="text-slate-700">{execution.user}</span></span>
              <span>시작: <span className="text-slate-700">{execution.startedAt}</span></span>
              <span>소요시간: <span className="text-slate-700 font-mono">{execution.duration}</span></span>
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            {(execution.status === 'RUNNING' || execution.status === 'WAITING_APPROVAL') && (
              <Button variant="danger" size="sm" icon={<X size={13} />}>실행 취소</Button>
            )}
          </div>
        </div>
      </div>

      {/* Tabs */}
      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'steps', label: 'Steps' },
            { id: 'events', label: 'Events' },
            { id: 'io', label: 'Inputs / Outputs' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && <OverviewTab execution={execution} />}
        {tab === 'steps' && <StepsTab steps={STEP_DATA} selectedStep={selectedStep} onSelectStep={setSelectedStep} />}
        {tab === 'events' && <EventsTab />}
        {tab === 'io' && <IOTab />}
        {tab === 'audit' && <AuditTab executionId={execution.id} />}
      </div>
    </div>
  );
}

function OverviewTab({ execution }: { execution: typeof mockExecutions[0] }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">원본 요청</h3>
        <p className="text-sm text-slate-700 bg-slate-50 rounded-lg p-3">{execution.name}</p>
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Plan 요약</h3>
        <div className="space-y-1.5">
          {['주간 데이터 조회', '보고서 파일 생성', '이메일 발송'].map((s, i) => (
            <div key={i} className="flex items-center gap-2 text-sm">
              <span className="text-xs text-slate-400 w-4">{i + 1}.</span>
              <span className="text-slate-700">{s}</span>
            </div>
          ))}
        </div>
        <div className="mt-3 flex gap-1.5 flex-wrap">
          <RiskBadge risk="NON_IDEMPOTENT_WRITE" />
          <span className="text-xs bg-amber-50 text-amber-700 px-2 py-0.5 rounded-full">외부 전송 포함</span>
        </div>
      </div>
      {execution.status === 'WAITING_APPROVAL' && (
        <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-start gap-3">
          <AlertTriangle size={16} className="text-amber-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">승인 대기 중</p>
            <p className="text-sm text-amber-700 mt-0.5">이메일 발송 작업이 팀 관리자의 승인을 기다리고 있습니다.</p>
            <button onClick={() => {}} className="mt-2 text-xs text-indigo-600 hover:underline flex items-center gap-1">
              승인 요청 보기 <ChevronRight size={11} />
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

function StepsTab({ steps, selectedStep, onSelectStep }: { steps: typeof STEP_DATA; selectedStep: typeof STEP_DATA[0] | null; onSelectStep: (s: typeof STEP_DATA[0] | null) => void }) {
  return (
    <div className="flex gap-4 max-w-5xl">
      {/* Graph */}
      <div className="flex-1 bg-white rounded-xl border border-slate-200 p-6">
        <h3 className="text-sm font-semibold text-slate-700 mb-6">Step Graph</h3>
        <div className="flex flex-col items-center gap-0">
          {steps.map((step, i) => (
            <div key={step.id} className="flex flex-col items-center">
              <button
                onClick={() => onSelectStep(selectedStep?.id === step.id ? null : step)}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl border-2 transition-all w-72
                  ${selectedStep?.id === step.id ? 'border-indigo-500 bg-indigo-50' : 'border-slate-200 bg-white hover:border-slate-300'}
                  step-node`}
              >
                <StepIcon status={step.status} />
                <div className="text-left min-w-0">
                  <p className="text-sm font-medium text-slate-800 truncate">{step.name}</p>
                  <p className="text-xs text-slate-400">{step.type}{step.tool ? ` · ${step.tool}` : ''}</p>
                </div>
                <StatusBadge status={step.status} size="sm" />
              </button>
              {i < steps.length - 1 && (
                <div className="w-px h-6 bg-slate-200 my-1" />
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Step detail */}
      {selectedStep && (
        <div className="w-72 bg-white rounded-xl border border-slate-200 p-4">
          <div className="flex items-center justify-between mb-3">
            <p className="text-sm font-semibold text-slate-800">{selectedStep.name}</p>
            <button onClick={() => onSelectStep(null)} className="text-slate-400 hover:text-slate-600"><X size={14} /></button>
          </div>
          <div className="space-y-2 text-xs">
            <Row label="Type" value={selectedStep.type} />
            <Row label="Status"><StatusBadge status={selectedStep.status} size="sm" /></Row>
            {selectedStep.tool && <Row label="Tool" value={selectedStep.tool} mono />}
            {selectedStep.version && <Row label="Version" value={selectedStep.version} mono />}
            <Row label="Attempts" value={String(selectedStep.attempts)} />
            {selectedStep.started && <Row label="시작" value={selectedStep.started} mono />}
            {selectedStep.ended && <Row label="종료" value={selectedStep.ended} mono />}
            <Row label="Duration" value={selectedStep.duration} mono />
          </div>
          {selectedStep.inputs && (
            <div className="mt-3">
              <p className="text-xs font-semibold text-slate-500 mb-1.5">Inputs</p>
              <pre className="text-xs bg-slate-50 rounded-lg p-2.5 overflow-auto font-mono text-slate-700">
                {JSON.stringify(selectedStep.inputs, null, 2)}
              </pre>
            </div>
          )}
          {selectedStep.outputs && (
            <div className="mt-3">
              <p className="text-xs font-semibold text-slate-500 mb-1.5">Outputs</p>
              <pre className="text-xs bg-slate-50 rounded-lg p-2.5 overflow-auto font-mono text-slate-700">
                {JSON.stringify(selectedStep.outputs, null, 2)}
              </pre>
            </div>
          )}
          {selectedStep.status === 'UNKNOWN_OUTCOME' && (
            <div className="mt-3 p-2.5 bg-orange-50 border border-orange-200 rounded-lg">
              <p className="text-xs font-semibold text-orange-700 mb-1">⚠ Unknown Outcome</p>
              <p className="text-xs text-orange-600">외부 시스템에서 작업이 수행되었는지 확인할 수 없습니다. 재실행 전에 결과를 확인하십시오.</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function EventsTab() {
  const events = [
    { time: '14:30:01', event: 'execution.started', detail: 'Execution 시작' },
    { time: '14:30:01', event: 'step.started', detail: 'Step 1: 주간 데이터 조회 시작' },
    { time: '14:30:08', event: 'step.succeeded', detail: 'Step 1: 완료 (8개 문서 검색됨)' },
    { time: '14:30:09', event: 'step.started', detail: 'Step 2: 보고서 파일 생성 시작' },
    { time: '14:30:35', event: 'step.succeeded', detail: 'Step 2: 완료 (report_2026-09-02.pdf 생성됨)' },
    { time: '14:30:36', event: 'approval.requested', detail: 'Step 3: 승인 요청됨 (Approver: Team Manager)' },
  ];
  return (
    <div className="max-w-2xl bg-white rounded-xl border border-slate-200 overflow-hidden">
      {events.map((ev, i) => (
        <div key={i} className="flex gap-4 px-4 py-3 border-b border-slate-100 last:border-0">
          <span className="font-mono text-xs text-slate-400 shrink-0 mt-0.5">{ev.time}</span>
          <div>
            <span className="font-mono text-xs text-indigo-600">{ev.event}</span>
            <p className="text-sm text-slate-700">{ev.detail}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function IOTab() {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Execution 입력</h3>
        <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700">
{`{
  "request": "주간 보고서를 생성하고 개발팀에 이메일로 발송해줘",
  "agent_id": "agt-002",
  "agent_version": "v1"
}`}
        </pre>
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Execution 출력 (부분)</h3>
        <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700">
{`{
  "status": "WAITING_APPROVAL",
  "completed_steps": 2,
  "artifact": "report_2026-09-02.pdf",
  "pending_approval": "apr-001"
}`}
        </pre>
      </div>
    </div>
  );
}

function AuditTab({ executionId }: { executionId: string }) {
  return (
    <div className="max-w-2xl bg-white rounded-xl border border-slate-200 overflow-hidden">
      {[
        { time: '14:30:01', actor: 'admin', action: 'execution.start', result: 'SUCCESS' },
        { time: '14:30:36', actor: 'system', action: 'approval.request', result: 'SUCCESS' },
      ].map((log, i) => (
        <div key={i} className="flex gap-4 px-4 py-3 border-b border-slate-100 last:border-0 text-sm">
          <span className="font-mono text-xs text-slate-400 shrink-0">{log.time}</span>
          <span className="text-slate-600">{log.actor}</span>
          <span className="font-mono text-xs text-indigo-600">{log.action}</span>
          <span className={`ml-auto text-xs font-medium ${log.result === 'SUCCESS' ? 'text-green-600' : 'text-red-600'}`}>{log.result}</span>
        </div>
      ))}
    </div>
  );
}

function StepIcon({ status }: { status: string }) {
  if (status === 'SUCCEEDED') return <CheckCircle2 size={18} className="text-green-500 shrink-0" />;
  if (status === 'RUNNING') return <Loader2 size={18} className="animate-spin text-indigo-500 shrink-0" />;
  if (status === 'WAITING_APPROVAL') return <Clock size={18} className="text-amber-500 shrink-0" />;
  if (status === 'FAILED') return <AlertTriangle size={18} className="text-red-500 shrink-0" />;
  return <Circle size={18} className="text-slate-300 shrink-0" />;
}

function Row({ label, value, mono, children }: { label: string; value?: string; mono?: boolean; children?: React.ReactNode }) {
  return (
    <div className="flex justify-between gap-2">
      <span className="text-slate-400 shrink-0">{label}</span>
      {children ?? <span className={`text-slate-700 text-right ${mono ? 'font-mono' : ''}`}>{value}</span>}
    </div>
  );
}
