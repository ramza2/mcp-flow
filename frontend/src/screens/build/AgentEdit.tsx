import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Plus, Search, X } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';
import { mockAgents, mockAgentFull, mockTools, mockModelProfiles, mockMCPServers } from '../../data/mock';

type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';

const CAPABILITY_BY_TOOL: Record<string, string> = {
  'tool-001': 'weather.lookup',
  'tool-002': 'document.search',
  'tool-003': 'report.generate',
  'tool-004': 'email.send',
  'tool-005': 'calendar.write',
  'tool-006': 'hr.lookup',
  'tool-007': 'data.delete',
  'tool-008': 'weather.forecast',
};

const RISK_OPTIONS = ['READ_ONLY', 'IDEMPOTENT_WRITE', 'NON_IDEMPOTENT_WRITE', 'DESTRUCTIVE', 'UNKNOWN'];

export default function AgentEdit() {
  const navigate = useNavigate();
  const { agentId, versionId } = useParams();
  const isNew = !versionId || versionId === 'new';

  const agent = mockAgents.find(a => a.id === agentId) ?? mockAgents[0];
  const full = mockAgentFull[agent.id] ?? mockAgentFull['agt-001'];
  const existing = !isNew ? full.versions.find(v => v.version === versionId) : undefined;
  const versionStatus = (isNew ? 'DRAFT' : (existing?.status ?? 'DRAFT')) as VersionStatus;
  const readOnly = versionStatus === 'DEPRECATED';
  const publishedBlocked = versionStatus === 'PUBLISHED';

  const [name, setName] = useState(agent.name);
  const [purpose, setPurpose] = useState(full.purpose);
  const [visibility, setVisibility] = useState(full.visibility);
  const [modelProfile, setModelProfile] = useState(agent.modelProfile);
  const [instructions, setInstructions] = useState(full.instructions);
  const [selectedTools, setSelectedTools] = useState<string[]>(isNew ? [] : full.allowedToolIds);
  const [autoSelectThreshold, setAutoSelectThreshold] = useState(0.82);
  const [confirmationThreshold, setConfirmationThreshold] = useState(0.60);
  const [maxCandidates, setMaxCandidates] = useState(5);
  const [requirePlanConfirmation, setRequirePlanConfirmation] = useState(true);
  const [maxSteps, setMaxSteps] = useState(10);
  const [maxDuration, setMaxDuration] = useState(300);
  const [maxParallelism, setMaxParallelism] = useState(4);
  const [maxLoopIterations, setMaxLoopIterations] = useState(50);
  const [evalDataset, setEvalDataset] = useState('default-work-assistant');
  const [saving, setSaving] = useState(false);
  const [publishing, setPublishing] = useState(false);

  const [toolSearch, setToolSearch] = useState('');
  const [serverFilter, setServerFilter] = useState('ALL');
  const [capabilityFilter, setCapabilityFilter] = useState('ALL');
  const [riskFilter, setRiskFilter] = useState('ALL');

  const capabilities = useMemo(
    () => Array.from(new Set(mockTools.map(t => CAPABILITY_BY_TOOL[t.id] ?? 'general'))).sort(),
    [],
  );

  const filteredTools = useMemo(() => {
    const q = toolSearch.trim().toLowerCase();
    return mockTools.filter(t => {
      const capability = CAPABILITY_BY_TOOL[t.id] ?? 'general';
      if (serverFilter !== 'ALL' && t.serverId !== serverFilter) return false;
      if (capabilityFilter !== 'ALL' && capability !== capabilityFilter) return false;
      if (riskFilter !== 'ALL' && t.riskClass !== riskFilter) return false;
      if (!q) return true;
      return (
        t.displayName.toLowerCase().includes(q) ||
        t.sourceName.toLowerCase().includes(q) ||
        t.serverName.toLowerCase().includes(q) ||
        capability.toLowerCase().includes(q)
      );
    });
  }, [toolSearch, serverFilter, capabilityFilter, riskFilter]);

  const selectedToolObjs = mockTools.filter(t => selectedTools.includes(t.id));

  const validationIssues = useMemo(() => {
    const errors: string[] = [];
    const warnings: string[] = [];
    if (!name.trim()) errors.push('Agent 이름이 필요합니다.');
    if (!instructions.trim()) errors.push('Instructions가 비어 있습니다.');
    if (!modelProfile) errors.push('Model Profile이 선택되지 않았습니다.');
    if (selectedTools.length === 0) errors.push('최소 1개 이상의 Tool Scope가 필요합니다.');
    if (maxSteps < 1) errors.push('max_steps는 1 이상이어야 합니다.');
    if (maxDuration < 1) errors.push('max_duration_seconds는 1 이상이어야 합니다.');
    if (maxLoopIterations < 1) errors.push('max_loop_iterations는 1 이상이어야 합니다.');
    if (autoSelectThreshold < confirmationThreshold) {
      warnings.push('auto_select_threshold가 confirmation_threshold보다 낮습니다.');
    }
    const inactive = selectedToolObjs.filter(t => t.status !== 'ACTIVE');
    if (inactive.length > 0) {
      errors.push(`비활성/차단 Tool이 포함되어 있습니다: ${inactive.map(t => t.displayName).join(', ')}`);
    }
    const unverified = selectedToolObjs.filter(t => t.verification !== 'VERIFIED');
    if (unverified.length > 0) {
      warnings.push(`미검증 Tool이 포함되어 있습니다: ${unverified.map(t => t.displayName).join(', ')}`);
    }
    return { errors, warnings };
  }, [name, instructions, modelProfile, selectedTools, selectedToolObjs, maxSteps, maxDuration, maxLoopIterations, autoSelectThreshold, confirmationThreshold]);

  const canPublish = validationIssues.errors.length === 0;

  const toggleTool = (id: string) => {
    if (readOnly || publishedBlocked) return;
    setSelectedTools(prev => (prev.includes(id) ? prev.filter(t => t !== id) : [...prev, id]));
  };

  const handleSave = async () => {
    setSaving(true);
    await new Promise(r => setTimeout(r, 700));
    setSaving(false);
    navigate(`/agents/${agent.id}`);
  };

  const handlePublish = async () => {
    if (!canPublish) return;
    setPublishing(true);
    await new Promise(r => setTimeout(r, 900));
    setPublishing(false);
    navigate(`/agents/${agent.id}`);
  };

  if (publishedBlocked) {
    return (
      <div>
        <PageHeader
          breadcrumbs={[
            { label: 'Agents', to: '/agents' },
            { label: agent.name, to: `/agents/${agent.id}` },
            { label: `${versionId}` },
          ]}
          title={`Agent Version ${versionId}`}
          actions={<Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>닫기</Button>}
        />
        <div className="p-6 max-w-2xl space-y-4">
          <InlineAlert type="warning" message="Published versions cannot be edited" />
          <div className="bg-white rounded-xl border border-slate-200 p-6 space-y-3">
            <p className="text-sm text-slate-600">
              PUBLISHED Version은 불변입니다. 변경이 필요하면 새 DRAFT Version을 생성하세요.
            </p>
            <div className="flex gap-2">
              <Button icon={<Plus size={13} />} onClick={() => navigate(`/agents/${agent.id}/versions/new/edit`)}>
                Create New Draft Version
              </Button>
              <Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>Agent로 돌아가기</Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        breadcrumbs={[
          { label: 'Agents', to: '/agents' },
          { label: agent.name, to: `/agents/${agent.id}` },
          { label: isNew ? '새 버전' : readOnly ? `${versionId} View` : `${versionId} 편집` },
        ]}
        title={isNew ? '새 Agent 버전' : readOnly ? `Agent Version View (${versionId})` : `Agent 버전 편집 (${versionId})`}
        actions={
          <>
            <Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>{readOnly ? '닫기' : '취소'}</Button>
            {!readOnly && <Button loading={saving} onClick={handleSave}>Draft 저장</Button>}
          </>
        }
      />
      <div className="p-6 max-w-5xl space-y-6">
        {readOnly ? (
          <InlineAlert type="info" message="DEPRECATED Version은 View만 가능합니다. 수정하려면 새 DRAFT Version을 생성하세요." />
        ) : (
          <InlineAlert type="info" message="편집 내용은 DRAFT 버전으로 저장됩니다. Blocking validation error가 없을 때만 Publish할 수 있습니다." />
        )}

        <Section title="1. Basic">
          <Field label="이름">
            <input value={name} onChange={e => setName(e.target.value)} disabled={readOnly} className={inputClass} />
          </Field>
          <Field label="Purpose">
            <textarea value={purpose} onChange={e => setPurpose(e.target.value)} disabled={readOnly} rows={2} className={`${inputClass} h-auto py-2 resize-y`} />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Visibility">
              <select value={visibility} onChange={e => setVisibility(e.target.value)} disabled={readOnly} className={inputClass}>
                <option value="PRIVATE">PRIVATE</option>
                <option value="RESTRICTED">RESTRICTED</option>
                <option value="INTERNAL">INTERNAL</option>
              </select>
            </Field>
            <Field label="Change Summary">
              <input defaultValue={existing?.changeSummary ?? ''} disabled={readOnly} placeholder="변경 요약" className={inputClass} />
            </Field>
          </div>
        </Section>

        <Section title="2. Instructions">
          <textarea
            value={instructions}
            onChange={e => setInstructions(e.target.value)}
            disabled={readOnly}
            rows={10}
            className={`${inputClass} h-auto py-2 font-mono text-xs leading-relaxed resize-y`}
          />
        </Section>

        <Section title="3. Model Profile">
          <Field label="LLM Profile">
            <select value={modelProfile} onChange={e => setModelProfile(e.target.value)} disabled={readOnly} className={inputClass}>
              {mockModelProfiles.filter(m => m.type === 'LLM').map(m => (
                <option key={m.id} value={m.name}>{m.name}</option>
              ))}
            </select>
          </Field>
        </Section>

        <Section title={`4. Tool Scope (${selectedTools.length})`}>
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="space-y-3">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  value={toolSearch}
                  onChange={e => setToolSearch(e.target.value)}
                  placeholder="Search tools..."
                  disabled={readOnly}
                  className={`${inputClass} pl-8`}
                />
              </div>
              <div className="grid grid-cols-3 gap-2">
                <select value={serverFilter} onChange={e => setServerFilter(e.target.value)} disabled={readOnly} className={inputClass}>
                  <option value="ALL">MCP Server</option>
                  {mockMCPServers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
                </select>
                <select value={capabilityFilter} onChange={e => setCapabilityFilter(e.target.value)} disabled={readOnly} className={inputClass}>
                  <option value="ALL">Capability</option>
                  {capabilities.map(c => <option key={c} value={c}>{c}</option>)}
                </select>
                <select value={riskFilter} onChange={e => setRiskFilter(e.target.value)} disabled={readOnly} className={inputClass}>
                  <option value="ALL">Risk Class</option>
                  {RISK_OPTIONS.map(r => <option key={r} value={r}>{r}</option>)}
                </select>
              </div>
              <div>
                <p className="text-xs font-medium text-slate-500 mb-2">Available Tools</p>
                <div className="space-y-1.5 max-h-80 overflow-auto border border-slate-200 rounded-lg p-2">
                  {filteredTools.map(tool => {
                    const selected = selectedTools.includes(tool.id);
                    return (
                      <label
                        key={tool.id}
                        className={`flex items-start gap-3 p-2.5 rounded-lg border cursor-pointer ${selected ? 'border-indigo-300 bg-indigo-50/40' : 'border-slate-200 hover:bg-slate-50'} ${readOnly ? 'cursor-default' : ''}`}
                      >
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleTool(tool.id)}
                          disabled={readOnly}
                          className="mt-0.5 accent-indigo-600"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium text-slate-800">{tool.displayName}</div>
                          <div className="text-xs text-slate-400 mt-0.5">{tool.serverName} · {CAPABILITY_BY_TOOL[tool.id]}</div>
                          <div className="flex items-center gap-2 mt-1.5">
                            <RiskBadge risk={tool.riskClass} />
                            <VerificationBadge status={tool.verification} />
                          </div>
                        </div>
                      </label>
                    );
                  })}
                  {filteredTools.length === 0 && (
                    <p className="text-xs text-slate-400 text-center py-6">조건에 맞는 Tool이 없습니다.</p>
                  )}
                </div>
              </div>
            </div>

            <div>
              <p className="text-xs font-medium text-slate-500 mb-2">Selected Tools</p>
              <div className="space-y-1.5 max-h-[28rem] overflow-auto border border-slate-200 rounded-lg p-2 bg-slate-50/50">
                {selectedToolObjs.map(tool => (
                  <div key={tool.id} className="flex items-start gap-3 p-2.5 rounded-lg border border-slate-200 bg-white">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-slate-800">{tool.displayName}</div>
                      <div className="text-xs text-slate-400 mt-0.5">
                        Tool Name: <span className="font-mono">{tool.sourceName}</span>
                      </div>
                      <div className="text-xs text-slate-400">MCP Server: {tool.serverName}</div>
                      <div className="flex items-center gap-2 mt-1.5">
                        <RiskBadge risk={tool.riskClass} />
                        <VerificationBadge status={tool.verification} />
                      </div>
                    </div>
                    {!readOnly && (
                      <button onClick={() => toggleTool(tool.id)} className="p-1 rounded text-slate-400 hover:text-red-500 hover:bg-red-50">
                        <X size={13} />
                      </button>
                    )}
                  </div>
                ))}
                {selectedToolObjs.length === 0 && (
                  <p className="text-xs text-slate-400 text-center py-8">선택된 Tool이 없습니다.</p>
                )}
              </div>
            </div>
          </div>
        </Section>

        <Section title="5. Planning / Confirmation Policy">
          <div className="grid grid-cols-3 gap-4">
            <Field label="auto_select_threshold">
              <input type="number" step="0.01" min={0} max={1} value={autoSelectThreshold} disabled={readOnly}
                onChange={e => setAutoSelectThreshold(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="confirmation_threshold">
              <input type="number" step="0.01" min={0} max={1} value={confirmationThreshold} disabled={readOnly}
                onChange={e => setConfirmationThreshold(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="max_candidates">
              <input type="number" min={1} value={maxCandidates} disabled={readOnly}
                onChange={e => setMaxCandidates(Number(e.target.value))} className={inputClass} />
            </Field>
          </div>
          <label className={`flex items-center gap-2 text-sm text-slate-700 ${readOnly ? 'opacity-70' : ''}`}>
            <input
              type="checkbox"
              checked={requirePlanConfirmation}
              disabled={readOnly}
              onChange={e => setRequirePlanConfirmation(e.target.checked)}
              className="accent-indigo-600"
            />
            실행 전 Plan Confirmation 요구 (WAITING_CONFIRMATION)
          </label>
        </Section>

        <Section title="6. Limits">
          <div className="grid grid-cols-2 gap-4">
            <Field label="max_steps">
              <input type="number" min={1} value={maxSteps} disabled={readOnly}
                onChange={e => setMaxSteps(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="max_duration_seconds">
              <input type="number" min={1} value={maxDuration} disabled={readOnly}
                onChange={e => setMaxDuration(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="max_parallelism">
              <input type="number" min={1} value={maxParallelism} disabled={readOnly}
                onChange={e => setMaxParallelism(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="max_loop_iterations">
              <input type="number" min={1} value={maxLoopIterations} disabled={readOnly}
                onChange={e => setMaxLoopIterations(Number(e.target.value))} className={inputClass} />
            </Field>
          </div>
        </Section>

        <Section title="7. Evaluation">
          <Field label="Evaluation Dataset">
            <select value={evalDataset} onChange={e => setEvalDataset(e.target.value)} disabled={readOnly} className={inputClass}>
              <option value="default-work-assistant">default-work-assistant</option>
              <option value="report-generation">report-generation</option>
              <option value="tool-selection-hard">tool-selection-hard</option>
            </select>
          </Field>
          <p className="text-xs text-slate-400">
            Evaluation Run은 AgentVersion, Model Profile, Registry snapshot을 고정해 재현합니다. (Mock)
          </p>
        </Section>

        <Section title="8. Validate & Publish">
          <div className="space-y-3">
            <div>
              <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-2">Validation Summary</p>
              {validationIssues.errors.length === 0 && validationIssues.warnings.length === 0 ? (
                <InlineAlert type="info" message="Blocking error가 없습니다. Publish할 수 있습니다." />
              ) : (
                <div className="space-y-2">
                  {validationIssues.errors.map(msg => (
                    <InlineAlert key={msg} type="error" message={msg} />
                  ))}
                  {validationIssues.warnings.map(msg => (
                    <InlineAlert key={msg} type="warning" message={msg} />
                  ))}
                </div>
              )}
            </div>
            {!readOnly && (
              <div className="flex gap-2">
                <Button variant="outline" loading={saving} onClick={handleSave}>Draft 저장</Button>
                <Button loading={publishing} disabled={!canPublish} onClick={handlePublish}>
                  Publish
                </Button>
              </div>
            )}
            {readOnly && (
              <Button icon={<Plus size={13} />} onClick={() => navigate(`/agents/${agent.id}/versions/new/edit`)}>
                Create New Draft Version
              </Button>
            )}
          </div>
        </Section>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-4">{title}</h3>
      <div className="space-y-4">{children}</div>
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

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white disabled:bg-slate-50 disabled:text-slate-500';
