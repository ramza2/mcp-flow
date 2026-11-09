import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, CheckCircle, Save, ShieldCheck } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { getWorkflow, getWorkflowVersion, saveWorkflowPlan, validateWorkflowVersion, type WorkflowDto, type WorkflowVersionDto } from '../../api/workflows';
import { isAbortError, isApiError } from '../../api/client';
import type { JsonValue } from '../../api/types';
import { labelWorkflowVersionStatus } from '../../domain';

/** Temporary advanced editor: exact backend Plan JSON is round-tripped without lossy UI projections. */
export default function WorkflowPlanEditor() {
  const { workflowId, versionId } = useParams();
  const navigate = useNavigate();
  const [workflow, setWorkflow] = useState<WorkflowDto | null>(null);
  const [version, setVersion] = useState<WorkflowVersionDto | null>(null);
  const [source, setSource] = useState('');
  const [savedSource, setSavedSource] = useState('');
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  useEffect(() => {
    if (!workflowId || !versionId) return;
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    Promise.all([
      getWorkflow(workflowId, controller.signal),
      getWorkflowVersion(workflowId, versionId, controller.signal),
    ]).then(([workflowRow, versionRow]) => {
      if (cancelled) return;
      const text = JSON.stringify(versionRow.plan_definition, null, 2);
      setWorkflow(workflowRow);
      setVersion(versionRow);
      setSource(text);
      setSavedSource(text);
    }).catch(err => {
      if (cancelled || isAbortError(err)) return;
      setError(isApiError(err) ? err.message : '실행 계획을 불러오지 못했습니다.');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.abort(); };
  }, [workflowId, versionId]);

  const readOnly = version?.status !== 'DRAFT';
  const dirty = source !== savedSource;

  const parsePlan = (): Record<string, JsonValue> | null => {
    try {
      const parsed: unknown = JSON.parse(source);
      if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
        setError('실행 계획은 JSON 객체여야 합니다.');
        return null;
      }
      return parsed as Record<string, JsonValue>;
    } catch {
      setError('JSON 문법이 잘못되었습니다. 괄호와 쉼표를 확인하세요.');
      return null;
    }
  };

  const save = async () => {
    if (!workflowId || !versionId || readOnly || saving || validating) return;
    setError(null);
    setSuccess(null);
    const plan = parsePlan();
    if (!plan) return;
    setSaving(true);
    try {
      // Preflight detects most stale-tab overwrites. The current API does not
      // expose an atomic If-Match for WorkflowVersion plan updates.
      const latest = await getWorkflowVersion(workflowId, versionId);
      if (latest.status !== 'DRAFT' || latest.content_hash !== version?.content_hash) {
        setError('다른 곳에서 버전이 변경되었거나 게시되었습니다. 상세 화면으로 돌아가 최신 버전을 확인하세요.');
        return;
      }
      const result = await saveWorkflowPlan(workflowId, versionId, plan);
      const normalized = JSON.stringify(result.plan_definition, null, 2);
      setVersion(result);
      setSource(normalized);
      setSavedSource(normalized);
      setSuccess('초안을 서버에 저장했습니다. 저장 후 검증 상태는 미검증(INVALID)으로 초기화됩니다.');
    } catch (err) {
      setError(isApiError(err) ? err.message : '실행 계획 저장에 실패했습니다.');
    } finally { setSaving(false); }
  };

  const validate = async () => {
    if (!workflowId || !versionId || readOnly || dirty || saving || validating) return;
    setError(null);
    setSuccess(null);
    setValidating(true);
    try {
      const latest = await getWorkflowVersion(workflowId, versionId);
      if (latest.status !== 'DRAFT' || latest.content_hash !== version?.content_hash) {
        setError('서버 버전이 변경되었습니다. 다시 불러온 뒤 검증해 주세요.');
        return;
      }
      const result = await validateWorkflowVersion(workflowId, versionId);
      setVersion(prev => prev ? { ...prev, validation_status: result.validation_status, validation_report: result.validation_report, content_hash: result.content_hash } : prev);
      if (result.validation_status === 'VALID') {
        setSuccess('서버 검증을 통과했습니다. 검증만으로 게시되거나 실행되지는 않습니다.');
      } else {
        setError('서버 검증에 실패했습니다. 아래 검증 보고서를 확인하고 수정하세요.');
      }
    } catch (err) {
      setError(isApiError(err) ? err.message : '실행 계획 검증에 실패했습니다.');
    } finally { setValidating(false); }
  };

  if (loading) return <div className="p-6"><LoadingSkeleton rows={6} /></div>;
  if (!workflow || !version || !workflowId) return (
    <div className="p-6 space-y-4">
      <ErrorState message={error ?? '워크플로 또는 버전을 찾을 수 없습니다.'} />
      <Button variant="outline" onClick={() => navigate('/workflows')}>워크플로 목록으로</Button>
    </div>
  );

  return (
    <div>
      <PageHeader
        title={`${workflow.name} · v${version.version_no} ${readOnly ? '조회' : '초안 편집'}`}
        description={`버전 상태: ${labelWorkflowVersionStatus(version.status)} · 서버 검증: ${version.validation_status}`}
        actions={(
          <div className="flex gap-2">
            <Button variant="outline" icon={<ArrowLeft size={13} />} onClick={() => navigate(`/workflows/${workflowId}`)}>상세로</Button>
            {!readOnly && <>
              <Button variant="outline" icon={<ShieldCheck size={13} />} disabled={dirty || saving || validating} loading={validating} onClick={() => { void validate(); }}>서버 검증</Button>
              <Button icon={<Save size={13} />} disabled={!dirty || saving || validating} loading={saving} onClick={() => { void save(); }}>초안 저장</Button>
            </>}
          </div>
        )}
      />
      <div className="p-6 space-y-4 max-w-6xl">
        {error && <InlineAlert type="error" message={error} />}
        {success && <div role="status" className="flex gap-2 text-sm text-green-700"><CheckCircle size={17} />{success}</div>}
        <InlineAlert type="info" message="실제 서버에 연결된 실행 계획입니다. 현재는 고급 JSON 편집만 지원하며, 시각적 단계 디자이너는 후속 개발 예정입니다. Execution Plan v1 형식을 사용하고 비밀번호·토큰 등 비밀값은 직접 입력하지 마세요(SECRET_REF만 사용)." />
        {readOnly && <InlineAlert type="warning" message="게시되었거나 폐기된 버전은 변경할 수 없습니다. 새 초안 버전을 생성하세요." />}
        {dirty && <InlineAlert type="warning" message="저장되지 않은 변경 사항이 있습니다. 서버 검증 전에 먼저 초안을 저장하세요." />}
        <label htmlFor="workflow-plan-json" className="block text-sm font-semibold text-slate-700">실행 계획 JSON</label>
        <textarea
          id="workflow-plan-json" aria-label="실행 계획 JSON" spellCheck={false}
          className="w-full min-h-96 rounded-md border border-slate-300 p-3 font-mono text-xs leading-relaxed focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500 disabled:bg-slate-50"
          value={source} disabled={readOnly || saving || validating} onChange={event => { setSource(event.target.value); setError(null); setSuccess(null); }}
        />
        {version.validation_report && <div className="rounded-xl border border-slate-200 p-4 bg-white">
          <h2 className="font-medium text-sm text-slate-800 mb-2">마지막 서버 검증 보고서</h2>
          <pre className="text-xs text-slate-600 whitespace-pre-wrap break-all">{JSON.stringify(version.validation_report, null, 2)}</pre>
        </div>}
      </div>
    </div>
  );
}
