import type { InputDefinition } from './workflowRunInputs';
import type { WorkflowInputFormIncluded, WorkflowInputFormValues } from './workflowRunInputForm';

interface Props {
  definitions: Record<string, InputDefinition>;
  values: WorkflowInputFormValues;
  included: WorkflowInputFormIncluded;
  busy: boolean;
  onValueChange: (name: string, value: string) => void;
  onIncludeChange: (name: string, included: boolean) => void;
}

const TYPE_LABELS: Record<string, string> = {
  string: '문자열',
  integer: '정수',
  number: '숫자',
  boolean: '참/거짓',
  object: 'JSON 객체',
  array: 'JSON 배열',
};

const inputClass = 'w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:bg-slate-100 disabled:text-slate-400';

export default function WorkflowRunInputForm({
  definitions, values, included, busy, onValueChange, onIncludeChange,
}: Props) {
  const entries = Object.entries(definitions);
  if (!entries.length) {
    return <p className="text-sm text-slate-500">추가 입력이 없는 워크플로입니다. 입력 없이 실행할 수 있습니다.</p>;
  }
  return (
    <div className="space-y-4">
      {entries.map(([name, definition]) => {
        const enabled = definition.required || included[name] === true;
        const fieldId = `workflow-run-field-${name}`;
        const value = values[name] ?? '';
        const typeLabel = definition.secret ? 'Secret UUID' : (TYPE_LABELS[definition.type] ?? definition.type);
        return (
          <div key={name} className="rounded-lg border border-slate-200 p-3 space-y-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label htmlFor={fieldId} className="text-sm font-medium text-slate-800 break-all">
                {name} <span className="text-xs font-normal text-slate-500">({typeLabel})</span>
                {definition.required && <span className="text-xs text-red-600 ml-1">필수</span>}
              </label>
              {!definition.required && (
                <label className="flex items-center gap-1.5 text-xs text-slate-600">
                  <input type="checkbox" checked={included[name] === true} disabled={busy}
                    aria-label={`${name} 입력 사용`}
                    onChange={e => onIncludeChange(name, e.target.checked)} />
                  선택 입력 사용
                </label>
              )}
            </div>
            {definition.secret ? (
              <>
                <input id={fieldId} aria-label={name} className={inputClass}
                  value={value} disabled={busy || !enabled} type="text" autoComplete="off"
                  placeholder="등록된 Secret UUID (비밀값 원문 입력 금지)"
                  onChange={e => onValueChange(name, e.target.value)} />
                <p className="text-xs text-slate-500">비밀번호나 토큰을 붙여넣지 마세요. 등록된 Secret의 UUID만 입력합니다.</p>
              </>
            ) : definition.type === 'boolean' ? (
              <select id={fieldId} aria-label={name} className={inputClass}
                value={value} disabled={busy || !enabled}
                onChange={e => onValueChange(name, e.target.value)}>
                <option value="">참/거짓 선택</option>
                <option value="true">참 (true)</option>
                <option value="false">거짓 (false)</option>
              </select>
            ) : definition.type === 'object' || definition.type === 'array' ? (
              <textarea id={fieldId} aria-label={name} className={`${inputClass} font-mono`}
                value={value} disabled={busy || !enabled} rows={4} spellCheck={false}
                placeholder={definition.type === 'object' ? '{ }' : '[ ]'}
                onChange={e => onValueChange(name, e.target.value)} />
            ) : (
              <input id={fieldId} aria-label={name} className={inputClass} type="text"
                inputMode={definition.type === 'integer' ? 'numeric' : definition.type === 'number' ? 'decimal' : 'text'}
                value={value} disabled={busy || !enabled}
                placeholder={definition.type === 'integer' ? '예: 10' : definition.type === 'number' ? '예: 1.5' : '값을 입력하세요'}
                onChange={e => onValueChange(name, e.target.value)} />
            )}
          </div>
        );
      })}
    </div>
  );
}
