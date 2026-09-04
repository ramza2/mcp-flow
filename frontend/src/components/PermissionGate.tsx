import type { Permission } from '../domain';
import { hasPermission } from '../domain';

/**
 * Frontend PermissionGate is UX only.
 * Backend authorization remains authoritative.
 */
export default function PermissionGate({
  permission,
  children,
  fallback = null,
  mode = 'hide',
}: {
  permission: Permission;
  children: React.ReactNode;
  fallback?: React.ReactNode;
  /** hide = remove from DOM; disable = render but non-interactive */
  mode?: 'hide' | 'disable';
}) {
  const allowed = hasPermission(permission);
  if (allowed) return <>{children}</>;
  if (mode === 'disable') {
    return (
      <span className="inline-flex opacity-40 pointer-events-none" aria-disabled="true" title="권한이 없습니다 (UX only)">
        {children}
      </span>
    );
  }
  return <>{fallback}</>;
}
