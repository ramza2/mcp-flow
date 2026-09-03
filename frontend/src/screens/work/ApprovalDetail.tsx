import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, AlertTriangle, CheckCircle2 } from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import { mockApprovals } from '../../data/mock';

export default function ApprovalDetail() {
  const { approvalId } = useParams();
  const navigate = useNavigate();
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectComment, setRejectComment] = useState('');
  const [approving, setApproving] = useState(false);
  const [done, setDone] = useState(false);

  const approval = mockApprovals.find(a => a.id === approvalId) ?? mockApprovals[0];
  const isPending = approval.status === 'PENDING' && !done;

  const handleApprove = async () => {
    setApproving(true);
    await delay(1000);
    setApproving(false);
    setDone(true);
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/approvals')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Approvals
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{approval.purpose}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={done ? 'APPROVED' : approval.status === 'PENDING' ? 'WAITING_APPROVAL' : approval.status} />
              <span className="text-xs text-slate-400">Execution: <span className="font-mono">{approval.executionId}</span></span>
            </div>
          </div>
          {isPending && (
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setRejectOpen(true)}>거절</Button>
              <Button size="sm" loading={approving} icon={<CheckCircle2 size={13} />} onClick={handleApprove}>승인</Button>
            </div>
          )}
          {done && (
            <div className="flex items-center gap-2 text-sm text-green-600 font-medium">
              <CheckCircle2 size={16} /> 승인 완료
            </div>
          )}
        </div>
      </div>

      <div className="p-6 grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
        {/* Context */}
        <InfoCard title="원본 요청">
          <p className="text-sm text-slate-700 bg-slate-50 rounded-lg p-3">{approval.purpose}</p>
        </InfoCard>

        <InfoCard title="Tool 정보">
          <div className="space-y-2 text-sm">
            <Row label="Tool">{<span className="font-mono text-slate-700">{approval.tool}</span>}</Row>
            <Row label="Risk"><RiskBadge risk={approval.riskClass} /></Row>
            <Row label="Agent">{<span className="text-slate-700">{approval.agent}</span>}</Row>
          </div>
        </InfoCard>

        <InfoCard title="Risk 설명">
          <div className={`p-3 rounded-lg border ${
            approval.riskClass === 'DESTRUCTIVE' ? 'bg-red-50 border-red-200' :
            approval.riskClass === 'NON_IDEMPOTENT_WRITE' ? 'bg-orange-50 border-orange-200' :
            'bg-blue-50 border-blue-200'
          }`}>
            {approval.riskClass === 'NON_IDEMPOTENT_WRITE' && (
              <>
                <p className="text-sm font-semibold text-orange-800">External side effect</p>
                <p className="text-xs text-orange-700 mt-1">중복 실행 시 추가 작업이 발생할 수 있습니다. 이메일은 한 번만 발송되도록 주의하세요.</p>
              </>
            )}
            {approval.riskClass === 'DESTRUCTIVE' && (
              <>
                <p className="text-sm font-semibold text-red-800 flex items-center gap-1.5"><AlertTriangle size={14} /> Destructive action</p>
                <p className="text-xs text-red-700 mt-1">삭제 또는 복구하기 어려운 작업이 포함됩니다.</p>
              </>
            )}
          </div>
        </InfoCard>

        <InfoCard title="승인 정책">
          <div className="space-y-2 text-sm">
            <Row label="Policy">{<span className="text-slate-700">Standard Email Approval</span>}</Row>
            <Row label="요청자">{<span className="font-mono text-slate-700">{approval.requester}</span>}</Row>
            <Row label="요청 시각">{<span className="text-slate-700">{approval.requestedAt}</span>}</Row>
            <Row label="만료 시각">{<span className="text-slate-700 font-medium text-amber-700">{approval.expiresAt}</span>}</Row>
            <Row label="Self Approval">{<span className="text-slate-500">허용 안 됨</span>}</Row>
          </div>
        </InfoCard>

        {isPending && (
          <div className="col-span-full bg-white border border-slate-200 rounded-xl p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">승인 / 거절</h3>
            <div className="flex gap-3">
              <Button variant="outline" onClick={() => setRejectOpen(true)}>거절</Button>
              <Button loading={approving} icon={<CheckCircle2 size={13} />} onClick={handleApprove}>
                승인하기
              </Button>
            </div>
            {approval.riskClass !== 'READ_ONLY' && (
              <p className="text-xs text-slate-400 mt-2">승인 전 위의 Risk 정보를 반드시 확인하세요.</p>
            )}
          </div>
        )}
      </div>

      {/* Reject dialog */}
      <Dialog
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        title="승인 거절"
        description="거절 사유를 입력하세요. (필수)"
        size="sm"
        footer={
          <>
            <Button variant="outline" onClick={() => setRejectOpen(false)}>취소</Button>
            <Button variant="danger" onClick={() => { setRejectOpen(false); setDone(true); }}>거절 확정</Button>
          </>
        }
      >
        <textarea
          value={rejectComment}
          onChange={e => setRejectComment(e.target.value)}
          placeholder="거절 사유를 입력하세요..."
          rows={3}
          className="w-full text-sm border border-slate-200 rounded-lg px-3 py-2 focus:outline-none focus:ring-2 focus:ring-red-400"
        />
      </Dialog>
    </div>
  );
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      {children}
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-slate-400 w-24 shrink-0 text-xs">{label}</span>
      {children}
    </div>
  );
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
