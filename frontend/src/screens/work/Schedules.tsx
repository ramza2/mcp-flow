import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import FilterBar from '../../components/ui/FilterBar';
import Button from '../../components/ui/Button';
import { mockSchedules } from '../../data/mock';
import { SCHEDULE_STATUSES, labelScheduleTarget } from '../../domain';

export default function Schedules() {
  const navigate = useNavigate();

  const columns: Column<typeof mockSchedules[0]>[] = [
    { key: 'name', label: '이름', render: r => <span className="font-medium text-slate-800">{r.name}</span> },
    { key: 'target', label: 'Target', render: r => (
      <div>
        <span className="text-sm text-slate-700">{r.target}</span>
        <span className="ml-1.5 text-xs font-mono text-slate-400">{r.version}</span>
      </div>
    )},
    { key: 'targetType', label: 'Type', render: r => (
      <span className="text-xs bg-slate-100 px-2 py-0.5 rounded text-slate-600">
        {labelScheduleTarget(r.targetType)}
      </span>
    )},
    { key: 'schedule', label: 'Schedule', render: r => <span className="font-mono text-xs text-slate-600">{r.schedule}</span> },
    { key: 'timezone', label: 'Timezone', render: r => <span className="text-xs text-slate-500">{r.timezone}</span> },
    { key: 'nextRun', label: 'Next Run', render: r => <span className="text-xs text-slate-600">{r.nextRun}</span> },
    { key: 'lastRun', label: 'Last Run', render: r => <span className="text-xs text-slate-500">{r.lastRun}</span> },
    { key: 'lastResult', label: 'Last Result', render: r => <StatusBadge status={r.lastResult} size="sm" /> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
  ];

  return (
    <div>
      <PageHeader
        title="Schedules"
        description="예약된 Agent 및 Workflow 실행을 관리합니다."
        actions={<Button icon={<Plus size={14} />} onClick={() => navigate('/schedules/new')}>새 Schedule</Button>}
      />
      <div className="p-6 space-y-4">
        <FilterBar
          search
          searchPlaceholder="Schedule 이름 검색..."
          filters={[
            { key: 'status', label: '상태', options: SCHEDULE_STATUSES.map(v => ({
              value: v,
              label: v.charAt(0) + v.slice(1).toLowerCase(),
            })) },
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockSchedules}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/schedules/${r.id}/edit`)}
          />
        </div>
      </div>
    </div>
  );
}
