import { afterEach, describe, expect, it, vi } from 'vitest';
import { setCachedCsrfTokenForTests } from '../csrf';
import { changeScheduleStatus, createSchedule, getSchedule, listScheduleOccurrences, listSchedules, updateSchedule } from '../schedules';

function ok(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

const targetId = '11111111-1111-4111-8111-111111111111';
const scheduleId = '22222222-2222-4222-8222-222222222222';

describe('Schedule registry API', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setCachedCsrfTokenForTests(null);
  });

  it('lists filtered, paginated live schedules', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ items: [], page: 2, page_size: 20, total: 0, has_next: false }));
    vi.stubGlobal('fetch', fetchMock);
    await listSchedules({ page: 2, q: 'night', status: 'ACTIVE' });
    const url = String(fetchMock.mock.calls[0][0]);
    expect(url).toContain('/api/v1/schedules?');
    expect(url).toContain('page=2');
    expect(url).toContain('q=night');
    expect(url).toContain('status=ACTIVE');
  });

  it('creates a PAUSED-by-default Schedule with an explicit Version ID', async () => {
    const fetchMock = vi.fn().mockResolvedValue(ok({ id: scheduleId }, 201));
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');
    await createSchedule({
      name: 'Agent once', target_type: 'AGENT_VERSION', target_id: targetId,
      schedule_type: 'ONCE', schedule_expression: '2026-12-10T09:00:00', timezone: 'Asia/Seoul',
      inputs: { request_text: 'list pets' }, overlap_policy: 'SKIP', misfire_policy: 'SKIP', max_catch_up: 1,
    });
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/schedules');
    expect(init.method).toBe('POST');
    const body = JSON.parse(String(init.body));
    expect(body.target_id).toBe(targetId);
    expect(body.inputs).toEqual({ request_text: 'list pets' });
    expect(body).not.toHaveProperty('status');
  });

  it('PATCH carries optimistic lock_version and lifecycle actions use canonical endpoints', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => ok({ id: scheduleId }));
    vi.stubGlobal('fetch', fetchMock);
    setCachedCsrfTokenForTests('csrf-test');
    await updateSchedule(scheduleId, { name: 'Changed' }, 7);
    await changeScheduleStatus(scheduleId, 'pause');
    await getSchedule(scheduleId);
    await listScheduleOccurrences(scheduleId);
    const [patchUrl, patchInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(patchUrl).toBe(`/api/v1/schedules/${scheduleId}`);
    expect(patchInit.method).toBe('PATCH');
    expect((patchInit.headers as Record<string, string>)['If-Match']).toBe('7');
    expect(JSON.parse(String(patchInit.body))).toEqual({ name: 'Changed', lock_version: 7 });
    expect(fetchMock.mock.calls[1][0]).toBe(`/api/v1/schedules/${scheduleId}/pause`);
    expect(fetchMock.mock.calls[2][0]).toBe(`/api/v1/schedules/${scheduleId}`);
    expect(fetchMock.mock.calls[3][0]).toContain(`/api/v1/schedules/${scheduleId}/occurrences?`);
  });
});
