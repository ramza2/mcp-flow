import { useNavigate } from 'react-router';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import DataTable, { Column } from '../../components/ui/DataTable';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { mockUsers } from '../../data/mock';

export default function Users() {
  const navigate = useNavigate();

  const columns: Column<typeof mockUsers[0]>[] = [
    { key: 'name', label: '이름', render: r => <span className="font-medium text-slate-800">{r.name}</span> },
    { key: 'username', label: 'Username', render: r => <span className="font-mono text-xs text-slate-600">{r.username}</span> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'roles', label: 'Roles', render: r => (
      <div className="flex gap-1 flex-wrap">
        {r.roles.map(role => <span key={role} className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full">{role}</span>)}
      </div>
    )},
    { key: 'lastLogin', label: '마지막 로그인', render: r => <span className="text-xs text-slate-400">{r.lastLogin}</span> },
    { key: 'updatedAt', label: '수정일', render: r => <span className="text-xs text-slate-400">{r.updatedAt}</span> },
  ];

  return (
    <div>
      <PageHeader
        title="Users & Roles"
        description="시스템 사용자를 관리합니다."
        actions={<Button icon={<Plus size={14} />}>사용자 초대</Button>}
      />
      <div className="p-6">
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <DataTable
            columns={columns}
            data={mockUsers}
            rowKey={r => r.id}
            onRowClick={r => navigate(`/admin/users/${r.id}`)}
          />
        </div>
      </div>
    </div>
  );
}
