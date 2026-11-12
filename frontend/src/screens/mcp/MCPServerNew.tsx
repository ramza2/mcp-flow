import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Check, ChevronRight } from 'lucide-react';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { createMCPServer } from '../../api/mcp';
import { isApiError } from '../../api/client';
import { CURRENT_MCP_PROTOCOL_VERSION } from '../../domain';

const STEPS = ['기본 정보', '연결 설정', '확인 및 등록'];

export default function MCPServerNew() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [endpointUrl, setEndpointUrl] = useState('');
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
          <ArrowLeft size={14} /> MCP 서버 목록
        </button>
        <h1 className="text-lg font-semibold text-slate-900">MCP 서버 등록</h1>
        <p className="text-sm text-slate-500 mt-1">기본 정보를 입력해 서버를 등록한 다음 상세 화면에서 연결 확인과 도구 탐색을 진행합니다.</p>
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

      <div className="p-6 max-w-2xl space-y-4">
        {step === 0 && (
          <Section title="1. 서버 기본 정보">
            <Field label="서버 이름" htmlFor="mcp-server-name">
              <input
                id="mcp-server-name"
                placeholder="Weather MCP"
                className={inputClass}
                value={name}
                onChange={e => setName(e.target.value)}
              />
            </Field>
            <Field label="설명 (선택)" htmlFor="mcp-server-description">
              <textarea
                id="mcp-server-description"
                placeholder="서버가 제공하는 기능을 간단히 입력하세요."
                rows={2}
                className={inputClass}
                value={description}
                onChange={e => setDescription(e.target.value)}
              />
            </Field>
            <p className="text-xs text-slate-500">목록에서 쉽게 찾을 수 있는 이름을 입력하세요.</p>
          </Section>
        )}

        {step === 1 && (
          <Section title="2. 서버 연결 설정">
            <Field label="연결 방식">
              <div className="rounded-lg border border-indigo-200 bg-indigo-50 px-3 py-2">
                <p className="text-sm font-medium text-slate-800">Streamable HTTP (현재 지원)</p>
                <p className="text-xs text-slate-600 mt-1">STDIO 및 이전 HTTP/SSE 방식은 이 등록 화면에서 사용할 수 없습니다.</p>
              </div>
            </Field>
            <Field label="MCP 서버 주소 (URL)" htmlFor="mcp-endpoint-url">
              <input
                id="mcp-endpoint-url"
                type="url"
                placeholder="https://mcp.example.com/mcp"
                className={inputClass}
                value={endpointUrl}
                onChange={e => setEndpointUrl(e.target.value)}
              />
            </Field>
            <p className="text-xs text-slate-500">MCP 서버에서 제공한 접속 주소를 입력하세요. 실제 연결 가능 여부는 등록 후 확인합니다.</p>
            <InlineAlert
              type="info"
              message="현재 이 화면에서는 인증 없는 서버만 등록할 수 있습니다. 토큰이나 API 키가 필요한 서버는 여기서 연결할 수 없으며, 비밀번호를 URL에 입력하지 마세요."
            />
          </Section>
        )}

        {step === 2 && (
          <Section title="3. 확인 및 등록">
            <div className="space-y-2 text-sm">
              <Row label="이름">{name || '—'}</Row>
              <Row label="설명">{description || '—'}</Row>
              <Row label="연결 방식">Streamable HTTP</Row>
              <Row label="서버 주소">{endpointUrl || '—'}</Row>
              <Row label="인증">없음 (NONE)</Row>
              <Row label="프로토콜">Current MCP ({CURRENT_MCP_PROTOCOL_VERSION})</Row>
            </div>
            <InlineAlert
              type="info"
              message="등록 후 서버 상세 화면으로 이동합니다. 그곳에서 ① 연결 테스트 ② 도구 탐색 미리보기 ③ 탐색 결과 적용 ④ 필요한 도구 활성화를 진행하세요. 등록만으로 도구가 활성화되지는 않습니다."
            />
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
            <Button
              onClick={() => setStep(step + 1)}
              disabled={step === 0 ? !name.trim() : !endpointUrl.trim()}
            >
              다음
            </Button>
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

function Field({ label, htmlFor, children }: { label: string; htmlFor?: string; children: React.ReactNode }) {
  return (
    <div>
      <label htmlFor={htmlFor} className="block text-xs font-medium text-slate-600 mb-1.5">{label}</label>
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
