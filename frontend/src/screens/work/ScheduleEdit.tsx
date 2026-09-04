import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockAgents, mockSchedules, mockWorkflows } from '../../data/mock';
import {
  SCHEDULE_MISFIRE_POLICIES,
  SCHEDULE_OVERLAP_POLICIES,
  SCHEDULE_TARGET_TYPES,
  labelScheduleTarget,
  type ScheduleMisfirePolicy,
  type ScheduleOverlapPolicy,
  type ScheduleTargetType,
} from '../../domain';

const DEFAULT_PREVIEW = [
  '2026-09-09 09:00 KST',
  '2026-09-16 09:00 KST',
  '2026-09-23 09:00 KST',
];

export default function ScheduleEdit() {
  const navigate = useNavigate();
  const { id } = useParams();
  const isNew = !id || id === 'new';
  const existing = isNew ? null : (mockSchedules.find(s => s.id === id) ?? mockSchedules[0]);

  const [name, setName] = useState(existing?.name ?? '');
  const [targetType, setTargetType] = useState<ScheduleTargetType>(existing?.targetType ?? 'WORKFLOW_VERSION');
  const [targetId, setTargetId] = useState(existing?.targetId ?? '');
  const [version, setVersion] = useState(existing?.version ?? '');
  const [inputJson, setInputJson] = useState('{\n  \n}');
  const [scheduleType, setScheduleType] = useState<'One-time' | 'Recurring'>(
    existing?.scheduleKind === 'RECURRING' ? 'Recurring' : isNew ? 'Recurring' : 'One-time'
  );
  const [oneTimeAt, setOneTimeAt] = useState('');
  const [cron, setCron] = useState(existing?.schedule ?? '0 9 * * 1');
  const [timezone, setTimezone] = useState(existing?.timezone ?? 'Asia/Seoul');
  const [overlap, setOverlap] = useState<ScheduleOverlapPolicy>(existing?.overlapPolicy ?? 'SKIP');
  const [misfire, setMisfire] = useState<ScheduleMisfirePolicy>(existing?.misfirePolicy ?? 'SKIP');
  // ACTIVE vs PAUSED — never INACTIVE
  const [active, setActive] = useState(existing ? existing.status === 'ACTIVE' : true);
  const [saving, setSaving] = useState(false);

  const targets = targetType === 'AGENT_VERSION'
    ? mockAgents.map(a => ({ id: a.id, name: a.name, versions: a.versions.map(v => v.version) }))
    : mockWorkflows.map(w => ({
      id: w.id,
      name: w.name,
      // Logical workflows list publishedVersion only; mock designer holds full version history
      versions: w.publishedVersion ? [w.publishedVersion, 'v1'] : ['v1'],
    }));

  const selectedTarget = targets.find(t => t.id === targetId) ?? targets[0];
  const versionOptions = selectedTarget?.versions ?? [];
  const previewRuns = !isNew && existing?.nextRunsPreview?.length
    ? existing.nextRunsPreview
    : scheduleType === 'Recurring'
      ? DEFAULT_PREVIEW
      : oneTimeAt
        ? [`${oneTimeAt} ${timezone.includes('Seoul') ? 'KST' : ''}`.trim()]
        : [];

  const handleSave = async () => {
    setSaving(true);
    await delay(900);
    setSaving(false);
    navigate('/schedules');
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Schedules', to: '/schedules' }, { label: isNew ? '새 Schedule' : existing?.name ?? '' }]}
        title={isNew ? '새 Schedule 등록' : 'Schedule 수정'}
        actions={
          <>
            <Button variant="outline" onClick={() => navigate('/schedules')}>취소</Button>
            <Button loading={saving} onClick={handleSave}>저장</Button>
          </>
        }
      />
      <div className="p-6 max-w-2xl space-y-6">
        {!isNew && (
          <InlineAlert type="info" message="특정 버전이 고정됩니다. 최신 버전이 자동으로 반영되지 않으므로 버전 업데이트 시 Schedule을 수동으로 수정하세요." />
        )}

        <Section title="기본 정보">
          <Field label="이름">
            <input value={name} onChange={e => setName(e.target.value)} placeholder="Schedule 이름" className={inputClass} />
          </Field>
          <Field label="Target Type">
            <div className="flex gap-3">
              {SCHEDULE_TARGET_TYPES.map(t => (
                <label key={t} className="flex items-center gap-2 cursor-pointer">
                  <input
                    type="radio"
                    checked={targetType === t}
                    onChange={() => {
                      setTargetType(t);
                      setTargetId('');
                      setVersion('');
                    }}
                    className="accent-indigo-600"
                  />
                  <span className="text-sm text-slate-700">{labelScheduleTarget(t)}</span>
                </label>
              ))}
            </div>
          </Field>
          <Field label="Target">
            <select
              value={targetId || selectedTarget?.id || ''}
              onChange={e => {
                setTargetId(e.target.value);
                setVersion('');
              }}
              className={inputClass}
            >
              {targets.map(t => (
                <option key={t.id} value={t.id}>{t.name}</option>
              ))}
            </select>
          </Field>
          <Field label="Version">
            <select
              value={version || versionOptions[0] || ''}
              onChange={e => setVersion(e.target.value)}
              className={inputClass}
            >
              {versionOptions.map(v => (
                <option key={v} value={v}>{v}</option>
              ))}
            </select>
          </Field>
          <Field label="Input">
            <textarea
              value={inputJson}
              onChange={e => setInputJson(e.target.value)}
              rows={4}
              placeholder='{"key": "value"}'
              className={`${inputClass} h-auto font-mono py-2`}
            />
          </Field>
        </Section>

        <Section title="실행 일정">
          <Field label="Schedule Type">
            <div className="flex gap-3">
              {(['One-time', 'Recurring'] as const).map(t => (
                <label key={t} className="flex items-center gap-2 cursor-pointer">
                  <input type="radio" checked={scheduleType === t} onChange={() => setScheduleType(t)} className="accent-indigo-600" />
                  <span className="text-sm text-slate-700">{t}</span>
                </label>
              ))}
            </div>
          </Field>
          {scheduleType === 'One-time' ? (
            <Field label="Date / Time">
              <input
                type="datetime-local"
                value={oneTimeAt}
                onChange={e => setOneTimeAt(e.target.value)}
                className={inputClass}
              />
            </Field>
          ) : (
            <Field label="Cron 표현식">
              <input value={cron} onChange={e => setCron(e.target.value)} placeholder="0 9 * * 1" className={`${inputClass} font-mono`} />
              <p className="text-xs text-slate-400 mt-1">매주 월요일 09:00</p>
            </Field>
          )}
          <Field label="Timezone">
            <select value={timezone} onChange={e => setTimezone(e.target.value)} className={inputClass}>
              <option value="Asia/Seoul">Asia/Seoul (KST, UTC+9)</option>
              <option value="UTC">UTC</option>
              <option value="America/New_York">America/New_York (EST)</option>
            </select>
          </Field>
        </Section>

        <Section title="예상 실행 시각 (Next Runs Preview)">
          {previewRuns.length === 0 ? (
            <p className="text-sm text-slate-400">일정을 설정하면 예상 실행 시각이 표시됩니다.</p>
          ) : (
            <div className="space-y-1.5">
              {previewRuns.map((r, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <span className="text-slate-400 w-4">{i + 1}.</span>
                  <span className="font-mono text-slate-700">{r}</span>
                </div>
              ))}
            </div>
          )}
        </Section>

        <Section title="옵션">
          <Field label="Overlap Policy">
            <select value={overlap} onChange={e => setOverlap(e.target.value as ScheduleOverlapPolicy)} className={inputClass}>
              {SCHEDULE_OVERLAP_POLICIES.map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </Field>
          <Field label="Misfire Policy">
            <select value={misfire} onChange={e => setMisfire(e.target.value as ScheduleMisfirePolicy)} className={inputClass}>
              {SCHEDULE_MISFIRE_POLICIES.map(p => (
                <option key={p} value={p}>{p}</option>
              ))}
            </select>
          </Field>
          <Field label="상태">
            <label className="flex items-center gap-2 cursor-pointer">
              <div
                onClick={() => setActive(v => !v)}
                className={`w-10 h-5 rounded-full transition-colors ${active ? 'bg-indigo-600' : 'bg-slate-200'} relative`}
              >
                <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${active ? 'left-5.5' : 'left-0.5'}`} />
              </div>
              <span className="text-sm text-slate-700">{active ? 'ACTIVE' : 'PAUSED'}</span>
            </label>
          </Field>
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

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
