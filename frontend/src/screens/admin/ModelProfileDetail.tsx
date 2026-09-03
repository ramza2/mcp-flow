import { useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, RefreshCw, Check } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import Button from '../../components/ui/Button';
import { mockModelProfiles } from '../../data/mock';

export default function ModelProfileDetail() {
  const { profileId } = useParams();
  const navigate = useNavigate();
  const [testing, setTesting] = useState(false);
  const [testOk, setTestOk] = useState(false);
  const profile = mockModelProfiles.find(p => p.id === profileId) ?? mockModelProfiles[0];

  const runTest = async () => {
    setTesting(true);
    await delay(1500);
    setTesting(false);
    setTestOk(true);
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/admin/model-profiles')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Model Profiles
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{profile.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={profile.status} />
              <span className="text-xs text-slate-400">{profile.provider} · {profile.type}</span>
            </div>
          </div>
          <Button variant="outline" size="sm">편집</Button>
        </div>
      </div>

      <div className="p-6 max-w-xl space-y-4">
        <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3 text-sm">
          <Row label="Provider">{profile.provider}</Row>
          <Row label="Model" mono>{profile.model}</Row>
          <Row label="Base URL" mono>{profile.baseUrl}</Row>
          <Row label="Secret">설정됨 ••••••••••••</Row>
          <Row label="수정일">{profile.updatedAt}</Row>
        </div>

        <div className="bg-white rounded-xl border border-slate-200 p-4">
          <h3 className="text-sm font-semibold text-slate-800 mb-3">연결 테스트</h3>
          <div className="flex items-center gap-3">
            <Button variant="secondary" size="sm" loading={testing} icon={<RefreshCw size={12} />} onClick={runTest}>
              연결 테스트
            </Button>
            {testOk && (
              <div className="flex items-center gap-1.5 text-sm text-green-600">
                <Check size={13} /> 연결 성공
              </div>
            )}
          </div>
          <p className="text-xs text-slate-400 mt-2">Secret 원문은 표시되지 않습니다.</p>
        </div>
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

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
