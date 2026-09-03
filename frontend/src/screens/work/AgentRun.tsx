import { useState, useRef, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router';
import {
  Send, Bot, ChevronDown, ChevronUp, AlertTriangle, CheckCircle2,
  Loader2, Circle, Check, ExternalLink, Clock, X, Shield
} from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';

const AGENTS = [
  { id: 'agt-001', name: 'General Work Assistant', description: '일반 업무 자동화 및 정보 조회', version: 'v2', available: true, capabilities: ['문서 검색', '이메일 발송', '일정 관리'] },
  { id: 'agt-002', name: 'Report Assistant', description: '보고서 생성 및 발송 특화', version: 'v1', available: true, capabilities: ['보고서 생성', '파일 내보내기', '이메일 발송'] },
  { id: 'agt-003', name: 'Research Assistant', description: '문서 및 정보 검색 특화', version: 'v1', available: true, capabilities: ['문서 검색', '날씨 조회', '직원 조회'] },
];

type MessageType = 'user' | 'analysis' | 'clarification' | 'plan' | 'confirmation' | 'execution' | 'runtime_input' | 'approval_waiting' | 'result';

interface Message {
  id: string;
  type: MessageType;
  content?: string;
  data?: Record<string, unknown>;
}

type FlowStep = 'idle' | 'analyzing' | 'plan' | 'confirming' | 'running' | 'approval' | 'done';

export default function AgentRun() {
  const { conversationId } = useParams();
  const navigate = useNavigate();
  const [selectedAgent, setSelectedAgent] = useState(AGENTS[0]);
  const [showAgentPicker, setShowAgentPicker] = useState(false);
  const [inputText, setInputText] = useState('');
  const [messages, setMessages] = useState<Message[]>([]);
  const [flowStep, setFlowStep] = useState<FlowStep>('idle');
  const [planExpanded, setPlanExpanded] = useState(true);
  const [executionSteps, setExecutionSteps] = useState<{ label: string; status: 'pending' | 'running' | 'done' | 'failed' }[]>([]);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const addMessage = (msg: Omit<Message, 'id'>) => {
    setMessages(prev => [...prev, { ...msg, id: Math.random().toString(36).slice(2) }]);
  };

  const runDemoFlow = async (text: string) => {
    addMessage({ type: 'user', content: text });
    setFlowStep('analyzing');

    await delay(1200);
    addMessage({ type: 'analysis', content: '보고서 파일을 생성한 후 이메일로 전송하는 요청으로 이해했습니다.' });

    await delay(800);
    setFlowStep('plan');
    addMessage({
      type: 'plan',
      data: {
        goal: '주간 보고서를 생성하고 개발팀에 이메일 발송',
        steps: ['주간 데이터 조회', '보고서 파일 생성', '이메일 발송'],
        tools: ['Search Documents', 'Generate Report', 'Send Email'],
        riskClass: 'NON_IDEMPOTENT_WRITE',
        externalTransfer: true,
        approvalRequired: true,
      },
    });

    await delay(600);
    setFlowStep('confirming');
    addMessage({ type: 'confirmation' });
  };

  const handleConfirm = async () => {
    setFlowStep('running');
    const steps = [
      { label: '주간 데이터 조회', status: 'pending' as const },
      { label: '보고서 파일 생성', status: 'pending' as const },
      { label: '이메일 발송', status: 'pending' as const },
    ];
    setExecutionSteps(steps);
    addMessage({ type: 'execution', data: { steps } });

    await delay(800);
    setExecutionSteps(s => s.map((st, i) => i === 0 ? { ...st, status: 'running' } : st));
    await delay(1200);
    setExecutionSteps(s => s.map((st, i) => i === 0 ? { ...st, status: 'done' } : i === 1 ? { ...st, status: 'running' } : st));
    await delay(1000);
    setExecutionSteps(s => s.map((st, i) => i <= 1 ? { ...st, status: 'done' } : { ...st, status: 'running' }));
    await delay(900);
    setFlowStep('approval');
    addMessage({ type: 'approval_waiting', data: { tool: 'Send Email', risk: 'NON_IDEMPOTENT_WRITE', approver: 'Team Manager', expires: '15:30' } });
  };

  const handleApprovalDone = async () => {
    setFlowStep('running');
    await delay(1200);
    setExecutionSteps(s => s.map(st => ({ ...st, status: 'done' })));
    setFlowStep('done');
    addMessage({
      type: 'result',
      data: {
        status: 'SUCCEEDED',
        summary: '주간 보고서가 생성되어 개발팀 3명에게 이메일을 발송했습니다.',
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

  const agentRequestStatus = flowStep === 'idle' ? null
    : flowStep === 'analyzing' ? 'ANALYZING'
    : flowStep === 'plan' ? 'PLANNING'
    : flowStep === 'confirming' ? 'WAITING_CONFIRMATION'
    : flowStep === 'running' ? 'RUNNING'
    : flowStep === 'approval' ? 'WAITING_APPROVAL'
    : 'SUCCEEDED';

  return (
    <div className="flex h-full" style={{ height: 'calc(100vh - 48px)' }}>
      {/* Conversation column */}
      <div className="flex flex-col flex-1 min-w-0 border-r border-slate-200">
        {/* Agent selector */}
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
                    <div className="flex gap-1 mt-1.5 flex-wrap">
                      {agent.capabilities.map(c => (
                        <span key={c} className="text-[10px] bg-slate-100 text-slate-600 px-1.5 py-0.5 rounded">{c}</span>
                      ))}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Messages */}
        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-3">
          {messages.length === 0 && (
            <div className="flex flex-col items-center justify-center h-full text-center">
              <div className="w-12 h-12 rounded-2xl bg-indigo-100 flex items-center justify-center mb-3">
                <Bot size={22} className="text-indigo-600" />
              </div>
              <p className="text-sm font-medium text-slate-700">무엇을 도와드릴까요?</p>
              <p className="text-xs text-slate-400 mt-1">자연어로 업무를 입력하면 AI가 실행 계획을 생성합니다.</p>
              <div className="mt-4 flex flex-col gap-2 w-full max-w-xs">
                {['주간 보고서를 생성하고 개발팀에 이메일로 발송해줘', '오늘 오후 3시에 팀 회의를 캘린더에 등록해줘', '최신 Q3 시장 분석 문서를 찾아줘'].map(t => (
                  <button key={t} onClick={() => { setInputText(t); }} className="text-left text-xs p-2.5 rounded-lg border border-slate-200 hover:border-indigo-300 hover:bg-indigo-50 text-slate-600 hover:text-indigo-700 transition-colors">
                    {t}
                  </button>
                ))}
              </div>
            </div>
          )}

          {messages.map(msg => (
            <MessageCard key={msg.id} message={msg} onConfirm={handleConfirm} onApprovalAction={handleApprovalDone} />
          ))}
          <div ref={messagesEndRef} />
        </div>

        {/* Input */}
        <div className="border-t border-slate-200 px-4 py-3 bg-white">
          <div className="flex items-end gap-2">
            <textarea
              value={inputText}
              onChange={e => setInputText(e.target.value)}
              onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder="MCPFlow에게 업무를 요청하세요..."
              rows={2}
              disabled={flowStep !== 'idle' && flowStep !== 'done'}
              className="flex-1 resize-none px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent disabled:bg-slate-50 disabled:text-slate-400"
            />
            <Button
              onClick={handleSend}
              icon={<Send size={14} />}
              disabled={!inputText.trim() || (flowStep !== 'idle' && flowStep !== 'done')}
              size="md"
            >
              전송
            </Button>
          </div>
        </div>
      </div>

      {/* Plan/Execution panel */}
      <div className="w-80 flex-shrink-0 flex flex-col bg-slate-50 overflow-y-auto">
        <div className="px-4 py-3 border-b border-slate-200 bg-white">
          <div className="flex items-center justify-between">
            <span className="text-sm font-semibold text-slate-800">Plan / Execution</span>
            {agentRequestStatus && <StatusBadge status={agentRequestStatus} size="sm" />}
          </div>
        </div>

        <div className="flex-1 p-4 space-y-3">
          {flowStep === 'idle' && (
            <div className="text-center py-8 text-sm text-slate-400">실행 정보가 여기에 표시됩니다.</div>
          )}
          {flowStep === 'analyzing' && (
            <div className="flex flex-col items-center py-8 gap-3">
              <Loader2 size={24} className="animate-spin text-indigo-500" />
              <p className="text-sm text-slate-500">요청을 분석하고 있습니다...</p>
            </div>
          )}

          {(flowStep === 'plan' || flowStep === 'confirming' || flowStep === 'running' || flowStep === 'approval' || flowStep === 'done') && (
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <button
                onClick={() => setPlanExpanded(v => !v)}
                className="w-full flex items-center justify-between px-4 py-3 border-b border-slate-100"
              >
                <span className="text-sm font-semibold text-slate-800">Execution Plan</span>
                {planExpanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
              </button>
              {planExpanded && (
                <div className="p-4 space-y-3">
                  <div>
                    <p className="text-xs text-slate-500 mb-1">업무 목적</p>
                    <p className="text-sm text-slate-800">주간 보고서를 생성하고 개발팀에 이메일 발송</p>
                  </div>
                  <div>
                    <p className="text-xs text-slate-500 mb-2">실행 단계 (3 Steps)</p>
                    <div className="space-y-1.5">
                      {['주간 데이터 조회', '보고서 파일 생성', '이메일 발송'].map((s, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm text-slate-700">
                          <span className="w-5 h-5 rounded-full bg-slate-100 text-slate-500 text-xs flex items-center justify-center shrink-0">{i + 1}</span>
                          {s}
                        </div>
                      ))}
                    </div>
                  </div>
                  <div className="flex flex-wrap gap-1.5">
                    <RiskBadge risk="NON_IDEMPOTENT_WRITE" />
                    <span className="text-xs bg-amber-50 text-amber-700 px-2 py-0.5 rounded-full">외부 전송 포함</span>
                    <span className="text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">승인 필요</span>
                  </div>
                </div>
              )}
            </div>
          )}

          {(flowStep === 'running' || flowStep === 'approval' || flowStep === 'done') && executionSteps.length > 0 && (
            <div className="bg-white rounded-xl border border-slate-200 p-4">
              <p className="text-xs font-semibold text-slate-500 mb-3">실행 진행 상황</p>
              <div className="space-y-2">
                {executionSteps.map((step, i) => (
                  <div key={i} className="flex items-center gap-2.5">
                    <span className="shrink-0">
                      {step.status === 'done' && <CheckCircle2 size={15} className="text-green-500" />}
                      {step.status === 'running' && <Loader2 size={15} className="animate-spin text-indigo-500" />}
                      {step.status === 'pending' && <Circle size={15} className="text-slate-300" />}
                      {step.status === 'failed' && <X size={15} className="text-red-500" />}
                    </span>
                    <span className={`text-sm ${step.status === 'done' ? 'text-slate-500 line-through' : step.status === 'running' ? 'text-slate-900 font-medium' : 'text-slate-400'}`}>
                      {step.label}
                    </span>
                  </div>
                ))}
              </div>
              <p className="text-xs text-slate-400 mt-3">
                {executionSteps.filter(s => s.status === 'done').length} / {executionSteps.length} Steps
              </p>
            </div>
          )}

          {flowStep === 'done' && (
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

function MessageCard({ message, onConfirm, onApprovalAction }: { message: Message; onConfirm: () => void; onApprovalAction: () => void }) {
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
    case 'plan': {
      const d = message.data!;
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-indigo-200 rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm">
            <p className="text-xs font-semibold text-indigo-700 mb-2">Execution Plan</p>
            <p className="text-sm text-slate-800 mb-2">{d.goal as string}</p>
            <div className="space-y-1 mb-3">
              {(d.steps as string[]).map((s, i) => (
                <div key={i} className="flex items-center gap-2 text-sm text-slate-600">
                  <span className="text-xs text-slate-400 w-4">{i + 1}.</span>{s}
                </div>
              ))}
            </div>
            <div className="flex flex-wrap gap-1">
              <RiskBadge risk={d.riskClass as string} />
              {Boolean(d.approvalRequired) && <span className="text-xs bg-blue-50 text-blue-700 px-2 py-0.5 rounded-full">승인 필요</span>}
              {Boolean(d.externalTransfer) && <span className="text-xs bg-orange-50 text-orange-700 px-2 py-0.5 rounded-full">외부 전송</span>}
            </div>
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
            <p className="text-xs text-slate-500 mb-3">3개의 Tool이 사용됩니다. 1개의 외부 이메일 발송 작업이 포함됩니다.</p>
            <div className="flex gap-2">
              <Button variant="outline" size="sm">취소</Button>
              <Button size="sm" onClick={onConfirm}>Confirm & Run</Button>
            </div>
          </div>
        </div>
      );
    case 'execution': {
      const steps = message.data?.steps as { label: string; status: string }[];
      return (
        <div className="flex gap-2.5">
          <div className="w-7 h-7 rounded-full bg-indigo-100 flex items-center justify-center shrink-0"><Bot size={14} className="text-indigo-600" /></div>
          <div className="bg-white border border-slate-200 rounded-2xl rounded-tl-sm px-4 py-3">
            <div className="flex items-center gap-2 mb-2">
              <Loader2 size={13} className="animate-spin text-indigo-500" />
              <span className="text-sm font-medium text-slate-800">실행 중</span>
            </div>
            {steps?.map((s, i) => (
              <div key={i} className="text-sm text-slate-500 pl-5">{i + 1}. {s.label}</div>
            ))}
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
            <p className="text-sm font-medium text-amber-800 mb-2">승인을 기다리고 있습니다.</p>
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
      const isSuccess = d.status === 'SUCCEEDED';
      return (
        <div className="flex gap-2.5">
          <div className={`w-7 h-7 rounded-full flex items-center justify-center shrink-0 ${isSuccess ? 'bg-green-100' : 'bg-red-100'}`}>
            {isSuccess ? <CheckCircle2 size={14} className="text-green-600" /> : <X size={14} className="text-red-600" />}
          </div>
          <div className={`border rounded-2xl rounded-tl-sm px-4 py-3 max-w-sm ${isSuccess ? 'bg-green-50 border-green-200' : 'bg-red-50 border-red-200'}`}>
            <p className={`text-sm font-medium mb-1 ${isSuccess ? 'text-green-800' : 'text-red-800'}`}>
              {isSuccess ? '업무가 완료되었습니다.' : '실행 중 오류가 발생했습니다.'}
            </p>
            <p className="text-sm text-slate-700 mb-2">{d.summary as string}</p>
            {d.artifact ? <div className="text-xs font-mono bg-white border border-slate-200 rounded px-2 py-1 text-slate-600">{d.artifact as string}</div> : null}
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
