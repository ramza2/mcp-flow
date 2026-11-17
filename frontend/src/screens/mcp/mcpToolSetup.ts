/**
 * Beginner MCP Tool activation checklist — presentation only.
 * Uses existing Tool status / version / permission UX helpers.
 * Does not invent Domain states or change API / lifecycle transitions.
 */

import { hasPermission } from '../../domain';
import type { MCPToolStatus } from '../../domain/types';
import { toolLifecycleAction } from './toolLifecycle';

export type McpToolSetupStepId = 'find' | 'status' | 'policy' | 'activate';

export type McpToolSetupStep = {
  id: McpToolSetupStepId;
  label: string;
  done: boolean;
};

export const MCP_TOOL_SETUP_FLOW_LABEL =
  '도구 찾기 → 상태 확인 → 실행 정책·권한 확인 → 활성화';

export function buildMcpToolSetupSteps(input: {
  found: boolean;
  statusReviewed: boolean;
  policyReviewed: boolean;
  activated: boolean;
}): McpToolSetupStep[] {
  return [
    { id: 'find', label: '도구 찾기', done: input.found },
    { id: 'status', label: '상태 확인', done: input.statusReviewed },
    { id: 'policy', label: '실행 정책·권한', done: input.policyReviewed },
    { id: 'activate', label: '활성화', done: input.activated },
  ];
}

/** Detail checklist derived from existing Tool fields only (no new Domain states). */
export function deriveMcpToolSetupFlags(tool: {
  status: string;
  current_version_id: string | null;
}): {
  found: boolean;
  statusReviewed: boolean;
  policyReviewed: boolean;
  activated: boolean;
} {
  const activated = tool.status === 'ACTIVE';
  return {
    found: true,
    statusReviewed: true,
    // Policy/permission check is a user step; treat as progressed once ACTIVE.
    policyReviewed: activated,
    activated,
  };
}

export function nextMcpToolSetupAction(
  steps: McpToolSetupStep[],
  tool?: { status: string; current_version_id: string | null },
): { stepId: McpToolSetupStepId; message: string } | null {
  const pending = steps.find(s => !s.done);
  if (!pending) {
    return null;
  }
  switch (pending.id) {
    case 'find':
      return {
        stepId: 'find',
        message: '검색·필터로 도구를 찾거나 MCP 서버에서 도구 탐색 결과를 적용하세요.',
      };
    case 'status':
      return {
        stepId: 'status',
        message: '도구 상태(활성/비활성/발견됨 등)를 확인하세요. 서버 DRAFT 상태와 혼동하지 마세요.',
      };
    case 'policy':
      return {
        stepId: 'policy',
        message:
          tool && !tool.current_version_id
            ? '현재 버전이 없습니다. 서버에서 도구 탐색을 다시 적용한 뒤 실행 정책과 권한을 확인하세요.'
            : '실행 정책 탭에서 위험도·확인/승인 설정을 확인하고, 실행에 필요한 권한이 있는지 검토하세요. 활성화와 검증 완료(VERIFIED)는 별개입니다.',
      };
    case 'activate':
      return {
        stepId: 'activate',
        message:
          '정책을 확인한 뒤 활성화를 실행하세요. 활성화만으로 검증 완료나 즉시 실행이 보장되지 않습니다.',
      };
    default:
      return null;
  }
}

/** Why activate/deactivate is unavailable — body text, not title-only. */
export function toolLifecycleDisabledReason(
  tool: { status: string; current_version_id: string | null },
  options: { canEdit?: boolean } = {},
): string | null {
  const canEdit = options.canEdit ?? hasPermission('mcp.edit');
  if (!canEdit) {
    return '도구 상태를 변경할 권한이 없습니다. 관리자에게 mcp.edit 권한을 요청하세요.';
  }

  const status = tool.status as MCPToolStatus | string;
  if (status === 'MISSING') {
    return '원격에서 누락된 도구는 활성화할 수 없습니다. 서버 연결을 확인한 뒤 도구 탐색을 다시 적용하세요.';
  }
  if (status === 'BLOCKED') {
    return '차단된 도구는 활성화/비활성화할 수 없습니다. 관리 정책을 확인하세요.';
  }
  if (toolLifecycleAction(status) === 'activate' && !tool.current_version_id) {
    return '현재 버전이 없어 활성화할 수 없습니다. 서버에서 도구 탐색 결과를 적용해 버전을 만드세요.';
  }
  if (toolLifecycleAction(status) == null) {
    return '현재 상태에서는 활성화/비활성화할 수 없습니다.';
  }
  return null;
}

export function toolStatusMeaningNote(): string {
  return [
    '도구 활성(ACTIVE)은 선택·사용 후보로 올린 상태입니다.',
    '비활성(INACTIVE)·발견됨(DISCOVERED)은 아직 활성 사용 전입니다.',
    '서버 초안(DRAFT)은 MCP 서버 등록 상태이며 도구 상태가 아닙니다.',
    '검증 완료(VERIFIED)는 특정 ToolVersion의 검증 증빙이며 활성화와 다릅니다.',
    '실행 가능 여부는 도구 활성 외에도 실행 정책과 권한(ResourceGrant 등)에 따릅니다.',
  ].join(' ');
}

export function listEmptyGuidance(input: {
  hasFilters: boolean;
}): { title: string; description: string } {
  if (input.hasFilters) {
    return {
      title: '조건에 맞는 MCP 도구가 없습니다',
      description: '검색어 또는 필터를 바꾼 뒤 다시 찾아보세요. 상태가 누락/차단이면 목록에 있어도 활성화할 수 없습니다.',
    };
  }
  return {
    title: '등록된 MCP 도구가 없습니다',
    description:
      'MCP 서버 상세에서 연결 테스트 → 도구 탐색 미리보기 → 탐색 결과 적용을 먼저 진행하세요. 적용 후 이 목록에서 상태를 확인하고 활성화합니다.',
  };
}

export function listLoadFailureGuidance(code?: string, status?: number): string {
  if (status === 403 || code === 'FORBIDDEN') {
    return '도구 목록을 볼 권한이 없습니다. 관리자에게 mcp.view 권한을 요청하세요.';
  }
  return '목록을 다시 불러오거나, 권한이 있다면 MCP 서버에서 도구 탐색 적용 여부를 확인하세요.';
}

export function lifecycleMutationNextAction(
  action: 'activate' | 'deactivate',
  err: { code?: string; message?: string },
): string {
  if (err.code === 'FORBIDDEN' || /권한|forbidden/i.test(err.message ?? '')) {
    return '다음: 관리자에게 mcp.edit 권한을 요청한 뒤 다시 시도하세요. UI 안내는 참고용이며 최종 인가는 서버가 결정합니다.';
  }
  if (err.code === 'RESOURCE_CONFLICT') {
    if (/current ToolVersion|version/i.test(err.message ?? '')) {
      return '다음: 서버에서 도구 탐색을 다시 적용해 현재 버전을 만든 뒤 활성화하세요.';
    }
    if (/MISSING|BLOCKED/i.test(err.message ?? '')) {
      return '다음: 누락/차단 상태는 활성화할 수 없습니다. 서버 연결과 탐색 결과를 확인하세요.';
    }
    return '다음: 최신 도구 상태를 새로고침한 뒤 허용된 상태에서만 다시 시도하세요.';
  }
  if (err.code === 'RESOURCE_VERSION_CONFLICT') {
    return '다음: 화면에 표시된 최신 상태로 다시 활성화/비활성화를 시도하세요.';
  }
  return action === 'activate'
    ? '다음: 상태·실행 정책·권한을 확인한 뒤 다시 활성화하세요. 자동 활성화는 하지 않습니다.'
    : '다음: 최신 상태를 확인한 뒤 다시 비활성화하세요.';
}
