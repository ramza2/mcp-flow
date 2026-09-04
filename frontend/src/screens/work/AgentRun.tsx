import { useState, useRef, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router';
import {
  Send, Bot, ChevronDown, ChevronUp, CheckCircle2,
  Loader2, Circle, ExternalLink, Clock, X, Shield, AlertTriangle,
} from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import type { AgentRequestStatus, ExecutionStatus, StepStatus } from '../../domain';

const AGENTS = [
  { id: 'agt-001', name: 'General Work Assistant', description: '일반 업무 자동화 및 정보 조회', version: 'v3', available: true, capabilities: ['문서 검색', '이메일 발송', '일정 관리'] },
  { id: 'agt-002', name: 'Report Assistant', description: '보고서 생성 및 발송 특화', version: 'v2', available: true, capabilities: ['보고서 생성', '파일 내보내기', '이메일 발송'] },
  { id: 'agt-003', name: 'Research Assistant', description: '문서 및 정보 검색 특화', version: 'v1', available: true, capabilities: ['문서 검색', '날씨 조회', '직원 조회'] },
];

type MessageType =
  | 'user'
  | 'analysis'
  | 'planning_input'
  | 'plan'
  | 'confirmation'
  | 'execution'
  | 'runtime_input'
  | 'approval_waiting'
  | 'result';

interface Message {
  id: string;
  type: MessageType;
  content?: string;
  data?: Record<string, unknown>;
}

type DemoPhase =
  | 'idle'
  | 'request'
  | 'planning_input'
  | 'confirming'
  | 'ready'
  | 'executing'
  | 'runtime_input'
  | 'approval'
  | 'done';

interface ExecStep {
  label: string;
  status: StepStatus;
}

export default function AgentRun() {
  const { conversationId: _conversationId } = useParams();
  const navigate = useNavigate();
  const [selectedAgent, setSelectedAgent] = useState(AGENTS[0]);
  const [showAgentPicker, setShowAgentPicker] = useState(false);
  const [inputText, setInputText] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [phase, setPhase] = useState<DemoPhase>('idle');
  const [agentRequestStatus, setAgentRequestStatus] = useState<AgentRequestStatus | null>(null);
  const [executionStatus, setExecutionStatus] = useState<ExecutionStatus | null>(null);
  const [planExpanded, setPlanExpanded] = useState(true);
  const [executionSteps, setExecutionSteps] = useState<ExecStep[]>([]);
  const [planningAnswer, setPlanningAnswer] = useState('');
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, agentRequestStatus, executionStatus]);

  const addMessage = (msg: Omit<Message, 'id'>) => {
    setMessages(prev => [...prev, { ...msg, id: Math.random().toString(36).slice(2) }]);
  };

  /** Demo: AgentRequest lifecycle then Execution lifecycle — never mix statuses. */
  const runDemoFlow = async (text: string) => {
    addMessage({ type: 'user', content: text });
    setPhase('request');
    setExecutionStatus(null);

    setAgentRequestStatus('RECEIVED');
    await delay(400);
    setAgentRequestStatus('ANALYZING');
    await delay(700);
    addMessage({ type: 'analysis', content: '보고서 파일을 생성한 후 이메일로 전송하는 요청으로 이해했습니다.' });

    setAgentRequestStatus('RETRIEVING');
    await delay(500);
    setAgentRequestStatus('SELECTING');
    await delay(500);
    setAgentRequestStatus('BUILDING_PARAMETERS');
    await delay(500);

    // Planning WAITING_INPUT (AgentRequest) — not Execution WAITING_INPUT
    setAgentRequestStatus('WAITING_INPUT');
    setPhase('planning_input');
    addMessage({
      type: 'planning_input',
      data: {
        title: 'Agent needs information',
        question: '보고서를 어느 기간 기준으로 생성할까요?',
        options: ['이번 주 (월–금)', '지난 7일', '이번 달'],
      },
    });
  };

  const handlePlanningInput = async (answer: string) => {
    setPlanningAnswer(answer);
    addMessage({ type: 'user', content: answer });
    setAgentRequestStatus('PLANNING');
    setPhase('request');
    await delay(700);
    setAgentRequestStatus('VALIDATING');
    await delay(500);

    addMessage({
      type: 'plan',
      data: {
        goal: `주간 보고서(${answer})를 생성하고 개발팀에 이메일 발송`,
        steps: ['주간 데이터 조회', '보고서 파일 생성', '이메일 발송'],
        tools: ['Search Documents', 'Generate Report', 'Send Email'],
        riskClass: 'NON_IDEMPOTENT_WRITE',
        externalTransfer: true,
        approvalRequired: true,
      },
    });

    setAgentRequestStatus('WAITING_CONFIRMATION');
    setPhase('confirming');
    addMessage({ type: 'confirmation' });
  };

  const handleConfirm = async () => {
    setAgentRequestStatus('READY');
    setPhase('ready');
    await delay(400);

    // Execution starts — AgentRequest stays READY (terminal for planning)
    setExecutionStatus('CREATED');
    setPhase('executing');
    const steps: ExecStep[] = [
      { label: '주간 데이터 조회', status: 'PENDING' },
      { label: '보고서 파일 생성', status: 'PENDING' },
      { label: '이메일 발송', status: 'PENDING' },
    ];
    setExecutionSteps(steps);
    addMessage({ type: 'execution', data: { steps } });

    await delay(500);
    setExecutionStatus('QUEUED');
    await delay(500);
    setExecutionStatus('RUNNING');
    setExecutionSteps(s => s.map((st, i) => (i === 0 ? { ...st, status: 'RUNNING' } : st)));
    await delay(900);
    setExecutionSteps(s => s.map((st, i) => (i === 0 ? { ...st, status: 'SUCCEEDED' } : i === 1 ? { ...st, status: 'RUNNING' } : st)));
    await delay(800);

    // Runtime MCP WAITING_INPUT (Execution/Step) — distinct from planning
    setExecutionStatus('WAITING_INPUT');
    setExecutionSteps(s => s.map((st, i) => (i === 1 ? { ...st, status: 'WAITING_INPUT' } : st)));
    setPhase('runtime_input');
    addMessage({
      type: 'runtime_input',
      data: {
        title: 'MCP Tool requests information',
        server: 'Report MCP',
        tool: 'Generate Report',
        message: '보고서 템플릿을 선택해 주세요.',
        schemaField: 'template',
        options: ['weekly_summary', 'detailed_status'],
        round: 1,
        expiresIn: '4분 52초',
        sideEffectWarning: '템플릿 확정 후 파일 생성이 시작됩니다.',
      },
    });
  };

  const handleRuntimeInput = async (template: string) => {
    addMessage({ type: 'user', content: `template=${template}` });
    setPhase('executing');
    setExecutionStatus('RUNNING');
    setExecutionSteps(s => s.map((st, i) => (i === 1 ? { ...st, status: 'RUNNING' } : st)));
    await delay(1000);
    setExecutionSteps(s => s.map((st, i) => (i <= 1 ? { ...st, status: 'SUCCEEDED' } : { ...st, status: 'WAITING_APPROVAL' })));
    setExecutionStatus('WAITING_APPROVAL');
    setPhase('approval');
    addMessage({
      type: 'approval_waiting',
      data: {
        tool: 'Send Email',
        riskClass: 'NON_IDEMPOTENT_WRITE',
        approver: 'Team Manager',
        expires: '15:30',
        approvalId: 'apr-001',
      },
    });
  };

  const handleApprovalDone = async () => {
    setPhase('executing');
    setExecutionStatus('RUNNING');
    await delay(800);
    setExecutionSteps(s => s.map(st => ({ ...st, status: 'SUCCEEDED' })));
    setExecutionStatus('SUCCEEDED');
    setPhase('done');
    addMessage({
      type: 'result',
      data: {
        status: 'SUCCEEDED',
        summary: `주간 보고서(${planningAnswer || '기간 미지정'})가 생성되어 개발팀 3명에게 이메일을 발송했습니다.`,
        artifact: 'report_2026-09-02.pdf',
        executionId: 'EXE-20260902-00125',
      },
    });
  };

  const handleSend = () => {
    if (!inputText.trim()) return;
    const text = inputText;
    setInputText('');
    runDemoFlow(text);
  };

  const busy = phase !== 'idle' && phase !== 'done';

  return (
    <div className="flex h-full" style={{ height: 'calc(100vh - 48px)' }}>
      <div className="flex flex-col flex-1 min-w-0 border-r border-slate-200">
        <div className="px-4 py-3 border-b border-slate-100 bg-white">
          <div className="relative">
            <button
              onClick={() => setShowAgentPicker(v => !v)}
              className="flex items-center gap-2.5 px-3 py-2 rounded-lg border border-slate-200 hover:border-indigo-300 hover:bg-slate-50 transition-colors text-sm"
            >
              <div className="w-6 h-6 rounded-md bg-indigo-600 flex items-center justify-center">
                <Bot size={12} className="text-white" />
              </div>
              <span className="font-medium text-slate-800">{selectedAgent.name}</span>
              <span className="text-xs text-slate-400 font-mono">{selectedAgent.version}</span>
              <ChevronDown size={13} className="text-slate-400 ml-1" />
            </button>
            {showAgentPicker && (
              <div className="absolute top-full mt-1 left-0 w-80 bg-white rounded-xl border border-slate-200 shadow-xl z-10 py-1">
                {AGENTS.map(agent => (
                  <button
                    key={agent.id}
                    onClick={() => { setSelectedAgent(agent); setShowAgentPicker(false); }}
                    className={`w-full text-left px-4 py-3 hover:bg-slate-50 transition-colors ${selectedAgent.id === agent.id ? 'bg-indigo-50' : ''}`}
                  >
                    <div className="flex items-center justify-between">
                      <span className="text-sm font-medium text-slate-800">{agent.name}</span>
                      <span className="text-xs font-mono text-slate-400">{agent.version}</span>
                    </div>
                    <p className="text-xs text-slate-500 mt-0.5">{agent.description}</p>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <div className="w-12 h-12 rounded-2xl bg-indigo-100 flex items-center justify-center mb-3">
                <Bot size={22} className="text-indigo-600" />
              </div>
              <p className="text-sm font-medium text-slate-700">무엇을 도와드릴까요?</p>
              <p className="text-xs text-slate-400 mt-1">AgentRequest 계획과 Execution 실행이 분리되어 표시됩니다.</p>
              <div className="mt-4 flex flex-col gap-2 w-full max-w-xs">
                {['주간 보고서를 생성하고 개발팀에 이메일로 발송해줘'].map(t => (
                  <button key={t} onClick={() => setInputText(t)} className="text-left text-xs p-2.5 rounded-lg border border-slate-200 hover:border-indigo-300 hover:bg-indigo-50 text-slate-600 hover:text-indigo-700 transition-colors">
                    {t}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map(msg => (
            <MessageCard
              key={msg.id}
              message={msg}
              onConfirm={handleConfirm}
              onPlanningAnswer={handlePlanningInput}
              onRuntimeAnswer={handleRuntimeInput}
              onApprovalAction={handleApprovalDone}
            />
          ))}
          <div ref={messagesEndRef} />
        </div>

        <div className="border-t border-slate-200 px-4 py-3 bg-white">
          <div className="flex items-end gap-2">
            <textarea
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder="MCPFlow에게 업무를 요청하세요..."
              rows={2}
              disabled={busy}
              className="flex-1 resize-none px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent disabled:bg-slate-50 disabled:text-slate-400"
            />
            <Button onClick={handleSend} icon={<Send size={14} />} disabled={!inputText.trim() || busy} size="md">전송</Button>
          </div>
        </div>
      </div>

      {/* Plan / Execution panel — statuses separated */}
      <div className="w-80 flex-shrink-0 flex flex-col bg-slate-50 overflow-y-auto">
        <div className="px-4 py-3 border-b border-slate-200 bg-white space-y-2">
          <span className="text-sm font-semibold text-slate-800">Plan / Execution</span>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] uppercase tracking-wide text-slate-400">Agent Request Status</span>
            {agentRequestStatus ? <StatusBadge status={agentRequestStatus} size="sm" /> : <span className="text-xs text-slate-300">–</span>}
          </div>
          <div className="flex items-center justify-between gap-2">
            <span className="text-[10px] uppercase tracking-wide text-slate-400">Execution Status</span>
            {executionStatus ? <StatusBadge status={executionStatus} size="sm" /> : <span className="text-xs text-slate-300">–</span>}
          </div>
        </div>

        <div className="flex-1 p-4 space-y-3">
          {phase === 'idle' && (
            <div className="text-center py-8 text-sm text-slate-400">실행 정보가 여기에 표시됩니다.</div>
          )}

          {(phase === 'request' || phase === 'planning_input') && (
            <div className="flex flex-col items-center py-6 gap-3">
              <Loader2 size={24} className="animate-spin text-indigo-500" />
              <p className="text-sm text-slate-500 text-center px-2">
                {agentRequestStatus === 'WAITING_INPUT'
                  ? 'Agent needs information (Planning WAITING_INPUT)'
                  : `AgentRequest: ${agentRequestStatus}`}
              </p>
            </div>
          )}

          {(phase === 'confirming' || phase === 'ready' || phase === 'executing' || phase === 'runtime_input' || phase === 'approval' || phase === 'done') && (
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <button onClick={() => setPlanExpanded(v => !v)} className="w-full flex items-center justify-between px-4 py-3 border-b border-slate-100">
                <span className="text-sm font-semibold text-slate-800">Execution Plan</span>
                {planExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
              {planExpanded && (
                <div className="p-4 space-y-3">
                  <p className="text-sm text-slate-800">주간 보고서를 생성하고 개발팀에 이메일 발송</p>
                  <div className="flex flex-wrap gap-1.5">
                    <RiskBadge risk="NON_IDEMPOTENT_WRITE" />
                    <span className="text-xs bg-amber-50 text-amber-700 px-2 py-0.5 rounded-full">외부 전송 포함</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {executionSteps.length > 0 && (
            <div className="bg-white rounded-xl border border-slate-200 p-4">
              <p className="text-xs font-semibold text-slate-500 mb-3">Execution Steps</p>
              <div className="space-y-2">
                {executionSteps.map((step, i) => (
                  <div key={i} className="flex items-center gap-2.5">
                    <StepGlyph status={step.status} />
                    <div className="min-w-0">
                      <p className="text-sm text-slate-800 truncate">{step.label}</p>
                      <StatusBadge status={step.status} size="sm" />
                    </div>
                  </div>
                ))}
              </div>
            </div>
          )}

          {phase === 'done' && (
            <button
              onClick={() => navigate('/executions/EXE-20260902-00125')}
              className="w-full flex items-center justify-center gap-2 py-2.5 text-sm text-indigo-600 hover:text-indigo-700 bg-indigo-50 hover:bg-indigo-100 rounded-lg transition-colors"
            >
              <ExternalLink size={13} /> Execution 상세 보기
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function StepGlyph({ status }: { status: StepStatus }) {
  if (status === 'SUCCEEDED') return <CheckCircle2 size={15} className="text-green-500" />;
  if (status === 'RUNNING') return <Loader2 size={15} className="animate-spin text-indigo-500" />;
  if (status === 'WAITING_INPUT' || status === 'WAITING_APPROVAL') return <Clock size={15} className="text-amber-500" />;
  if (status === 'FAILED') return <X size={15} className="text-red-500" />;
  return <Circle size={15} className="text-slate-300" />;
}

function MessageCard({
  message, onConfirm, onPlanningAnswer, onRuntimeAnswer, onApprovalAction,
}: {
  message: Message;
  onConfirm: () => void;
  onPlanningAnswer: (a: string) => void;
  onRuntimeAnswer: (a: string) => void;
  onApprovalAction: () => void;
}) {
  switch (message.type) {
    case 'user':
      return (
        <div className="flex justify-end">
          <div className="max-w-sm bg-indigo-600 text-white rounded-2xl rounded-tr-sm px-4 py-3 text-sm">{message.content}</div>
        </div>
      );
    case 'analysis':
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-slate-200 rounded-2xl rounded-tl-sm px-4 py-3 text-sm text-slate-700">{message.content}</div>
        </div>
      );
    case 'planning_input': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-blue-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-blue-600" /></div>
          <div className="bg-blue-50 border border-blue-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm space-y-2">
            <p className="text-xs font-semibold text-blue-700">{d.title as string}</p>
            <p className="text-sm text-slate-800">{d.question as string}</p>
            <p className="text-[10px] text-blue-500 font-mono">AgentRequest Status: WAITING_INPUT (Planning)</p>
            <div className="flex flex-col gap-1.5 pt-1">
              {(d.options as string[]).map(opt => (
                <Button key={opt} size="sm" variant="outline" onClick={() => onPlanningAnswer(opt)}>{opt}</Button>
              ))}
            </div>
          </div>
        </div>
      );
    }
    case 'plan': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-indigo-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm">
            <p className="text-xs font-semibold text-indigo-700 mb-2">Execution Plan</p>
            <p className="text-sm text-slate-800 mb-2">{d.goal as string}</p>
            <RiskBadge risk={d.riskClass as string} />
          </div>
        </div>
      );
    }
    case 'confirmation':
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-slate-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm">
            <p className="text-sm font-medium text-slate-800 mb-1">다음 작업을 실행할까요?</p>
            <p className="text-xs text-slate-500 mb-1">AgentRequest: WAITING_CONFIRMATION → Confirm 시 READY</p>
            <p className="text-xs text-slate-400 mb-3">이후 Execution CREATED → QUEUED → RUNNING</p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm">취소</Button>
              <Button size="sm" onClick={onConfirm}>Confirm & Run</Button>
            </div>
          </div>
        </div>
      );
    case 'execution':
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-slate-200 rounded-2xl rounded-tl-sm px-4 py-3">
            <div className="flex items-center gap-2 mb-1">
              <Loader2 size={13} className="animate-spin text-indigo-500" />
              <span className="text-sm font-medium text-slate-800">Execution 시작</span>
            </div>
            <p className="text-[10px] text-slate-400 font-mono">AgentRequest remains READY · Execution Status separate</p>
          </div>
        </div>
      );
    case 'runtime_input': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-amber-100 flex items-center justify-center shrink-0"><AlertTriangle size={14} className="text-amber-600" /></div>
          <div className="bg-amber-50 border border-amber-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm space-y-2">
            <p className="text-xs font-semibold text-amber-800">{d.title as string}</p>
            <p className="text-sm text-slate-800">{d.message as string}</p>
            <div className="text-xs text-amber-700 space-y-0.5">
              <p>MCP Server: <span className="font-medium">{d.server as string}</span></p>
              <p>Tool: <span className="font-mono">{d.tool as string}</span></p>
              <p>Round: {d.round as number}</p>
              <p>Remaining: {d.expiresIn as string}</p>
            </div>
            <p className="text-xs text-orange-700 flex items-start gap-1"><AlertTriangle size={11} className="mt-0.5" />{d.sideEffectWarning as string}</p>
            <p className="text-[10px] text-amber-500 font-mono">Execution/Step WAITING_INPUT (MCP Runtime MRTR) — requestState not shown</p>
            <div className="flex flex-col gap-1.5 pt-1">
              {(d.options as string[]).map(opt => (
                <Button key={opt} size="sm" onClick={() => onRuntimeAnswer(opt)}>{opt}</Button>
              ))}
            </div>
          </div>
        </div>
      );
    }
    case 'approval_waiting': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-amber-100 flex items-center justify-center shrink-0"><Shield size={14} className="text-amber-600" /></div>
          <div className="bg-amber-50 border border-amber-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm">
            <p className="text-sm font-medium text-amber-800 mb-2">Execution WAITING_APPROVAL</p>
            <p className="text-xs text-amber-700">Tool: {d.tool as string}</p>
            <RiskBadge risk={d.riskClass as string} />
            <p className="text-xs text-amber-600 mt-2">Approver: {d.approver as string} · 만료: {d.expires as string}</p>
            <div className="mt-3 flex gap-2">
              <Button size="sm" onClick={onApprovalAction}>승인 처리 (시뮬레이션)</Button>
            </div>
          </div>
        </div>
      );
    }
    case 'result': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-green-100 flex items-center justify-center shrink-0">
            <CheckCircle2 size={14} className="text-green-600" />
          </div>
          <div className="border rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm bg-green-50 border-green-200">
            <p className="text-sm font-medium mb-1 text-green-800">Execution SUCCEEDED</p>
            <p className="text-sm text-slate-700 mb-2">{d.summary as string}</p>
            <p className="text-xs text-slate-400 mt-2 font-mono">Execution: {d.executionId as string}</p>
          </div>
        </div>
      );
    }
    default:
      return null;
  }
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
