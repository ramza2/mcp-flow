import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';
import StatusBadge, { RiskBadge } from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import { mockApprovals } from '../../data/mock';
import type { ApprovalStatus } from '../../domain';
import PermissionGate from '../../components/PermissionGate';

export default function ApprovalDetail() {
  const { approvalId } = useParams();
  const navigate = useNavigate();
  const [rejectOpen, setRejectOpen] = useState(false);
  const [rejectComment, setRejectComment] = useState('');
  const [approving, setApproving] = useState(false);
  /** Distinct decision state — never reuse a single `done` flag for approve+reject. */
  const [decisionState, setDecisionState] = useState<ApprovalStatus | null>(null);

  const approval = mockApprovals.find(a => a.id === approvalId) ?? mockApprovals[0];
  const displayStatus: ApprovalStatus = decisionState ?? approval.status;
  const isPending = displayStatus === 'PENDING';
  const rejectCommentRequired = approval.rejectCommentRequired;
  const canConfirmReject = !rejectCommentRequired || rejectComment.trim().length > 0;

  const handleApprove = async () => {
    setApproving(true);
    await delay(800);
    setApproving(false);
    setDecisionState('APPROVED');
  };

  const handleRejectConfirm = () => {
    if (!canConfirmReject) return;
    setRejectOpen(false);
    setDecisionState('REJECTED');
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
              {/* Approval Entity Status — never map PENDING → WAITING_APPROVAL */}
              <StatusBadge status={displayStatus} />
              <span className="text-xs text-slate-400">
                Execution: <button className="font-mono text-indigo-600 hover:underline" onClick={() => navigate(`/executions/${approval.executionId}`)}>{approval.executionId}</button>
              </span>
            </div>
          </div>
          {isPending && (
            <PermissionGate permission="approval.approve">
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={() => setRejectOpen(true)}>거절</Button>
                <Button size="sm" loading={approving} icon={<CheckCircle2 size={13} />} onClick={handleApprove}>승인</Button>
              </div>
            </PermissionGate>
          )}
          {displayStatus === 'APPROVED' && (
            <div className="flex items-center gap-2 text-sm text-green-600 font-medium">
              <CheckCircle2 size={16} /> 승인 완료
            </div>
          )}
          {displayStatus === 'REJECTED' && (
            <div className="flex items-center gap-2 text-sm text-red-600 font-medium">
              <XCircle size={16} /> 거절 완료
              {rejectComment && <span className="text-xs text-slate-500 font-normal">· {rejectComment}</span>}
            </div>
          )}
        </div>
      </div>

      <div className="p-6 grid grid-cols-1 lg:grid-cols-2 gap-4 max-w-4xl">
        <InfoCard title="원본 요청 / 목적">
          <p className="text-sm text-slate-700 bg-slate-50 rounded-lg p-3">{approval.originalRequest}</p>
          <p className="text-xs text-slate-500 mt-2">목적: {approval.purpose}</p>
        </InfoCard>

        <InfoCard title="완료된 선행 Step">
          {approval.completedPriorSteps.length === 0 ? (
            <p className="text-sm text-slate-400">선행 Step 없음</p>
          ) : (
            <ol className="list-decimal list-inside text-sm text-slate-700 space-y-1">
              {approval.completedPriorSteps.map(s => <li key={s}>{s}</li>)}
            </ol>
          )}
        </InfoCard>

        <InfoCard title="승인 대상 Tool">
          <div className="space-y-2 text-sm">
            <Row label="Tool"><span className="font-mono text-slate-700">{approval.tool}</span></Row>
            <Row label="ToolVersion"><span className="font-mono text-slate-700">{approval.toolVersion}</span></Row>
            <Row label="MCP Server"><span className="text-slate-700">{approval.serverName}</span></Row>
            <Row label="Risk"><RiskBadge risk={approval.riskClass} /></Row>
            <Row label="Agent"><span className="text-slate-700">{approval.agent}</span></Row>
          </div>
        </InfoCard>

        <InfoCard title="Masked Input">
          <pre className="text-xs font-mono bg-slate-50 rounded-lg p-3 text-slate-700 overflow-auto">
            {JSON.stringify(approval.maskedInput, null, 2)}
          </pre>
        </InfoCard>

        <InfoCard title="외부 Side Effect / Policy">
          <div className="space-y-2 text-sm">
            <Row label="External Side Effect">
              <span className={approval.externalSideEffect ? 'text-orange-700 font-medium' : 'text-slate-500'}>
                {approval.externalSideEffect ? 'Yes' : 'No'}
              </span>
            </Row>
            <Row label="ToolPolicy"><span className="text-slate-700">{approval.toolPolicy}</span></Row>
            <Row label="ApprovalPolicy"><span className="text-slate-700">{approval.approvalPolicyName}</span></Row>
          </div>
          {approval.riskClass === 'NON_IDEMPOTENT_WRITE' && (
            <div className="mt-3 p-3 rounded-lg border bg-orange-50 border-orange-200">
              <p className="text-sm font-semibold text-orange-800">External side effect</p>
              <p className="text-xs text-orange-700 mt-1">중복 실행 시 추가 작업이 발생할 수 있습니다.</p>
            </div>
          )}
          {approval.riskClass === 'DESTRUCTIVE' && (
            <div className="mt-3 p-3 rounded-lg border bg-red-50 border-red-200">
              <p className="text-sm font-semibold text-red-800 flex items-center gap-1.5"><AlertTriangle size={14} /> Destructive action</p>
              <p className="text-xs text-red-700 mt-1">삭제 또는 복구하기 어려운 작업이 포함됩니다.</p>
            </div>
          )}
        </InfoCard>

        <InfoCard title="요청 메타">
          <div className="space-y-2 text-sm">
            <Row label="Requester"><span className="font-mono text-slate-700">{approval.requester}</span></Row>
            <Row label="Executor"><span className="font-mono text-slate-700">{approval.executor}</span></Row>
            <Row label="Requested"><span className="text-slate-700">{approval.requestedAt}</span></Row>
            <Row label="Expiry"><span className="text-amber-700 font-medium">{approval.expiresAt}</span></Row>
          </div>
          <p className="text-[10px] text-slate-400 mt-3">
            Approval Reject/Expiry는 Execution을 REJECTED/EXPIRED로 만들지 않습니다. Execution은 FAILED / PARTIALLY_SUCCEEDED / CANCELLED 등으로 판정됩니다.
          </p>
        </InfoCard>

        {isPending && (
          <div className="col-span-full bg-white border border-slate-200 rounded-xl p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">승인 / 거절</h3>
            <PermissionGate permission="approval.approve">
              <div className="flex gap-3">
                <Button variant="outline" onClick={() => setRejectOpen(true)}>거절</Button>
                <Button loading={approving} icon={<CheckCircle2 size={13} />} onClick={handleApprove}>승인하기</Button>
              </div>
            </PermissionGate>
          </div>
        )}
      </div>

      <Dialog
        open={rejectOpen}
        onClose={() => setRejectOpen(false)}
        title="승인 거절"
        description={rejectCommentRequired ? '거절 사유를 입력하세요. (필수)' : '거절 사유를 입력하세요. (선택)'}
        size="sm"
        footer={
          <>
            <Button variant="outline" onClick={() => setRejectOpen(false)}>취소</Button>
            <Button variant="danger" disabled={!canConfirmReject} onClick={handleRejectConfirm}>거절 확정</Button>
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
        {rejectCommentRequired && !rejectComment.trim() && (
          <p className="text-xs text-red-500 mt-2">이 ApprovalPolicy는 Reject Comment가 필수입니다.</p>
        )}
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
    <div className="flex items-start gap-3">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <div className="min-w-0">{children}</div>
    </div>
  );
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
