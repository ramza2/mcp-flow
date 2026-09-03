import { Search, SlidersHorizontal } from 'lucide-react';
import { useState } from 'react';

interface Filter {
  key: string;
  label: string;
  options: { value: string; label: string }[];
}

interface FilterBarProps {
  search?: boolean;
  searchPlaceholder?: string;
  onSearch?: (q: string) => void;
  filters?: Filter[];
  onFilter?: (key: string, value: string) => void;
  actions?: React.ReactNode;
}

export default function FilterBar({ search, searchPlaceholder, onSearch, filters, onFilter, actions }: FilterBarProps) {
  const [q, setQ] = useState('');

  return (
    <div className="flex items-center gap-3 flex-wrap">
      {search && (
        <div className="relative">
          <Search size={13} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-400" />
          <input
            type="text"
            value={q}
            onChange={e => { setQ(e.target.value); onSearch?.(e.target.value); }}
            placeholder={searchPlaceholder ?? '검색...'}
            className="pl-8 pr-3 py-1.5 h-8 text-sm border border-slate-200 rounded-md bg-white text-slate-700 placeholder-slate-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent w-56"
          />
        </div>
      )}
      {filters?.map(f => (
        <select
          key={f.key}
          onChange={e => onFilter?.(f.key, e.target.value)}
          className="h-8 text-sm border border-slate-200 rounded-md bg-white text-slate-600 px-2.5 focus:outline-none focus:ring-2 focus:ring-indigo-500"
        >
          <option value="">{f.label}: 전체</option>
          {f.options.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
      ))}
      {actions && <div className="ml-auto flex items-center gap-2">{actions}</div>}
    </div>
  );
}
