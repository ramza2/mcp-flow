import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Check, ChevronRight, Upload } from 'lucide-react';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';

const STEPS = ['Source', 'Analyze', 'Candidate Tools', 'Build', 'Security / Contract Test', 'Review', 'Publish'];

export default function ToolFactoryNew() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [sourceType, setSourceType] = useState('OpenAPI');
  const [building, setBuilding] = useState(false);

  const handleBuild = async () => {
    setBuilding(true);
    await delay(2000);
    setBuilding(false);
    setStep(4);
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/tool-factory')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Tool Factory
        </button>
        <h1 className="text-lg font-semibold text-slate-900">새 Tool 생성</h1>
      </div>

      {/* Step bar */}
      <div className="bg-white border-b border-slate-200 px-6 py-4 overflow-x-auto">
        <div className="flex items-center gap-0">
          {STEPS.map((s, i) => (
            <div key={s} className="flex items-center shrink-0">
              <div className={`flex items-center gap-1.5 text-xs font-medium px-1
                ${i === step ? 'text-indigo-600' : i < step ? 'text-green-600' : 'text-slate-400'}`}>
                <span className={`w-5 h-5 rounded-full flex items-center justify-center border text-[10px]
                  ${i === step ? 'border-indigo-600 bg-indigo-600 text-white' : i < step ? 'border-green-500 bg-green-500 text-white' : 'border-slate-300 text-slate-400'}`}>
                  {i < step ? <Check size={10} /> : i + 1}
                </span>
                <span className="whitespace-nowrap">{s}</span>
              </div>
              {i < STEPS.length - 1 && <ChevronRight size={12} className="text-slate-300 mx-1.5" />}
            </div>
          ))}
        </div>
      </div>

      <div className="p-6 max-w-xl space-y-4">
        {step === 0 && (
          <Section title="Source Type 선택">
            <div className="space-y-2">
              {['OpenAPI', 'Python'].map(t => (
                <label key={t} className={`flex items-start gap-3 p-4 rounded-xl border-2 cursor-pointer ${sourceType === t ? 'border-indigo-400 bg-indigo-50' : 'border-slate-200'}`}>
                  <input type="radio" checked={sourceType === t} onChange={() => setSourceType(t)} className="mt-0.5 accent-indigo-600" />
                  <div>
                    <p className="text-sm font-semibold text-slate-800">{t}</p>
                    <p className="text-xs text-slate-500 mt-0.5">
                      {t === 'OpenAPI' ? 'OpenAPI 3.0/Swagger YAML/JSON에서 Tool 자동 생성' : 'Python 함수/모듈에서 Tool 추출'}
                    </p>
                  </div>
                </label>
              ))}
            </div>

            <Field label="Source 업로드">
              <div className="border-2 border-dashed border-slate-300 rounded-xl p-8 text-center cursor-pointer hover:border-indigo-400 hover:bg-indigo-50 transition-colors">
                <Upload size={22} className="text-slate-300 mx-auto mb-2" />
                <p className="text-sm text-slate-500">파일을 드래그하거나 클릭하여 업로드</p>
                <p className="text-xs text-slate-400 mt-1">{sourceType === 'OpenAPI' ? '.yaml, .json' : '.py, .zip'}</p>
              </div>
            </Field>
          </Section>
        )}

        {step === 1 && (
          <Section title="소스 분석 중...">
            <div className="flex flex-col items-center py-8 gap-3">
              <div className="w-10 h-10 rounded-full border-4 border-indigo-600 border-t-transparent animate-spin" />
              <p className="text-sm text-slate-500">OpenAPI 스펙을 분석하여 Tool Candidate를 추출하고 있습니다...</p>
            </div>
          </Section>
        )}

        {step === 2 && (
          <Section title="Candidate Tools (3개)">
            <InlineAlert type="info" message="아래 Tool Candidate를 검토하세요. 생성 성공이 Tool 활성화 성공을 의미하지 않습니다." />
            {[
              { name: 'get_employee_info', desc: '직원 정보 조회', risk: 'READ_ONLY' },
              { name: 'update_employee', desc: '직원 정보 수정', risk: 'IDEMPOTENT_WRITE' },
              { name: 'delete_employee', desc: '직원 삭제', risk: 'DESTRUCTIVE' },
            ].map(t => (
              <div key={t.name} className="flex items-center gap-3 p-3 rounded-lg border border-slate-200">
                <input type="checkbox" defaultChecked={t.risk !== 'DESTRUCTIVE'} className="accent-indigo-600" />
                <div className="flex-1">
                  <p className="text-sm font-mono text-slate-700">{t.name}</p>
                  <p className="text-xs text-slate-400">{t.desc}</p>
                </div>
                <span className={`text-xs px-2 py-0.5 rounded-full font-medium
                  ${t.risk === 'READ_ONLY' ? 'bg-green-50 text-green-700' : t.risk === 'IDEMPOTENT_WRITE' ? 'bg-blue-50 text-blue-700' : 'bg-red-50 text-red-700'}`}>
                  {t.risk}
                </span>
              </div>
            ))}
          </Section>
        )}

        {step === 3 && (
          <Section title="Build">
            <p className="text-sm text-slate-600">선택한 Tool을 MCP Tool로 빌드합니다.</p>
            <Button loading={building} onClick={handleBuild} icon={<Check size={13} />}>Build 시작</Button>
          </Section>
        )}

        {step >= 4 && step < 6 && (
          <Section title={STEPS[step]}>
            {step === 4 && (
              <div className="space-y-3">
                <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-sm text-green-700">✓ Security Scan 통과 (이슈 없음)</div>
                <div className="p-3 bg-green-50 border border-green-200 rounded-lg text-sm text-green-700">✓ Contract Test 통과 (2/2)</div>
                <div className="p-3 bg-amber-50 border border-amber-200 rounded-lg text-sm text-amber-700">⚠ delete_employee: DESTRUCTIVE 등급 — 수동 검토 권장</div>
              </div>
            )}
            {step === 5 && (
              <div className="space-y-3">
                <InlineAlert type="warning" message="Publish 후에도 MCP Server에 등록하고 Tool Discovery를 실행해야 활성화됩니다." />
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between"><span className="text-slate-400">생성 Tool</span><span>2개 (delete_employee 제외)</span></div>
                  <div className="flex justify-between"><span className="text-slate-400">Source Type</span><span>OpenAPI</span></div>
                  <div className="flex justify-between"><span className="text-slate-400">Build Status</span><span className="text-green-600">SUCCEEDED</span></div>
                </div>
              </div>
            )}
          </Section>
        )}

        {step === 6 && (
          <Section title="Publish 완료">
            <div className="flex flex-col items-center py-6 gap-3 text-center">
              <div className="w-12 h-12 rounded-full bg-green-100 flex items-center justify-center">
                <Check size={22} className="text-green-600" />
              </div>
              <p className="text-sm font-semibold text-slate-800">Tool이 생성되었습니다.</p>
              <p className="text-xs text-slate-500">MCP Server에 등록하고 Tool Discovery를 실행하면 활성화됩니다.</p>
              <Button size="sm" onClick={() => navigate('/tool-factory')}>목록으로</Button>
            </div>
          </Section>
        )}

        <div className="flex justify-between pt-2">
          <Button variant="outline" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0 || building}>이전</Button>
          {step < STEPS.length - 1 && step !== 3 && (
            <Button onClick={() => setStep(step + 1)}>다음</Button>
          )}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-4">{title}</h3>
      <div className="space-y-4">{children}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
