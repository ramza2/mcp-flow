import { AlertTriangle, CheckCircle2, Info } from 'lucide-react';
import type { JsonValue } from '../../api/types';
import type { WorkflowVersionValidationStatus } from '../../domain/types';

const CHECK_LABELS: Record<string, string> = {
  plan_schema: '계획 데이터 형식',
  static_plan: '실행 단계 및 연결',
  tool_versions: '도구 버전',
  approval_policies: '승인 정책',
};

interface Props {
  report: Record<string, JsonValue>;
  status: WorkflowVersionValidationStatus;
  contentHash: string;
  dirty: boolean;
}

/** Presentation only: the server's validation_report and canonical status stay unchanged. */
export function isCurrentWorkflowValidation(
  status: WorkflowVersionValidationStatus,
  report: Record<string, JsonValue> | null,
  contentHash: string,
): boolean {
  return status === 'VALID' && report?.valid === true &&
    typeof report.content_hash === 'string' && report.content_hash === contentHash;
}

function isRecord(value: JsonValue | undefined): value is Record<string, JsonValue> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function errorLabel(value: JsonValue): string {
  if (typeof value === 'string') return value.slice(0, 240);
  if (isRecord(value)) {
    const message = typeof value.message === 'string' ? value.message :
      typeof value.detail === 'string' ? value.detail :
      typeof value.code === 'string' ? value.code : null;
    const location = typeof value.path === 'string' ? value.path : null;
    if (message) return [location, message].filter(Boolean).join(' · ').slice(0, 240);
  }
  return '검증 오류가 있습니다. 전체 보고서에서 자세한 내용을 확인하세요.';
}

export default function WorkflowValidationReport({ report, status, contentHash, dirty }: Props) {
  const current = isCurrentWorkflowValidation(status, report, contentHash);
  const failed = !dirty && status === 'INVALID' && report.valid === false;
  const success = !dirty && current;
  const checks = isRecord(report.checks) ? Object.entries(report.checks) : [];
  const errors = Array.isArray(report.errors) ? report.errors : [];
  const title = dirty
    ? '변경된 초안은 아직 검증되지 않았습니다'
    : success ? '워크플로 검증 완료' :
      failed ? '워크플로 검증 실패' : '검증 결과를 다시 확인해 주세요';
  const detail = dirty
    ? '아래 결과는 저장된 이전 계획에 대한 것입니다. 초안을 저장한 뒤 다시 검증하세요.'
    : success ? '현재 저장된 버전의 서버 검증을 통과했습니다. 검증만으로 실행되거나 게시되지는 않습니다.'
      : failed ? '서버가 검증 오류를 보고했습니다. 아래 항목을 확인하고 수정한 뒤 다시 검증하세요.'
        : '현재 버전의 검증 상태와 보고서가 일치하지 않거나 확인 정보가 부족합니다. 다시 검증하세요.';

  return (
    <section aria-label="마지막 서버 검증 결과"
      className="rounded-xl border border-slate-200 bg-white p-4 space-y-3">
      <div className="flex items-start gap-2">
        {success
          ? <CheckCircle2 size={20} className="shrink-0 text-green-600" aria-hidden="true" />
          : failed
            ? <AlertTriangle size={20} className="shrink-0 text-red-600" aria-hidden="true" />
            : <Info size={20} className="shrink-0 text-amber-600" aria-hidden="true" />}
        <div className="min-w-0 space-y-1">
          <h2 className="font-semibold text-sm text-slate-800">{title}</h2>
          <p className="text-xs text-slate-600">{detail}</p>
        </div>
      </div>
      {checks.length > 0 && (
        <div aria-label="서버 검증 항목" className="grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
          {checks.map(([key, result]) => {
            const ok = result === 'OK';
            return (
              <div key={key} className="flex items-center gap-2 rounded-lg bg-slate-50 border border-slate-100 p-2 text-xs">
                {ok
                  ? <CheckCircle2 size={14} className="shrink-0 text-green-600" aria-hidden="true" />
                  : <AlertTriangle size={14} className="shrink-0 text-amber-600" aria-hidden="true" />}
                <span className="min-w-0 break-words text-slate-700">{CHECK_LABELS[key] ?? key}</span>
                <span className="ml-auto shrink-0 text-slate-500">{ok ? '정상' : '확인 필요'}</span>
              </div>
            );
          })}
        </div>
      )}
      {errors.length > 0 && (
        <div className="rounded-lg border border-red-100 bg-red-50 p-3 text-xs text-red-800 space-y-2">
          <p className="font-medium">검증 오류 {errors.length}건</p>
          <ul className="list-disc pl-4 space-y-1">
            {errors.slice(0, 5).map((error, index) => (
              <li key={index} className="break-words">{errorLabel(error)}</li>
            ))}
          </ul>
          {errors.length > 5 && <p>나머지 {errors.length - 5}건은 전체 보고서에서 확인하세요.</p>}
        </div>
      )}
      <details className="rounded-md border border-slate-200 p-3 text-xs">
        <summary className="cursor-pointer font-medium text-slate-700">
          전체 서버 검증 보고서 (JSON)
        </summary>
        <pre className="mt-3 max-h-80 overflow-auto whitespace-pre-wrap break-all font-mono text-slate-600">
          {JSON.stringify(report, null, 2)}
        </pre>
      </details>
    </section>
  );
}
