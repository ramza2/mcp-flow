/** Admin RBAC / ResourceGrant API helpers (docs/06 §6). */

import { apiRequest } from './client';
import type { PageDto } from './types';
import type { UserStatus } from './authTypes';

export interface AdminUser {
  id: string;
  username: string;
  display_name: string;
  email: string;
  status: UserStatus;
  last_login_at: string | null;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export interface AdminRole {
  id: string;
  code: string;
  name: string;
  description: string | null;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export interface AdminPermission {
  id: string;
  code: string;
  name: string;
  description: string | null;
  created_at: string;
}

export type ResourceType = 'AGENT' | 'WORKFLOW' | 'MCP_SERVER' | 'MCP_TOOL';

export interface ResourceGrant {
  id: string;
  user_id: string | null;
  role_id: string | null;
  resource_type: ResourceType;
  resource_id: string;
  created_at: string;
}

type PageOptions = { page?: number; page_size?: number; signal?: AbortSignal };

function listPage<T>(path: string, options: PageOptions = {}) {
  return apiRequest<PageDto<T>>(path, {
    query: { page: options.page ?? 1, page_size: options.page_size ?? 20 },
    signal: options.signal,
  });
}

/**
 * Reference-data pickers must not silently omit later pages.
 * Stop at the server's has_next boundary and preserve cancellation.
 */
export async function listAllPages<T>(path: string, signal?: AbortSignal): Promise<T[]> {
  const items: T[] = [];
  for (let page = 1; ; page += 1) {
    const response = await listPage<T>(path, { page, page_size: 100, signal });
    items.push(...response.items);
    if (!response.has_next) return items;
  }
}

export const listUsers = (options: PageOptions = {}) => listPage<AdminUser>('/users', options);
export const getUser = (id: string, signal?: AbortSignal) =>
  apiRequest<AdminUser>(`/users/${id}`, { signal });
export const listUserRoles = (id: string, signal?: AbortSignal) =>
  apiRequest<AdminRole[]>(`/users/${id}/roles`, { signal });
export const listRoles = (signal?: AbortSignal) =>
  listAllPages<AdminRole>('/roles', signal);
export const listPermissions = (signal?: AbortSignal) =>
  listAllPages<AdminPermission>('/permissions', signal);
export const listRolePermissions = (id: string, signal?: AbortSignal) =>
  apiRequest<AdminPermission[]>(`/roles/${id}/permissions`, { signal });

export function replaceUserRoles(id: string, roleIds: string[], lockVersion: number) {
  return apiRequest<AdminRole[]>(`/users/${id}/roles`, {
    method: 'PUT',
    headers: { 'If-Match': String(lockVersion) },
    body: { role_ids: roleIds },
  });
}

export function replaceRolePermissions(id: string, permissionIds: string[], lockVersion: number) {
  return apiRequest<AdminPermission[]>(`/roles/${id}/permissions`, {
    method: 'PUT',
    headers: { 'If-Match': String(lockVersion) },
    body: { permission_ids: permissionIds },
  });
}

export const getRole = (id: string, signal?: AbortSignal) =>
  apiRequest<AdminRole>(`/roles/${id}`, { signal });

export type GrantOwner = 'user' | 'role';

function grantPath(owner: GrantOwner, ownerId: string) {
  return `/${owner === 'user' ? 'users' : 'roles'}/${ownerId}/resource-grants`;
}

export function listResourceGrants(owner: GrantOwner, ownerId: string, signal?: AbortSignal) {
  return listAllPages<ResourceGrant>(grantPath(owner, ownerId), signal);
}

export function createResourceGrant(owner: GrantOwner, ownerId: string, resourceType: ResourceType, resourceId: string) {
  return apiRequest<ResourceGrant>(grantPath(owner, ownerId), {
    method: 'POST',
    body: { resource_type: resourceType, resource_id: resourceId },
  });
}

export function deleteResourceGrant(owner: GrantOwner, ownerId: string, grantId: string) {
  return apiRequest<void>(`${grantPath(owner, ownerId)}/${grantId}`, { method: 'DELETE' });
}
