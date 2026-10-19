import { useEffect, useMemo, useState } from 'react';
import { useNavigate } from 'react-router';
import Button from '../../components/ui/Button';
import JsonViewer from '../../components/ui/JsonViewer';
import { InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { createManualToolTest } from '../../api/mcp';
import { isAbortError } from '../../api/client';
import type { JsonValue, ManualToolTestCreateResult, MCPToolVersionDto } from '../../api/types';
import { shortenId } from '../../domain';
import { toFeedbackError, type FeedbackError } from './toolLifecycle';

function defaultArgumentsJson(version: MCPToolVersionDto | null): string {
  if (!version?.input_schema || typeof version.input_schema !== 'object') {
    return '{\n}\n';
  }
  const schema = version.input_schema as {
    properties?: Record<string, unknown>;
    required?: string[];
  };
  const props = schema.properties ?? {};
  const seed: Record<string, unknown> = {};
  for (const key of Object.keys(props)) {
    seed[key] = null;
  }
  return `${JSON.stringify(seed, null, 2)}\n`;
}

interface Props {
  toolId: string;
  selectedVersion: MCPToolVersionDto | null;
  currentVersionId: string | null;
  active: boolean;
}

export default function ToolTestCallTab({
  toolId,
  selectedVersion,
  currentVersionId,
  active,
}: Props) {
  const navigate = useNavigate();
  const [argumentsText, setArgumentsText] = useState('{\n}\n');
  const [parseError, setParseError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [result, setResult] = useState<ManualToolTestCreateResult | null>(null);

  useEffect(() => {
    if (!active) return;
    setArgumentsText(defaultArgumentsJson(selectedVersion));
    setParseError(null);
    setError(null);
    setResult(null);
  }, [active, selectedVersion?.id]);

  const canRun = useMemo(() => {
    if (!selectedVersion) return false;
    if (selectedVersion.validation_status !== 'VALID') return false;
    if (currentVersionId && selectedVersion.id !== currentVersionId) return false;
    return true;
  }, [selectedVersion, currentVersionId]);

  async function onRun() {
    if (!selectedVersion || !canRun) return;
    let parsed: Record<string, unknown>;
    try {
      const value = JSON.parse(argumentsText) as unknown;
      if (value === null || typeof value !== 'object' || Array.isArray(value)) {
        setParseError('arguments는 JSON object여야 합니다.');
        return;
      }
      parsed = value as Record<string, unknown>;
      setParseError(null);
    } catch {
      setParseError('JSON 파싱에 실패했습니다.');
      return;
    }

    setSubmitting(true);
    setError(null);
    setResult(null);
    try {
      const created = await createManualToolTest(
        toolId,
        {
          mcp_tool_version_id: selectedVersion.id,
          arguments: parsed as Record<string, JsonValue>,
        },
        { idempotencyKey: crypto.randomUUID() },
      );
      setResult(created);
    } catch (err) {
      if (isAbortError(err)) return;
      setError(toFeedbackError(err, 'Test Call 실행에 실패했습니다.'));
    } finally {
      setSubmitting(false);
    }
  }

  if (!active) return null;

  if (!selectedVersion) {
    return <LoadingSkeleton rows={3} />;
  }

  return (
    <div className="space-y-4">
      <div className="bg-white rounded-xl border border-slate-200 p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-slate-800">Test Call</h3>
            <p className="text-xs text-slate-500 mt-1">
              source_type=MANUAL_TOOL_TEST Execution을 생성합니다. Verification VERIFIED는
              자동 생성되지 않습니다.
            </p>
          </div>
          <Button
            size="sm"
            disabled={!canRun || submitting}
            onClick={() => void onRun()}
          >
            {submitting ? 'Running…' : 'Run Test'}
          </Button>
        </div>

        <dl className="grid grid-cols-2 gap-2 text-xs text-slate-600">
          <div>
            <dt className="text-slate-400">ToolVersion</dt>
            <dd className="font-mono">v{selectedVersion.version_no}</dd>
          </div>
          <div>
            <dt className="text-slate-400">Validation</dt>
            <dd>{selectedVersion.validation_status}</dd>
          </div>
          <div className="col-span-2">
            <dt className="text-slate-400">Version ID</dt>
            <dd className="font-mono">{shortenId(selectedVersion.id)}</dd>
          </div>
        </dl>

        {!canRun && (
          <InlineAlert
            type="warning"
            message={
              selectedVersion.validation_status !== 'VALID'
                ? 'VALID ToolVersion만 Test Call을 실행할 수 있습니다.'
                : '현재 Tool의 current_version만 Test Call을 실행할 수 있습니다.'
            }
          />
        )}

        <div>
          <label className="block text-xs font-medium text-slate-600 mb-1">
            Arguments (JSON)
          </label>
          <textarea
            className="w-full min-h-[180px] font-mono text-xs border border-slate-200 rounded-lg p-3 focus:outline-none focus:ring-2 focus:ring-slate-300"
            value={argumentsText}
            onChange={(e) => setArgumentsText(e.target.value)}
            spellCheck={false}
            disabled={submitting}
          />
          {parseError && <InlineAlert type="error" message={parseError} />}
        </div>

        {error && (
          <div className="space-y-1">
            <InlineAlert type="error" message={error.message} />
            {error.requestId && (
              <p className="font-mono text-xs text-slate-400">Request ID: {error.requestId}</p>
            )}
          </div>
        )}

        {result && (
          <div className="rounded-lg border border-emerald-200 bg-emerald-50 p-3 space-y-2">
            <p className="text-sm text-emerald-900">
              Execution 생성됨: <span className="font-mono">{result.id}</span>
            </p>
            <p className="text-xs text-emerald-800">
              status={result.status} · source={result.source_type} · trigger=
              {result.trigger_type}
            </p>
            <Button
              size="sm"
              variant="outline"
              onClick={() => navigate(`/executions/${result.id}`)}
            >
              Execution 상세로 이동
            </Button>
          </div>
        )}
      </div>

      <div className="bg-white rounded-xl border border-slate-200 p-4">
        <h3 className="text-sm font-semibold text-slate-800 mb-3">Input Schema</h3>
        <JsonViewer
          value={selectedVersion.input_schema ?? null}
          emptyLabel="Input schema가 없습니다."
        />
      </div>
    </div>
  );
}
