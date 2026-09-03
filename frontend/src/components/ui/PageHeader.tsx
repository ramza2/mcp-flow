import { ChevronRight } from 'lucide-react';
import { Link } from 'react-router';

interface Crumb { label: string; to?: string; }

interface PageHeaderProps {
  breadcrumbs?: Crumb[];
  title: string;
  description?: string;
  actions?: React.ReactNode;
  tabs?: React.ReactNode;
}

export default function PageHeader({ breadcrumbs, title, description, actions, tabs }: PageHeaderProps) {
  return (
    <div className="bg-white border-b border-slate-200">
      {breadcrumbs && breadcrumbs.length > 0 && (
        <div className="flex items-center gap-1 px-6 pt-4 text-xs text-slate-400">
          {breadcrumbs.map((crumb, i) => (
            <span key={i} className="flex items-center gap-1">
              {i > 0 && <ChevronRight size={10} />}
              {crumb.to ? (
                <Link to={crumb.to} className="hover:text-slate-600 transition-colors">{crumb.label}</Link>
              ) : (
                <span className="text-slate-600">{crumb.label}</span>
              )}
            </span>
          ))}
        </div>
      )}
      <div className="flex items-start justify-between px-6 py-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold text-slate-900 truncate">{title}</h1>
          {description && <p className="text-sm text-slate-500 mt-0.5">{description}</p>}
        </div>
        {actions && <div className="flex items-center gap-2 ml-4 shrink-0">{actions}</div>}
      </div>
      {tabs && <div className="px-6">{tabs}</div>}
    </div>
  );
}
