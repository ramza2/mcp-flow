import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, X, CheckCircle2, Loader2, Circle, AlertTriangle, Clock, ChevronRight, RefreshCw } from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockExecutions } from '../../data/mock';
import { labelExecutionSource, type ExecutionStatus, type StepStatus } from '../../domain';
import PermissionGate from '../../components/PermissionGate';

type StepRow = {
  id: string;
  name: string;
  type: string;
  status: StepStatus;
  tool: string | null;
  version: string | null;
  attempts: number;
  started: string | null;
  ended: string | null;
  duration: string;
  inputs: Record<string, unknown> | null;
  outputs: Record<string, unknown> | null;
  error: string | null;
  runtimeInput?: {
    server: string;
    tool: string;
    message: string;
    round: number;
    expiresIn: string;
    responded?: boolean;
  };
};

const DEFAULT_STEPS: StepRow[] = [
  { id: 's1', name: '주간 데이터 조회', type: 'Tool', status: 'SUCCEEDED', tool: 'Search Documents', version: 'v2.1.0', attempts: 1, started: '14:30:01', ended: '14:30:08', duration: '7s', inputs: { query: '주간 보고 데이터 2026-09-02', limit: 10 }, outputs: { count: 8 }, error: null },
  { id: 's2', name: '보고서 파일 생성', type: 'Tool', status: 'SUCCEEDED', tool: 'Generate Report', version: 'v1.0.3', attempts: 1, started: '14:30:09', ended: '14:30:35', duration: '26s', inputs: { template: 'weekly_summary' }, outputs: { filename: 'report_2026-09-02.pdf' }, error: null },
  { id: 's3', name: '이메일 발송 승인 대기', type: 'Approval', status: 'WAITING_APPROVAL', tool: null, version: null, attempts: 0, started: '14:30:36', ended: null, duration: '–', inputs: null, outputs: null, error: null },
  { id: 's4', name: '이메일 발송', type: 'Tool', status: 'PENDING', tool: 'Send Email', version: 'v3.0.1', attempts: 0, started: null, ended: null, duration: '–', inputs: null, outputs: null, error: null },
];

const MRTR_STEPS: StepRow[] = [
  {
    id: 'm1',
    name: '보고서 생성 (MRTR)',
    type: 'Tool',
    status: 'WAITING_INPUT',
    tool: 'Generate Report',
    version: 'v1.0.3',
    attempts: 1,
    started: '15:00:05',
    ended: null,
    duration: '–',
    inputs: null,
    outputs: null,
    error: null,
    runtimeInput: {
      server: 'Report MCP',
      tool: 'Generate Report',
      message: '보고서 템플릿을 선택해 주세요.',
      round: 1,
      expiresIn: '4분 12초',
      responded: false,
    },
  },
];

const UNKNOWN_STEPS: StepRow[] = [
  { id: 'u1', name: '외부 전송', type: 'Tool', status: 'UNKNOWN_OUTCOME', tool: 'Send Email', version: 'v3.0.1', attempts: 1, started: '11:20:01', ended: null, duration: '–', inputs: { to: '***' }, outputs: null, error: '응답 타임아웃 — 외부 전송 여부 불명' },
];

export default function ExecutionDetail() {
  const { executionId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const base = mockExecutions.find(e => e.id === executionId) ?? mockExecutions[0];
  const [status, setStatus] = useState<ExecutionStatus>(base.status);
  const [cancelling, setCancelling] = useState(false);
  const [mrtrResponded, setMrtrResponded] = useState(false);
  const isMrtrExecution = base.id === 'EXE-20260902-00126' || base.status === 'WAITING_INPUT';

  const steps = useMemo(() => {
    if (isMrtrExecution || status === 'WAITING_INPUT' || (mrtrResponded && status === 'RUNNING')) {
      return MRTR_STEPS.map(s => s.runtimeInput
        ? {
          ...s,
          runtimeInput: { ...s.runtimeInput, responded: mrtrResponded },
          status: (mrtrResponded || status === 'RUNNING' ? 'RUNNING' : s.status) as StepStatus,
        }
        : s);
    }
    if (base.status === 'CANCELLED' || status === 'CANCELLED' || status === 'CANCEL_REQUESTED') {
      return DEFAULT_STEPS.map((s, i) => (i === 0 ? { ...s, status: 'SUCCEEDED' as StepStatus } : { ...s, status: (status === 'CANCELLED' ? 'CANCELLED' : s.status) as StepStatus }));
    }
    if (base.id === 'EXE-20260901-00119') return UNKNOWN_STEPS;
    return DEFAULT_STEPS;
  }, [base.id, base.status, status, mrtrResponded, isMrtrExecution]);

  const [selectedStep, setSelectedStep] = useState<StepRow | null>(null);

  /** Runtime MRTR: WAITING_INPUT → user response → Execution RUNNING (and Step RUNNING). */
  const handleMrtrRespond = () => {
    setMrtrResponded(true);
    setStatus('RUNNING');
  };

  const handleCancel = async () => {
    if (cancelling) return;
    setCancelling(true);
    // RUNNING → CANCEL_REQUESTED → CANCELLED (never jump straight to CANCELLED)
    setStatus('CANCEL_REQUESTED');
    await delay(1200);
    setStatus('CANCELLED');
    setCancelling(false);
  };

  const canCancel = status === 'RUNNING' || status === 'WAITING_APPROVAL' || status === 'WAITING_INPUT';
  const showSafeRetry = base.canRetry && (status === 'FAILED' || status === 'PARTIALLY_SUCCEEDED');
  const hasUnknown = steps.some(s => s.status === 'UNKNOWN_OUTCOME');
  const approvalId = 'approvalId' in base ? (base as { approvalId?: string }).approvalId : undefined;

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/executions')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Executions
        </button>
        <div className="flex items-start justify-between">
          <div>
            <div className="flex items-center gap-3 mb-1">
              <h1 className="text-lg font-semibold text-slate-900 font-mono">{base.id}</h1>
              <StatusBadge status={status} />
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-slate-500">
              <span>Source: <span className="text-slate-700">{labelExecutionSource(base.sourceType)}</span></span>
              <span>Agent: <span className="text-slate-700">{base.agent ?? base.workflow ?? '–'}</span></span>
              <span>시작자: <span className="text-slate-700">{base.user}</span></span>
              <span>시작: <span className="text-slate-700">{base.startedAt}</span></span>
              <span>소요시간: <span className="text-slate-700 font-mono">{base.duration}</span></span>
            </div>
          </div>
          <div className="flex gap-2 shrink-0">
            {(canCancel || status === 'CANCEL_REQUESTED') && (
              <PermissionGate permission="execution.cancel">
                <Button variant="danger" size="sm" icon={<X size={13} />} loading={cancelling || status === 'CANCEL_REQUESTED'} onClick={handleCancel} disabled={status === 'CANCEL_REQUESTED'}>
                  {status === 'CANCEL_REQUESTED' ? '취소 요청됨' : '실행 취소'}
                </Button>
              </PermissionGate>
            )}
            {showSafeRetry && !hasUnknown && (
              <PermissionGate permission="execution.retry">
                <Button variant="outline" size="sm" icon={<RefreshCw size={13} />} onClick={() => navigate('/executions')}>
                  New Execution (Retry)
                </Button>
              </PermissionGate>
            )}
          </div>
        </div>
        {status === 'CANCEL_REQUESTED' && (
          <div className="mt-3"><InlineAlert type="warning" message="Cancel requested — 진행 중 Step 정리 후 CANCELLED로 전환됩니다." /></div>
        )}
        {hasUnknown && (
          <div className="mt-3"><InlineAlert type="warning" message="UNKNOWN_OUTCOME Step이 있습니다. 자동 Retry CTA는 제공하지 않습니다. 외부 시스템 결과를 운영 확인하세요." /></div>
        )}
      </div>

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
        {tab === 'overview' && (
          <OverviewTab
            execution={base}
            status={status}
            approvalId={approvalId}
            onOpenApproval={() => navigate(`/approvals/${approvalId ?? 'apr-001'}`)}
            steps={steps}
            onMrtrRespond={handleMrtrRespond}
          />
        )}
        {tab === 'steps' && <StepsTab steps={steps} selectedStep={selectedStep} onSelectStep={setSelectedStep} />}
        {tab === 'events' && <EventsTab status={status} />}
        {tab === 'io' && <IOTab status={status} />}
        {tab === 'audit' && <AuditTab executionId={base.id} />}
      </div>
    </div>
  );
}

function OverviewTab({
  execution, status, approvalId, onOpenApproval, steps, onMrtrRespond,
}: {
  execution: typeof mockExecutions[0];
  status: ExecutionStatus;
  approvalId?: string;
  onOpenApproval: () => void;
  steps: StepRow[];
  onMrtrRespond: () => void;
}) {
  // Keep Runtime Input card visible after respond (Step may already be RUNNING).
  const mrtr = steps.find(s => s.runtimeInput)?.runtimeInput;
  const showMrtrCard = !!mrtr && (status === 'WAITING_INPUT' || !!mrtr.responded);

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
        </div>
      </div>

      {status === 'WAITING_APPROVAL' && (
        <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4 flex items-start gap-3">
          <AlertTriangle size={16} className="text-amber-600 shrink-0 mt-0.5" />
          <div>
            <p className="text-sm font-semibold text-amber-800">Execution WAITING_APPROVAL</p>
            <p className="text-sm text-amber-700 mt-0.5">이메일 발송 작업이 승인을 기다리고 있습니다. (Approval Entity Status는 PENDING)</p>
            <button onClick={onOpenApproval} className="mt-2 text-xs text-indigo-600 hover:underline flex items-center gap-1">
              승인 요청 보기 {approvalId && <span className="font-mono">({approvalId})</span>} <ChevronRight size={11} />
            </button>
          </div>
        </div>
      )}

      {showMrtrCard && mrtr && (
        <div className="col-span-full bg-amber-50 border border-amber-200 rounded-xl p-4 space-y-2">
          <p className="text-sm font-semibold text-amber-800">
            {mrtr.responded
              ? 'MCP Tool input received — Execution resumed'
              : 'MCP Tool requests information (Runtime WAITING_INPUT)'}
          </p>
          <div className="text-xs text-amber-700 space-y-0.5">
            <p>MCP Server: {mrtr.server}</p>
            <p>Tool: <span className="font-mono">{mrtr.tool}</span></p>
            <p>{mrtr.message}</p>
            <p>Round: {mrtr.round} · Remaining: {mrtr.expiresIn}</p>
          </div>
          <p className="text-[10px] text-amber-500 font-mono">requestState is not user-visible / not editable</p>
          {!mrtr.responded && (
            <Button size="sm" onClick={onMrtrRespond}>응답 후 Resume</Button>
          )}
          {mrtr.responded && status === 'RUNNING' && (
            <InlineAlert type="info" message="응답 제출됨 — Execution RUNNING으로 재개" />
          )}
        </div>
      )}
    </div>
  );
}

function StepsTab({ steps, selectedStep, onSelectStep }: { steps: StepRow[]; selectedStep: StepRow | null; onSelectStep: (s: StepRow | null) => void }) {
  return (
    <div className="flex gap-4 max-w-5xl">
      <div className="flex-1 bg-white rounded-xl border border-slate-200 p-6">
        <h3 className="text-sm font-semibold text-slate-700 mb-6">Step Graph</h3>
        <div className="flex flex-col items-center gap-0">
          {steps.map((step, i) => (
            <div key={step.id} className="flex flex-col items-center">
              <button
                onClick={() => onSelectStep(selectedStep?.id === step.id ? null : step)}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl border-2 transition-all w-80
                  ${selectedStep?.id === step.id ? 'border-indigo-500 bg-indigo-50' : 'border-slate-200 bg-white hover:border-slate-300'}`}
              >
                <StepIcon status={step.status} />
                <div className="text-left min-w-0 flex-1">
                  <p className="text-sm font-medium text-slate-800 truncate">{step.name}</p>
                  <p className="text-xs text-slate-400">{step.type}{step.tool ? ` · ${step.tool}` : ''}</p>
                </div>
                <StatusBadge status={step.status} size="sm" />
              </button>
              {i < steps.length - 1 && <div className="w-px h-6 bg-slate-200 my-1" />}
            </div>
          ))}
        </div>
      </div>

      {selectedStep && (
        <div className="w-80 bg-white rounded-xl border border-slate-200 p-4">
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
          </div>
          {selectedStep.status === 'UNKNOWN_OUTCOME' && (
            <div className="mt-3 p-2.5 bg-orange-50 border border-orange-200 rounded-lg">
              <p className="text-xs font-semibold text-orange-700 mb-1">⚠ Unknown Outcome</p>
              <p className="text-xs text-orange-600">자동 Retry CTA 없음. 외부 시스템 결과를 운영 확인하세요.</p>
            </div>
          )}
          {selectedStep.runtimeInput && (
            <div className="mt-3 p-2.5 bg-amber-50 border border-amber-200 rounded-lg text-xs text-amber-800 space-y-1">
              <p className="font-semibold">Runtime Input (MRTR)</p>
              <p>Round {selectedStep.runtimeInput.round}</p>
              <p>{selectedStep.runtimeInput.message}</p>
              <p>Expiry: {selectedStep.runtimeInput.expiresIn}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function EventsTab({ status }: { status: ExecutionStatus }) {
  const events = [
    { time: '14:30:01', event: 'execution.started', detail: 'Execution CREATED → QUEUED → RUNNING' },
    { time: '14:30:36', event: 'approval.requested', detail: 'Step WAITING_APPROVAL (Approval Entity = PENDING)' },
    ...(status === 'CANCEL_REQUESTED' || status === 'CANCELLED'
      ? [{ time: '14:31:10', event: 'execution.cancel_requested', detail: 'CANCEL_REQUESTED' },
         ...(status === 'CANCELLED' ? [{ time: '14:31:12', event: 'execution.cancelled', detail: 'CANCELLED' }] : [])]
      : []),
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

function IOTab({ status }: { status: ExecutionStatus }) {
  return (
    <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Execution 입력</h3>
        <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700">{`{
  "request": "...",
  "agent_version_id": "agt-002/v2"
}`}</pre>
      </div>
      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Execution 출력</h3>
        <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700">{`{
  "status": ${JSON.stringify(status)}
}`}</pre>
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
          <span className="ml-auto text-xs font-medium text-green-600">{log.result}</span>
        </div>
      ))}
      <p className="px-4 py-2 text-[10px] text-slate-400">ref: {executionId}</p>
    </div>
  );
}

function StepIcon({ status }: { status: string }) {
  if (status === 'SUCCEEDED') return <CheckCircle2 size={18} className="text-green-500 shrink-0" />;
  if (status === 'RUNNING') return <Loader2 size={18} className="animate-spin text-indigo-500 shrink-0" />;
  if (status === 'WAITING_APPROVAL' || status === 'WAITING_INPUT') return <Clock size={18} className="text-amber-500 shrink-0" />;
  if (status === 'FAILED' || status === 'UNKNOWN_OUTCOME') return <AlertTriangle size={18} className="text-red-500 shrink-0" />;
  if (status === 'CANCELLED') return <X size={18} className="text-slate-400 shrink-0" />;
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

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
