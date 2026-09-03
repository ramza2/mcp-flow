import { useState } from 'react';

interface Tab { id: string; label: string; icon?: React.ReactNode; badge?: number; }

interface TabsProps {
  tabs: Tab[];
  activeTab: string;
  onChange: (id: string) => void;
  variant?: 'underline' | 'pills';
}

export function TabBar({ tabs, activeTab, onChange, variant = 'underline' }: TabsProps) {
  if (variant === 'pills') {
    return (
      <div className="flex gap-1">
        {tabs.map(tab => (
          <button
            key={tab.id}
            onClick={() => onChange(tab.id)}
            className={`flex items-center gap-1.5 px-3 py-1.5 rounded-md text-sm font-medium transition-colors
              ${activeTab === tab.id
                ? 'bg-indigo-600 text-white'
                : 'text-slate-600 hover:bg-slate-100'}`}
          >
            {tab.icon}
            {tab.label}
            {tab.badge != null && (
              <span className={`text-xs rounded-full px-1.5 py-0.5 min-w-5 text-center
                ${activeTab === tab.id ? 'bg-indigo-500 text-white' : 'bg-slate-200 text-slate-600'}`}>
                {tab.badge}
              </span>
            )}
          </button>
        ))}
      </div>
    );
  }

  return (
    <div className="flex border-b border-transparent">
      {tabs.map(tab => (
        <button
          key={tab.id}
          onClick={() => onChange(tab.id)}
          className={`flex items-center gap-1.5 px-4 py-2.5 text-sm font-medium border-b-2 -mb-px transition-colors
            ${activeTab === tab.id
              ? 'border-indigo-600 text-indigo-600'
              : 'border-transparent text-slate-500 hover:text-slate-700 hover:border-slate-300'}`}
        >
          {tab.icon}
          {tab.label}
          {tab.badge != null && (
            <span className={`text-xs rounded-full px-1.5 min-w-5 text-center
              ${activeTab === tab.id ? 'bg-indigo-100 text-indigo-700' : 'bg-slate-100 text-slate-600'}`}>
              {tab.badge}
            </span>
          )}
        </button>
      ))}
    </div>
  );
}

interface SimpleTabs {
  tabs: { id: string; label: string; content: React.ReactNode }[];
  defaultTab?: string;
}

export function SimpleTabs({ tabs, defaultTab }: SimpleTabs) {
  const [active, setActive] = useState(defaultTab ?? tabs[0]?.id);
  const content = tabs.find(t => t.id === active)?.content;
  return (
    <div>
      <TabBar
        tabs={tabs.map(t => ({ id: t.id, label: t.label }))}
        activeTab={active}
        onChange={setActive}
      />
      <div className="pt-4">{content}</div>
    </div>
  );
}
