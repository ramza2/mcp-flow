/**
 * Beginner MCP server setup checklist — presentation only.
 * Derives from existing Server / ConnectionTest / Discovery / Tool fields.
 * Does not invent Domain states or change API / lifecycle transitions.
 */

import type { ConnectionTestDto, DiscoveryDto, FactoryDiscoveryReconcileStatus } from '../../api/types';

export type McpServerSetupStepId = 'register' | 'connect' | 'discover' | 'apply';

export type McpServerSetupStep = {
  id: McpServerSetupStepId;
  label: string;
  done: boolean;
};

export const MCP_SERVER_SETUP_FLOW_LABEL =
  '등록 → 연결 확인 → 도구 탐색 → 결과 적용';

export function buildMcpServerSetupSteps(input: {
  registered: boolean;
  connectionOk: boolean;
  discoveryPreviewed: boolean;
  applied: boolean;
}): McpServerSetupStep[] {
  return [
    { id: 'register', label: '등록', done: input.registered },
    { id: 'connect', label: '연결 확인', done: input.connectionOk },
    { id: 'discover', label: '도구 탐색', done: input.discoveryPreviewed },
    { id: 'apply', label: '결과 적용', done: input.applied },
  ];
}

export function deriveMcpServerSetupFlags(input: {
  lastHealthyAt: string | null;
  lastTest: Pick<ConnectionTestDto, 'status'> | null;
  previewResult: Pick<DiscoveryDto, 'success'> | null;
  discoveries: Pick<DiscoveryDto, 'success' | 'apply_changes'>[];
  toolsTotal: number;
}): {
  connectionOk: boolean;
  discoveryPreviewed: boolean;
  applied: boolean;
} {
  const connectionOk =
    Boolean(input.lastHealthyAt) || input.lastTest?.status === 'SUCCEEDED';
  const discoveryPreviewed =
    Boolean(input.previewResult?.success)
    || input.discoveries.some(d => d.success)
    || input.toolsTotal > 0;
  const applied =
    input.toolsTotal > 0
    || input.discoveries.some(d => d.apply_changes && d.success);
  return { connectionOk, discoveryPreviewed, applied };
}

export function nextMcpServerSetupAction(
  steps: McpServerSetupStep[],
): { stepId: McpServerSetupStepId; message: string } | null {
  const pending = steps.find(s => !s.done);
  if (!pending) return null;
  switch (pending.id) {
    case 'register':
      return {
        stepId: 'register',
        message: '기본 정보와 서버 주소를 입력해 MCP 서버를 등록하세요. 등록만으로 도구가 활성화되지는 않습니다.',
      };
    case 'connect':
      return {
        stepId: 'connect',
        message: '연결 테스트를 실행해 서버에 접속되는지 확인하세요.',
      };
    case 'discover':
      return {
        stepId: 'discover',
        message: '도구 탐색 미리보기로 추가·변경·누락 항목을 확인하세요. 미리보기만으로는 도구가 등록되지 않습니다.',
      };
    case 'apply':
      return {
        stepId: 'apply',
        message: '미리보기 결과를 확인한 뒤 탐색 결과 적용을 실행하세요. 적용 후에도 필요한 도구는 별도로 활성화해야 합니다.',
      };
    default:
      return null;
  }
}

/** Why "탐색 결과 적용" is disabled — body text, not title-only. */
export function applyDiscoveryDisabledReason(input: {
  previewResult: Pick<DiscoveryDto, 'success' | 'factory_diff' | 'error_message'> | null;
  discovering: boolean;
  applying: boolean;
}): string | null {
  if (input.discovering) {
    return '도구 탐색 미리보기가 진행 중입니다. 완료될 때까지 기다려 주세요.';
  }
  if (input.applying) {
    return '탐색 결과 적용이 진행 중입니다.';
  }
  if (!input.previewResult) {
    return '먼저 도구 탐색 미리보기를 실행하세요. 미리보기 성공 후에만 결과를 적용할 수 있습니다.';
  }
  if (!input.previewResult.success) {
    return '도구 탐색 미리보기가 실패했습니다. 연결을 확인한 뒤 다시 탐색하세요.';
  }
  const factoryStatus = input.previewResult.factory_diff?.status;
  if (factoryStatus && factoryStatus !== 'CLEAN') {
    return 'Factory 계약 정합성이 일치하지 않아 적용할 수 없습니다. 승인된 패키지와 원격 도구 계약을 확인하세요.';
  }
  return null;
}

export function registerWizardNextDisabledReason(
  step: number,
  name: string,
  endpointUrl: string,
): string | null {
  if (step === 0 && !name.trim()) {
    return '다음 단계로 가려면 서버 이름을 입력하세요.';
  }
  if (step === 1 && !endpointUrl.trim()) {
    return '다음 단계로 가려면 MCP 서버 주소를 입력하세요.';
  }
  return null;
}

export function registerSubmitDisabledReason(
  name: string,
  endpointUrl: string,
): string | null {
  if (!name.trim() && !endpointUrl.trim()) {
    return '서버 이름과 MCP 서버 주소를 입력해야 등록할 수 있습니다.';
  }
  if (!name.trim()) {
    return '서버 이름을 입력해야 등록할 수 있습니다.';
  }
  if (!endpointUrl.trim()) {
    return 'MCP 서버 주소를 입력해야 등록할 수 있습니다.';
  }
  return null;
}

/** Presentation label for Factory reconcile status — canonical value unchanged. */
export function labelFactoryReconcileStatus(value: string): string {
  const map: Record<FactoryDiscoveryReconcileStatus, string> = {
    CLEAN: '계약 일치',
    MISMATCHED: '계약 불일치',
    AMBIGUOUS: '판정 불명확',
  };
  return map[value as FactoryDiscoveryReconcileStatus] ?? value;
}
