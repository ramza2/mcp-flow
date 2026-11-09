import { useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import { listAgents, listAgentVersions } from '../../api/agents';
import { isAbortError, isApiError } from '../../api/client';
import type { JsonValue } from '../../api/types';
import {
  changeScheduleStatus,
  createSchedule,
  getSchedule,
  listScheduleOccurrences,
  updateSchedule,
  type ScheduleDto,
  type ScheduleOccurrenceDto,
  type ScheduleType,
  type ScheduleUpdateRequest,
} from '../../api/schedules';
import { listWorkflowTargets, listWorkflowTargetVersions } from '../../api/workflows';
import { SCHEDULE_MISFIRE_POLICIES, SCHEDULE_OVERLAP_POLICIES, SCHEDULE_TARGET_TYPES, formatScheduleTimestamp, labelScheduleTarget, type ScheduleMisfirePolicy, type ScheduleOverlapPolicy, type ScheduleTargetType } from '../../domain';

type TargetOption = { id: string; name: string };
type VersionOption = { id: string; version_no: number };
type IntervalUnit = 'MINUTES' | 'HOURS' | 'DAYS';
type CronPreset = 'DAILY' | 'WEEKDAYS' | 'WEEKLY' | 'ADVANCED';

function parseBasicInterval(value: string): { count: string; unit: IntervalUnit } | null {
  const match = /^(?:P(\d+)D|PT(\d+)([HM]))$/.exec(value);
  if (!match) return null;
  const count = match[1] ?? match[2];
  if (!/^[1-9]\d*$/.test(count) || !Number.isSafeInteger(Number(count))) return null;
  return { count, unit: match[1] ? 'DAYS' : match[3] === 'H' ? 'HOURS' : 'MINUTES' };
}

function buildInterval(count: string, unit: IntervalUnit): string {
  if (!/^[1-9]\d*$/.test(count) || !Number.isSafeInteger(Number(count))) return '';
  return unit === 'DAYS' ? `P${count}D` : `PT${count}${unit === 'HOURS' ? 'H' : 'M'}`;
}

function parseCronPreset(value: string): { preset: CronPreset; time: string; weekday: string } | null {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|1-5|[0-6])$/.exec(value);
  if (!match || Number(match[1]) > 59 || Number(match[2]) > 23) return null;
  const preset = match[3] === '*' ? 'DAILY' : match[3] === '1-5' ? 'WEEKDAYS' : 'WEEKLY';
  return { preset, time: `${match[2].padStart(2, '0')}:${match[1].padStart(2, '0')}`, weekday: preset === 'WEEKLY' ? match[3] : '1' };
}

function buildCron(preset: CronPreset, time: string, weekday: string): string {
  if (!/^\d{2}:\d{2}$/.test(time)) return '';
  const [hour, minute] = time.split(':').map(Number);
  if (hour > 23 || minute > 59) return '';
  return `${minute} ${hour} * * ${preset === 'DAILY' ? '*' : preset === 'WEEKDAYS' ? '1-5' : weekday}`;
}

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white disabled:bg-slate-100 disabled:text-slate-500';

function errorText(err: unknown): string {
  if (isApiError(err)) return `${err.message}${err.requestId ? ` (Request ID: ${err.requestId})` : ''}`;
  return err instanceof Error ? err.message : '요청을 처리하지 못했습니다.';
}

export default function ScheduleEdit() {
  const navigate = useNavigate();
  const { id } = useParams();
  const isNew = !id || id === 'new';
  const [existing, setExisting] = useState<ScheduleDto | null>(null);
  const [loading, setLoading] = useState(!isNew);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [refresh, setRefresh] = useState(0);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [targetType, setTargetType] = useState<ScheduleTargetType>('AGENT_VERSION');
  const [logicalTargetId, setLogicalTargetId] = useState('');
  const [versionId, setVersionId] = useState('');
  const [targets, setTargets] = useState<TargetOption[]>([]);
  const [versions, setVersions] = useState<VersionOption[]>([]);
  const [targetError, setTargetError] = useState<string | null>(null);
  const [targetLoading, setTargetLoading] = useState(false);
  const [versionLoading, setVersionLoading] = useState(false);

  const [requestText, setRequestText] = useState('');
  const [inputJson, setInputJson] = useState('{}');
  const [scheduleType, setScheduleType] = useState<ScheduleType>('ONCE');
  const [oneTimeAt, setOneTimeAt] = useState('');
  const [cron, setCron] = useState('0 9 * * *');
  const [cronPreset, setCronPreset] = useState<CronPreset>('DAILY');
  const [cronTime, setCronTime] = useState('09:00');
  const [cronWeekday, setCronWeekday] = useState('1');
  const [interval, setInterval] = useState('PT1H');
  const [intervalAdvanced, setIntervalAdvanced] = useState(false);
  const [intervalCount, setIntervalCount] = useState('1');
  const [intervalUnit, setIntervalUnit] = useState<IntervalUnit>('HOURS');
  const [timezone, setTimezone] = useState('Asia/Seoul');
  const [overlap, setOverlap] = useState<ScheduleOverlapPolicy>('SKIP');
  const [misfire, setMisfire] = useState<ScheduleMisfirePolicy>('SKIP');
  const [maxCatchUp, setMaxCatchUp] = useState(1);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [occurrences, setOccurrences] = useState<ScheduleOccurrenceDto[]>([]);
  const [occurrenceError, setOccurrenceError] = useState<string | null>(null);

  useEffect(() => {
    if (isNew || !id) return;
    const controller = new AbortController();
    setLoading(true);
    setLoadError(null);
    getSchedule(id, controller.signal)
      .then(row => {
        if (controller.signal.aborted) return;
        setExisting(row);
        setName(row.name);
        setDescription(row.description ?? '');
        setTargetType(row.target_type);
        setLogicalTargetId('');
        setVersionId('');
        setRequestText(typeof row.inputs.request_text === 'string' ? row.inputs.request_text : '');
        setInputJson(JSON.stringify(row.inputs, null, 2));
        setScheduleType(row.schedule_type);
        setOneTimeAt(row.schedule_type === 'ONCE' ? row.schedule_expression.slice(0, 16) : '');
        if (row.schedule_type === 'CRON') {
          setCron(row.schedule_expression);
          const parsed = parseCronPreset(row.schedule_expression);
          setCronPreset(parsed?.preset ?? 'ADVANCED');
          if (parsed) { setCronTime(parsed.time); setCronWeekday(parsed.weekday); }
        }
        if (row.schedule_type === 'INTERVAL') {
          setInterval(row.schedule_expression);
          const parsed = parseBasicInterval(row.schedule_expression);
          setIntervalAdvanced(parsed === null);
          if (parsed) { setIntervalCount(parsed.count); setIntervalUnit(parsed.unit); }
        }
        setTimezone(row.timezone);
        setOverlap(row.overlap_policy);
        setMisfire(row.misfire_policy);
        setMaxCatchUp(row.max_catch_up);
      })
      .catch(err => { if (!controller.signal.aborted && !isAbortError(err)) setLoadError(errorText(err)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [id, isNew, refresh]);

  useEffect(() => {
    const controller = new AbortController();
    setTargets([]);
    setTargetError(null);
    setTargetLoading(true);
    const task = targetType === 'AGENT_VERSION' ? listAgents({ page_size: 100, signal: controller.signal }) : listWorkflowTargets(controller.signal);
    task.then(data => {
      if (controller.signal.aborted) return;
      setTargets(data.items.filter(row => row.status === 'ACTIVE' && row.current_version_id).map(row => ({ id: row.id, name: row.name })));
      if (data.has_next) setTargetError('첫 100개 대상만 표시됩니다. 대상이 더 많으면 별도 검색 기능을 이용해야 합니다.');
    }).catch(err => {
      if (!controller.signal.aborted && !isAbortError(err)) setTargetError(`대상 목록 조회 실패: ${errorText(err)}`);
    }).finally(() => { if (!controller.signal.aborted) setTargetLoading(false); });
    return () => controller.abort();
  }, [targetType]);

  useEffect(() => {
    setVersions([]);
    if (!logicalTargetId) return;
    const controller = new AbortController();
    setVersionLoading(true);
    setTargetError(null);
    const task = targetType === 'AGENT_VERSION'
      ? listAgentVersions(logicalTargetId, { page_size: 100, signal: controller.signal })
      : listWorkflowTargetVersions(logicalTargetId, controller.signal);
    task.then(data => {
      if (controller.signal.aborted) return;
      setVersions(data.items.filter(row => row.status === 'PUBLISHED' && row.validation_status === 'VALID')
        .map(row => ({ id: row.id, version_no: row.version_no })));
      if (data.has_next) setTargetError('버전은 최근 100개까지만 표시됩니다.');
    }).catch(err => {
      if (!controller.signal.aborted && !isAbortError(err)) setTargetError(`버전 조회 실패: ${errorText(err)}`);
    }).finally(() => { if (!controller.signal.aborted) setVersionLoading(false); });
    return () => controller.abort();
  }, [logicalTargetId, targetType]);

  useEffect(() => {
    if (!id || isNew) return;
    const controller = new AbortController();
    listScheduleOccurrences(id, { page_size: 20, signal: controller.signal })
      .then(data => { if (!controller.signal.aborted) setOccurrences(data.items); })
      .catch(err => { if (!controller.signal.aborted && !isAbortError(err)) setOccurrenceError(errorText(err)); });
    return () => controller.abort();
  }, [id, isNew, refresh]);

  const configLocked = existing !== null && existing.status !== 'PAUSED';
  const readOnly = loading || saving;
  const pinnedTargetId = logicalTargetId
    ? versionId
    : existing?.target_type === targetType ? existing.target_id : '';

  const handleSave = async () => {
    setFormError(null);
    setNotice(null);
    if (!name.trim()) { setFormError('예약 이름을 입력해 주세요.'); return; }
    if (existing && configLocked) {
      setSaving(true);
      try {
        await updateSchedule(existing.id, { name: name.trim(), description: description.trim() || null }, existing.lock_version);
        navigate('/schedules');
      } catch (err) { setFormError(errorText(err)); } finally { setSaving(false); }
      return;
    }
    if (!pinnedTargetId) { setFormError('게시·검증이 완료된 대상 버전을 선택해 주세요.'); return; }
    if (!timezone.trim()) { setFormError('IANA 시간대가 필요합니다. 예: Asia/Seoul'); return; }
    const expression = scheduleType === 'ONCE'
      ? (existing?.schedule_type === 'ONCE' && existing.schedule_expression.slice(0, 16) === oneTimeAt
        ? existing.schedule_expression : `${oneTimeAt}:00`)
      : scheduleType === 'CRON' ? cron.trim() : interval.trim();
    if (!expression.trim() || (scheduleType === 'ONCE' && oneTimeAt.length !== 16)) {
      setFormError('실행 시각 또는 반복 표현식을 입력해 주세요.'); return;
    }
    if (scheduleType === 'INTERVAL' && !intervalAdvanced && !buildInterval(intervalCount, intervalUnit)) {
      setFormError('반복 간격은 1 이상의 정수로 입력해 주세요.'); return;
    }
    if (!Number.isInteger(maxCatchUp) || maxCatchUp < 1 || maxCatchUp > 100) {
      setFormError('최대 보충 실행 횟수는 1~100 사이 정수로 지정해 주세요.'); return;
    }
    let inputs: Record<string, JsonValue>;
    if (targetType === 'AGENT_VERSION') {
      if (!requestText.trim() || requestText.trim().length > 4000) {
        setFormError('Agent 업무 지시는 1~4000자로 입력해 주세요.'); return;
      }
      inputs = { request_text: requestText.trim() };
    } else {
      try {
        const parsed: unknown = JSON.parse(inputJson);
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('JSON object required');
        inputs = parsed as Record<string, JsonValue>;
      } catch {
        setFormError('Workflow 입력은 JSON 객체 형식이어야 합니다.'); return;
      }
    }
    const payload = {
      name: name.trim(), description: description.trim() || null,
      target_type: targetType, target_id: pinnedTargetId,
      schedule_type: scheduleType, schedule_expression: expression,
      timezone: timezone.trim(), inputs,
      overlap_policy: overlap, misfire_policy: misfire, max_catch_up: maxCatchUp,
    };
    setSaving(true);
    try {
      if (existing) {
        // PATCH only changed fields: a metadata-only edit must not reset a PAUSED
        // Schedule's preserved next_run_at / misfire recovery cursor.
        const changed: ScheduleUpdateRequest = {};
        for (const key of Object.keys(payload) as (keyof typeof payload)[]) {
          if (JSON.stringify(payload[key]) !== JSON.stringify(existing[key])) {
            (changed as Record<string, unknown>)[key] = payload[key];
          }
        }
        if (Object.keys(changed).length) {
          await updateSchedule(existing.id, changed, existing.lock_version);
        }
      } else await createSchedule(payload);
      navigate('/schedules');
    } catch (err) {
      setFormError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  const handleStatus = async () => {
    if (!existing || (existing.status !== 'ACTIVE' && existing.status !== 'PAUSED')) return;
    setSaving(true);
    setFormError(null);
    try {
      const action = existing.status === 'ACTIVE' ? 'pause'
        : existing.last_run_at || existing.next_run_at ? 'resume' : 'activate';
      await changeScheduleStatus(existing.id, action);
      setNotice('예약 상태가 변경되었습니다.');
      setRefresh(n => n + 1);
    } catch (err) { setFormError(errorText(err)); } finally { setSaving(false); }
  };

  if (loadError) return <div className="p-6"><ErrorState message={loadError} onRetry={() => setRefresh(n => n + 1)} /></div>;
  if (loading) return <div className="p-6"><LoadingSkeleton rows={7} /></div>;

  return (
    <div>
      <PageHeader
        breadcrumbs={[{ label: '예약 실행', to: '/schedules' }, { label: isNew ? '새 예약' : existing?.name ?? '' }]}
        title={isNew ? '새 예약 등록' : '예약 설정'}
        actions={<>
          <Button variant="outline" onClick={() => navigate('/schedules')}>목록</Button>
          {existing && ['ACTIVE', 'PAUSED'].includes(existing.status) && (
            <Button variant="outline" disabled={readOnly} onClick={() => void handleStatus()}>
              {existing.status === 'ACTIVE' ? '일시정지' : existing.last_run_at || existing.next_run_at ? '재개' : '활성화'}
            </Button>
          )}
          <Button loading={saving} onClick={() => void handleSave()}>저장</Button>
        </>}
      />
      <div className="p-6 max-w-3xl space-y-5">
        {existing && <InlineAlert type="info" message={`현재 상태: ${existing.status}. ${configLocked ? '설정 변경은 일시정지 상태에서만 가능하며, 완료·오류 예약은 이름과 설명만 수정할 수 있습니다.' : '설정을 저장한 후 활성화해야 실제 실행됩니다.'}`} />}
        {isNew && <InlineAlert type="info" message="새 예약은 일시정지(PAUSED) 상태로 저장됩니다. 안전하게 확인한 뒤 목록에서 활성화해 주세요. 대상은 선택한 Version ID로 고정됩니다." />}
        {notice && <InlineAlert type="info" message={notice} />}
        {formError && <InlineAlert type="error" message={formError} />}
        <Section title="기본 정보">
          <Field label="이름"><input aria-label="이름" value={name} onChange={event => setName(event.target.value)} maxLength={255} disabled={readOnly} className={inputClass} /></Field>
          <Field label="설명"><input aria-label="설명" value={description} onChange={event => setDescription(event.target.value)} disabled={readOnly} className={inputClass} /></Field>
          <Field label="예약 대상 유형">
            <div className="flex gap-4">
              {SCHEDULE_TARGET_TYPES.map(type => <label key={type} className="flex items-center gap-2 text-sm">
                <input type="radio" checked={targetType === type} disabled={configLocked || readOnly} onChange={() => {
                  setTargetType(type); setLogicalTargetId(''); setVersionId('');
                }} />{labelScheduleTarget(type)}
              </label>)}
            </div>
          </Field>
          {existing && <div className="text-xs text-slate-600">현재 고정된 Version ID: <span className="font-mono break-all">{existing.target_id}</span></div>}
          <Field label="대상 선택">
            <select aria-label="대상 선택" value={logicalTargetId} disabled={configLocked || targetLoading || readOnly} onChange={event => { setLogicalTargetId(event.target.value); setVersionId(''); }} className={inputClass}>
              <option value="">{existing ? '현재 버전 유지 (대상을 변경하려면 선택)' : '대상을 선택하세요'}</option>
              {targets.map(target => <option key={target.id} value={target.id}>{target.name}</option>)}
            </select>
          </Field>
          <Field label="게시된 Version">
            <select aria-label="게시된 Version" value={versionId} disabled={!logicalTargetId || configLocked || versionLoading || readOnly} onChange={event => setVersionId(event.target.value)} className={inputClass}>
              <option value="">{existing && !logicalTargetId ? '현재 버전 유지' : '실행할 게시 버전을 선택하세요'}</option>
              {versions.map(version => <option key={version.id} value={version.id}>v{version.version_no} · {version.id.slice(0, 8)}…</option>)}
            </select>
          </Field>
          {targetError && <InlineAlert type="warning" message={targetError} />}
          {targetType === 'AGENT_VERSION' ? <Field label="Agent 업무 지시 (최대 4000자)">
            <textarea aria-label="Agent 업무 지시" value={requestText} disabled={configLocked || readOnly} onChange={event => setRequestText(event.target.value)} rows={3} maxLength={4000} placeholder="예: 반려동물 목록을 조회해 줘. 데이터를 변경하지 마." className={`${inputClass} h-auto py-2`} />
          </Field> : <Field label="Workflow 입력 (JSON object)">
            <textarea aria-label="Workflow 입력" value={inputJson} disabled={configLocked || readOnly} onChange={event => setInputJson(event.target.value)} rows={4} className={`${inputClass} h-auto py-2 font-mono`} />
          </Field>}
        </Section>

        <Section title="실행 일정">
          <Field label="예약 종류">
            <select aria-label="예약 종류" value={scheduleType} disabled={configLocked || readOnly} onChange={event => setScheduleType(event.target.value as ScheduleType)} className={inputClass}>
              <option value="ONCE">일회성 (ONCE)</option>
              <option value="CRON">반복 (CRON)</option>
              <option value="INTERVAL">간격 (INTERVAL)</option>
            </select>
            {targetType === 'AGENT_VERSION' && <p className="text-xs text-slate-500 mt-1">반복 예약은 실행할 때마다 선택한 Agent 버전으로 업무 지시를 새로 분석합니다. 자동 실행은 안전한 조회 작업만 허용됩니다.</p>}
          </Field>
          {scheduleType === 'ONCE' ? <Field label="실행 시각 (선택한 시간대 기준)">
            <input aria-label="실행 시각" type="datetime-local" value={oneTimeAt} disabled={configLocked || readOnly} onChange={event => setOneTimeAt(event.target.value)} className={inputClass} />
          </Field> : scheduleType === 'CRON' ? <Field label="반복 실행 규칙">
            <select aria-label="반복 규칙" value={cronPreset} disabled={configLocked || readOnly} onChange={event => {
              const preset = event.target.value as CronPreset;
              setCronPreset(preset);
              if (preset !== 'ADVANCED') setCron(buildCron(preset, cronTime, cronWeekday));
            }} className={inputClass}>
              <option value="DAILY">매일</option>
              <option value="WEEKDAYS">평일 (월~금)</option>
              <option value="WEEKLY">매주 지정 요일</option>
              <option value="ADVANCED">고급 (CRON 직접 입력)</option>
            </select>
            {cronPreset !== 'ADVANCED' ? <div className="flex gap-2 mt-2">
              <input aria-label="반복 실행 시간" type="time" value={cronTime} disabled={configLocked || readOnly} onChange={event => {
                setCronTime(event.target.value);
                setCron(buildCron(cronPreset, event.target.value, cronWeekday));
              }} className={inputClass} />
              {cronPreset === 'WEEKLY' && <select aria-label="반복 요일" value={cronWeekday} disabled={configLocked || readOnly} onChange={event => {
                setCronWeekday(event.target.value);
                setCron(buildCron(cronPreset, cronTime, event.target.value));
              }} className={inputClass}>
                {['일', '월', '화', '수', '목', '금', '토'].map((day, index) => <option key={day} value={index}>{day}요일</option>)}
              </select>}
            </div> : <input aria-label="CRON 표현식" value={cron} disabled={configLocked || readOnly} onChange={event => setCron(event.target.value)} className={`${inputClass} font-mono mt-2`} />}
            <p className="text-xs text-slate-500 mt-1">표현식: <span className="font-mono">{cron || '시간을 선택해 주세요'}</span> · 지정한 시간대 기준 (실제 예정 시각은 서버에서 계산)</p>
          </Field> : <Field label="반복 간격">
            <label className="flex items-center gap-2 text-xs text-slate-600 mb-2">
              <input type="checkbox" checked={intervalAdvanced} disabled={configLocked || readOnly} onChange={event => {
                if (!event.target.checked) {
                  const parsed = parseBasicInterval(interval);
                  if (!parsed) {
                    setFormError('현재 복합 실행 간격은 숫자·단위 입력으로 표현할 수 없습니다. 고급 입력에서 수정해 주세요.');
                    return;
                  }
                  setIntervalCount(parsed.count);
                  setIntervalUnit(parsed.unit);
                }
                setFormError(null);
                setIntervalAdvanced(event.target.checked);
              }} /> 고급 입력 (ISO-8601)
            </label>
            {intervalAdvanced ? <input aria-label="실행 간격 (ISO-8601)" value={interval} disabled={configLocked || readOnly} onChange={event => setInterval(event.target.value)} placeholder="예: PT3M, P1DT2H" className={`${inputClass} font-mono`} />
              : <div className="flex gap-2">
                <input aria-label="반복 간격 숫자" type="number" min={1} step={1} value={intervalCount} disabled={configLocked || readOnly} onChange={event => {
                  setIntervalCount(event.target.value);
                  setInterval(buildInterval(event.target.value, intervalUnit));
                }} className={inputClass} />
                <select aria-label="반복 간격 단위" value={intervalUnit} disabled={configLocked || readOnly} onChange={event => {
                  const unit = event.target.value as IntervalUnit;
                  setIntervalUnit(unit);
                  setInterval(buildInterval(intervalCount, unit));
                }} className={inputClass}>
                  <option value="MINUTES">분</option>
                  <option value="HOURS">시간</option>
                  <option value="DAYS">일</option>
                </select>
              </div>}
            <p className="text-xs text-slate-500 mt-1">저장 표현식: <span className="font-mono">{interval || '간격을 입력해 주세요'}</span></p>
          </Field>}
          <Field label="시간대 (IANA)">
            <input aria-label="시간대" value={timezone} disabled={configLocked || readOnly} onChange={event => setTimezone(event.target.value)} placeholder="Asia/Seoul" className={inputClass} />
          </Field>
          <p className="text-xs text-slate-500">시간대는 예약 해석에 사용됩니다. 서버와 브라우저의 시간대가 달라도 입력한 시간대 기준으로 처리합니다.</p>
        </Section>

        <Section title="실행 정책">
          <Field label="동시 실행 정책 (Overlap)">
            <select aria-label="동시 실행 정책" value={overlap} disabled={configLocked || readOnly} onChange={event => setOverlap(event.target.value as ScheduleOverlapPolicy)} className={inputClass}>
              {SCHEDULE_OVERLAP_POLICIES.map(policy => <option key={policy} value={policy}>{policy}</option>)}
            </select>
          </Field>
          <Field label="실행 시각을 놓쳤을 때 (Misfire)">
            <select aria-label="미실행 처리 정책" value={misfire} disabled={configLocked || readOnly} onChange={event => setMisfire(event.target.value as ScheduleMisfirePolicy)} className={inputClass}>
              {SCHEDULE_MISFIRE_POLICIES.map(policy => <option key={policy} value={policy}>{policy}</option>)}
            </select>
          </Field>
          {misfire === 'CATCH_UP_LIMITED' && <Field label="최대 보충 실행 횟수">
            <input aria-label="최대 보충 실행 횟수" type="number" min={1} max={100} value={maxCatchUp} disabled={configLocked || readOnly} onChange={event => setMaxCatchUp(Number(event.target.value))} className={inputClass} />
          </Field>}
        </Section>

        {existing && <Section title="최근 예약 실행 이력">
          {occurrenceError && <InlineAlert type="error" message={`이력 조회 실패: ${occurrenceError}`} />}
          {occurrences.length === 0 ? <p className="text-sm text-slate-500">실행 이력이 없습니다.</p> : (
            <ul className="space-y-2">
              {occurrences.map(occ => <li key={occ.id} className="border-b border-slate-100 pb-2 text-sm flex flex-wrap items-center justify-between gap-2">
                <span>{formatScheduleTimestamp(occ.scheduled_for, timezone)} · {occ.status}{occ.decision_reason ? ` · ${occ.decision_reason}` : ''}</span>
                {occ.execution_id && <Link className="text-indigo-700 hover:underline" to={`/executions/${occ.execution_id}`}>실행 상세</Link>}
              </li>)}
            </ul>
          )}
        </Section>}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="bg-white rounded-xl border border-slate-200 p-4">
    <h3 className="text-sm font-semibold text-slate-800 mb-4">{title}</h3>
    <div className="space-y-4">{children}</div>
  </section>;
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return <div><label className="block text-xs font-medium text-slate-600 mb-1.5">{label}</label>{children}</div>;
}
