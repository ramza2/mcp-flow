import { useState } from 'react';
import { getMCPToolPolicy } from '../../api/mcp';
import { createResourceGrant, type GrantOwner } from '../../api/administration';
import { isApiError } from '../../api/client';
import type { MCPToolDto, MCPToolPolicyDto } from '../../api/types';
import Button from '../../components/ui/Button';
import { InlineAlert } from '../../components/ui/EmptyState';

interface Props {
  owner: GrantOwner;
  ownerId: string;
  available: MCPToolDto[];
  busy: boolean;
  setBusy: (busy: boolean) => void;
  onChanged: () => Promise<void>;
}

function safeReadOnly(policy: MCPToolPolicyDto): boolean {
  return policy.risk_class === 'READ_ONLY'
    && policy.allow_auto_select
    && !policy.requires_confirmation
    && !policy.requires_approval;
}

function errorMessage(error: unknown): string {
  return isApiError(error) ? error.message : '요청 처리 중 오류가 발생했습니다.';
}

/**
 * Convenience only: exact per-tool ResourceGrant writes.
 * No wildcard grant, implicit ToolPolicy, or AgentVersion grant is created.
 */
export default function SafeReadOnlyGrantPicker({
  owner, ownerId, available, busy, setBusy, onChanged,
}: Props) {
  const [eligible, setEligible] = useState<MCPToolDto[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [checking, setChecking] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const availableIds = new Set(available.map(tool => tool.id));
  const listed = eligible.filter(tool => availableIds.has(tool.id));
  const choices = listed.filter(tool => selected.includes(tool.id));

  async function checkPolicies() {
    if (checking || busy) return;
    setChecking(true);
    setConfirming(false);
    setSelected([]);
    setEligible([]);
    setMessage(null);
    setError(null);

    const candidates = available.filter(tool => Boolean(tool.current_version_id));
    const accepted: MCPToolDto[] = [];
    let unreadable = 0;

    try {
      // Check policies only on demand, in bounded batches to limit API load.
      for (let offset = 0; offset < candidates.length; offset += 8) {
        const checked = await Promise.all(candidates.slice(offset, offset + 8).map(async tool => {
          try {
            const policy = await getMCPToolPolicy(tool.id);
            return safeReadOnly(policy) ? tool : null;
          } catch {
            unreadable += 1; // Missing/unreadable policy is never treated as safe.
            return null;
          }
        }));
        accepted.push(...checked.filter((tool): tool is MCPToolDto => tool !== null));
      }
      setEligible(accepted);
      setMessage(
        '안전 조회 후보 ' + accepted.length + '개를 확인했습니다.' +
        (unreadable > 0 ? ' 정책을 확인할 수 없는 ' + unreadable + '개는 제외했습니다.' : ''),
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setChecking(false);
    }
  }

  function toggle(id: string) {
    setSelected(current => current.includes(id)
      ? current.filter(item => item !== id)
      : [...current, id]);
    setConfirming(false);
  }

  async function applySelected() {
    if (busy || checking || choices.length === 0 || !confirming) return;
    setBusy(true);
    setError(null);
    setMessage(null);
    let created = 0;
    const failed: string[] = [];

    try {
      for (const tool of choices) {
        try {
          // Recheck mutable ToolPolicy before each grant, fail closed on drift.
          const policy = await getMCPToolPolicy(tool.id);
          if (!safeReadOnly(policy)) {
            failed.push(tool.id);
            continue;
          }
          await createResourceGrant(owner, ownerId, 'MCP_TOOL', tool.id);
          created += 1;
        } catch {
          failed.push(tool.id);
        }
      }

      await onChanged();
      setSelected(failed);
      setConfirming(false);
      setMessage(created + '개 도구에 접근 권한을 부여했습니다.' +
        (failed.length > 0 ? ' ' + failed.length + '개는 실패하거나 정책이 변경되어 제외했습니다. 해당 항목을 확인해 주세요.' : ''));
    } catch (err) {
      setError('권한 목록 갱신 실패: ' + errorMessage(err));
      setSelected(failed);
      setConfirming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="border-t border-slate-100 pt-3 space-y-3">
      <h4 className="text-sm font-medium text-slate-800">기본 조회 도구 권한 일괄 설정</h4>
      <p className="text-xs text-slate-500">
        사용 중인 ACTIVE Tool 중 정책이 READ_ONLY이고, 자동 선택이 허용되며,
        확인·승인이 필요 없는 도구만 추천합니다. 선택 후 확인을 거쳐
        현재 사용자 또는 역할에 개별 MCP_TOOL 권한을 부여합니다.
        AgentVersion Tool Grant와 실행자 권한·실행 시 정책 검증은 별도로 유지됩니다.
      </p>
      <Button
        size="sm"
        variant="outline"
        disabled={busy || checking}
        loading={checking}
        onClick={() => void checkPolicies()}
      >
        안전 조회 도구 찾기
      </Button>
      {error && <InlineAlert type="error" message={error} />}
      {message && <InlineAlert type="info" message={message} />}
      {listed.length > 0 && (
        <>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs text-slate-500">후보 {listed.length}개 · 선택 {choices.length}개</span>
            <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={busy || checking}
                onClick={() => { setSelected(listed.map(tool => tool.id)); setConfirming(false); }}>
                후보 전체 선택
              </Button>
              <Button size="sm" variant="outline" disabled={busy || checking}
                onClick={() => { setSelected([]); setConfirming(false); }}>
                선택 해제
              </Button>
            </div>
          </div>
          <div className="max-h-52 overflow-y-auto border border-slate-200 rounded-md p-2 space-y-1">
            {listed.map(tool => (
              <label key={tool.id} className="flex items-start gap-2 p-2 text-sm hover:bg-slate-50 rounded">
                <input
                  type="checkbox"
                  checked={selected.includes(tool.id)}
                  disabled={busy || checking}
                  onChange={() => toggle(tool.id)}
                  className="mt-1"
                />
                <span className="min-w-0">
                  <span className="block text-slate-800">{tool.display_name || tool.remote_name}</span>
                  <span className="block text-xs text-slate-500 font-mono">{tool.remote_name}</span>
                </span>
              </label>
            ))}
          </div>
        </>
      )}
      {choices.length > 0 && !confirming && (
        <Button size="sm" disabled={busy || checking} onClick={() => setConfirming(true)}>
          선택한 {choices.length}개 접근 권한 부여
        </Button>
      )}
      {confirming && choices.length > 0 && (
        <div className="rounded-md border border-amber-300 bg-amber-50 p-3 space-y-2">
          <p className="text-xs text-amber-900">
            {owner === 'role' ? '이 역할을 가진 모든 사용자에게' : '이 사용자에게'}
            선택한 {choices.length}개 도구의 접근 범위를 추가합니다.
            실제 실행에는 mcp.tool.execute와 AgentVersion의 Tool Grant도 필요합니다.
          </p>
          <div className="flex gap-2">
            <Button size="sm" loading={busy} onClick={() => void applySelected()}>부여 확인</Button>
            <Button size="sm" variant="outline" disabled={busy} onClick={() => setConfirming(false)}>취소</Button>
          </div>
        </div>
      )}
    </div>
  );
}
