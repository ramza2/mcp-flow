// MCPFlow Mock Data

export const mockMCPServers = [
  { id: 'srv-001', name: 'Weather MCP', transport: 'Streamable HTTP', status: 'ACTIVE', protocol: 'Current MCP', version: '2025-03-26', discovery: 'Inferred Current', toolCount: 3, lastHealth: '2분 전', updatedAt: '2026-09-02 14:20' },
  { id: 'srv-002', name: 'Internal Document MCP', transport: 'Streamable HTTP', status: 'ACTIVE', protocol: 'Current MCP', version: '2025-03-26', discovery: 'Listed', toolCount: 8, lastHealth: '5분 전', updatedAt: '2026-09-01 10:05' },
  { id: 'srv-003', name: 'Email MCP', transport: 'Streamable HTTP', status: 'ACTIVE', protocol: 'Current MCP', version: '2025-03-26', discovery: 'Inferred Current', toolCount: 4, lastHealth: '1분 전', updatedAt: '2026-08-30 09:00' },
  { id: 'srv-004', name: 'Calendar MCP', transport: 'STDIO', status: 'ACTIVE', protocol: 'Current MCP', version: '2025-03-26', discovery: 'Listed', toolCount: 5, lastHealth: '3분 전', updatedAt: '2026-08-28 11:30' },
  { id: 'srv-005', name: 'Report MCP', transport: 'Streamable HTTP', status: 'INACTIVE', protocol: 'Current MCP', version: '2025-03-26', discovery: 'Inferred Current', toolCount: 6, lastHealth: '2시간 전', updatedAt: '2026-08-25 16:40' },
  { id: 'srv-006', name: 'Legacy ERP MCP', transport: 'Legacy HTTP/SSE', status: 'ACTIVE', protocol: 'Legacy MCP', version: '2024-11-05', discovery: 'Listed', toolCount: 12, lastHealth: '10분 전', updatedAt: '2026-08-20 08:15' },
];

export const mockTools = [
  { id: 'tool-001', displayName: 'Get Current Weather', sourceName: 'get_current_weather', serverId: 'srv-001', serverName: 'Weather MCP', status: 'ACTIVE', riskClass: 'READ_ONLY', currentVersion: 'v1.2.0', validation: 'VALID', verification: 'VERIFIED', usedBy: 2, updatedAt: '2026-09-01' },
  { id: 'tool-002', displayName: 'Search Documents', sourceName: 'search_documents', serverId: 'srv-002', serverName: 'Internal Document MCP', status: 'ACTIVE', riskClass: 'READ_ONLY', currentVersion: 'v2.1.0', validation: 'VALID', verification: 'VERIFIED', usedBy: 4, updatedAt: '2026-09-01' },
  { id: 'tool-003', displayName: 'Generate Report', sourceName: 'generate_report', serverId: 'srv-005', serverName: 'Report MCP', status: 'INACTIVE', riskClass: 'IDEMPOTENT_WRITE', currentVersion: 'v1.0.3', validation: 'WARNING', verification: 'EXPIRED', usedBy: 1, updatedAt: '2026-08-28' },
  { id: 'tool-004', displayName: 'Send Email', sourceName: 'send_email', serverId: 'srv-003', serverName: 'Email MCP', status: 'ACTIVE', riskClass: 'NON_IDEMPOTENT_WRITE', currentVersion: 'v3.0.1', validation: 'VALID', verification: 'VERIFIED', usedBy: 3, updatedAt: '2026-08-30' },
  { id: 'tool-005', displayName: 'Create Calendar Event', sourceName: 'create_calendar_event', serverId: 'srv-004', serverName: 'Calendar MCP', status: 'ACTIVE', riskClass: 'IDEMPOTENT_WRITE', currentVersion: 'v1.4.0', validation: 'VALID', verification: 'VERIFIED', usedBy: 2, updatedAt: '2026-09-01' },
  { id: 'tool-006', displayName: 'Lookup Employee', sourceName: 'lookup_employee', serverId: 'srv-006', serverName: 'Legacy ERP MCP', status: 'ACTIVE', riskClass: 'READ_ONLY', currentVersion: 'v0.9.2', validation: 'WARNING', verification: 'PENDING', usedBy: 1, updatedAt: '2026-08-20' },
  { id: 'tool-007', displayName: 'Delete Record', sourceName: 'delete_record', serverId: 'srv-006', serverName: 'Legacy ERP MCP', status: 'BLOCKED', riskClass: 'DESTRUCTIVE', currentVersion: 'v1.0.0', validation: 'VALID', verification: 'FAILED', usedBy: 0, updatedAt: '2026-08-15' },
  { id: 'tool-008', displayName: 'Get Weather Forecast', sourceName: 'get_weather_forecast', serverId: 'srv-001', serverName: 'Weather MCP', status: 'ACTIVE', riskClass: 'READ_ONLY', currentVersion: 'v1.1.0', validation: 'VALID', verification: 'VERIFIED', usedBy: 1, updatedAt: '2026-09-01' },
];

export const mockAgents = [
  { id: 'agt-001', name: 'General Work Assistant', status: 'ACTIVE', publishedVersion: 'v2', allowedTools: 6, modelProfile: 'Claude 3.5 Sonnet', owner: 'admin', updatedAt: '2026-09-01', versions: [
    { version: 'v3', status: 'DRAFT', createdAt: '2026-09-02', author: 'admin' },
    { version: 'v2', status: 'PUBLISHED', createdAt: '2026-08-31', author: 'admin' },
    { version: 'v1', status: 'DEPRECATED', createdAt: '2026-08-20', author: 'admin' },
  ]},
  { id: 'agt-002', name: 'Report Assistant', status: 'ACTIVE', publishedVersion: 'v1', allowedTools: 4, modelProfile: 'Claude 3.5 Sonnet', owner: 'admin', updatedAt: '2026-08-31', versions: [
    { version: 'v2', status: 'DRAFT', createdAt: '2026-09-01', author: 'jkim' },
    { version: 'v1', status: 'PUBLISHED', createdAt: '2026-08-25', author: 'admin' },
  ]},
  { id: 'agt-003', name: 'Research Assistant', status: 'ACTIVE', publishedVersion: 'v1', allowedTools: 3, modelProfile: 'Claude 3 Haiku', owner: 'jkim', updatedAt: '2026-08-28' , versions: [
    { version: 'v1', status: 'PUBLISHED', createdAt: '2026-08-20', author: 'jkim' },
  ]},
  { id: 'agt-004', name: 'Operations Assistant', status: 'INACTIVE', publishedVersion: null, allowedTools: 8, modelProfile: 'Claude 3.5 Sonnet', owner: 'admin', updatedAt: '2026-08-10', versions: [
    { version: 'v1', status: 'DRAFT', createdAt: '2026-08-10', author: 'admin' },
  ]},
];

export const mockWorkflows = [
  { id: 'wf-001', name: 'Weekly Report Workflow', status: 'PUBLISHED', publishedVersion: 'v2', steps: 5, owner: 'admin', lastPublished: '2026-08-31', updatedAt: '2026-09-01' },
  { id: 'wf-002', name: 'Document Review Workflow', status: 'PUBLISHED', publishedVersion: 'v1', steps: 4, owner: 'jkim', lastPublished: '2026-08-28', updatedAt: '2026-08-29' },
  { id: 'wf-003', name: 'Approval & Send Workflow', status: 'DRAFT', publishedVersion: null, steps: 6, owner: 'admin', lastPublished: null, updatedAt: '2026-09-02' },
];

export const mockExecutions = [
  { id: 'EXE-20260902-00125', name: '주간 보고서 생성 및 발송', source: 'Agent', user: 'admin', agent: 'Report Assistant', workflow: null, status: 'RUNNING', stepCount: 3, totalSteps: 5, duration: '1m 23s', startedAt: '2026-09-02 14:30', updatedAt: '2026-09-02 14:31' },
  { id: 'EXE-20260902-00124', name: '문서 검색: Q3 시장분석', source: 'Agent', user: 'jkim', agent: 'Research Assistant', workflow: null, status: 'SUCCEEDED', stepCount: 2, totalSteps: 2, duration: '12s', startedAt: '2026-09-02 14:10', updatedAt: '2026-09-02 14:10' },
  { id: 'EXE-20260902-00123', name: '고객 이메일 발송', source: 'Agent', user: 'admin', agent: 'General Work Assistant', workflow: null, status: 'WAITING_APPROVAL', stepCount: 2, totalSteps: 3, duration: '45s', startedAt: '2026-09-02 13:55', updatedAt: '2026-09-02 13:56' },
  { id: 'EXE-20260902-00122', name: 'Weekly Report Workflow', source: 'Schedule', user: 'system', agent: null, workflow: 'Weekly Report Workflow', status: 'PARTIALLY_SUCCEEDED', stepCount: 4, totalSteps: 5, duration: '3m 12s', startedAt: '2026-09-02 09:00', updatedAt: '2026-09-02 09:03' },
  { id: 'EXE-20260901-00121', name: '인사팀 직원 조회', source: 'Agent', user: 'mpark', agent: 'General Work Assistant', workflow: null, status: 'FAILED', stepCount: 1, totalSteps: 2, duration: '5s', startedAt: '2026-09-01 17:30', updatedAt: '2026-09-01 17:30' },
  { id: 'EXE-20260901-00120', name: '문서 리뷰 워크플로우', source: 'Workflow', user: 'jkim', agent: null, workflow: 'Document Review Workflow', status: 'SUCCEEDED', stepCount: 4, totalSteps: 4, duration: '8m 44s', startedAt: '2026-09-01 14:00', updatedAt: '2026-09-01 14:08' },
  { id: 'EXE-20260901-00119', name: '캘린더 이벤트 생성', source: 'Agent', user: 'admin', agent: 'General Work Assistant', workflow: null, status: 'CANCELLED', stepCount: 1, totalSteps: 2, duration: '–', startedAt: '2026-09-01 11:20', updatedAt: '2026-09-01 11:21' },
];

export const mockApprovals = [
  { id: 'apr-001', purpose: '고객사 이메일 발송 승인', requester: 'admin', agent: 'General Work Assistant', tool: 'Send Email', riskClass: 'NON_IDEMPOTENT_WRITE', requestedAt: '2026-09-02 13:55', expiresAt: '2026-09-02 14:55', status: 'PENDING', executionId: 'EXE-20260902-00123' },
  { id: 'apr-002', purpose: '주간 보고서 파일 삭제', requester: 'system', agent: 'Report Assistant', tool: 'Delete Record', riskClass: 'DESTRUCTIVE', requestedAt: '2026-09-01 09:00', expiresAt: '2026-09-01 10:00', status: 'APPROVED', executionId: 'EXE-20260901-00118' },
  { id: 'apr-003', purpose: '임직원 정보 일괄 조회', requester: 'mpark', agent: 'General Work Assistant', tool: 'Lookup Employee', riskClass: 'READ_ONLY', requestedAt: '2026-08-31 16:30', expiresAt: '2026-08-31 17:30', status: 'REJECTED', executionId: 'EXE-20260831-00117' },
];

export const mockSchedules = [
  { id: 'sch-001', name: '주간 보고서 자동 실행', targetType: 'Workflow', target: 'Weekly Report Workflow', version: 'v2', schedule: 'Mondays 09:00', timezone: 'Asia/Seoul', nextRun: '2026-09-09 09:00', lastRun: '2026-09-02 09:00', lastResult: 'PARTIALLY_SUCCEEDED', status: 'ACTIVE' },
  { id: 'sch-002', name: '일일 날씨 알림', targetType: 'Agent', target: 'General Work Assistant', version: 'v2', schedule: 'Daily 08:00', timezone: 'Asia/Seoul', nextRun: '2026-09-03 08:00', lastRun: '2026-09-02 08:00', lastResult: 'SUCCEEDED', status: 'ACTIVE' },
  { id: 'sch-003', name: '월말 정산 보고서', targetType: 'Workflow', target: 'Document Review Workflow', version: 'v1', schedule: 'Last day of month 23:00', timezone: 'Asia/Seoul', nextRun: '2026-09-30 23:00', lastRun: '2026-08-31 23:00', lastResult: 'SUCCEEDED', status: 'INACTIVE' },
];

export const mockUsers = [
  { id: 'usr-001', name: '관리자', username: 'admin', status: 'ACTIVE', roles: ['Super Admin'], lastLogin: '2026-09-02 14:00', updatedAt: '2026-09-01' },
  { id: 'usr-002', name: '김지현', username: 'jkim', status: 'ACTIVE', roles: ['Operator', 'Approver'], lastLogin: '2026-09-02 11:30', updatedAt: '2026-08-30' },
  { id: 'usr-003', name: '박민수', username: 'mpark', status: 'ACTIVE', roles: ['User'], lastLogin: '2026-09-01 17:25', updatedAt: '2026-08-25' },
  { id: 'usr-004', name: '이영희', username: 'ylee', status: 'INACTIVE', roles: ['User'], lastLogin: '2026-08-20 09:00', updatedAt: '2026-08-20' },
];

export const mockModelProfiles = [
  { id: 'mdl-001', name: 'Claude 3.5 Sonnet', type: 'LLM', provider: 'Anthropic', model: 'claude-sonnet-4-6', status: 'ACTIVE', baseUrl: 'https://api.anthropic.com', secret: '설정됨', updatedAt: '2026-09-01' },
  { id: 'mdl-002', name: 'Claude 3 Haiku', type: 'LLM', provider: 'Anthropic', model: 'claude-haiku-4-5-20251001', status: 'ACTIVE', baseUrl: 'https://api.anthropic.com', secret: '설정됨', updatedAt: '2026-08-28' },
  { id: 'mdl-003', name: 'Text Embedding Ada', type: 'Embedding', provider: 'OpenAI', model: 'text-embedding-3-small', status: 'ACTIVE', baseUrl: 'https://api.openai.com', secret: '설정됨', dimension: 1536, distanceMetric: 'cosine', activeForToolSearch: true, updatedAt: '2026-08-25' },
];

export const mockAuditLogs = [
  { id: 'aud-001', time: '2026-09-02 14:30:01', actor: 'admin', action: 'execution.start', resource: 'Execution EXE-20260902-00125', result: 'SUCCESS', requestId: 'req-abc123' },
  { id: 'aud-002', time: '2026-09-02 14:10:05', actor: 'jkim', action: 'execution.start', resource: 'Execution EXE-20260902-00124', result: 'SUCCESS', requestId: 'req-def456' },
  { id: 'aud-003', time: '2026-09-02 13:55:12', actor: 'admin', action: 'approval.request', resource: 'Approval apr-001', result: 'SUCCESS', requestId: 'req-ghi789' },
  { id: 'aud-004', time: '2026-09-02 11:00:00', actor: 'admin', action: 'agent.publish', resource: 'Agent agt-001 v2', result: 'SUCCESS', requestId: 'req-jkl012' },
  { id: 'aud-005', time: '2026-09-01 17:30:55', actor: 'mpark', action: 'execution.start', resource: 'Execution EXE-20260901-00121', result: 'FAILED', requestId: 'req-mno345' },
  { id: 'aud-006', time: '2026-09-01 16:00:00', actor: 'admin', action: 'mcp_server.register', resource: 'Server srv-002', result: 'SUCCESS', requestId: 'req-pqr678' },
];

export const mockJobs = [
  { id: 'job-001', type: 'Tool Discovery', resource: 'Internal Document MCP', status: 'SUCCEEDED', progress: null, started: '2026-09-02 10:00', duration: '3s', error: null },
  { id: 'job-002', type: 'Model Connection Test', resource: 'Claude 3.5 Sonnet', status: 'RUNNING', progress: null, started: '2026-09-02 14:35', duration: '–', error: null },
  { id: 'job-003', type: 'Tool Factory Build', resource: 'HR API Tools', status: 'FAILED', progress: null, started: '2026-09-01 09:00', duration: '12s', error: '스키마 변환 오류: 필수 응답 필드 누락' },
  { id: 'job-004', type: 'Agent Validation', resource: 'General Work Assistant v3', status: 'SUCCEEDED', progress: null, started: '2026-09-02 09:30', duration: '2s', error: null },
];

export const mockToolFactoryProjects = [
  { id: 'fac-001', project: 'HR API Tools', sourceType: 'OpenAPI', buildStatus: 'FAILED', testStatus: null, reviewStatus: null, publishStatus: null, updatedAt: '2026-09-01' },
  { id: 'fac-002', project: 'Finance Report Tools', sourceType: 'Python', buildStatus: 'SUCCEEDED', testStatus: 'SUCCEEDED', reviewStatus: 'PENDING', publishStatus: null, updatedAt: '2026-09-02' },
  { id: 'fac-003', project: 'Calendar Sync Tools', sourceType: 'OpenAPI', buildStatus: 'SUCCEEDED', testStatus: 'SUCCEEDED', reviewStatus: 'SUCCEEDED', publishStatus: 'SUCCEEDED', updatedAt: '2026-08-30' },
];

export const mockDiscoveryCandidates = [
  { id: 'disc-001', name: 'Slack MCP', registry: 'mcp.run', description: 'Slack 메시지 전송 및 채널 관리', repository: 'https://github.com/example/slack-mcp', transport: 'Streamable HTTP', version: 'v0.8.2', trustState: 'UNDER_REVIEW', warning: null },
  { id: 'disc-002', name: 'Notion MCP', registry: 'mcp.run', description: 'Notion 페이지 및 데이터베이스 관리', repository: 'https://github.com/example/notion-mcp', transport: 'Streamable HTTP', version: 'v1.2.0', trustState: 'CANDIDATE', warning: null },
  { id: 'disc-003', name: 'GitHub MCP', registry: 'mcp.run', description: 'GitHub 이슈, PR, 코드 관리', repository: 'https://github.com/modelcontextprotocol/github-mcp', transport: 'Streamable HTTP', version: 'v2.0.1', trustState: 'REVIEWED', warning: null },
  { id: 'disc-004', name: 'Unknown Tool Server', registry: 'registry.x', description: '출처 불명확한 서버', repository: null, transport: 'STDIO', version: 'v0.1.0', trustState: 'CANDIDATE', warning: '출처 레지스트리가 공식 목록에 없습니다.' },
];

export const mockApprovalPolicies = [
  { id: 'pol-001', name: 'Standard Email Approval', status: 'ACTIVE', decisionMode: 'ANY', requiredApprovals: 1, approverRoles: ['Approver'], expiryMinutes: 60, selfApproval: false, rejectCommentRequired: true },
  { id: 'pol-002', name: 'Destructive Action Approval', status: 'ACTIVE', decisionMode: 'ALL', requiredApprovals: 2, approverRoles: ['Admin', 'Senior Approver'], expiryMinutes: 30, selfApproval: false, rejectCommentRequired: true },
  { id: 'pol-003', name: 'Read-Only Self Approval', status: 'INACTIVE', decisionMode: 'ANY', requiredApprovals: 1, approverRoles: ['User'], expiryMinutes: 120, selfApproval: true, rejectCommentRequired: false },
];

// ─── Build Screen Extended Data ───────────────────────────────────────────────

export const mockAgentFull: Record<string, {
  description: string;
  purpose: string;
  visibility: string;
  createdAt: string;
  currentVersion: string | null;
  allowedToolIds: string[];
  instructions: string;
  versions: {
    version: string; status: string; changeSummary: string;
    validation: string | null; createdBy: string;
    createdAt: string; publishedAt: string | null;
    toolCount?: number; maxPlanSteps?: number; modelProfile?: string;
  }[];
}> = {
  'agt-001': {
    description: '범용 업무 자동화 및 정보 조회를 위한 Agent입니다.',
    purpose: '일반적인 업무 자동화, 문서 검색, 이메일 발송 등 다목적 업무를 처리합니다.',
    visibility: 'INTERNAL',
    createdAt: '2026-08-01',
    currentVersion: 'v3',
    allowedToolIds: ['tool-001', 'tool-002', 'tool-004', 'tool-005', 'tool-006', 'tool-008'],
    instructions: '당신은 MCPFlow 업무 자동화 Agent입니다.\n사용자의 자연어 요청을 분석하고 적절한 Tool을 선택하여 업무를 수행합니다.\n\n주요 규칙:\n- 외부 작업(이메일 발송, 파일 생성 등) 전 위험도를 사용자에게 안내하세요.\n- 문서 검색 결과는 요약하여 제공하세요.\n- 이메일 발송 전 항상 수신자와 내용을 확인하세요.\n- Secret이나 민감한 정보를 응답에 포함하지 마세요.',
    versions: [
      { version: 'v4', status: 'DRAFT', changeSummary: 'Tool 선택 임계값 조정', validation: null, createdBy: 'admin', createdAt: '2026-09-02', publishedAt: null },
      { version: 'v3', status: 'PUBLISHED', changeSummary: '보고서 생성 Tool 추가', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-31', publishedAt: '2026-09-01', toolCount: 6, maxPlanSteps: 10, modelProfile: 'Claude 3.5 Sonnet' },
      { version: 'v2', status: 'DEPRECATED', changeSummary: 'Instructions 업데이트', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-20', publishedAt: '2026-08-25' },
      { version: 'v1', status: 'DEPRECATED', changeSummary: '초기 버전', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-01', publishedAt: '2026-08-05' },
    ],
  },
  'agt-002': {
    description: '정기 보고서 자동 생성 및 배포를 위한 Agent입니다.',
    purpose: '주간/월간 업무 보고서를 자동으로 생성하고 관련 담당자에게 배포합니다.',
    visibility: 'INTERNAL',
    createdAt: '2026-08-20',
    currentVersion: 'v2',
    allowedToolIds: ['tool-002', 'tool-003', 'tool-004', 'tool-008'],
    instructions: '당신은 보고서 생성 전문 Agent입니다.\n데이터를 수집하고 분석하여 전문적인 보고서를 작성합니다.',
    versions: [
      { version: 'v3', status: 'DRAFT', changeSummary: 'PDF 생성 지원 추가', validation: 'WARNING', createdBy: 'jkim', createdAt: '2026-09-01', publishedAt: null },
      { version: 'v2', status: 'PUBLISHED', changeSummary: '다국어 보고서 지원', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-28', publishedAt: '2026-08-31', toolCount: 4, maxPlanSteps: 8, modelProfile: 'Claude 3.5 Sonnet' },
      { version: 'v1', status: 'DEPRECATED', changeSummary: '초기 버전', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-20', publishedAt: '2026-08-25' },
    ],
  },
  'agt-003': {
    description: '내부 문서 및 데이터를 검색하고 분석하는 Research Agent입니다.',
    purpose: '사용자의 조사 요청에 따라 내부 자료를 검색하고 인사이트를 도출합니다.',
    visibility: 'INTERNAL',
    createdAt: '2026-08-18',
    currentVersion: 'v1',
    allowedToolIds: ['tool-001', 'tool-002', 'tool-008'],
    instructions: '당신은 조사 및 분석 전문 Agent입니다.\n다양한 자료를 수집하고 종합적인 분석 결과를 제공합니다.',
    versions: [
      { version: 'v1', status: 'PUBLISHED', changeSummary: '초기 버전', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-18', publishedAt: '2026-08-20', toolCount: 3, maxPlanSteps: 6, modelProfile: 'Claude 3 Haiku' },
    ],
  },
  'agt-004': {
    description: '운영 업무 자동화를 위한 Agent입니다.',
    purpose: 'ERP 데이터 조회, 직원 정보 관리 등 운영 업무를 처리합니다.',
    visibility: 'INTERNAL',
    createdAt: '2026-08-10',
    currentVersion: null,
    allowedToolIds: ['tool-005', 'tool-006'],
    instructions: '당신은 운영 업무 지원 Agent입니다.\nERP 시스템과 연동하여 직원 정보 조회 및 일정 관리를 지원합니다.',
    versions: [
      { version: 'v1', status: 'DRAFT', changeSummary: '초기 작성', validation: null, createdBy: 'admin', createdAt: '2026-08-10', publishedAt: null },
    ],
  },
};

export const mockWorkflowFull: Record<string, {
  description: string;
  owner: string;
  currentVersion: string | null;
  toolCount: number;
  scheduleCount: number;
  createdAt: string;
  lastPublished: string | null;
  versions: {
    version: string; status: string; steps: number; changeSummary: string;
    validation: string | null; createdBy: string;
    createdAt: string; publishedAt: string | null;
  }[];
  tools: {
    toolId: string; toolName: string; serverName: string;
    version: string; riskClass: string; verification: string; step: string;
  }[];
  schedules: { id: string; name: string; schedule: string; timezone: string; nextRun: string; status: string }[];
}> = {
  'wf-001': {
    description: '주간 데이터를 조회하고 보고서를 생성하여 승인 후 전달합니다.',
    owner: 'admin',
    currentVersion: 'v5',
    toolCount: 4,
    scheduleCount: 1,
    createdAt: '2026-08-01',
    lastPublished: '2026-08-31',
    versions: [
      { version: 'v6', status: 'DRAFT', steps: 7, changeSummary: 'Approval 단계 추가', validation: null, createdBy: 'admin', createdAt: '2026-09-02', publishedAt: null },
      { version: 'v5', status: 'PUBLISHED', steps: 6, changeSummary: '병렬 처리 최적화', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-28', publishedAt: '2026-08-31' },
      { version: 'v4', status: 'DEPRECATED', steps: 5, changeSummary: 'PDF 변환 Step 추가', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-20', publishedAt: '2026-08-25' },
      { version: 'v3', status: 'DEPRECATED', steps: 4, changeSummary: '보고서 형식 개선', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-10', publishedAt: '2026-08-15' },
      { version: 'v2', status: 'DEPRECATED', steps: 4, changeSummary: '발송 자동화 추가', validation: 'VALID', createdBy: 'admin', createdAt: '2026-08-05', publishedAt: '2026-08-08' },
    ],
    tools: [
      { toolId: 'tool-002', toolName: 'Search Documents', serverName: 'Internal Document MCP', version: 'v2.1.0', riskClass: 'READ_ONLY', verification: 'VERIFIED', step: 'Get Data' },
      { toolId: 'tool-003', toolName: 'Generate Report', serverName: 'Report MCP', version: 'v1.0.3', riskClass: 'IDEMPOTENT_WRITE', verification: 'EXPIRED', step: 'Generate Report' },
      { toolId: 'tool-004', toolName: 'Send Email', serverName: 'Email MCP', version: 'v3.0.1', riskClass: 'NON_IDEMPOTENT_WRITE', verification: 'VERIFIED', step: 'Send Email' },
      { toolId: 'tool-005', toolName: 'Create Calendar Event', serverName: 'Calendar MCP', version: 'v1.4.0', riskClass: 'IDEMPOTENT_WRITE', verification: 'VERIFIED', step: 'Schedule Follow-up' },
    ],
    schedules: [{ id: 'sch-001', name: '주간 보고서 자동 실행', schedule: 'Mondays 09:00', timezone: 'Asia/Seoul', nextRun: '2026-09-09 09:00', status: 'ACTIVE' }],
  },
  'wf-002': {
    description: '업로드된 문서를 자동으로 검토하고 결재 흐름을 시작합니다.',
    owner: 'jkim',
    currentVersion: 'v3',
    toolCount: 3,
    scheduleCount: 0,
    createdAt: '2026-08-15',
    lastPublished: '2026-08-28',
    versions: [
      { version: 'v3', status: 'PUBLISHED', steps: 5, changeSummary: '검토 기준 업데이트', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-25', publishedAt: '2026-08-28' },
      { version: 'v2', status: 'DEPRECATED', steps: 4, changeSummary: '알림 추가', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-18', publishedAt: '2026-08-22' },
      { version: 'v1', status: 'DEPRECATED', steps: 3, changeSummary: '초기 버전', validation: 'VALID', createdBy: 'jkim', createdAt: '2026-08-15', publishedAt: '2026-08-18' },
    ],
    tools: [
      { toolId: 'tool-002', toolName: 'Search Documents', serverName: 'Internal Document MCP', version: 'v2.1.0', riskClass: 'READ_ONLY', verification: 'VERIFIED', step: 'Find Document' },
      { toolId: 'tool-004', toolName: 'Send Email', serverName: 'Email MCP', version: 'v3.0.1', riskClass: 'NON_IDEMPOTENT_WRITE', verification: 'VERIFIED', step: 'Notify Reviewer' },
      { toolId: 'tool-006', toolName: 'Lookup Employee', serverName: 'Legacy ERP MCP', version: 'v0.9.2', riskClass: 'READ_ONLY', verification: 'PENDING', step: 'Get Reviewer Info' },
    ],
    schedules: [],
  },
  'wf-003': {
    description: '결재 요청을 처리하고 승인된 경우 이메일을 발송합니다.',
    owner: 'admin',
    currentVersion: null,
    toolCount: 5,
    scheduleCount: 2,
    createdAt: '2026-09-01',
    lastPublished: null,
    versions: [
      { version: 'v1', status: 'DRAFT', steps: 6, changeSummary: '초기 작성', validation: null, createdBy: 'admin', createdAt: '2026-09-01', publishedAt: null },
    ],
    tools: [],
    schedules: [],
  },
};
