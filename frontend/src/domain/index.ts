export * from './types';
export * from './labels';

/**
 * Frontend PermissionGate is UX only.
 * Backend authorization remains authoritative.
 */
export type Permission =
  | 'agent.view'
  | 'agent.edit'
  | 'agent.publish'
  | 'workflow.view'
  | 'workflow.edit'
  | 'workflow.publish'
  | 'execution.view'
  | 'execution.cancel'
  | 'execution.retry'
  | 'approval.view'
  | 'approval.approve'
  | 'schedule.view'
  | 'schedule.edit'
  | 'mcp.view'
  | 'mcp.edit'
  | 'admin.view'
  | 'admin.manage';

/** Mock current user permissions (UX helper only). */
export const MOCK_CURRENT_PERMISSIONS: Permission[] = [
  'agent.view',
  'agent.edit',
  'agent.publish',
  'workflow.view',
  'workflow.edit',
  'workflow.publish',
  'execution.view',
  'execution.cancel',
  'execution.retry',
  'approval.view',
  'approval.approve',
  'schedule.view',
  'schedule.edit',
  'mcp.view',
  'mcp.edit',
  'admin.view',
  'admin.manage',
];

export function hasPermission(permission: Permission, granted: Permission[] = MOCK_CURRENT_PERMISSIONS): boolean {
  return granted.includes(permission);
}
