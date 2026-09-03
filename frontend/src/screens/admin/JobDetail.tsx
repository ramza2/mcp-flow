import { useNavigate, useParams } from 'react-router';
import { ArrowLeft } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { mockJobs } from '../../data/mock';

export default function JobDetail() {
  const { jobId } = useParams();
  const navigate = useNavigate();
  const job = mockJobs.find(j => j.id === jobId) ?? mockJobs[0];

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/admin/jobs')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Jobs
        </button>
        <div className="flex items-center gap-3">
          <h1 className="text-lg font-semibold text-slate-900">{job.type}</h1>
          <StatusBadge status={job.status} />
        </div>
      </div>
      <div className="p-6 max-w-xl space-y-4">
        <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
          <Row label="Resource">{job.resource}</Row>
          <Row label="Status"><StatusBadge status={job.status} size="sm" /></Row>
          <Row label="Progress">{job.progress ?? '계산 불가'}</Row>
          <Row label="시작">{job.started}</Row>
          <Row label="Duration" mono>{job.duration}</Row>
          {job.error && <Row label="오류"><span className="text-red-600">{job.error}</span></Row>}
        </div>

        {job.status === 'FAILED' && (
          <div className="bg-red-50 border border-red-200 rounded-xl p-4">
            <p className="text-sm font-semibold text-red-700">오류 상세</p>
            <p className="text-sm text-red-600 mt-1 font-mono">{job.error}</p>
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-24 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
