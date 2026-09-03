import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import {
  Plus, Trash2, ArrowDown, GitBranch, Split, Repeat, CheckSquare, Zap, Circle,
  AlertTriangle, Link2,
} from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { RiskBadge } from '../../components/ui/StatusBadge';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockWorkflows, mockWorkflowFull, mockTools, mockApprovalPolicies } from '../../data/mock';

/** Canonical authorable Step Types (Execution Plan v1). No PARALLEL / CONDITIONAL / End as domain types. */
type StepType = 'TOOL' | 'CONDITION' | 'JOIN' | 'APPROVAL' | 'LOOP';
type BindingKind = 'LITERAL' | 'PLAN_INPUT' | 'STEP_OUTPUT' | 'EXECUTION_CONTEXT' | 'LOOP_CONTEXT' | 'SECRET_REF';
type JoinPolicy = 'ALL_SUCCESS' | 'ALL_COMPLETE' | 'ANY_SUCCESS';
type LoopMode = 'FOR_EACH' | 'WHILE';
type PredicateOp = 'eq' | 'ne' | 'gt' | 'gte' | 'lt' | 'lte' | 'in' | 'contains' | 'exists' | 'is_null' | 'and' | 'or' | 'not';
type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';
type ValidationCode =
  | 'Circular Dependency'
  | 'Missing Tool'
  | 'Inactive Tool'
  | 'Invalid Tool Version'
  | 'Invalid Binding'
  | 'Missing Required Input'
  | 'Invalid Predicate'
  | 'Missing Approval Policy'
  | 'Loop Without Limit'
  | 'Disconnected Node';

interface BindingValue {
  kind: BindingKind;
  value?: string;
  path?: string;
  stepId?: string;
  secretRef?: string;
}

interface PredicateClause {
  left: BindingValue;
  op: PredicateOp;
  right: BindingValue;
}

interface Step {
  id: string;
  type: StepType;
  name: string;
  dependsOn: string[];
  toolId?: string;
  toolVersion?: string;
  risk?: string;
  joinPolicy?: JoinPolicy;
  approvalPolicyId?: string;
  loopMode?: LoopMode;
  maxIterations?: number;
  loopCollection?: BindingValue;
  predicate?: PredicateClause;
  bindings?: Record<string, BindingValue>;
}

type PaletteItem =
  | { kind: 'step'; type: StepType; label: string; icon: React.ReactNode; color: string }
  | { kind: 'visual'; id: 'PARALLEL' | 'END'; label: string; hint: string; icon: React.ReactNode; color: string };

const STEP_META: Record<StepType, { label: string; icon: React.ReactNode; color: string }> = {
  TOOL: { label: 'Tool', icon: <Zap size={14} />, color: 'text-indigo-600 bg-indigo-50' },
  CONDITION: { label: 'Condition', icon: <Split size={14} />, color: 'text-amber-600 bg-amber-50' },
  JOIN: { label: 'Join', icon: <GitBranch size={14} />, color: 'text-cyan-600 bg-cyan-50' },
  APPROVAL: { label: 'Approval', icon: <CheckSquare size={14} />, color: 'text-green-600 bg-green-50' },
  LOOP: { label: 'Loop', icon: <Repeat size={14} />, color: 'text-violet-600 bg-violet-50' },
};

const PALETTE: PaletteItem[] = [
  { kind: 'step', type: 'TOOL', label: 'Tool', icon: <Zap size={14} />, color: 'text-indigo-600 bg-indigo-50' },
  { kind: 'step', type: 'CONDITION', label: 'Condition', icon: <Split size={14} />, color: 'text-amber-600 bg-amber-50' },
  { kind: 'visual', id: 'PARALLEL', label: 'Parallel / Join', hint: 'JOIN step 추가 (PARALLEL 타입 아님)', icon: <GitBranch size={14} />, color: 'text-cyan-600 bg-cyan-50' },
  { kind: 'step', type: 'APPROVAL', label: 'Approval', icon: <CheckSquare size={14} />, color: 'text-green-600 bg-green-50' },
  { kind: 'step', type: 'LOOP', label: 'Loop', icon: <Repeat size={14} />, color: 'text-violet-600 bg-violet-50' },
  { kind: 'visual', id: 'END', label: 'End', hint: '시각적 종료 표현 (persisted type 아님)', icon: <Circle size={14} />, color: 'text-slate-500 bg-slate-100' },
];

const PREDICATE_OPS: PredicateOp[] = ['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'in', 'contains', 'exists', 'is_null', 'and', 'or', 'not'];
const BINDING_KINDS: BindingKind[] = ['LITERAL', 'PLAN_INPUT', 'STEP_OUTPUT', 'EXECUTION_CONTEXT', 'LOOP_CONTEXT', 'SECRET_REF'];
const JOIN_POLICIES: JoinPolicy[] = ['ALL_SUCCESS', 'ALL_COMPLETE', 'ANY_SUCCESS'];

const defaultBinding = (): BindingValue => ({ kind: 'LITERAL', value: '' });
const defaultPredicate = (): PredicateClause => ({
  left: { kind: 'STEP_OUTPUT', stepId: '', path: '' },
  op: 'eq',
  right: { kind: 'LITERAL', value: '' },
});

function createStep(type: StepType, index: number, dependsOn: string[] = []): Step {
  const base: Step = {
    id: `step-${Date.now()}-${Math.floor(Math.random() * 1000)}`,
    type,
    name: `${STEP_META[type].label} ${index + 1}`,
    dependsOn,
  };
  if (type === 'JOIN') return { ...base, joinPolicy: 'ALL_SUCCESS' };
  if (type === 'APPROVAL') return { ...base, approvalPolicyId: mockApprovalPolicies[0]?.id };
  if (type === 'LOOP') {
    return {
      ...base,
      loopMode: 'FOR_EACH',
      maxIterations: 10,
      loopCollection: { kind: 'PLAN_INPUT', path: '/items' },
      predicate: defaultPredicate(),
    };
  }
  if (type === 'CONDITION') return { ...base, predicate: defaultPredicate() };
  if (type === 'TOOL') {
    return {
      ...base,
      bindings: { input: defaultBinding() },
    };
  }
  return base;
}

export default function WorkflowDesigner() {
  const navigate = useNavigate();
  const { workflowId, versionId } = useParams();
  const isNew = !versionId || versionId === 'new';

  const workflow = mockWorkflows.find(w => w.id === workflowId) ?? mockWorkflows[0];
  const full = mockWorkflowFull[workflow.id] ?? mockWorkflowFull['wf-001'];
  const existing = !isNew ? full.versions.find(v => v.version === versionId) : undefined;
  const versionStatus = (isNew ? 'DRAFT' : (existing?.status ?? 'DRAFT')) as VersionStatus;
  const readOnly = versionStatus === 'DEPRECATED';
  const publishedBlocked = versionStatus === 'PUBLISHED';

  const [steps, setSteps] = useState<Step[]>(() => {
    if (full.tools.length > 0) {
      return full.tools.map((t, i) => ({
        id: `step-${i}`,
        type: 'TOOL' as StepType,
        name: t.step,
        toolId: t.toolId,
        toolVersion: t.version,
        risk: t.riskClass,
        dependsOn: i === 0 ? [] : [`step-${i - 1}`],
        bindings: { input: { kind: 'PLAN_INPUT' as BindingKind, path: '/input' } },
      }));
    }
    return [createStep('TOOL', 0)];
  });
  const [selected, setSelected] = useState<string | null>(steps[0]?.id ?? null);
  const [showEndMarker, setShowEndMarker] = useState(true);
  const [saving, setSaving] = useState(false);

  const updateStep = (id: string, patch: Partial<Step>) => {
    if (readOnly || publishedBlocked) return;
    setSteps(prev => prev.map(s => (s.id === id ? { ...s, ...patch } : s)));
  };

  const addStep = (type: StepType) => {
    if (readOnly || publishedBlocked) return;
    const prevId = steps[steps.length - 1]?.id;
    const step = createStep(type, steps.length, prevId ? [prevId] : []);
    setSteps(prev => [...prev, step]);
    setSelected(step.id);
  };

  const removeStep = (id: string) => {
    if (readOnly || publishedBlocked) return;
    setSteps(prev =>
      prev
        .filter(s => s.id !== id)
        .map(s => ({ ...s, dependsOn: s.dependsOn.filter(d => d !== id) })),
    );
    if (selected === id) setSelected(null);
  };

  const handlePalette = (item: PaletteItem) => {
    if (readOnly || publishedBlocked) return;
    if (item.kind === 'step') {
      addStep(item.type);
      return;
    }
    if (item.id === 'PARALLEL') {
      addStep('JOIN');
      return;
    }
    if (item.id === 'END') {
      setShowEndMarker(true);
    }
  };

  const validations = useMemo(() => validatePlan(steps), [steps]);

  const handleSave = async () => {
    setSaving(true);
    await new Promise(r => setTimeout(r, 800));
    setSaving(false);
    navigate(`/workflows/${workflow.id}`);
  };

  const selectedStep = steps.find(s => s.id === selected) ?? null;

  if (publishedBlocked) {
    return (
      <div>
        <PageHeader
          breadcrumbs={[
            { label: 'Workflows', to: '/workflows' },
            { label: workflow.name, to: `/workflows/${workflow.id}` },
            { label: `${versionId}` },
          ]}
          title={`Workflow Version ${versionId}`}
          actions={<Button variant="outline" onClick={() => navigate(`/workflows/${workflow.id}`)}>닫기</Button>}
        />
        <div className="p-6 max-w-2xl space-y-4">
          <InlineAlert type="warning" message="Published versions cannot be edited" />
          <div className="bg-white rounded-xl border border-slate-200 p-6 space-y-3">
            <p className="text-sm text-slate-600">
              PUBLISHED Workflow Version의 Execution Plan은 불변입니다. 변경이 필요하면 새 DRAFT Version을 생성하세요.
            </p>
            <Button icon={<Plus size={13} />} onClick={() => navigate(`/workflows/${workflow.id}/versions/new/edit`)}>
              Create New Draft Version
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      <PageHeader
        breadcrumbs={[
          { label: 'Workflows', to: '/workflows' },
          { label: workflow.name, to: `/workflows/${workflow.id}` },
          { label: readOnly ? `View (${versionId})` : `Designer (${versionId})` },
        ]}
        title={readOnly ? 'Workflow Designer (View)' : 'Workflow Designer'}
        description={`${workflow.name} · ${steps.length} steps · Execution Plan v1`}
        actions={
          <>
            <Button variant="outline" onClick={() => navigate(`/workflows/${workflow.id}`)}>{readOnly ? '닫기' : '취소'}</Button>
            {!readOnly && <Button loading={saving} onClick={handleSave}>Draft 저장</Button>}
            {readOnly && (
              <Button icon={<Plus size={13} />} onClick={() => navigate(`/workflows/${workflow.id}/versions/new/edit`)}>
                Create New Draft Version
              </Button>
            )}
          </>
        }
      />

      <div className="flex flex-1 min-h-0">
        {/* Palette */}
        <div className="w-56 border-r border-slate-200 bg-white p-3 space-y-2 shrink-0 overflow-auto">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-1">Step Palette</p>
          {PALETTE.map(item => (
            <button
              key={item.kind === 'step' ? item.type : item.id}
              onClick={() => handlePalette(item)}
              disabled={readOnly}
              className="w-full flex items-start gap-2 px-3 py-2 rounded-lg border border-slate-200 text-sm text-slate-700 hover:bg-slate-50 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            >
              <span className={`w-6 h-6 rounded-md flex items-center justify-center shrink-0 mt-0.5 ${item.color}`}>
                {item.icon}
              </span>
              <span className="flex-1 text-left">
                <span className="block">{item.label}</span>
                {item.kind === 'visual' && (
                  <span className="block text-[10px] text-slate-400 mt-0.5 leading-snug">{item.hint}</span>
                )}
              </span>
              <Plus size={13} className="text-slate-400 mt-1" />
            </button>
          ))}
          <p className="text-[10px] text-slate-400 pt-2 leading-relaxed">
            Canonical types: TOOL, CONDITION, JOIN, APPROVAL, LOOP. Parallel은 UI 편의이며 JOIN으로 저장됩니다.
          </p>
        </div>

        {/* Canvas */}
        <div className="flex-1 overflow-auto bg-slate-50 p-6">
          <div className="max-w-lg mx-auto space-y-2">
            {readOnly && (
              <InlineAlert type="info" message="DEPRECATED Version은 View만 가능합니다." />
            )}
            <InlineAlert
              type="info"
              message="실행 순서는 Canvas 위치가 아니라 Step dependency / edge(depends_on)로 결정됩니다. 임의 JavaScript/Python expression은 사용할 수 없습니다."
            />
            {steps.map((step, i) => (
              <div key={step.id}>
                <div
                  onClick={() => setSelected(step.id)}
                  className={`bg-white rounded-xl border p-3 flex items-center gap-3 cursor-pointer transition-all
                    ${selected === step.id ? 'border-indigo-400 ring-2 ring-indigo-100' : 'border-slate-200 hover:border-slate-300'}`}
                >
                  <Link2 size={14} className="text-slate-300 shrink-0" />
                  <span className={`w-8 h-8 rounded-lg flex items-center justify-center shrink-0 ${STEP_META[step.type].color}`}>
                    {STEP_META[step.type].icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium text-slate-800 truncate">{step.name}</div>
                    <div className="text-xs text-slate-400">
                      {STEP_META[step.type].label}
                      {step.toolId ? ` · ${mockTools.find(t => t.id === step.toolId)?.displayName ?? step.toolId}` : ''}
                      {step.dependsOn.length > 0 ? ` · depends_on: ${step.dependsOn.join(', ')}` : ' · root'}
                    </div>
                  </div>
                  {step.risk && <RiskBadge risk={step.risk} />}
                  {!readOnly && (
                    <button
                      onClick={e => { e.stopPropagation(); removeStep(step.id); }}
                      className="p-1.5 rounded text-slate-300 hover:text-red-500 hover:bg-red-50 shrink-0"
                    >
                      <Trash2 size={13} />
                    </button>
                  )}
                </div>
                {i < steps.length - 1 && (
                  <div className="flex justify-center py-1"><ArrowDown size={14} className="text-slate-300" /></div>
                )}
              </div>
            ))}
            {showEndMarker && steps.length > 0 && (
              <>
                <div className="flex justify-center py-1"><ArrowDown size={14} className="text-slate-300" /></div>
                <div className="bg-white/70 rounded-xl border border-dashed border-slate-300 p-3 flex items-center gap-3 text-slate-500">
                  <span className="w-8 h-8 rounded-lg flex items-center justify-center bg-slate-100">
                    <Circle size={14} />
                  </span>
                  <div>
                    <div className="text-sm font-medium">End</div>
                    <div className="text-xs text-slate-400">시각적 종료 표현 · Canonical Step Type 아님</div>
                  </div>
                </div>
              </>
            )}
            {steps.length === 0 && (
              <div className="text-center text-sm text-slate-400 py-12">왼쪽 팔레트에서 Step을 추가하세요.</div>
            )}

            {/* Validation Results */}
            <div className="mt-6 bg-white rounded-xl border border-slate-200 p-4">
              <div className="flex items-center gap-2 mb-3">
                <AlertTriangle size={14} className={validations.length ? 'text-amber-500' : 'text-slate-400'} />
                <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide">
                  Validation Results ({validations.length})
                </p>
              </div>
              {validations.length === 0 ? (
                <p className="text-sm text-slate-500">Blocking validation issue가 없습니다.</p>
              ) : (
                <ul className="space-y-1.5">
                  {validations.map((v, idx) => (
                    <li key={`${v.code}-${v.stepId}-${idx}`}>
                      <button
                        type="button"
                        onClick={() => v.stepId && setSelected(v.stepId)}
                        className="w-full text-left px-2.5 py-2 rounded-lg border border-red-100 bg-red-50/60 hover:bg-red-50 text-sm"
                      >
                        <span className="font-medium text-red-700">{v.code}</span>
                        <span className="text-red-600/80"> — {v.message}</span>
                        {v.stepId && <span className="block text-[10px] text-red-400 mt-0.5 font-mono">{v.stepId}</span>}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>

        {/* Inspector */}
        <div className="w-80 border-l border-slate-200 bg-white p-4 shrink-0 overflow-auto">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">Step 설정</p>
          {selectedStep ? (
            <div className="space-y-4">
              <Field label="Step 이름">
                <input
                  value={selectedStep.name}
                  disabled={readOnly}
                  onChange={e => updateStep(selectedStep.id, { name: e.target.value })}
                  className={inputClass}
                />
              </Field>
              <Field label="Type (Canonical)">
                <div className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md text-sm ${STEP_META[selectedStep.type].color}`}>
                  {STEP_META[selectedStep.type].icon} {STEP_META[selectedStep.type].label}
                </div>
              </Field>
              <Field label="depends_on">
                <select
                  multiple
                  disabled={readOnly}
                  value={selectedStep.dependsOn}
                  onChange={e => {
                    const values = Array.from(e.target.selectedOptions).map(o => o.value);
                    updateStep(selectedStep.id, { dependsOn: values });
                  }}
                  className={`${inputClass} h-24 py-1`}
                >
                  {steps.filter(s => s.id !== selectedStep.id).map(s => (
                    <option key={s.id} value={s.id}>{s.name} ({s.id})</option>
                  ))}
                </select>
                <p className="text-[10px] text-slate-400 mt-1">Ctrl/Cmd로 다중 선택. 실행 의미는 edge에 의해 결정됩니다.</p>
              </Field>

              {selectedStep.type === 'TOOL' && (
                <>
                  <Field label="Tool">
                    <select
                      value={selectedStep.toolId ?? ''}
                      disabled={readOnly}
                      onChange={e => {
                        const tool = mockTools.find(t => t.id === e.target.value);
                        updateStep(selectedStep.id, {
                          toolId: tool?.id,
                          toolVersion: tool?.currentVersion,
                          risk: tool?.riskClass,
                        });
                      }}
                      className={inputClass}
                    >
                      <option value="">Tool 선택...</option>
                      {mockTools.map(t => (
                        <option key={t.id} value={t.id}>{t.displayName} ({t.serverName})</option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Tool Version">
                    <input
                      value={selectedStep.toolVersion ?? ''}
                      disabled={readOnly}
                      onChange={e => updateStep(selectedStep.id, { toolVersion: e.target.value })}
                      className={inputClass}
                    />
                  </Field>
                  <BindingEditor
                    label="Input Binding"
                    value={selectedStep.bindings?.input ?? defaultBinding()}
                    steps={steps}
                    disabled={readOnly}
                    onChange={binding => updateStep(selectedStep.id, {
                      bindings: { ...(selectedStep.bindings ?? {}), input: binding },
                    })}
                  />
                </>
              )}

              {selectedStep.type === 'CONDITION' && (
                <PredicateBuilder
                  value={selectedStep.predicate ?? defaultPredicate()}
                  steps={steps}
                  disabled={readOnly}
                  onChange={predicate => updateStep(selectedStep.id, { predicate })}
                />
              )}

              {selectedStep.type === 'JOIN' && (
                <Field label="Join Policy">
                  <select
                    value={selectedStep.joinPolicy ?? 'ALL_SUCCESS'}
                    disabled={readOnly}
                    onChange={e => updateStep(selectedStep.id, { joinPolicy: e.target.value as JoinPolicy })}
                    className={inputClass}
                  >
                    {JOIN_POLICIES.map(p => <option key={p} value={p}>{p}</option>)}
                  </select>
                </Field>
              )}

              {selectedStep.type === 'APPROVAL' && (
                <Field label="Approval Policy">
                  <select
                    value={selectedStep.approvalPolicyId ?? ''}
                    disabled={readOnly}
                    onChange={e => updateStep(selectedStep.id, { approvalPolicyId: e.target.value })}
                    className={inputClass}
                  >
                    <option value="">Policy 선택...</option>
                    {mockApprovalPolicies.map(p => (
                      <option key={p.id} value={p.id}>{p.name} ({p.status})</option>
                    ))}
                  </select>
                </Field>
              )}

              {selectedStep.type === 'LOOP' && (
                <>
                  <Field label="Loop Mode">
                    <select
                      value={selectedStep.loopMode ?? 'FOR_EACH'}
                      disabled={readOnly}
                      onChange={e => updateStep(selectedStep.id, { loopMode: e.target.value as LoopMode })}
                      className={inputClass}
                    >
                      <option value="FOR_EACH">FOR_EACH</option>
                      <option value="WHILE">WHILE</option>
                    </select>
                  </Field>
                  <Field label="max_iterations (required)">
                    <input
                      type="number"
                      min={1}
                      value={selectedStep.maxIterations ?? ''}
                      disabled={readOnly}
                      onChange={e => updateStep(selectedStep.id, {
                        maxIterations: e.target.value === '' ? undefined : Number(e.target.value),
                      })}
                      className={inputClass}
                    />
                    <p className="text-[10px] text-slate-400 mt-1">무제한 Loop는 허용되지 않습니다.</p>
                  </Field>
                  {selectedStep.loopMode === 'FOR_EACH' && (
                    <BindingEditor
                      label="Collection Binding"
                      value={selectedStep.loopCollection ?? { kind: 'PLAN_INPUT', path: '/items' }}
                      steps={steps}
                      disabled={readOnly}
                      onChange={loopCollection => updateStep(selectedStep.id, { loopCollection })}
                    />
                  )}
                  {selectedStep.loopMode === 'WHILE' && (
                    <PredicateBuilder
                      value={selectedStep.predicate ?? defaultPredicate()}
                      steps={steps}
                      disabled={readOnly}
                      onChange={predicate => updateStep(selectedStep.id, { predicate })}
                    />
                  )}
                </>
              )}
            </div>
          ) : (
            <p className="text-sm text-slate-400">Step을 선택하세요.</p>
          )}
        </div>
      </div>
    </div>
  );
}

function BindingEditor({
  label, value, steps, disabled, onChange,
}: {
  label: string;
  value: BindingValue;
  steps: Step[];
  disabled?: boolean;
  onChange: (v: BindingValue) => void;
}) {
  return (
    <div className="space-y-2 border border-slate-200 rounded-lg p-3">
      <p className="text-xs font-medium text-slate-600">{label}</p>
      <Field label="Binding Kind">
        <select
          value={value.kind}
          disabled={disabled}
          onChange={e => onChange({ ...value, kind: e.target.value as BindingKind })}
          className={inputClass}
        >
          {BINDING_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
        </select>
      </Field>
      {value.kind === 'LITERAL' && (
        <Field label="Value">
          <input value={value.value ?? ''} disabled={disabled} onChange={e => onChange({ ...value, value: e.target.value })} className={inputClass} />
        </Field>
      )}
      {(value.kind === 'PLAN_INPUT' || value.kind === 'EXECUTION_CONTEXT' || value.kind === 'LOOP_CONTEXT') && (
        <Field label="JSON Pointer path">
          <input value={value.path ?? ''} disabled={disabled} placeholder="/field" onChange={e => onChange({ ...value, path: e.target.value })} className={inputClass} />
        </Field>
      )}
      {value.kind === 'STEP_OUTPUT' && (
        <>
          <Field label="Step">
            <select value={value.stepId ?? ''} disabled={disabled} onChange={e => onChange({ ...value, stepId: e.target.value })} className={inputClass}>
              <option value="">Step 선택...</option>
              {steps.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </Field>
          <Field label="path">
            <input value={value.path ?? ''} disabled={disabled} placeholder="/structuredContent/..." onChange={e => onChange({ ...value, path: e.target.value })} className={inputClass} />
          </Field>
        </>
      )}
      {value.kind === 'SECRET_REF' && (
        <Field label="Secret Ref">
          <input value={value.secretRef ?? ''} disabled={disabled} placeholder="secret://..." onChange={e => onChange({ ...value, secretRef: e.target.value })} className={inputClass} />
        </Field>
      )}
      <p className="text-[10px] text-slate-400">임의 expression은 허용되지 않습니다. Canonical Binding만 사용합니다.</p>
    </div>
  );
}

function PredicateBuilder({
  value, steps, disabled, onChange,
}: {
  value: PredicateClause;
  steps: Step[];
  disabled?: boolean;
  onChange: (v: PredicateClause) => void;
}) {
  const unary = value.op === 'exists' || value.op === 'is_null' || value.op === 'not';
  return (
    <div className="space-y-3 border border-slate-200 rounded-lg p-3">
      <p className="text-xs font-medium text-slate-600">Predicate Builder</p>
      <InlineAlert type="info" message="JavaScript/Python Editor는 사용할 수 없습니다. 제한된 Predicate AST만 지원합니다." />
      <BindingEditor
        label="Left operand"
        value={value.left}
        steps={steps}
        disabled={disabled}
        onChange={left => onChange({ ...value, left })}
      />
      <Field label="Operator">
        <select
          value={value.op}
          disabled={disabled}
          onChange={e => onChange({ ...value, op: e.target.value as PredicateOp })}
          className={inputClass}
        >
          {PREDICATE_OPS.map(op => <option key={op} value={op}>{op}</option>)}
        </select>
      </Field>
      {!unary && (
        <BindingEditor
          label="Right operand"
          value={value.right}
          steps={steps}
          disabled={disabled}
          onChange={right => onChange({ ...value, right })}
        />
      )}
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

function isBindingInvalid(b?: BindingValue): boolean {
  if (!b) return true;
  if (b.kind === 'LITERAL') return b.value === undefined;
  if (b.kind === 'SECRET_REF') return !b.secretRef?.trim();
  if (b.kind === 'STEP_OUTPUT') return !b.stepId || !b.path?.trim();
  return !b.path?.trim();
}

function validatePlan(steps: Step[]): { code: ValidationCode; message: string; stepId?: string }[] {
  const issues: { code: ValidationCode; message: string; stepId?: string }[] = [];
  const ids = new Set(steps.map(s => s.id));

  // Circular dependency (DFS)
  const visiting = new Set<string>();
  const visited = new Set<string>();
  const cycleNodes = new Set<string>();
  const dfs = (id: string, stack: string[]) => {
    if (visiting.has(id)) {
      stack.slice(stack.indexOf(id)).forEach(n => cycleNodes.add(n));
      return;
    }
    if (visited.has(id)) return;
    visiting.add(id);
    const step = steps.find(s => s.id === id);
    step?.dependsOn.forEach(dep => dfs(dep, [...stack, id]));
    visiting.delete(id);
    visited.add(id);
  };
  steps.forEach(s => dfs(s.id, []));
  cycleNodes.forEach(id => {
    issues.push({ code: 'Circular Dependency', message: 'depends_on에 순환이 있습니다.', stepId: id });
  });

  // Reachability / disconnected
  const roots = steps.filter(s => s.dependsOn.length === 0);
  const reachable = new Set<string>();
  const walk = (id: string) => {
    if (reachable.has(id)) return;
    reachable.add(id);
    steps.filter(s => s.dependsOn.includes(id)).forEach(s => walk(s.id));
  };
  roots.forEach(r => walk(r.id));
  if (steps.length > 0 && roots.length === 0) {
    steps.forEach(s => {
      issues.push({ code: 'Disconnected Node', message: 'root Step가 없고 dependency만 있습니다.', stepId: s.id });
    });
  }
  steps.forEach(s => {
    if (!reachable.has(s.id) && roots.length > 0) {
      issues.push({ code: 'Disconnected Node', message: '어떤 root에서도 도달할 수 없습니다.', stepId: s.id });
    }
    s.dependsOn.forEach(dep => {
      if (!ids.has(dep)) {
        issues.push({ code: 'Invalid Binding', message: `존재하지 않는 dependency: ${dep}`, stepId: s.id });
      }
    });
  });

  steps.forEach(s => {
    if (s.type === 'TOOL') {
      if (!s.toolId) {
        issues.push({ code: 'Missing Tool', message: 'Tool이 선택되지 않았습니다.', stepId: s.id });
      } else {
        const tool = mockTools.find(t => t.id === s.toolId);
        if (!tool) {
          issues.push({ code: 'Missing Tool', message: '등록되지 않은 Tool입니다.', stepId: s.id });
        } else {
          if (tool.status !== 'ACTIVE') {
            issues.push({ code: 'Inactive Tool', message: `${tool.displayName} 상태가 ${tool.status}입니다.`, stepId: s.id });
          }
          if (s.toolVersion && s.toolVersion !== tool.currentVersion) {
            issues.push({
              code: 'Invalid Tool Version',
              message: `요청 버전 ${s.toolVersion} ≠ current ${tool.currentVersion}`,
              stepId: s.id,
            });
          }
          if (tool.validation !== 'VALID') {
            issues.push({
              code: 'Invalid Tool Version',
              message: `Tool validation 상태: ${tool.validation}`,
              stepId: s.id,
            });
          }
        }
      }
      if (isBindingInvalid(s.bindings?.input)) {
        issues.push({ code: 'Invalid Binding', message: 'Tool input binding이 불완전합니다.', stepId: s.id });
      }
      if (s.bindings?.input?.kind === 'PLAN_INPUT' && !s.bindings.input.path) {
        issues.push({ code: 'Missing Required Input', message: 'PLAN_INPUT path가 필요합니다.', stepId: s.id });
      }
    }

    if (s.type === 'CONDITION') {
      const p = s.predicate;
      if (!p || !PREDICATE_OPS.includes(p.op) || isBindingInvalid(p.left)) {
        issues.push({ code: 'Invalid Predicate', message: 'Predicate AST가 불완전하거나 허용되지 않습니다.', stepId: s.id });
      } else if (p.op !== 'exists' && p.op !== 'is_null' && p.op !== 'not' && isBindingInvalid(p.right)) {
        issues.push({ code: 'Invalid Predicate', message: 'Right operand binding이 필요합니다.', stepId: s.id });
      }
    }

    if (s.type === 'APPROVAL' && !s.approvalPolicyId) {
      issues.push({ code: 'Missing Approval Policy', message: 'approval_policy_id가 필요합니다.', stepId: s.id });
    }

    if (s.type === 'LOOP') {
      if (!s.maxIterations || s.maxIterations < 1) {
        issues.push({ code: 'Loop Without Limit', message: 'max_iterations는 필수이며 1 이상이어야 합니다.', stepId: s.id });
      }
      if (s.loopMode === 'FOR_EACH' && isBindingInvalid(s.loopCollection)) {
        issues.push({ code: 'Invalid Binding', message: 'FOR_EACH collection binding이 필요합니다.', stepId: s.id });
      }
      if (s.loopMode === 'WHILE') {
        const p = s.predicate;
        if (!p || isBindingInvalid(p.left)) {
          issues.push({ code: 'Invalid Predicate', message: 'WHILE predicate가 필요합니다.', stepId: s.id });
        }
      }
    }

    if (s.type === 'JOIN' && s.dependsOn.length < 1) {
      issues.push({ code: 'Disconnected Node', message: 'JOIN은 하나 이상의 upstream dependency가 필요합니다.', stepId: s.id });
    }
  });

  return issues;
}

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white disabled:bg-slate-50 disabled:text-slate-500';
