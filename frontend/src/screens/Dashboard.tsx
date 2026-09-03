import { useNavigate } from 'react-router';
import { Activity, CheckCircle2, XCircle, Clock, AlertTriangle, Pause, Server, Wrench, Bot, ChevronRight } from 'lucide-react';
import StatusBadge from '../components/ui/StatusBadge';
import { mockExecutions, mockApprovals, mockMCPServers, mockTools } from '../data/mock';

function MetricCard({ label, value, sub, icon, color }: { label: string; value: string | number; sub?: string; icon: React.ReactNode; color: string }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <div className="flex items-center justify-between mb-3">
        <span className="text-xs font-medium text-slate-500">{label}</span>
        <span className={`p-1.5 rounded-lg ${color}`}>{icon}</span>
      </div>
      <div className="text-2xl font-semibold text-slate-900">{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-1">{sub}</div>}
    </div>
  );
}

function SectionCard({ title, children, linkTo, linkLabel }: { title: string; children: React.ReactNode; linkTo?: string; linkLabel?: string }) {
  const navigate = useNavigate();
  return (
    <div className="bg-white rounded-xl border border-slate-200">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-100">
        <span className="text-sm font-semibold text-slate-800">{title}</span>
        {linkTo && (
          <button onClick={() => navigate(linkTo)} className="text-xs text-indigo-600 hover:text-indigo-700 flex items-center gap-0.5">
            {linkLabel ?? '전체 보기'} <ChevronRight size={12} />
          </button>
        )}
      </div>
      <div className="p-4">{children}</div>
    </div>
  );
}

export default function Dashboard() {
  const navigate = useNavigate();
  const running = mockExecutions.filter(e => e.status === 'RUNNING').length;
  const waiting = mockExecutions.filter(e => e.status === 'WAITING_APPROVAL' || e.status === 'WAITING_INPUT').length;
  const failed = mockExecutions.filter(e => e.status === 'FAILED').length;
  const succeeded = mockExecutions.filter(e => e.status === 'SUCCEEDED').length;
  const pendingApprovals = mockApprovals.filter(a => a.status === 'PENDING');
  const unhealthyServers = mockMCPServers.filter(s => s.status === 'INACTIVE');
  const problematicTools = mockTools.filter(t => t.status === 'MISSING' || t.status === 'BLOCKED');

  return (
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-lg font-semibold text-slate-900">Dashboard</h1>
        <p className="text-sm text-slate-500 mt-0.5">2026년 9월 2일 — 전체 시스템 운영 현황</p>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
        <MetricCard label="Total Executions" value={mockExecutions.length} sub="오늘" icon={<Activity size={14} />} color="bg-slate-100 text-slate-600" />
        <MetricCard label="Success Rate" value={`${Math.round(succeeded / mockExecutions.length * 100)}%`} sub="오늘" icon={<CheckCircle2 size={14} />} color="bg-green-100 text-green-600" />
        <MetricCard label="Running" value={running} icon={<Activity size={14} className="animate-pulse" />} color="bg-cyan-100 text-cyan-600" />
        <MetricCard label="Waiting" value={waiting} icon={<Pause size={14} />} color="bg-amber-100 text-amber-600" />
        <MetricCard label="Failed" value={failed} icon={<XCircle size={14} />} color="bg-red-100 text-red-600" />
        <MetricCard label="Avg Duration" value="1m 44s" sub="p95: 8m 12s" icon={<Clock size={14} />} color="bg-indigo-100 text-indigo-600" />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* Recent Executions */}
        <SectionCard title="최근 Execution" linkTo="/executions">
          <div className="space-y-2">
            {mockExecutions.slice(0, 5).map(exe => (
              <div
                key={exe.id}
                onClick={() => navigate(`/executions/${exe.id}`)}
                className="flex items-center justify-between p-2.5 rounded-lg hover:bg-slate-50 cursor-pointer transition-colors"
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium text-slate-800 truncate">{exe.name}</div>
                  <div className="text-xs text-slate-400 font-mono">{exe.id}</div>
                </div>
                <StatusBadge status={exe.status} size="sm" />
              </div>
            ))}
          </div>
        </SectionCard>

        {/* Pending Approvals */}
        <SectionCard title="승인 대기" linkTo="/approvals">
          {pendingApprovals.length === 0 ? (
            <div className="py-6 text-center text-sm text-slate-400">승인 대기 항목이 없습니다.</div>
          ) : (
            <div className="space-y-2">
              {pendingApprovals.map(apr => (
                <div
                  key={apr.id}
                  onClick={() => navigate(`/approvals/${apr.id}`)}
                  className="flex items-center justify-between p-2.5 rounded-lg hover:bg-slate-50 cursor-pointer transition-colors"
                >
                  <div className="min-w-0">
                    <div className="text-sm font-medium text-slate-800 truncate">{apr.purpose}</div>
                    <div className="text-xs text-slate-400">{apr.requester} · 만료: {apr.expiresAt.split(' ')[1]}</div>
                  </div>
                  <span className="text-xs font-medium text-amber-700 bg-amber-50 px-2 py-0.5 rounded-full shrink-0 ml-2">
                    대기중
                  </span>
                </div>
              ))}
            </div>
          )}
        </SectionCard>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-4">
        {/* MCP Server Health */}
        <SectionCard title="MCP Server Health" linkTo="/mcp/servers">
          <div className="space-y-2">
            {mockMCPServers.slice(0, 4).map(srv => (
              <div key={srv.id} className="flex items-center justify-between py-1.5">
                <div className="flex items-center gap-2 min-w-0">
                  <Server size={13} className="text-slate-400 shrink-0" />
                  <span className="text-sm text-slate-700 truncate">{srv.name}</span>
                </div>
                <StatusBadge status={srv.status} size="sm" />
              </div>
            ))}
          </div>
          {unhealthyServers.length > 0 && (
            <div className="mt-3 p-2.5 bg-amber-50 border border-amber-100 rounded-lg flex items-center gap-2">
              <AlertTriangle size={13} className="text-amber-600 shrink-0" />
              <span className="text-xs text-amber-700">{unhealthyServers.length}개 서버가 비활성화 상태입니다.</span>
            </div>
          )}
        </SectionCard>

        {/* Tool Issues */}
        <SectionCard title="Tool 이슈" linkTo="/mcp/tools">
          {problematicTools.length === 0 ? (
            <div className="py-4 text-center text-sm text-slate-400">이슈 없음</div>
          ) : (
            <div className="space-y-2">
              {problematicTools.map(t => (
                <div key={t.id} className="flex items-center justify-between py-1.5">
                  <div className="flex items-center gap-2 min-w-0">
                    <Wrench size={13} className="text-slate-400 shrink-0" />
                    <span className="text-sm text-slate-700 truncate">{t.displayName}</span>
                  </div>
                  <StatusBadge status={t.status} size="sm" />
                </div>
              ))}
            </div>
          )}
          <div className="mt-3 p-2.5 bg-slate-50 border border-slate-100 rounded-lg">
            <div className="text-xs text-slate-500 font-medium">Verification 만료</div>
            <div className="text-xs text-orange-600 mt-1">Generate Report — EXPIRED</div>
          </div>
        </SectionCard>

        {/* Recently Used Agents */}
        <SectionCard title="최근 사용 Agent" linkTo="/agents">
          <div className="space-y-2">
            {[
              { name: 'Report Assistant', usage: '14:30 실행' },
              { name: 'General Work Assistant', usage: '13:55 실행' },
              { name: 'Research Assistant', usage: '14:10 실행' },
            ].map((agent, i) => (
              <div key={i} className="flex items-center gap-2 py-1.5">
                <div className="w-6 h-6 rounded-md bg-indigo-100 flex items-center justify-center shrink-0">
                  <Bot size={12} className="text-indigo-600" />
                </div>
                <div className="min-w-0">
                  <div className="text-sm text-slate-700 truncate">{agent.name}</div>
                  <div className="text-xs text-slate-400">{agent.usage}</div>
                </div>
              </div>
            ))}
          </div>
        </SectionCard>
      </div>
    </div>
  );
}
