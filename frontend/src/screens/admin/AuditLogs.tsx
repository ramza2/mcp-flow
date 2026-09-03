import { useState } from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import FilterBar from '../../components/ui/FilterBar';
import { mockAuditLogs } from '../../data/mock';

export default function AuditLogs() {
  const [expanded, setExpanded] = useState<string | null>(null);

  return (
    <div>
      <PageHeader title="Audit Logs" description="시스템 전체 감사 로그를 검색하고 조회합니다." />
      <div className="p-6 space-y-4">
        <FilterBar
          search searchPlaceholder="Actor, Action, Resource 검색..."
          filters={[
            { key: 'result', label: 'Result', options: [{ value: 'SUCCESS', label: 'Success' }, { value: 'FAILED', label: 'Failed' }] },
            { key: 'action', label: 'Action', options: [
              { value: 'execution.start', label: 'execution.start' },
              { value: 'approval.request', label: 'approval.request' },
              { value: 'agent.publish', label: 'agent.publish' },
            ]},
          ]}
        />
        <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50">
                {['Time', 'Actor', 'Action', 'Resource', 'Result', 'Request ID'].map(h => (
                  <th key={h} className="text-left px-4 py-3 text-xs font-semibold text-slate-500 uppercase tracking-wide">{h}</th>
                ))}
                <th className="px-4 py-3 w-8" />
              </tr>
            </thead>
            <tbody>
              {mockAuditLogs.map(log => (
                <>
                  <tr
                    key={log.id}
                    className="border-b border-slate-100 hover:bg-slate-50 cursor-pointer transition-colors"
                    onClick={() => setExpanded(expanded === log.id ? null : log.id)}
                  >
                    <td className="px-4 py-3 font-mono text-xs text-slate-500 whitespace-nowrap">{log.time}</td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-700">{log.actor}</td>
                    <td className="px-4 py-3 font-mono text-xs text-indigo-600">{log.action}</td>
                    <td className="px-4 py-3 text-slate-700">{log.resource}</td>
                    <td className="px-4 py-3">
                      <span className={`text-xs font-medium px-2 py-0.5 rounded-full ${log.result === 'SUCCESS' ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'}`}>
                        {log.result}
                      </span>
                    </td>
                    <td className="px-4 py-3 font-mono text-xs text-slate-400">{log.requestId}</td>
                    <td className="px-4 py-3 text-slate-400">
                      {expanded === log.id ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    </td>
                  </tr>
                  {expanded === log.id && (
                    <tr key={`${log.id}-detail`} className="border-b border-slate-100 bg-slate-50">
                      <td colSpan={7} className="px-4 py-3">
                        <div className="grid grid-cols-3 gap-4 text-xs">
                          <div>
                            <p className="font-semibold text-slate-500 mb-1">Before</p>
                            <pre className="font-mono bg-white rounded p-2 border border-slate-200 text-slate-600">
                              {log.action === 'execution.start' ? '{}' : '{ "status": "DRAFT" }'}
                            </pre>
                          </div>
                          <div>
                            <p className="font-semibold text-slate-500 mb-1">After</p>
                            <pre className="font-mono bg-white rounded p-2 border border-slate-200 text-slate-600">
                              {log.action === 'execution.start' ? '{ "status": "RUNNING" }' : '{ "status": "PUBLISHED" }'}
                            </pre>
                          </div>
                          <div>
                            <p className="font-semibold text-slate-500 mb-1">Change Summary</p>
                            <p className="text-slate-600">{log.action}</p>
                            <p className="text-slate-400 mt-1">Request ID: {log.requestId}</p>
                          </div>
                        </div>
                      </td>
                    </tr>
                  )}
                </>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
