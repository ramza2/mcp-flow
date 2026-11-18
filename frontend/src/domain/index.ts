export * from './types';
export * from './labels';

/**
 * Formal permission codes — aligned with backend BOOTSTRAP_PERMISSION_CODES
 * (docs/05–06). Frontend PermissionGate is UX only; Backend remains authoritative.
 *
 * Do not invent UX-only codes such as agent.view / admin.view / mcp.edit.
 */
export const FORMAL_PERMISSION_CODES = [
  'mcp.server.read',
  'mcp.server.manage',
  'mcp.tool.read',
  'mcp.tool.execute',
  'agent.read',
  'agent.manage',
  'workflow.execute',
  'execution.read',
  'execution.cancel',
  'approval.decide',
  'approval.policy.manage',
  'audit.read',
  'audit.export',
  'schedule.manage',
] as const;

export type Permission = (typeof FORMAL_PERMISSION_CODES)[number];

/** Pure checker — pass the session's effective codes (no mock defaults). */
export function hasPermission(
  permission: Permission,
  granted: readonly string[],
): boolean {
  return granted.includes(permission);
}
