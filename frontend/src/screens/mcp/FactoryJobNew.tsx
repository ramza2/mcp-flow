import { useRef, useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { isApiError } from '../../api/client';
import { createFactoryAnalyzeJob, FACTORY_MAX_SOURCE_BYTES } from '../../api/factory';

const VALID_SOURCE_NAME = /\.(json|yaml|yml)$/i;

function newIdempotencyKey(): string {
  // An unpredictable key prevents accidental collisions between independent uploads.
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  return 'factory-analyze-' + Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
}

function validateSourceFile(file: File): string | null {
  if (!VALID_SOURCE_NAME.test(file.name)) return 'OpenAPI 문서는 .json, .yaml 또는 .yml 파일만 사용할 수 있습니다.';
  if (file.size === 0) return '빈 파일은 업로드할 수 없습니다.';
  if (file.size > FACTORY_MAX_SOURCE_BYTES) return '파일 크기는 2MiB(2,097,152바이트)를 넘을 수 없습니다.';
  return null;
}

export default function FactoryJobNew() {
  const navigate = useNavigate();
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  // Retain the same key on retry after an uncertain network/timeout response.
  // A different file always starts a new independent request.
  const idempotencyKeyRef = useRef<string | null>(null);
  const validationError = file ? validateSourceFile(file) : null;

  const handleSubmit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting || !file || validationError) return;
    setError(null);
    setSubmitting(true);

    try {
      const key = idempotencyKeyRef.current ?? newIdempotencyKey();
      idempotencyKeyRef.current = key;
      const result = await createFactoryAnalyzeJob(file, key);
      navigate(`/tool-factory/jobs/${result.id}`, { replace: true });
    } catch (err: unknown) {
      const apiErr = isApiError(err) ? err : null;
      setError({
        message: apiErr?.message ?? '분석 요청에 실패했습니다. 같은 파일로 다시 시도할 수 있습니다.',
        requestId: apiErr?.requestId ?? undefined,
      });
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: 'Factory 실제 작업', to: '/tool-factory/jobs' }]}
        title="OpenAPI 분석 작업 생성"
        description="OpenAPI 파일을 업로드해 실제 Factory 분석 작업을 등록합니다."
      />
      <div className="p-6 max-w-2xl space-y-4">
        <InlineAlert
          type="warning"
          message="실제 서버에 파일을 저장하고 비동기 분석 작업을 생성합니다. 소스에 비밀번호, API 키, 개인정보 등 민감정보가 포함되지 않았는지 확인하세요. 분석 완료만으로 MCP 서버나 도구가 생성·활성화되지는 않습니다."
        />
        <form onSubmit={(event) => void handleSubmit(event)} className="rounded-xl border border-slate-200 bg-white p-5 space-y-4">
          <div className="space-y-2">
            <label htmlFor="factory-source-file" className="block text-sm font-semibold text-slate-800">
              OpenAPI 문서
            </label>
            <input
              id="factory-source-file"
              type="file"
              accept=".json,.yaml,.yml"
              disabled={submitting}
              onChange={(event) => {
                setFile(event.target.files?.[0] ?? null);
                setError(null);
                idempotencyKeyRef.current = null;
              }}
              className="block w-full text-sm text-slate-700 file:mr-4 file:rounded-md file:border-0 file:bg-indigo-50 file:px-3 file:py-2 file:text-indigo-700"
            />
            <p className="text-xs text-slate-500">OpenAPI 3.x JSON 또는 YAML · 파일 최대 2MiB · Swagger 2.0 미지원</p>
          </div>
          {file && (
            <p className="text-sm text-slate-600 break-all">
              선택한 파일: <span className="font-medium">{file.name}</span> ({file.size.toLocaleString()}바이트)
            </p>
          )}
          {validationError && <InlineAlert type="error" message={validationError} />}
          {error && (
            <div className="space-y-1">
              <InlineAlert type="error" message={error.message} />
              {error.requestId && <p className="text-xs font-mono text-slate-500">Request ID: {error.requestId}</p>}
              <p className="text-xs text-slate-500">
                요청 결과가 불확실한 경우에도 같은 파일로 다시 시도하면 기존 멱등성 키를 재사용해 중복 작업 생성을 방지합니다.
              </p>
            </div>
          )}
          <div className="flex flex-wrap gap-2">
            <Button type="submit" loading={submitting} disabled={!file || !!validationError || submitting}>
              {submitting ? '분석 작업 등록 중…' : '분석 작업 생성'}
            </Button>
            <Button type="button" variant="outline" disabled={submitting} onClick={() => navigate('/tool-factory/jobs')}>
              취소
            </Button>
          </div>
        </form>
        <p className="text-xs text-slate-500">
          성공적으로 등록되면 실제 분석 작업 상세 화면으로 이동합니다. 이후 진행 상태는 새로고침으로 확인하세요.
        </p>
      </div>
    </div>
  );
}
