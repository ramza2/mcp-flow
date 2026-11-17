/**
 * Beginner ExecutionDetail guidance — presentation only.
 * Canonical Execution / Step statuses are unchanged.
 */

import type { ExecutionStatus } from '../../domain';

export type ExecutionStatusGuidance = {
  meaning: string;
  nextAction: string;
};

/** Status meaning + next action for beginner UX (no new Domain states). */
export function executionStatusGuidance(
  status: ExecutionStatus | string,
): ExecutionStatusGuidance | null {
  switch (status) {
    case 'WAITING_APPROVAL':
      return {
        meaning:
          '승인 대기입니다. 실행이 승인이 필요한 단계에서 멈춰 있습니다. 승인 요청(Approval)의 PENDING과 별개로, 실행 상태는 WAITING_APPROVAL입니다.',
        nextAction:
          '승인 화면에서 요청을 확인·결정하세요. 이 화면에서 새 승인·실행을 시작하지 않습니다.',
      };
    case 'WAITING_INPUT':
      return {
        meaning:
          'MCP 도구가 추가 입력을 요청해 실행이 대기 중입니다. 기획 단계의 확인 대기와 구분됩니다.',
        nextAction:
          '아래 입력 패널에서 요청된 값을 제출하세요. requestState 등 내부 토큰은 표시·수정하지 않습니다.',
      };
    case 'FAILED':
      return {
        meaning: '실행이 실패로 종료되었습니다. 일부 단계는 이미 외부 효과를 남겼을 수 있습니다.',
        nextAction:
          '오류 코드와 단계 상세를 확인하세요. 이 화면에서 자동 재시도나 새 실행 버튼을 제공하지 않습니다.',
      };
    case 'TIMED_OUT':
      return {
        meaning: '제한 시간 초과로 실행이 종료되었습니다.',
        nextAction:
          '시간 초과된 단계와 외부 시스템 상태를 확인하세요. 자동 재시도 안내는 하지 않습니다.',
      };
    case 'PARTIALLY_SUCCEEDED':
      return {
        meaning:
          '일부 단계만 성공하고 실행이 종료되었습니다. 실패한 단계와 성공한 외부 효과를 함께 확인해야 합니다.',
        nextAction:
          '단계 탭에서 실패·성공 단계를 구분해 확인하세요. 자동으로 나머지를 재실행하지 않습니다.',
      };
    case 'CANCEL_REQUESTED':
      return {
        meaning:
          '취소가 요청된 중간 상태입니다. 아직 최종 취소(CANCELLED)가 아닙니다.',
        nextAction:
          '진행 중 단계 정리 후 취소 완료로 바뀔 때까지 기다리세요. 버튼 클릭 직후 완료로 가정하지 마세요.',
      };
    case 'UNKNOWN_OUTCOME':
      return {
        meaning:
          '외부 호출 결과를 확정하지 못했습니다. 이미 외부에서 처리되었을 수 있습니다.',
        nextAction:
          '외부 시스템에서 결과부터 확인하세요. 자동 재시도는 하지 않으며 이 화면에도 재시도 버튼을 두지 않습니다.',
      };
    case 'RUNNING':
      return {
        meaning: '실행이 진행 중입니다.',
        nextAction: '단계·이벤트 탭에서 진행을 확인하세요. 필요하면 권한이 있을 때 취소를 요청할 수 있습니다.',
      };
    case 'SUCCEEDED':
      return {
        meaning: '실행이 성공적으로 완료되었습니다.',
        nextAction: '결과 요약과 단계 이력을 확인하세요.',
      };
    case 'CANCELLED':
      return {
        meaning: '실행이 취소로 종료되었습니다.',
        nextAction: '필요 시 원인·시점과 단계 상태를 확인하세요.',
      };
    case 'QUEUED':
    case 'CREATED':
      return {
        meaning: '실행이 생성되었거나 대기열에 있습니다.',
        nextAction: '시작될 때까지 기다리거나, 권한이 있으면 취소를 요청할 수 있습니다.',
      };
    default:
      return null;
  }
}

export function unknownOutcomeGuidance(): ExecutionStatusGuidance {
  return {
    meaning:
      '결과 확인이 필요한 단계가 있습니다. 외부 부작용이 이미 발생했을 수 있습니다.',
    nextAction:
      '외부 시스템 결과를 우선 확인하세요. 자동 재시도 안내나 재시도 버튼은 제공하지 않습니다.',
  };
}

export function labelSseConnectionState(state: string): string {
  switch (state) {
    case 'connecting':
      return '연결 중';
    case 'live':
      return '실시간';
    case 'reconnecting':
      return '재연결 중';
    case 'polling':
      return '폴링 대체';
    case 'unavailable':
      return '사용 불가';
    case 'disconnected':
    default:
      return '연결 끊김';
  }
}
