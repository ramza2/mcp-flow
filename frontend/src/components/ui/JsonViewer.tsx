import type { JsonValue } from '../../api/types';

interface JsonViewerProps {
  value: JsonValue | undefined;
  emptyLabel?: string;
  className?: string;
}

/** Renders arbitrary JSON values (object/array/primitive/null) — ToolVersion schemas may be malformed. */
export default function JsonViewer({
  value,
  emptyLabel = '값이 없습니다.',
  className = '',
}: JsonViewerProps) {
  if (value === undefined) {
    return <p className="text-sm text-slate-400">{emptyLabel}</p>;
  }

  let text: string;
  try {
    text = JSON.stringify(value, null, 2);
  } catch {
    text = String(value);
  }

  return (
    <pre
      className={`text-xs font-mono bg-slate-50 border border-slate-200 rounded-lg p-4 overflow-auto max-h-[32rem] text-slate-700 whitespace-pre-wrap break-all ${className}`}
    >
      {text}
    </pre>
  );
}
