/**
 * Beginner Approvals / ApprovalDetail guidance — presentation only.
 * Canonical Approval / Execution statuses and decide APIs are unchanged.
 */

import type { ApprovalStatus } from '../../domain';

export type ApprovalStatusGuidance = {
  meaning: string;
  nextAction: string;
};

/** Fixed beginner flow: request → review risk → approve/reject. */
export const APPROVAL_REVIEW_FLOW_LABEL =
  '요청 확인 → 내용·위험 검토 → 승인/거절';

export const APPROVAL_REVIEW_STEPS = [
  { id: 'request', label: '요청 확인' },
  { id: 'review', label: '내용·위험 검토' },
  { id: 'decide', label: '승인/거절' },
] as const;

/** Approval Entity status ≠ Execution WAITING_APPROVAL; approve ≠ immediate RUNNING. */
export function approvalVsExecutionNote(): string {
  return (
    'Approval 상태(PENDING/APPROVED 등)와 Execution 상태(WAITING_APPROVAL/RUNNING 등)는 별개입니다. ' +
    'Approval PENDING을 WAITING_APPROVAL로 바꾸어 표시하지 않습니다. ' +
    '승인 결정이 저장되어도 API가 Execution을 즉시 RUNNING으로 올리지 않으며, 재개는 Outbox로 이어질 수 있습니다.'
  );
}

export function approvalStatusGuidance(
  status: ApprovalStatus | string,
): ApprovalStatusGuidance | null {
  switch (status) {
    case 'PENDING':
      return {
        meaning:
          '승인 요청이 대기(PENDING)입니다. 연결된 실행은 보통 WAITING_APPROVAL일 수 있으나, 그 실행 상태를 이 배지로 대체하지 않습니다.',
        nextAction:
          'safe_context에서 목적·위험·마스킹된 입력을 검토한 뒤, 결정 가능할 때만 승인 또는 거절하세요. 자동 결정하지 않습니다.',
      };
    case 'APPROVED':
      return {
        meaning: '승인 요청이 승인됨(APPROVED)으로 종료되었습니다.',
        nextAction:
          '실행 상세에서 재개·진행을 확인하세요. 승인만으로 즉시 실행 완료나 RUNNING을 보장하지 않습니다.',
      };
    case 'REJECTED':
      return {
        meaning: '승인 요청이 거절됨(REJECTED)입니다. Execution 상태값 REJECTED/EXPIRED를 만들지 않습니다.',
        nextAction:
          '실행·단계가 계획 정책에 따라 FAILED 등으로 종료됐는지 실행 상세에서 확인하세요.',
      };
    case 'EXPIRED':
      return {
        meaning: '승인 요청이 만료됨(EXPIRED)입니다. 더 이상 승인·거절할 수 없습니다.',
        nextAction:
          '만료된 요청은 재결정하지 않습니다. 필요하면 실행 상세에서 이후 처리·실패 여부를 확인하세요.',
      };
    case 'CANCELLED':
      return {
        meaning: '승인 요청이 취소됨(CANCELLED)입니다.',
        nextAction: '이 요청으로는 더 이상 결정하지 않습니다. 관련 실행 상태를 실행 상세에서 확인하세요.',
      };
    default:
      return null;
  }
}

export function approvalCannotDecideGuidance(status: string, canDecide: boolean): string | null {
  if (status === 'PENDING' && !canDecide) {
    return (
      '이 요청은 조회만 가능합니다. 이미 결정했거나 정책·만료·범위·자기승인 규칙 때문에 ' +
      '지금은 결정할 수 없습니다. 자동으로 결정을 대신하지 않습니다.'
    );
  }
  if (status === 'EXPIRED') {
    return '만료된 승인 요청입니다. 승인·거절 버튼을 제공하지 않습니다.';
  }
  if (status === 'APPROVED' || status === 'REJECTED' || status === 'CANCELLED') {
    return `이미 ${status} 상태입니다. 추가 결정은 할 수 없습니다.`;
  }
  return null;
}

export function approvalsListEmptyGuidance(status: ApprovalStatus | string): {
  title: string;
  description: string;
} {
  if (status === 'PENDING') {
    return {
      title: '대기 중인 승인 요청이 없습니다.',
      description:
        '처리할 PENDING 요청이 생기면 여기에 표시됩니다. 빈 목록을 승인된 것으로 해석하지 마세요.',
    };
  }
  return {
    title: '조건에 맞는 승인 요청이 없습니다.',
    description: '상태 필터를 바꾸거나 새로고침해 보세요. 권한·범위 밖 요청은 목록에 나타나지 않을 수 있습니다.',
  };
}

export function approvalsForbiddenGuidance(): string {
  return (
    '승인 목록·상세를 보려면 관리자에게 승인 결정 권한(approval.decide)을 요청하세요. ' +
    '권한 없이 승인·거절할 수 없습니다.'
  );
}

/** User-facing decide/submit errors — no success claim; no API contract change. */
export function formatApprovalDecisionError(err: {
  status?: number;
  code?: string;
  message?: string;
  requestId?: string | null;
} | null): { message: string; requestId?: string } {
  if (!err) {
    return { message: '결정을 제출하지 못했습니다. 네트워크 상태를 확인한 뒤 다시 시도하세요.' };
  }
  const requestId = err.requestId ?? undefined;
  if (err.status === 0 || err.code === 'NETWORK_ERROR') {
    return {
      message:
        '네트워크 오류로 결정을 확인하지 못했습니다. 성공으로 표시하지 않습니다. 목록을 새로고침해 상태를 확인하세요.',
      requestId,
    };
  }
  if (err.status === 403) {
    return {
      message:
        err.message ||
        '결정 권한이 없거나 정책·범위에 맞지 않습니다. 관리자에게 권한을 확인하세요.',
      requestId,
    };
  }
  if (err.status === 404) {
    return {
      message:
        err.message ||
        '승인 요청을 찾을 수 없거나 더 이상 볼 수 없습니다. (서버는 권한 부족을 404로 반환할 수 있습니다.)',
      requestId,
    };
  }
  if (err.status === 409) {
    return {
      message:
        err.message ||
        '이미 결정했거나 상태가 변경되어 처리할 수 없습니다(중복·충돌). 상세를 다시 불러오세요. 자동으로 재결정하지 않습니다.',
      requestId,
    };
  }
  if (err.status === 422) {
    return {
      message: err.message || '입력값이 올바르지 않습니다. 거절 사유 등 필수 항목을 확인하세요.',
      requestId,
    };
  }
  return {
    message: err.message || '결정을 제출하지 못했습니다. 성공으로 표시하지 않습니다.',
    requestId,
  };
}
