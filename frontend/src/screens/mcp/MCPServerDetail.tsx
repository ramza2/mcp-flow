import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import { mockMCPServers, mockTools } from '../../data/mock';
import DataTable, { Column } from '../../components/ui/DataTable';

export default function MCPServerDetail() {
  const { serverId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const server = mockMCPServers.find(s => s.id === serverId) ?? mockMCPServers[0];
  const serverTools = mockTools.filter(t => t.serverId === server.id);

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/mcp/servers')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> MCP Servers
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{server.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={server.status} />
              <span className="text-xs text-slate-400 font-mono">{server.transport}</span>
              <span className="text-xs text-slate-400">{server.protocol} · {server.version}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" icon={<RefreshCw size={13} />}>Discovery 실행</Button>
            <Button variant="secondary" size="sm">연결 테스트</Button>
          </div>
        </div>
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'tools', label: 'Tools', badge: serverTools.length },
            { id: 'discovery', label: 'Discovery History' },
            { id: 'health', label: 'Health' },
            { id: 'audit', label: 'Audit' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-2xl">
            <InfoCard title="연결 정보">
              <Row label="Transport">{server.transport}</Row>
              <Row label="Protocol">{server.protocol}</Row>
              <Row label="Version" mono>{server.version}</Row>
              <Row label="Discovery">{server.discovery}</Row>
              <Row label="Last Health">{server.lastHealth}</Row>
            </InfoCard>
            <InfoCard title="인증">
              <Row label="방식">API Key</Row>
              <Row label="상태">설정됨</Row>
              <Row label="원문">••••••••••••</Row>
            </InfoCard>
          </div>
        )}
        {tab === 'tools' && (
          <div className="max-w-4xl">
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <DataTable
                columns={toolCols}
                data={serverTools}
                rowKey={r => r.id}
                onRowClick={r => navigate(`/mcp/tools/${r.id}`)}
                emptyMessage="이 서버에 등록된 Tool이 없습니다."
              />
            </div>
          </div>
        )}
        {tab === 'discovery' && (
          <div className="max-w-2xl space-y-2">
            {[
              { time: '2026-09-02 14:20', result: 'SUCCEEDED', discovered: 3, added: 0 },
              { time: '2026-09-01 10:00', result: 'SUCCEEDED', discovered: 3, added: 3 },
            ].map((d, i) => (
              <div key={i} className="bg-white border border-slate-200 rounded-lg p-4 flex justify-between items-center">
                <div>
                  <p className="text-sm font-medium text-slate-800">{d.time}</p>
                  <p className="text-xs text-slate-400">발견: {d.discovered}개 · 신규: {d.added}개</p>
                </div>
                <StatusBadge status={d.result} size="sm" />
              </div>
            ))}
          </div>
        )}
        {tab === 'health' && (
          <div className="max-w-xl space-y-3">
            {[
              { time: '14:20', latency: '42ms', status: 'SUCCEEDED' },
              { time: '14:10', latency: '38ms', status: 'SUCCEEDED' },
              { time: '14:00', latency: '51ms', status: 'SUCCEEDED' },
              { time: '13:50', latency: '–', status: 'FAILED' },
            ].map((h, i) => (
              <div key={i} className="bg-white border border-slate-200 rounded-lg px-4 py-3 flex items-center justify-between">
                <span className="text-xs font-mono text-slate-500">{h.time}</span>
                <span className="text-sm font-mono text-slate-600">{h.latency}</span>
                <StatusBadge status={h.status} size="sm" />
              </div>
            ))}
          </div>
        )}
        {tab === 'audit' && (
          <div className="max-w-2xl bg-white border border-slate-200 rounded-xl overflow-hidden">
            {[
              { time: '14:20', actor: 'system', action: 'server.health_check', result: 'SUCCESS' },
              { time: '10:00', actor: 'admin', action: 'server.discover_tools', result: 'SUCCESS' },
            ].map((log, i) => (
              <div key={i} className="flex gap-4 px-4 py-3 border-b last:border-0 text-sm">
                <span className="font-mono text-xs text-slate-400">{log.time}</span>
                <span className="text-slate-600">{log.actor}</span>
                <span className="font-mono text-xs text-indigo-600">{log.action}</span>
                <span className={`ml-auto text-xs ${log.result === 'SUCCESS' ? 'text-green-600' : 'text-red-600'}`}>{log.result}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

import { RiskBadge, VerificationBadge } from '../../components/ui/StatusBadge';

const toolCols: Column<typeof mockTools[0]>[] = [
  { key: 'displayName', label: 'Display Name', render: r => <span className="font-medium text-slate-800">{r.displayName}</span> },
  { key: 'sourceName', label: 'Source Name', render: r => <span className="font-mono text-xs text-slate-500">{r.sourceName}</span> },
  { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
  { key: 'riskClass', label: 'Risk', render: r => <RiskBadge risk={r.riskClass} /> },
  { key: 'verification', label: 'Verification', render: r => <VerificationBadge status={r.verification} /> },
];

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex gap-4">
      <span className="text-slate-400 w-24 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
