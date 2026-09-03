import { useState } from 'react';
import { Plus } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { mockApprovalPolicies } from '../../data/mock';

export default function ApprovalPolicies() {
  const [selected, setSelected] = useState(mockApprovalPolicies[0]);

  return (
    <div>
      <PageHeader
        title="Approval Policies"
        description="승인 정책을 구성하고 Tool 및 Workflow에 적용합니다."
        actions={<Button icon={<Plus size={14} />} size="sm">새 Policy</Button>}
      />
      <div className="flex h-[calc(100vh-160px)]">
        {/* List */}
        <div className="w-72 border-r border-slate-200 bg-white overflow-y-auto">
          {mockApprovalPolicies.map(p => (
            <button
              key={p.id}
              onClick={() => setSelected(p)}
              className={`w-full text-left px-4 py-3 border-b border-slate-100 hover:bg-slate-50 transition-colors
                ${selected.id === p.id ? 'bg-indigo-50' : ''}`}
            >
              <div className="flex items-center justify-between">
                <span className="text-sm font-medium text-slate-800">{p.name}</span>
                <StatusBadge status={p.status} size="sm" />
              </div>
              <p className="text-xs text-slate-400 mt-0.5">{p.decisionMode} · {p.requiredApprovals}명 필요</p>
            </button>
          ))}
        </div>

        {/* Detail */}
        <div className="flex-1 p-6 overflow-y-auto">
          <div className="max-w-lg space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-base font-semibold text-slate-900">{selected.name}</h2>
              <Button size="sm" variant="outline">편집</Button>
            </div>
            <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
              <Row label="상태"><StatusBadge status={selected.status} size="sm" /></Row>
              <Row label="Decision Mode">{selected.decisionMode}</Row>
              <Row label="Required Approvals">{selected.requiredApprovals}명</Row>
              <Row label="Approver Roles">{selected.approverRoles.join(', ')}</Row>
              <Row label="만료 시간">{selected.expiryMinutes}분</Row>
              <Row label="Self Approval">{selected.selfApproval ? '허용' : '불허'}</Row>
              <Row label="거절 코멘트">{selected.rejectCommentRequired ? '필수' : '선택'}</Row>
            </div>
            <div className="bg-blue-50 border border-blue-200 rounded-xl p-4 text-sm text-blue-700">
              이 Policy는 Tool Policy 또는 Workflow Approval 설정에서 선택할 수 있습니다.
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-36 shrink-0 text-xs">{label}</span>
      <span className="text-slate-700">{children}</span>
    </div>
  );
}
