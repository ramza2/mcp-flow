/** Factory-imported MCP Discovery: immutable contract reconciliation result, not generic add/change counts. */

import type { FactoryDiscoveryDiffDto } from '../../api/types';

const LABELS = {
  MATCHED: '계약 일치',
  MISSING_EXPECTED: '예상 도구 누락',
  UNEXPECTED_REMOTE: '미승인 원격 도구',
  CONTRACT_CHANGED: '계약 변경',
} as const;

export default function FactoryDiscoverySummary({ diff }: { diff: FactoryDiscoveryDiffDto }) {
  const clean = diff.status === 'CLEAN';
  const mismatches = diff.classifications.filter(item => item.classification !== 'MATCHED');
  const statusText = clean
    ? 'Factory 계약 정합성 일치 (CLEAN)'
    : diff.status === 'AMBIGUOUS'
      ? 'Factory 계약 판정 불명확 (AMBIGUOUS)'
      : 'Factory 계약 불일치 (MISMATCHED)';
  return (
    <div
      role="region"
      aria-label="Factory Discovery 계약 정합성"
      className={`mt-2 rounded-lg border p-3 text-sm ${clean
        ? 'border-green-200 bg-green-50'
        : 'border-red-200 bg-red-50'}`}
    >
      <p className={`font-semibold ${clean ? 'text-green-800' : 'text-red-800'}`}>
        {statusText}
      </p>
      <p className="mt-1 text-xs text-slate-700">
        계약 일치 {diff.matched}개 · 예상 도구 누락 {diff.missing_expected}개 ·
        미승인 원격 도구 {diff.unexpected_remote}개 · 계약 변경 {diff.contract_changed}개
      </p>
      {clean ? (
        <p className="mt-2 text-xs text-green-800">
          이 결과는 미리보기 기준입니다. 정합성 검증은 적용 시 Backend에서 다시 수행합니다.
          CLEAN 미리보기만으로 서버 또는 도구가 활성화되지는 않습니다.
        </p>
      ) : (
        <p className="mt-2 text-xs text-red-800">
          불일치 또는 불명확한 항목이 있어 탐색 결과를 적용할 수 없습니다.
          승인된 패키지와 실제 MCP 도구 계약을 확인한 후 다시 테스트하세요.
        </p>
      )}
      {diff.error_message && (
        <p className="mt-2 text-xs text-red-800 break-all">{diff.error_message}</p>
      )}
      {mismatches.length > 0 && (
        <div className="mt-2 text-xs text-slate-700 space-y-1">
          <p className="font-semibold">일치하지 않는 도구</p>
          <ul className="list-disc pl-5 space-y-1 max-h-48 overflow-y-auto">
            {mismatches.slice(0, 30).map((item, index) => (
              <li key={`${item.remote_name}-${index}`} className="break-all">
                {item.remote_name}: {LABELS[item.classification]}
              </li>
            ))}
          </ul>
          {mismatches.length > 30 && (
            <p>전체 {mismatches.length}건 중 30건만 표시합니다.</p>
          )}
        </div>
      )}
    </div>
  );
}
