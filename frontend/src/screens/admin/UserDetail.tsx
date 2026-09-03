import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import { mockUsers } from '../../data/mock';

export default function UserDetail() {
  const { userId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('profile');
  const user = mockUsers.find(u => u.id === userId) ?? mockUsers[0];

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/admin/users')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Users
        </button>
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-indigo-600 flex items-center justify-center text-white font-semibold">
            {user.name[0]}
          </div>
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{user.name}</h1>
            <div className="flex items-center gap-2 mt-0.5">
              <StatusBadge status={user.status} size="sm" />
              <span className="text-xs font-mono text-slate-400">{user.username}</span>
            </div>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'profile', label: 'Profile' },
            { id: 'roles', label: 'Roles' },
            { id: 'grants', label: 'Resource Grants' },
            { id: 'activity', label: 'Activity' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6 max-w-2xl">
        {tab === 'profile' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
            <Row label="이름">{user.name}</Row>
            <Row label="Username" mono>{user.username}</Row>
            <Row label="이메일" mono>{user.username}@mcpflow.io</Row>
            <Row label="상태"><StatusBadge status={user.status} size="sm" /></Row>
            <Row label="마지막 로그인">{user.lastLogin}</Row>
          </div>
        )}
        {tab === 'roles' && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">할당된 Role</h3>
            <div className="space-y-2">
              {user.roles.map(role => (
                <div key={role} className="flex items-center justify-between p-2.5 rounded-lg bg-slate-50 border border-slate-100">
                  <span className="text-sm font-medium text-slate-700">{role}</span>
                  <span className="text-xs bg-indigo-50 text-indigo-700 px-2 py-0.5 rounded-full">{role}</span>
                </div>
              ))}
            </div>
          </div>
        )}
        {tab === 'grants' && (
          <div className="text-sm text-slate-500 py-8 text-center">추가 Resource Grant가 없습니다.</div>
        )}
        {tab === 'activity' && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            {[
              { time: '2026-09-02 14:00', action: 'Login', result: '성공' },
              { time: '2026-09-02 13:55', action: 'Execution 시작', result: '성공' },
              { time: '2026-09-01 17:30', action: 'Execution 시작', result: '실패' },
            ].map((a, i) => (
              <div key={i} className="flex gap-4 px-4 py-3 border-b last:border-0 text-sm">
                <span className="font-mono text-xs text-slate-400 shrink-0">{a.time}</span>
                <span className="text-slate-700">{a.action}</span>
                <span className={`ml-auto text-xs ${a.result === '성공' ? 'text-green-600' : 'text-red-600'}`}>{a.result}</span>
              </div>
            ))}
          </div>
        )}
        {tab === 'audit' && (
          <div className="text-sm text-slate-500 py-8 text-center">감사 로그는 Audit Logs에서 확인하세요.</div>
        )}
      </div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
