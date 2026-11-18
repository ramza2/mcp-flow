/**
 * Beginner WorkflowDetail run-readiness guidance — presentation only.
 * Does not invent Domain states or change API / lifecycle transitions.
 */

export type WorkflowRunReadyStepId =
  | 'draft'
  | 'validate'
  | 'publish'
  | 'activate'
  | 'run';

export type WorkflowRunReadyStep = {
  id: WorkflowRunReadyStepId;
  label: string;
  done: boolean;
};

export const WORKFLOW_RUN_READY_FLOW_LABEL =
  '초안 → 검증 → 게시 → 활성화 → 실행';

export function buildWorkflowRunReadySteps(input: {
  hasDraft: boolean;
  draftValidated: boolean;
  hasPublishedValid: boolean;
  isActive: boolean;
}): WorkflowRunReadyStep[] {
  return [
    { id: 'draft', label: '초안', done: input.hasDraft || input.hasPublishedValid },
    { id: 'validate', label: '검증', done: input.draftValidated || input.hasPublishedValid },
    { id: 'publish', label: '게시', done: input.hasPublishedValid },
    { id: 'activate', label: '활성화', done: input.isActive && input.hasPublishedValid },
    // "done" means ready to run (ACTIVE + published valid), not that an execution already started.
    { id: 'run', label: '실행', done: input.isActive && input.hasPublishedValid },
  ];
}

export function nextWorkflowRunReadyAction(
  steps: WorkflowRunReadyStep[],
): { stepId: WorkflowRunReadyStepId; message: string } | null {
  const pending = steps.find((step) => !step.done);
  if (!pending) return null;
  switch (pending.id) {
    case 'draft':
      return {
        stepId: 'draft',
        message:
          '초안 버전을 만들거나 편집해 실행 계획을 작성하세요. 초안만으로 게시·활성화·실행되지 않습니다.',
      };
    case 'validate':
      return {
        stepId: 'validate',
        message:
          '디자이너에서 초안을 저장한 뒤 서버 검증을 통과하세요. 검증 통과 전에는 게시할 수 없습니다.',
      };
    case 'publish':
      return {
        stepId: 'publish',
        message:
          '검증된 초안을 게시하세요. 게시만으로 활성화되거나 자동 실행되지 않습니다.',
      };
    case 'activate':
      return {
        stepId: 'activate',
        message:
          '검증된 게시 버전으로 워크플로를 활성화하세요. 활성화만으로 실행되지 않습니다.',
      };
    case 'run':
      return {
        stepId: 'run',
        message:
          'ACTIVE 워크플로의 현재 게시 버전으로 수동 실행할 수 있습니다. 실행 전 서버가 권한·정책을 다시 검증합니다.',
      };
    default:
      return null;
  }
}

export function publishDisabledReason(input: {
  busy: boolean;
  hasDraft: boolean;
  canPublish: boolean;
}): string | null {
  if (input.canPublish && !input.busy) return null;
  if (input.busy) return '다른 작업이 진행 중입니다. 완료될 때까지 기다려 주세요.';
  if (!input.hasDraft) {
    return '게시할 초안이 없습니다. 먼저 초안 버전을 생성·편집하세요.';
  }
  return '게시하려면 초안을 저장하고 서버 검증(VALID)을 통과해야 합니다. 자동으로 게시하지 않습니다.';
}

export function activateDisabledReason(input: {
  busy: boolean;
  workflowStatus: string;
  canActivate: boolean;
}): string | null {
  if (input.workflowStatus === 'ACTIVE') return null;
  if (input.canActivate && !input.busy) return null;
  if (input.busy) return '다른 작업이 진행 중입니다. 완료될 때까지 기다려 주세요.';
  return '활성화하려면 검증된 게시 버전이 현재 버전으로 있어야 합니다. 자동으로 활성화하지 않습니다.';
}

export function runDisabledReason(input: {
  busy: boolean;
  canRun: boolean;
  workflowStatus: string;
  hasPublishedValid: boolean;
}): string | null {
  if (input.canRun && !input.busy) return null;
  if (input.busy) return '다른 작업이 진행 중입니다. 완료될 때까지 기다려 주세요.';
  if (input.workflowStatus !== 'ACTIVE') {
    return '수동 실행은 ACTIVE 워크플로에서만 가능합니다. 먼저 활성화하세요.';
  }
  if (!input.hasPublishedValid) {
    return '검증된 게시 버전이 있어야 실행할 수 있습니다. 게시·검증 상태를 확인하세요.';
  }
  return 'ACTIVE 워크플로의 검증된 게시 버전만 실행할 수 있습니다.';
}

function isNetworkUncertainty(err: {
  status?: number;
  code?: string;
} | null): boolean {
  return Boolean(err && (err.status === 0 || err.code === 'NETWORK_ERROR'));
}

/** Preserve server text when present; append Korean next action without inventing success. */
export function formatWorkflowDetailActionError(
  err: { status?: number; code?: string; message?: string; requestId?: string | null } | null,
  kind: 'publish' | 'status' | 'draft' | 'generic',
): { message: string; requestId?: string } {
  if (!err) {
    return {
      message:
        kind === 'publish'
          ? '게시하지 못했습니다. 서버 검증 상태를 확인해 주세요.'
          : kind === 'status'
            ? '워크플로 상태를 변경하지 못했습니다.'
            : kind === 'draft'
              ? '초안 버전 생성에 실패했습니다.'
              : '요청을 처리하지 못했습니다.',
    };
  }
  const requestId = err.requestId ?? undefined;
  if (isNetworkUncertainty(err)) {
    return {
      message:
        '네트워크 오류로 결과를 확인하지 못했습니다. 성공으로 표시하지 않습니다. 화면을 새로고침해 최신 상태를 확인하세요. 무조건 재시도하지 마세요.',
      requestId,
    };
  }
  if (err.status === 403) {
    const next =
      '권한이 없습니다. 관리자에게 워크플로 관련 권한을 확인하세요. 자동으로 재시도하지 않습니다.';
    return {
      message: composeMessage(err.message, next),
      requestId,
    };
  }
  if (err.status === 409) {
    const next =
      '상태가 충돌했거나 다른 곳에서 변경되었을 수 있습니다. 최신 상태를 다시 불러오세요. 자동으로 재시도하지 않습니다.';
    return {
      message: composeMessage(err.message, next),
      requestId,
    };
  }
  const fallback =
    kind === 'publish'
      ? '게시하지 못했습니다. 서버 검증 상태를 확인해 주세요.'
      : kind === 'status'
        ? '워크플로 상태를 변경하지 못했습니다.'
        : kind === 'draft'
          ? '초안 버전 생성에 실패했습니다.'
          : '요청을 처리하지 못했습니다.';
  const original = (err.message ?? '').trim();
  return {
    message: original || fallback,
    requestId,
  };
}

/**
 * Manual-run submit errors.
 * Network uncertainty must not claim execution creation or push blind retries.
 * Non-network failures may note the same Idempotency-Key can be reused — never auto-retry.
 */
export function formatWorkflowRunSubmitError(
  err: { status?: number; code?: string; message?: string; requestId?: string | null } | null,
): string {
  if (!err) {
    return '실행 요청에 실패했습니다. 동일한 요청 키로 재시도할 수 있습니다. 자동으로 재시도하지는 않습니다.';
  }
  if (isNetworkUncertainty(err)) {
    return (
      '네트워크 오류로 실행 생성 여부를 확인하지 못했습니다. 성공으로 표시하지 않습니다. ' +
      '실행 목록에서 생성 여부를 확인하세요. 결과가 불명확할 때 무조건 재시도하지 마세요.'
    );
  }
  if (err.status === 403) {
    return composeMessage(
      err.message,
      '실행 권한이 없습니다. workflow.execute + WORKFLOW 권한 및 도구별 mcp.tool.execute + MCP_TOOL 권한을 확인하세요.',
    );
  }
  if (err.status === 409) {
    return composeMessage(
      err.message,
      '상태가 충돌했거나 변경되었을 수 있습니다. 최신 워크플로·버전을 다시 확인하세요. 자동으로 재시도하지 않습니다.',
    );
  }
  const original = (err.message ?? '').trim();
  if (original) {
    return `${original} 동일한 요청 키로 재시도할 수 있습니다. 자동으로 재시도하지는 않습니다.`;
  }
  return '실행 요청에 실패했습니다. 동일한 요청 키로 재시도할 수 있습니다. 자동으로 재시도하지는 않습니다.';
}

function composeMessage(serverMessage: string | undefined | null, next: string): string {
  const original = (serverMessage ?? '').trim();
  const guidance = next.trim();
  if (!guidance) return original;
  if (!original) return guidance;
  if (original.includes(guidance) || guidanceIncludesOverlap(original, guidance)) {
    return original;
  }
  return `${original} 다음: ${guidance}`;
}

function guidanceIncludesOverlap(original: string, guidance: string): boolean {
  const markers = [
    '권한이 없습니다',
    '무조건 재시도',
    '성공으로 표시하지',
    '다시 불러',
    '다시 확인',
    '동일한 요청 키',
  ];
  const nextMarkers = markers.filter((m) => guidance.includes(m));
  if (nextMarkers.length === 0) return false;
  return nextMarkers.every((m) => original.includes(m));
}
