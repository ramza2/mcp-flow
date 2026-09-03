import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockAgents, mockAgentFull, mockTools, mockModelProfiles } from '../../data/mock';

export default function AgentEdit() {
  const navigate = useNavigate();
  const { agentId, versionId } = useParams();
  const isNew = !versionId || versionId === 'new';

  const agent = mockAgents.find(a => a.id === agentId) ?? mockAgents[0];
  const full = mockAgentFull[agent.id] ?? mockAgentFull['agt-001'];

  const [name, setName] = useState(agent.name);
  const [modelProfile, setModelProfile] = useState(agent.modelProfile);
  const [maxSteps, setMaxSteps] = useState(10);
  const [instructions, setInstructions] = useState(full.instructions);
  const [selectedTools, setSelectedTools] = useState<string[]>(isNew ? [] : full.allowedToolIds);
  const [saving, setSaving] = useState(false);

  const toggleTool = (id: string) =>
    setSelectedTools(prev => prev.includes(id) ? prev.filter(t => t !== id) : [...prev, id]);

  const handleSave = async () => {
    setSaving(true);
    await new Promise(r => setTimeout(r, 900));
    setSaving(false);
    navigate(`/agents/${agent.id}`);
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[
          { label: 'Agents', to: '/agents' },
          { label: agent.name, to: `/agents/${agent.id}` },
          { label: isNew ? '새 버전' : `${versionId} 편집` },
        ]}
        title={isNew ? '새 Agent 버전' : `Agent 버전 편집 (${versionId})`}
        actions={
          <>
            <Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>취소</Button>
            <Button loading={saving} onClick={handleSave}>Draft 저장</Button>
          </>
        }
      />
      <div className="p-6 max-w-3xl space-y-6">
        <InlineAlert type="info" message="편집 내용은 새로운 DRAFT 버전으로 저장됩니다. Publish 하기 전까지 실행에 영향을 주지 않습니다." />

        <Section title="기본 정보">
          <Field label="이름">
            <input value={name} onChange={e => setName(e.target.value)} className={inputClass} />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="Model Profile">
              <select value={modelProfile} onChange={e => setModelProfile(e.target.value)} className={inputClass}>
                {mockModelProfiles.filter(m => m.type === 'LLM').map(m => (
                  <option key={m.id} value={m.name}>{m.name}</option>
                ))}
              </select>
            </Field>
            <Field label="Max Plan Steps">
              <input type="number" value={maxSteps} onChange={e => setMaxSteps(Number(e.target.value))} className={inputClass} />
            </Field>
          </div>
        </Section>

        <Section title="Instructions">
          <textarea
            value={instructions}
            onChange={e => setInstructions(e.target.value)}
            rows={10}
            className={`${inputClass} h-auto py-2 font-mono text-xs leading-relaxed resize-y`}
          />
        </Section>

        <Section title={`Allowed Tools (${selectedTools.length})`}>
          <div className="space-y-1.5 max-h-80 overflow-auto">
            {mockTools.map(tool => (
              <label key={tool.id} className="flex items-center gap-3 p-2.5 rounded-lg border border-slate-200 hover:bg-slate-50 cursor-pointer">
                <input
                  type="checkbox"
                  checked={selectedTools.includes(tool.id)}
                  onChange={() => toggleTool(tool.id)}
                  className="accent-indigo-600"
                />
                <div className="min-w-0 flex-1">
                  <div className="text-sm font-medium text-slate-800">{tool.displayName}</div>
                  <div className="text-xs text-slate-400 font-mono">{tool.sourceName} · {tool.serverName}</div>
                </div>
                <span className="text-[10px] font-mono text-slate-400">{tool.riskClass}</span>
              </label>
            ))}
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

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white';
