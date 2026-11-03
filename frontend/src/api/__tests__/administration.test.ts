import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import {
  createResourceGrant,
  deleteResourceGrant,
  listPermissions,
  listResourceGrants,
  replaceRolePermissions,
  replaceUserRoles,
} from '../administration';

function response(data: unknown, status = 200) {
  return new Response(status === 204 ? null : JSON.stringify(data), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('admin permission + exact ResourceGrant API', () => {
  beforeEach(() => setCachedCsrfTokenForTests('test-csrf'));
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('loads every permission page instead of only the first 20 entries', async () => {
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      const page = new URL(url, 'http://localhost').searchParams.get('page');
      return Promise.resolve(response({
        items: [{ id: page, code: `p-${page}` }],
        has_next: page === '1',
      }));
    });
    vi.stubGlobal('fetch', fetchMock);
    const items = await listPermissions();
    expect(items.map(item => item.code)).toEqual(['p-1', 'p-2']);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/permissions?page=1&page_size=100');
  });

  it('sends version-guarded role and user permission updates', async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(response([])));
    vi.stubGlobal('fetch', fetchMock);
    await replaceRolePermissions('role-a', ['permission-a'], 5);
    await replaceUserRoles('user-a', ['role-a'], 3);
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/roles/role-a/permissions');
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'PUT',
      headers: expect.objectContaining({ 'If-Match': '5', 'X-CSRF-Token': 'test-csrf' }),
      body: JSON.stringify({ permission_ids: ['permission-a'] }),
    }));
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/users/user-a/roles');
    expect(fetchMock.mock.calls[1][1]).toEqual(expect.objectContaining({
      method: 'PUT',
      headers: expect.objectContaining({ 'If-Match': '3' }),
      body: JSON.stringify({ role_ids: ['role-a'] }),
    }));
  });

  it('creates and removes an exact MCP_TOOL grant without broadening scope', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({ id: 'grant-1' }, 201))
      .mockResolvedValueOnce(response(null, 204));
    vi.stubGlobal('fetch', fetchMock);
    await createResourceGrant('user', 'user-a', 'MCP_TOOL', 'tool-a');
    await deleteResourceGrant('user', 'user-a', 'grant-1');
    expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/users/user-a/resource-grants');
    expect(fetchMock.mock.calls[0][1]).toEqual(expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ resource_type: 'MCP_TOOL', resource_id: 'tool-a' }),
    }));
    expect(fetchMock.mock.calls[1][0]).toBe('/api/v1/users/user-a/resource-grants/grant-1');
    expect(fetchMock.mock.calls[1][1].method).toBe('DELETE');
  });

  it('reads role-specific grants through the documented endpoint', async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ items: [], has_next: false }));
    vi.stubGlobal('fetch', fetchMock);
    await listResourceGrants('role', 'role-a');
    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/roles/role-a/resource-grants?page=1&page_size=100');
  });
});
