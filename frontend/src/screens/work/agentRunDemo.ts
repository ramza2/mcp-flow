/** Fixed visual-only AgentRun scenario. Never sends data or starts real executions. */
export const DEMO_REQUEST = '주간 보고서를 생성하고 개발팀에 이메일로 발송해줘';

export const DEMO_LIMITATION =
  '이 화면은 상태 전이 안내용 시뮬레이션입니다. 입력 문장을 LLM으로 분석하지 않으며, ' +
  '실제 MCP Tool 실행·파일 생성·이메일 발송·Execution 저장을 수행하지 않습니다.';

export const DEMO_UNSUPPORTED_INPUT =
  '현재 이 화면에서는 위의 고정된 예시만 시뮬레이션할 수 있습니다. ' +
  '입력하신 요청은 분석하거나 실행하지 않았습니다.';

export const DEMO_FINISHED =
  '상태 전이 시뮬레이션만 완료되었습니다. 보고서 파일을 생성하거나 이메일을 발송하지 않았으며, ' +
  '실제 Execution 기록도 없습니다.';

export const REAL_EXECUTIONS_PATH = '/executions';

export function isDemoRequest(value: string): boolean {
  return value.trim() === DEMO_REQUEST;
}
