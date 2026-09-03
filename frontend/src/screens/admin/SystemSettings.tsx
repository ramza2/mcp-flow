import { useState } from 'react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';

const CATEGORIES = ['General', 'Execution', 'Agent', 'MCP', 'Scheduler', 'Retention', 'Security', 'Feature Capabilities'];

const SETTINGS: Record<string, { label: string; type: 'text' | 'number' | 'toggle' | 'select'; value: string | number | boolean; options?: string[] }[]> = {
  General: [
    { label: 'Platform Name', type: 'text', value: 'MCPFlow' },
    { label: 'Default Timezone', type: 'select', value: 'Asia/Seoul', options: ['Asia/Seoul', 'UTC', 'America/New_York'] },
    { label: 'UI Language', type: 'select', value: '한국어', options: ['한국어', 'English'] },
  ],
  Execution: [
    { label: 'Max Concurrent Executions', type: 'number', value: 50 },
    { label: 'Default Execution Timeout (s)', type: 'number', value: 600 },
    { label: 'Max Steps per Execution', type: 'number', value: 100 },
    { label: 'Enable MRTR', type: 'toggle', value: true },
  ],
  Agent: [
    { label: 'Default Model Profile', type: 'select', value: 'Claude 3.5 Sonnet', options: ['Claude 3.5 Sonnet', 'Claude 3 Haiku'] },
    { label: 'Auto Tool Selection', type: 'toggle', value: true },
    { label: 'Max Planning Depth', type: 'number', value: 5 },
  ],
  MCP: [
    { label: 'Tool Discovery Interval (min)', type: 'number', value: 60 },
    { label: 'Health Check Interval (min)', type: 'number', value: 5 },
    { label: 'Auto Discover on Register', type: 'toggle', value: true },
  ],
  Scheduler: [
    { label: 'Max Scheduled Jobs', type: 'number', value: 100 },
    { label: 'Misfire Threshold (min)', type: 'number', value: 30 },
  ],
  Retention: [
    { label: 'Execution Log Retention (days)', type: 'number', value: 90 },
    { label: 'Audit Log Retention (days)', type: 'number', value: 365 },
  ],
  Security: [
    { label: 'Session Timeout (min)', type: 'number', value: 60 },
    { label: 'Max Login Attempts', type: 'number', value: 5 },
    { label: 'Require MFA', type: 'toggle', value: false },
  ],
  'Feature Capabilities': [
    { label: 'External MCP Discovery', type: 'toggle', value: true },
    { label: 'Tool Factory', type: 'toggle', value: true },
    { label: 'Workflow Designer', type: 'toggle', value: true },
  ],
};

export default function SystemSettings() {
  const [category, setCategory] = useState('General');
  const settings = SETTINGS[category] ?? [];

  return (
    <div>
      <PageHeader title="System Settings" description="플랫폼 전역 설정을 구성합니다." />
      <InlineAlert type="warning" message="Bootstrap Secret, DB Password, Encryption Master Key 같은 민감한 값은 이 화면에 노출되지 않습니다." />
      <div className="flex h-[calc(100vh-200px)]">
        {/* Category nav */}
        <div className="w-48 border-r border-slate-200 bg-white flex-shrink-0 py-3">
          {CATEGORIES.map(cat => (
            <button
              key={cat}
              onClick={() => setCategory(cat)}
              className={`w-full text-left px-4 py-2.5 text-sm transition-colors
                ${category === cat ? 'bg-indigo-50 text-indigo-700 font-medium' : 'text-slate-600 hover:bg-slate-50'}`}
            >
              {cat}
            </button>
          ))}
        </div>

        {/* Settings form */}
        <div className="flex-1 p-6 overflow-y-auto">
          <div className="max-w-lg space-y-4">
            <h3 className="text-sm font-semibold text-slate-700">{category}</h3>
            <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-4">
              {settings.map(setting => (
                <div key={setting.label} className="flex items-center justify-between gap-4">
                  <label className="text-sm text-slate-700">{setting.label}</label>
                  {setting.type === 'toggle' ? (
                    <div className={`w-9 h-5 rounded-full relative cursor-pointer ${setting.value ? 'bg-indigo-600' : 'bg-slate-200'}`}>
                      <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${setting.value ? 'left-4.5' : 'left-0.5'}`} />
                    </div>
                  ) : setting.type === 'select' ? (
                    <select defaultValue={String(setting.value)} className="h-8 px-2 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 bg-white w-48">
                      {setting.options?.map(o => <option key={o}>{o}</option>)}
                    </select>
                  ) : (
                    <input
                      type={setting.type}
                      defaultValue={String(setting.value)}
                      className="h-8 px-2.5 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-1 focus:ring-indigo-500 w-32"
                    />
                  )}
                </div>
              ))}
            </div>
            <div className="flex justify-end">
              <Button size="sm">변경사항 저장</Button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
