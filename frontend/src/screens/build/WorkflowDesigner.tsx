import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Plus, GripVertical, Trash2, ArrowDown, GitBranch, Repeat, CheckSquare, Zap } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { RiskBadge } from '../../components/ui/StatusBadge';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockWorkflows, mockWorkflowFull } from '../../data/mock';

type StepType = 'TOOL' | 'PARALLEL' | 'CONDITIONAL' | 'APPROVAL';

interface Step {
  id: string;
  type: StepType;
  name: string;
  tool?: string;
  risk?: string;
}

const STEP_META: Record<StepType, { label: string; icon: React.ReactNode; color: string }> = {
  TOOL: { label: 'Tool Call', icon: <Zap size={14} />, color: 'text-indigo-600 bg-indigo-50' },
  PARALLEL: { label: 'Parallel', icon: <GitBranch size={14} />, color: 'text-cyan-600 bg-cyan-50' },
  CONDITIONAL: { label: 'Conditional', icon: <Repeat size={14} />, color: 'text-amber-600 bg-amber-50' },
  APPROVAL: { label: 'Approval', icon: <CheckSquare size={14} />, color: 'text-green-600 bg-green-50' },
};

export default function WorkflowDesigner() {
  const navigate = useNavigate();
  const { workflowId, versionId } = useParams();

  const workflow = mockWorkflows.find(w => w.id === workflowId) ?? mockWorkflows[0];
  const full = mockWorkflowFull[workflow.id] ?? mockWorkflowFull['wf-001'];

  const [steps, setSteps] = useState<Step[]>(() =>
    full.tools.length > 0
      ? full.tools.map((t, i) => ({ id: `step-${i}`, type: 'TOOL' as StepType, name: t.step, tool: t.toolName, risk: t.riskClass }))
      : [{ id: 'step-0', type: 'TOOL', name: 'Step 1', tool: undefined }]
  );
  const [selected, setSelected] = useState<string | null>(steps[0]?.id ?? null);
  const [saving, setSaving] = useState(false);

  const addStep = (type: StepType) => {
    const id = `step-${Date.now()}`;
    setSteps(prev => [...prev, { id, type, name: `${STEP_META[type].label} ${prev.length + 1}` }]);
    setSelected(id);
  };

  const removeStep = (id: string) => {
    setSteps(prev => prev.filter(s => s.id !== id));
    if (selected === id) setSelected(null);
  };

  const handleSave = async () => {
    setSaving(true);
    await new Promise(r => setTimeout(r, 900));
    setSaving(false);
    navigate(`/workflows/${workflow.id}`);
  };

  const selectedStep = steps.find(s => s.id === selected) ?? null;

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        breadcrumbs={[
          { label: 'Workflows', to: '/workflows' },
          { label: workflow.name, to: `/workflows/${workflow.id}` },
          { label: `Designer (${versionId})` },
        ]}
        title="Workflow Designer"
        description={`${workflow.name} · ${steps.length} steps`}
        actions={
          <>
            <Button variant="outline" onClick={() => navigate(`/workflows/${workflow.id}`)}>취소</Button>
            <Button loading={saving} onClick={handleSave}>Draft 저장</Button>
          </>
        }
      />

      <div className="flex flex-1 min-h-0">
        {/* Palette */}
        <div className="w-52 border-r border-slate-200 bg-white p-3 space-y-2 shrink-0">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Step 추가</p>
          {(Object.keys(STEP_META) as StepType[]).map(type => (
            <button
              key={type}
              onClick={() => addStep(type)}
              className="w-full flex items-center gap-2 px-3 py-2 rounded-lg border border-slate-200 text-sm text-slate-700 hover:bg-slate-50 transition-colors"
            >
              <span className={`w-6 h-6 rounded-md flex items-center justify-center ${STEP_META[type].color}`}>{STEP_META[type].icon}</span>
              <span className="flex-1 text-left">{STEP_META[type].label}</span>
              <Plus size={13} className="text-slate-400" />
            </button>
          ))}
        </div>

        {/* Canvas */}
        <div className="flex-1 overflow-auto bg-slate-50 p-6">
          <div className="max-w-md mx-auto space-y-2">
            <InlineAlert type="info" message="Step을 클릭하여 상세 설정을 편집합니다. 위에서 아래로 순차 실행되며, Parallel/Conditional로 흐름을 분기할 수 있습니다." />
            {steps.map((step, i) => (
              <div key={step.id}>
                <div
                  onClick={() => setSelected(step.id)}
                  className={`bg-white rounded-xl border p-3 flex items-center gap-3 cursor-pointer transition-all
                    ${selected === step.id ? 'border-indigo-400 ring-2 ring-indigo-100' : 'border-slate-200 hover:border-slate-300'}`}
                >
                  <GripVertical size={14} className="text-slate-300 shrink-0" />
                  <span className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${STEP_META[step.type].color}`}>
                    {STEP_META[step.type].icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-slate-800 truncate">{step.name}</div>
                    <div className="text-xs text-slate-400">
                      {STEP_META[step.type].label}{step.tool ? ` · ${step.tool}` : ''}
                    </div>
                  </div>
                  {step.risk && <RiskBadge risk={step.risk} />}
                  <button
                    onClick={e => { e.stopPropagation(); removeStep(step.id); }}
                    className="p-1.5 rounded text-slate-300 hover:text-red-500 hover:bg-red-50 shrink-0"
                  >
                    <Trash2 size={13} />
                  </button>
                </div>
                {i < steps.length - 1 && (
                  <div className="flex justify-center py-1"><ArrowDown size={14} className="text-slate-300" /></div>
                )}
              </div>
            ))}
            {steps.length === 0 && (
              <div className="text-center text-sm text-slate-400 py-12">왼쪽 팔레트에서 Step을 추가하세요.</div>
            )}
          </div>
        </div>

        {/* Inspector */}
        <div className="w-72 border-l border-slate-200 bg-white p-4 shrink-0 overflow-auto">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">Step 설정</p>
          {selectedStep ? (
            <div className="space-y-4">
              <Field label="Step 이름">
                <input
                  value={selectedStep.name}
                  onChange={e => setSteps(prev => prev.map(s => s.id === selectedStep.id ? { ...s, name: e.target.value } : s))}
                  className={inputClass}
                />
              </Field>
              <Field label="Type">
                <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm ${STEP_META[selectedStep.type].color}`}>
                  {STEP_META[selectedStep.type].icon} {STEP_META[selectedStep.type].label}
                </div>
              </Field>
              {selectedStep.type === 'TOOL' && (
                <Field label="Tool">
                  <input
                    value={selectedStep.tool ?? ''}
                    placeholder="Tool 선택..."
                    onChange={e => setSteps(prev => prev.map(s => s.id === selectedStep.id ? { ...s, tool: e.target.value } : s))}
                    className={inputClass}
                  />
                </Field>
              )}
              <Field label="Retry Policy">
                <select className={inputClass}>
                  <option>NONE</option>
                  <option>RETRY (max 3)</option>
                  <option>RETRY_BACKOFF</option>
                </select>
              </Field>
            </div>
          ) : (
            <p className="text-sm text-slate-400">Step을 선택하세요.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white';
