import { useState } from 'react';
import { useNavigate } from 'react-router';
import { Search, AlertTriangle, ExternalLink, ArrowRight } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { mockDiscoveryCandidates } from '../../data/mock';
import { InlineAlert } from '../../components/ui/EmptyState';

export default function ExternalDiscovery() {
  const [q, setQ] = useState('');
  const [searched, setSearched] = useState(false);
  const [importing, setImporting] = useState<string | null>(null);

  const results = mockDiscoveryCandidates.filter(c =>
    !q || c.name.toLowerCase().includes(q.toLowerCase()) || c.description.includes(q)
  );

  const handleSearch = () => setSearched(true);

  const handleImport = async (id: string) => {
    setImporting(id);
    await delay(1200);
    setImporting(null);
  };

  const trustColor = (state: string) => ({
    CANDIDATE: 'bg-slate-100 text-slate-600',
    UNDER_REVIEW: 'bg-amber-50 text-amber-700',
    REVIEWED: 'bg-green-50 text-green-700',
  }[state] ?? 'bg-slate-100 text-slate-500');

  return (
    <div>
      <PageHeader
        title="External MCP Discovery"
        description="외부 레지스트리에서 MCP Server Candidate를 탐색합니다."
      />
      <div className="p-6 space-y-4 max-w-3xl">
        <InlineAlert type="warning" message="외부 MCP는 Candidate 상태입니다. Import가 즉시 운영 활성화를 의미하지 않습니다. 반드시 Connection Test → Tool Discovery → 검토 후 활성화하세요." />

        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              value={q}
              onChange={e => setQ(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleSearch()}
              placeholder="Slack, Notion, GitHub..."
              className="w-full h-10 pl-9 pr-4 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-indigo-500"
            />
          </div>
          <Button onClick={handleSearch} icon={<Search size={13} />}>검색</Button>
        </div>

        {searched && (
          <div className="space-y-3">
            <p className="text-xs text-slate-500">{results.length}개 Candidate 검색됨</p>
            {results.map(c => (
              <div key={c.id} className="bg-white rounded-xl border border-slate-200 p-4">
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2 mb-1">
                      <h3 className="text-sm font-semibold text-slate-800">{c.name}</h3>
                      <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${trustColor(c.trustState)}`}>
                        {c.trustState}
                      </span>
                    </div>
                    <p className="text-sm text-slate-600 mb-2">{c.description}</p>
                    <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-400">
                      <span>Registry: {c.registry}</span>
                      <span>Transport: {c.transport}</span>
                      <span>Version: {c.version}</span>
                      {c.repository && (
                        <a href="#" className="flex items-center gap-0.5 text-indigo-600 hover:underline">
                          Repository <ExternalLink size={10} />
                        </a>
                      )}
                    </div>
                    {c.warning && (
                      <div className="mt-2 flex items-center gap-1.5 text-xs text-amber-700">
                        <AlertTriangle size={12} /> {c.warning}
                      </div>
                    )}
                  </div>
                  <div className="shrink-0">
                    <Button
                      size="sm"
                      variant="outline"
                      loading={importing === c.id}
                      icon={<ArrowRight size={12} />}
                      onClick={() => handleImport(c.id)}
                    >
                      Import
                    </Button>
                  </div>
                </div>

                {/* Flow hint */}
                <div className="mt-3 flex items-center gap-1 text-xs text-slate-300 flex-wrap">
                  {['Import', '→ Draft Server 생성', '→ Connection Test', '→ Tool Discovery', '→ 검토', '→ 활성화'].map((s, i) => (
                    <span key={i} className={i === 0 ? 'text-slate-500' : ''}>{s}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}

        {!searched && (
          <div className="flex flex-col items-center py-12 text-center">
            <Search size={32} className="text-slate-300 mb-3" />
            <p className="text-sm text-slate-500">외부 MCP 레지스트리를 검색하세요.</p>
            <p className="text-xs text-slate-400 mt-1">검색 결과는 Candidate로 표시됩니다.</p>
          </div>
        )}
      </div>
    </div>
  );
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
