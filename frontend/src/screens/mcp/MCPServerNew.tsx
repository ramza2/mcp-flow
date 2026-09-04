import { useState } from 'react';
import { useNavigate } from 'react-router';
import { ArrowLeft, Check, ChevronRight, RefreshCw, AlertTriangle } from 'lucide-react';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { mockSecrets } from '../../data/mock';
import {
  CURRENT_MCP_PROTOCOL_VERSION,
  MCP_AUTH_TYPES,
  labelAuthType,
  type MCPAuthType,
} from '../../domain';

const STEPS = ['Basic', 'Transport', 'Authentication', 'Connection Test', 'Protocol / Capability', 'Tool Preview', 'Review & Register'];

export default function MCPServerNew() {
  const navigate = useNavigate();
  const [step, setStep] = useState(0);
  const [transport, setTransport] = useState('Streamable HTTP');
  const [authType, setAuthType] = useState<MCPAuthType>('API_KEY_HEADER');
  const [secretMode, setSecretMode] = useState<'existing' | 'new'>('existing');
  const [secretRefId, setSecretRefId] = useState(mockSecrets[0]?.id ?? '');
  const [newSecretValue, setNewSecretValue] = useState('');
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<'idle' | 'ok' | 'fail'>('idle');
  const [registering, setRegistering] = useState(false);

  const isLegacy = transport === 'Legacy HTTP/SSE';
  const discoveryMode = isLegacy ? 'LEGACY_HANDSHAKE' : 'INFERRED_CURRENT';
  const protocolLabel = isLegacy ? 'Legacy MCP' : 'Current MCP';
  const protocolVersion = isLegacy ? '2024-11-05' : CURRENT_MCP_PROTOCOL_VERSION;
  const needsSecret = authType !== 'NONE';

  const runTest = async () => {
    setTesting(true);
    setTestResult('idle');
    await delay(1500);
    setTesting(false);
    setTestResult('ok');
  };

  const handleRegister = async () => {
    setRegistering(true);
    await delay(1200);
    setRegistering(false);
    navigate('/mcp/servers/srv-new');
  };

  const canNext = step !== 3 || testResult === 'ok';

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/mcp/servers')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> MCP Servers
        </button>
        <h1 className="text-lg font-semibold text-slate-900">MCP Server 등록</h1>
      </div>

      {/* Step indicator */}
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <div className="flex items-center gap-0 overflow-x-auto">
          {STEPS.map((s, i) => (
            <div key={s} className="flex items-center shrink-0">
              <button
                onClick={() => i < step ? setStep(i) : undefined}
                className={`flex items-center gap-2 text-xs font-medium px-1
                  ${i === step ? 'text-indigo-600' : i < step ? 'text-green-600 cursor-pointer' : 'text-slate-400'}`}
              >
                <span className={`w-6 h-6 rounded-full flex items-center justify-center shrink-0 border
                  ${i === step ? 'border-indigo-600 bg-indigo-600 text-white' : i < step ? 'border-green-500 bg-green-500 text-white' : 'border-slate-300 text-slate-400'}`}>
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
            <Field label="서버 이름"><input placeholder="Weather MCP" className={inputClass} /></Field>
            <Field label="설명"><textarea placeholder="서버 설명 입력..." rows={2} className={inputClass} /></Field>
            <Field label="태그"><input placeholder="weather, external" className={inputClass} /></Field>
          </Section>
        )}

        {step === 1 && (
          <Section title="Transport">
            <Field label="Transport Type">
              <div className="space-y-2">
                {['Streamable HTTP', 'STDIO', 'Legacy HTTP/SSE'].map(t => (
                  <label key={t} className={`flex items-start gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${transport === t ? 'border-indigo-300 bg-indigo-50' : 'border-slate-200 hover:border-slate-300'}`}>
                    <input type="radio" checked={transport === t} onChange={() => setTransport(t)} className="mt-0.5 accent-indigo-600" />
                    <div>
                      <p className="text-sm font-medium text-slate-800">{t}</p>
                      <p className="text-xs text-slate-400">
                        {t === 'Streamable HTTP' ? '현재 MCP 표준 Transport' :
                         t === 'STDIO' ? 'Registered Manifest를 통해 실행' :
                         'Legacy MCP 지원 (2024-11-05)'}
                      </p>
                    </div>
                  </label>
                ))}
              </div>
            </Field>
            {transport === 'Streamable HTTP' && (
              <Field label="Server URL"><input placeholder="https://mcp.example.com/mcp" className={inputClass} /></Field>
            )}
            {transport === 'STDIO' && (
              <>
                <InlineAlert type="info" message="STDIO 모드에서는 자유로운 Shell Command를 직접 입력할 수 없습니다. 등록된 Manifest 목록에서 선택하세요." />
                <Field label="Registered Manifest">
                  <select className={inputClass}>
                    <option>weather-mcp v0.8.0</option>
                    <option>document-mcp v1.2.0</option>
                  </select>
                </Field>
              </>
            )}
          </Section>
        )}

        {step === 2 && (
          <Section title="Authentication">
            <Field label="인증 방식">
              <select
                value={authType}
                onChange={e => setAuthType(e.target.value as MCPAuthType)}
                className={inputClass}
              >
                {MCP_AUTH_TYPES.map(t => (
                  <option key={t} value={t}>{labelAuthType(t)}</option>
                ))}
              </select>
            </Field>
            {needsSecret && (
              <div className="space-y-3">
                <Field label="Secret">
                  <div className="flex gap-3 mb-2">
                    {(['existing', 'new'] as const).map(mode => (
                      <label key={mode} className="flex items-center gap-2 cursor-pointer">
                        <input
                          type="radio"
                          checked={secretMode === mode}
                          onChange={() => setSecretMode(mode)}
                          className="accent-indigo-600"
                        />
                        <span className="text-sm text-slate-700">
                          {mode === 'existing' ? 'Existing Secret Reference' : 'New Secret registration'}
                        </span>
                      </label>
                    ))}
                  </div>
                  {secretMode === 'existing' ? (
                    <select
                      value={secretRefId}
                      onChange={e => setSecretRefId(e.target.value)}
                      className={inputClass}
                    >
                      {mockSecrets.map(s => (
                        <option key={s.id} value={s.id}>{s.name} ({s.type})</option>
                      ))}
                    </select>
                  ) : (
                    <div>
                      <input
                        type="password"
                        value={newSecretValue}
                        onChange={e => setNewSecretValue(e.target.value)}
                        placeholder="새 Secret 입력 (저장 후 다시 표시되지 않음)"
                        className={inputClass}
                      />
                      <p className="text-xs text-slate-400 mt-1.5">
                        Raw secret is not shown again after save. 등록 후 원문을 다시 확인할 수 없습니다.
                      </p>
                    </div>
                  )}
                </Field>
                <InlineAlert type="warning" message="Secret 원문은 저장 후 다시 표시되지 않습니다. 안전하게 보관하세요." />
              </div>
            )}
          </Section>
        )}

        {step === 3 && (
          <Section title="Connection Test">
            <p className="text-sm text-slate-600">설정한 Transport와 Authentication으로 연결을 테스트합니다.</p>
            <Button onClick={runTest} loading={testing} icon={<RefreshCw size={13} />} variant="secondary">연결 테스트 실행</Button>
            {testResult === 'ok' && (
              <div className="p-3 bg-green-50 border border-green-200 rounded-lg flex items-center gap-2">
                <Check size={14} className="text-green-600" />
                <p className="text-sm text-green-700 font-medium">연결 성공</p>
              </div>
            )}
            {testResult === 'fail' && (
              <div className="p-3 bg-red-50 border border-red-200 rounded-lg flex items-center gap-2">
                <AlertTriangle size={14} className="text-red-600" />
                <p className="text-sm text-red-700">연결 실패: 서버에 도달할 수 없습니다.</p>
              </div>
            )}
          </Section>
        )}

        {step === 4 && (
          <Section title="Protocol / Capability">
            <div className="space-y-2 text-sm">
              <Row label="Protocol">{protocolLabel}</Row>
              <Row label="Protocol Version">{protocolVersion}</Row>
              <Row label="Discovery Mode">{discoveryMode}</Row>
              {!isLegacy && (
                <Row label="server/discover">Optional — Current MCP에서는 명시적 Discovery가 선택사항입니다</Row>
              )}
            </div>
            {isLegacy ? (
              <InlineAlert type="info" message="Legacy MCP는 LEGACY_HANDSHAKE Discovery Mode를 사용합니다. initialize/initialized lifecycle은 Legacy Adapter 책임입니다." />
            ) : (
              <InlineAlert
                type="info"
                message="Current MCP: server/discover는 optional입니다. Explicit discovery 없이도 self-describing Current 요청이 정상 동작하면 INFERRED_CURRENT로 호환 가능합니다."
              />
            )}
          </Section>
        )}

        {step === 5 && (
          <Section title="Tool Preview">
            <p className="text-xs text-slate-500 mb-3">Discovery된 Tool 목록 (등록 전 미리보기)</p>
            <div className="space-y-2">
              {[
                { name: 'get_current_weather', desc: '현재 날씨 조회' },
                { name: 'get_weather_forecast', desc: '날씨 예보 조회' },
                { name: 'get_weather_alert', desc: '날씨 경보 조회' },
              ].map(t => (
                <div key={t.name} className="flex items-center gap-3 p-3 rounded-lg border border-slate-200 bg-white">
                  <div className="w-2 h-2 rounded-full bg-slate-300" />
                  <div>
                    <p className="text-sm font-mono text-slate-700">{t.name}</p>
                    <p className="text-xs text-slate-400">{t.desc}</p>
                  </div>
                </div>
              ))}
            </div>
            <p className="text-xs text-amber-600 mt-2">Tool은 등록 후 Tool Discovery를 실행하면 활성화됩니다.</p>
          </Section>
        )}

        {step === 6 && (
          <Section title="Review & Register">
            <div className="space-y-2 text-sm">
              <Row label="이름">Weather MCP (새 서버)</Row>
              <Row label="Transport">{transport}</Row>
              <Row label="인증">
                {labelAuthType(authType)}
                {needsSecret
                  ? secretMode === 'existing'
                    ? ` — Ref: ${mockSecrets.find(s => s.id === secretRefId)?.name ?? secretRefId}`
                    : ' — New Secret (원문 저장 후 비표시)'
                  : ''}
              </Row>
              <Row label="Protocol">{protocolLabel} ({protocolVersion})</Row>
              <Row label="Discovery">{discoveryMode}</Row>
              <Row label="Tool 수">3개 (Discovery 후 활성화)</Row>
            </div>
            <Button loading={registering} onClick={handleRegister} className="mt-2">서버 등록</Button>
          </Section>
        )}

        <div className="flex justify-between pt-2">
          <Button variant="outline" onClick={() => setStep(Math.max(0, step - 1))} disabled={step === 0}>이전</Button>
          {step < STEPS.length - 1 ? (
            <Button onClick={() => setStep(step + 1)} disabled={!canNext}>다음</Button>
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

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white';

function delay(ms: number) { return new Promise(r => setTimeout(r, ms)); }
