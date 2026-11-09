import { useState } from 'react';
import { useNavigate } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';
import { createWorkflow } from '../../api/workflows';
import { isApiError } from '../../api/client';
import { WORKFLOW_VISIBILITIES, type WorkflowVisibility } from '../../domain/types';
import { labelWorkflowVisibility } from '../../domain';

const inputClass = 'w-full rounded-md border border-slate-300 p-2 text-sm focus:border-indigo-500 focus:outline-none focus:ring-1 focus:ring-indigo-500';

export default function WorkflowNew() {
  const navigate = useNavigate();
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [visibility, setVisibility] = useState<WorkflowVisibility>('PRIVATE');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    if (!name.trim() || saving) return;
    setError(null);
    setSaving(true);
    try {
      const row = await createWorkflow({ name: name.trim(), description: description.trim() || null, visibility });
      navigate(`/workflows/${row.id}`);
    } catch (err) {
      setError(isApiError(err) ? err.message : '워크플로 등록에 실패했습니다.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <PageHeader title="새 워크플로" description="저장 전까지는 서버에 생성되지 않습니다." />
      <form className="p-6 max-w-2xl space-y-5" onSubmit={event => { event.preventDefault(); void save(); }}>
        {error && <InlineAlert type="error" message={error} />}
        <label className="block text-sm font-medium text-slate-700">
          이름 (필수)
          <input className={`${inputClass} mt-2`} value={name} maxLength={255} onChange={event => setName(event.target.value)} required />
        </label>
        <label className="block text-sm font-medium text-slate-700">
          설명
          <textarea className={`${inputClass} mt-2`} rows={3} value={description} onChange={event => setDescription(event.target.value)} />
        </label>
        <label className="block text-sm font-medium text-slate-700">
          공개 범위
          <select className={`${inputClass} mt-2`} value={visibility} onChange={event => setVisibility(event.target.value as WorkflowVisibility)}>
            {WORKFLOW_VISIBILITIES.map(value => <option key={value} value={value}>{labelWorkflowVisibility(value)}</option>)}
          </select>
        </label>
        <InlineAlert type="info" message="워크플로 등록과 실행 계획 버전 작성은 별도 단계입니다. 등록 직후에는 초안 상태이며 실제 실행되지 않습니다." />
        <div className="flex gap-2">
          <Button type="submit" loading={saving} disabled={!name.trim()}>워크플로 등록</Button>
          <Button type="button" variant="outline" disabled={saving} onClick={() => navigate('/workflows')}>취소</Button>
        </div>
      </form>
    </div>
  );
}
