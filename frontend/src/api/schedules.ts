/** Live Schedule registry contract — backend/app/api/v1/schedules.py. */
import { apiRequest } from './client';
import type { JsonValue, PageDto } from './types';
import type {
  OccurrenceStatus,
  ScheduleMisfirePolicy,
  ScheduleOverlapPolicy,
  ScheduleStatus,
  ScheduleTargetType,
} from '../domain';

export type ScheduleType = 'ONCE' | 'CRON' | 'INTERVAL';

export interface ScheduleDto {
  id: string;
  name: string;
  description: string | null;
  owner_id: string;
  target_type: ScheduleTargetType;
  target_id: string;
  schedule_type: ScheduleType;
  schedule_expression: string;
  timezone: string;
  inputs: Record<string, JsonValue>;
  misfire_policy: ScheduleMisfirePolicy;
  overlap_policy: ScheduleOverlapPolicy;
  max_catch_up: number;
  status: ScheduleStatus;
  next_run_at: string | null;
  last_run_at: string | null;
  start_at: string | null;
  end_at: string | null;
  created_at: string;
  updated_at: string;
  lock_version: number;
}

export type ScheduleCreateRequest = Pick<ScheduleDto,
  'name' | 'target_type' | 'target_id' | 'schedule_type' | 'schedule_expression' |
  'timezone' | 'inputs' | 'misfire_policy' | 'overlap_policy' | 'max_catch_up'
> & { description?: string | null };

export type ScheduleUpdateRequest = Partial<ScheduleCreateRequest>;

export interface ScheduleOccurrenceDto {
  id: string;
  schedule_id: string;
  scheduled_for: string;
  status: OccurrenceStatus;
  decision_reason: string | null;
  execution_id: string | null;
  created_at: string;
  enqueued_at: string | null;
  finished_at: string | null;
}

export function listSchedules(params: {
  page?: number;
  page_size?: number;
  q?: string;
  status?: ScheduleStatus;
  signal?: AbortSignal;
} = {}) {
  return apiRequest<PageDto<ScheduleDto>>('/schedules', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 20,
      q: params.q,
      status: params.status,
    },
    signal: params.signal,
  });
}

export function getSchedule(id: string, signal?: AbortSignal) {
  return apiRequest<ScheduleDto>(`/schedules/${id}`, { signal });
}

export function createSchedule(body: ScheduleCreateRequest) {
  return apiRequest<ScheduleDto>('/schedules', { method: 'POST', body });
}

export function updateSchedule(id: string, body: ScheduleUpdateRequest, lockVersion: number) {
  return apiRequest<ScheduleDto>(`/schedules/${id}`, {
    method: 'PATCH',
    body: { ...body, lock_version: lockVersion },
    headers: { 'If-Match': String(lockVersion) },
  });
}

export function changeScheduleStatus(id: string, action: 'activate' | 'pause' | 'resume') {
  return apiRequest<ScheduleDto>(`/schedules/${id}/${action}`, { method: 'POST' });
}

export function listScheduleOccurrences(id: string, params: {
  page?: number;
  page_size?: number;
  signal?: AbortSignal;
} = {}) {
  return apiRequest<PageDto<ScheduleOccurrenceDto>>(`/schedules/${id}/occurrences`, {
    query: { page: params.page ?? 1, page_size: params.page_size ?? 20 },
    signal: params.signal,
  });
}
