import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import {
  activateAgent,
  createAgent,
  createAgentVersion,
  deleteAgent,
  listAgents,
  publishAgentVersion,
  putAgentToolGrants,
  validateAgentVersion,
} from '../agents';

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('Agent registry API contracts', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('listAgents builds query path and params', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({ items: [], page: 1, page_size: 20, total: 0, has_next: false }),
    );
    vi.stubGlobal('fetch', fetchMock);

    await listAgents({
      page: 2,
      page_size: 20,
      q: 'ops',
      status: 'ACTIVE',
      sort: '-updated_at',
    });

    expect(fetchMock).toHaveBeenCalled();
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/agents');
    expect(url).toContain('page=2');
    expect(url).toContain('q=ops');
    expect(url).toContain('status=ACTIVE');
  });

  it('createAgent posts name/visibility without owner_id', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: 'agt-1',
        code: 'new-agent',
        name: 'New Agent',
        description: null,
        status: 'DRAFT',
        current_version_id: null,
        owner_id: 'user-1',
        visibility: 'PRIVATE',
        created_at: '2026-10-23T00:00:00Z',
        created_by: 'user-1',
        updated_at: '2026-10-23T00:00:00Z',
        updated_by: 'user-1',
        lock_version: 1,
      }, 201),
    );
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await createAgent({ name: 'New Agent', visibility: 'PRIVATE' });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body));
    expect(body).toEqual({ name: 'New Agent', visibility: 'PRIVATE' });
    expect(body).not.toHaveProperty('owner_id');
  });

  it('validate/publish/activate follow lifecycle paths', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        id: 'ver-1',
        agent_id: 'agt-1',
        version_no: 1,
        status: 'DRAFT',
        validation_status: 'VALID',
        validation_report: { valid: true, errors: [] },
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'ver-1',
        agent_id: 'agt-1',
        version_no: 1,
        status: 'PUBLISHED',
        system_instruction: 'x',
        llm_profile_id: 'p1',
        request_schema_version: '1.0',
        plan_schema_version: '1.0',
        selection_settings: {},
        planning_settings: {},
        response_settings: {},
        validation_status: 'VALID',
        validation_report: null,
        content_hash: 'a'.repeat(64),
        change_summary: null,
        published_at: '2026-10-23T00:00:00Z',
        published_by: null,
        deprecated_at: null,
        deprecated_by: null,
        created_at: '2026-10-23T00:00:00Z',
        created_by: null,
      }))
      .mockResolvedValueOnce(jsonResponse({
        id: 'agt-1',
        code: 'a',
        name: 'A',
        description: null,
        status: 'ACTIVE',
        current_version_id: 'ver-1',
        owner_id: 'user-1',
        visibility: 'PRIVATE',
        created_at: '2026-10-23T00:00:00Z',
        created_by: 'user-1',
        updated_at: '2026-10-23T00:00:00Z',
        updated_by: 'user-1',
        lock_version: 2,
      }));
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await validateAgentVersion('agt-1', 'ver-1');
    await publishAgentVersion('agt-1', 'ver-1');
    await activateAgent('agt-1', 1);

    expect(String(fetchMock.mock.calls[0][0])).toContain('/agents/agt-1/versions/ver-1/validate');
    expect(String(fetchMock.mock.calls[1][0])).toContain('/agents/agt-1/versions/ver-1/publish');
    expect(String(fetchMock.mock.calls[2][0])).toContain('/agents/agt-1');
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe('PATCH');
  });

  it('createAgentVersion posts canonical version body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        id: 'ver-1',
        agent_id: 'agt-1',
        version_no: 1,
        status: 'DRAFT',
        system_instruction: 'plan safely',
        llm_profile_id: 'p1',
        request_schema_version: '1.0',
        plan_schema_version: '1.0',
        selection_settings: {
          auto_select_threshold: 0.82,
          confirmation_threshold: 0.6,
          max_candidates: 5,
        },
        planning_settings: {},
        response_settings: {},
        validation_status: 'INVALID',
        validation_report: null,
        content_hash: 'b'.repeat(64),
        change_summary: 'initial',
        published_at: null,
        published_by: null,
        deprecated_at: null,
        deprecated_by: null,
        created_at: '2026-10-23T00:00:00Z',
        created_by: null,
      }, 201),
    );
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await createAgentVersion('agt-1', {
      system_instruction: 'plan safely',
      llm_profile_id: 'p1',
      selection_settings: {
        auto_select_threshold: 0.82,
        confirmation_threshold: 0.6,
        max_candidates: 5,
      },
      change_summary: 'initial',
    });

    const body = JSON.parse(String((fetchMock.mock.calls[0][1] as RequestInit).body));
    expect(body.llm_profile_id).toBe('p1');
    expect(body.selection_settings.max_candidates).toBe(5);
  });

  it('deleteAgent issues DELETE without body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await deleteAgent('agt-1');

    expect(String(fetchMock.mock.calls[0][0])).toContain('/api/v1/agents/agt-1');
    expect((fetchMock.mock.calls[0][1] as RequestInit).method).toBe('DELETE');
  });

  it('putAgentToolGrants keeps parameter_constraints null in the request body', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ items: [] }));
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');

    await putAgentToolGrants('agt-1', 'ver-1', {
      items: [{
        mcp_tool_id: 'tool-1',
        effect: 'ALLOW',
        parameter_constraints: null,
        requires_confirmation: false,
      }],
    });

    const raw = String((fetchMock.mock.calls[0][1] as RequestInit).body);
    expect(raw).toContain('"parameter_constraints":null');
    const body = JSON.parse(raw) as {
      items: Array<{ parameter_constraints: unknown }>;
    };
    expect(body.items).toHaveLength(1);
    expect(body.items[0].parameter_constraints).toBeNull();
    expect(body.items[0]).not.toEqual(
      expect.objectContaining({ parameter_constraints: {} }),
    );
  });
});
