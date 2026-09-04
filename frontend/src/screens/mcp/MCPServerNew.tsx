import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Check, ChevronRight } from 'lucide-react';
import Button from '../../components/ui/Button';
import { EmptyState, InlineAlert } from '../../components/ui/EmptyState';
import { createMCPServer } from '../../api/mcp';
import { ApiError, isApiError } from '../../api/client';
import {
  CURRENT_MCP_PROTOCOL_VERSION,
  MCP_AUTH_TYPES,
  labelAuthType,
  type MCPAuthType,
} from '../../domain';

const STEPS = [
  'Basic',
  'Transport',
  'Authentication',
  'Connection Test',
  'Protocol / Capability',
  'Tool Preview',
  'Review & Register',
];

export default function MCPServerNew() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [endpointUrl, setEndpointUrl] = useState('');
  const [authType, setAuthType] = useState<MCPAuthType>('NONE');
  const [registering, setRegistering] = useState(false);
  const [registerError, setRegisterError] = useState<string | null>(null);

  const handleRegister = async () => {
    setRegistering(true);
    setRegisterError(null);
    try {
      const server = await createMCPServer({
        name: name.trim(),
        description: description.trim() || null,
        transport_type: 'STREAMABLE_HTTP',
        endpoint_url: endpointUrl.trim(),
        auth_type: 'NONE',
      });
      navigate(`/mcp/servers/${server.id}`);
    } catch (err) {
      if (isApiError(err) && err.status === 422) {
        setRegisterError(err.message);
      } else if (isApiError(err)) {
        setRegisterError(err.message);
      } else {
        setRegisterError('서버 등록에 실패했습니다.');
      }
    } finally {
      setRegistering(false);
    }
  };

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button
          onClick={() => navigate('/mcp/servers')}
          className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3"
        >
          <ArrowLeft size={14} /> MCP Servers
        </button>
        <h1 className="text-lg font-semibold text-slate-900">MCP Server 등록</h1>
      </div>

      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <div className="flex items-center gap-0 overflow-x-auto">
          {STEPS.map((s, i) => (
            <div key={s} className="flex items-center shrink-0">
              <button
                onClick={() => (i < step ? setStep(i) : undefined)}
                className={`flex items-center gap-2 text-xs font-medium px-1
                  ${i === step ? 'text-indigo-600' : i < step ? 'text-green-600 cursor-pointer' : 'text-slate-400'}`}
              >
                <span
                  className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 border
                  ${i === step ? 'border-indigo-600 bg-indigo-600 text-white' : i < step ? 'border-green-500 bg-green-500 text-white' : 'border-slate-300 text-slate-400'}`}
                >
                  {i < step ? <Check size={11} /> : <span>{i + 1}</span>}
                </span>
                <span className="whitespace-nowrap">{s}</span>
              </button>
              {i < STEPS.length - 1 && <ChevronRight size={14} className="text-slate-300 mx-2" />}
            </div>
          ))}
        </div>
      </div>

      <div className="p-6 max-w-lg space-y-4">
        {step === 0 && (
          <Section title="기본 정보">
            <Field label="서버 이름">
              <input
                placeholder="Weather MCP"
                className={inputClass}
                value={name}
                onChange={e => setName(e.target.value)}
              />
            </Field>
            <Field label="설명">
              <textarea
                placeholder="서버 설명 입력..."
                rows={2}
                className={inputClass}
                value={description}
                onChange={e => setDescription(e.target.value)}
              />
            </Field>
          </Section>
        )}

        {step === 1 && (
          <Section title="Transport">
            <Field label="Transport Type">
              <div className="space-y-2">
                <label
                  className="flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors border-indigo-300 bg-indigo-50"
                >
                  <input type="radio" checked readOnly className="mt-0.5 accent-indigo-600" />
                  <div>
                    <p className="text-sm font-medium text-slate-800">Streamable HTTP</p>
                    <p className="text-xs text-slate-400">현재 MCP 표준 Transport</p>
                  </div>
                </label>
                <label className="flex items-start gap-3 p-3 rounded-lg border border-slate-200 bg-slate-50 opacity-60 cursor-not-allowed">
                  <input type="radio" disabled className="mt-0.5" />
                  <div>
                    <p className="text-sm font-medium text-slate-800">STDIO</p>
                    <p className="text-xs text-slate-400">Manifest 연동 후 사용 가능 (deferred)</p>
                  </div>
                </label>
                <label className="flex items-start gap-3 p-3 rounded-lg border border-slate-200 bg-slate-50 opacity-60 cursor-not-allowed">
                  <input type="radio" disabled className="mt-0.5" />
                  <div>
                    <p className="text-sm font-medium text-slate-800">Legacy HTTP/SSE</p>
                    <p className="text-xs text-slate-400">Legacy MCP 지원 — deferred</p>
                  </div>
                </label>
              </div>
            </Field>
            <Field label="Server URL">
              <input
                placeholder="https://mcp.example.com/mcp"
                className={inputClass}
                value={endpointUrl}
                onChange={e => setEndpointUrl(e.target.value)}
              />
            </Field>
          </Section>
        )}

        {step === 2 && (
          <Section title="Authentication">
            <Field label="인증 방식">
              <div className="space-y-2">
                <label className="flex items-start gap-3 p-3 rounded-lg border border-indigo-300 bg-indigo-50 cursor-pointer">
                  <input type="radio" checked readOnly className="mt-0.5 accent-indigo-600" />
                  <div>
                    <p className="text-sm font-medium text-slate-800">{labelAuthType('NONE')}</p>
                  </div>
                </label>
                {MCP_AUTH_TYPES.filter(t => t !== 'NONE').map(t => (
                  <label
                    key={t}
                    className="flex items-start gap-3 p-3 rounded-lg border border-slate-200 bg-slate-50 opacity-60 cursor-not-allowed"
                  >
                    <input type="radio" disabled className="mt-0.5" />
                    <div>
                      <p className="text-sm font-medium text-slate-800">{labelAuthType(t)}</p>
                      <p className="text-xs text-slate-400">Secret Store 연동 후 사용 가능</p>
                    </div>
                  </label>
                ))}
              </div>
            </Field>
          </Section>
        )}

        {step === 3 && (
          <Section title="Connection Test">
            <InlineAlert
              type="info"
              message="Connection Test는 DRAFT 서버 등록 후 상세 페이지에서 실행할 수 있습니다. 이 단계에서는 연결 테스트를 수행하지 않습니다."
            />
          </Section>
        )}

        {step === 4 && (
          <Section title="Protocol / Capability">
            <div className="space-y-2 text-sm">
              <Row label="Protocol">Current MCP</Row>
              <Row label="Protocol Version">{CURRENT_MCP_PROTOCOL_VERSION}</Row>
              <Row label="Protocol Era">CURRENT</Row>
              <Row label="Discovery Mode">
                미결정 — Discovery 실행 후 EXPLICIT_DISCOVERY 또는 INFERRED_CURRENT
              </Row>
              <Row label="server/discover">Optional — Current MCP에서는 명시적 Discovery가 선택사항입니다</Row>
            </div>
            <InlineAlert
              type="info"
              message="Current MCP: server/discover는 optional입니다. Explicit discovery 없이도 self-describing Current 요청이 정상 동작하면 INFERRED_CURRENT로 호환 가능합니다."
            />
          </Section>
        )}

        {step === 5 && (
          <Section title="Tool Preview">
            <EmptyState
              title="등록 후 Discovery 필요"
              description="Tool 목록은 서버 등록 후 Discovery를 실행하면 표시됩니다."
            />
          </Section>
        )}

        {step === 6 && (
          <Section title="Review & Register">
            <div className="space-y-2 text-sm">
              <Row label="이름">{name || '—'}</Row>
              <Row label="설명">{description || '—'}</Row>
              <Row label="Transport">Streamable HTTP</Row>
              <Row label="Endpoint">{endpointUrl || '—'}</Row>
              <Row label="인증">{labelAuthType(authType)}</Row>
              <Row label="Protocol">Current MCP ({CURRENT_MCP_PROTOCOL_VERSION})</Row>
            </div>
            {registerError && <InlineAlert type="error" message={registerError} />}
            <Button
              loading={registering}
              onClick={handleRegister}
              className="mt-2"
              disabled={!name.trim() || !endpointUrl.trim()}
            >
              서버 등록
            </Button>
          </Section>
        )}

        <div className="flex justify-between pt-2">
          <Button variant="outline" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>
            이전
          </Button>
          {step < STEPS.length - 1 ? (
            <Button onClick={() => setStep(step + 1)}>다음</Button>
          ) : null}
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

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex gap-4">
      <span className="text-slate-400 w-28 shrink-0 text-xs">{label}</span>
      <span className="text-slate-700">{children}</span>
    </div>
  );
}

const inputClass =
  'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white';
