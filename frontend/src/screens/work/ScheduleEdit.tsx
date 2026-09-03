import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Info } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockSchedules } from '../../data/mock';

const PREVIEW_RUNS = [
  '2026-09-09 09:00 KST',
  '2026-09-16 09:00 KST',
  '2026-09-23 09:00 KST',
];

export default function ScheduleEdit() {
  const navigate = useNavigate();
  const { id } = useParams();
  const isNew = !id || id === 'new';
  const existing = isNew ? null : mockSchedules[0];

  const [name, setName] = useState(existing?.name ?? '');
  const [targetType, setTargetType] = useState(existing?.targetType ?? 'Workflow');
  const [scheduleType, setScheduleType] = useState('Recurring');
  const [cron, setCron] = useState('0 9 * * 1');
  const [timezone, setTimezone] = useState('Asia/Seoul');
  const [active, setActive] = useState(existing?.status !== 'INACTIVE');
  const [saving, setSaving] = useState(false);

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
              {['Agent', 'Workflow'].map(t => (
                <label key={t} className="flex items-center gap-2 cursor-pointer">
                  <input type="radio" checked={targetType === t} onChange={() => setTargetType(t)} className="accent-indigo-600" />
                  <span className="text-sm text-slate-700">{t} Version</span>
                </label>
              ))}
            </div>
          </Field>
          <Field label="Target">
            <select className={inputClass}>
              <option>Weekly Report Workflow</option>
              <option>Document Review Workflow</option>
              <option>General Work Assistant</option>
            </select>
          </Field>
          <Field label="Version">
            <select className={inputClass}>
              <option>v2 (PUBLISHED)</option>
              <option>v1 (DEPRECATED)</option>
            </select>
          </Field>
        </Section>

        <Section title="실행 일정">
          <Field label="Schedule Type">
            <div className="flex gap-3">
              {['One-time', 'Recurring'].map(t => (
                <label key={t} className="flex items-center gap-2 cursor-pointer">
                  <input type="radio" checked={scheduleType === t} onChange={() => setScheduleType(t)} className="accent-indigo-600" />
                  <span className="text-sm text-slate-700">{t}</span>
                </label>
              ))}
            </div>
          </Field>
          {scheduleType === 'Recurring' && (
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

        {scheduleType === 'Recurring' && (
          <Section title="예상 실행 시각 (Next 3 Runs)">
            <div className="space-y-1.5">
              {PREVIEW_RUNS.map((r, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <span className="text-slate-400 w-4">{i + 1}.</span>
                  <span className="font-mono text-slate-700">{r}</span>
                </div>
              ))}
            </div>
          </Section>
        )}

        <Section title="옵션">
          <Field label="Overlap Policy">
            <select className={inputClass}>
              <option>SKIP</option>
              <option>QUEUE</option>
              <option>CANCEL_RUNNING</option>
            </select>
          </Field>
          <Field label="Misfire Policy">
            <select className={inputClass}>
              <option>SKIP</option>
              <option>RUN_ONCE</option>
              <option>RUN_ALL</option>
            </select>
          </Field>
          <Field label="활성화">
            <label className="flex items-center gap-2 cursor-pointer">
              <div
                onClick={() => setActive(v => !v)}
                className={`w-10 h-5 rounded-full transition-colors ${active ? 'bg-indigo-600' : 'bg-slate-200'} relative`}
              >
                <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${active ? 'left-5.5' : 'left-0.5'}`} />
              </div>
              <span className="text-sm text-slate-700">{active ? '활성' : '비활성'}</span>
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
