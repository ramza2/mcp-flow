import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate, useParams } from 'react-router';
import { ArrowLeft, CheckCircle2, Circle } from 'lucide-react';
import StatusBadge from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import JsonViewer from '../../components/ui/JsonViewer';
import Button from '../../components/ui/Button';
import Dialog from '../../components/ui/Dialog';
import { EmptyState, ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import {
  activateMCPTool,
  deactivateMCPTool,
  getMCPTool,
  getToolVersion,
  listToolVersions,
  updateMCPTool,
} from '../../api/mcp';
import { isAbortError, isApiError } from '../../api/client';
import type { MCPToolDto, MCPToolVersionDto } from '../../api/types';
import { formatTimestamp, hasPermission, shortenId } from '../../domain';
import ToolPolicyTab from './ToolPolicyTab';
import ToolTestCallTab from './ToolTestCallTab';
import ToolVerificationTab from './ToolVerificationTab';
import {
  isLifecycleActionDisabled,
  isVersionConflict,
  parseTagInput,
  stringTags,
  toFeedbackError,
  toolLifecycleAction,
  type FeedbackError,
} from './toolLifecycle';
import {
  MCP_TOOL_SETUP_FLOW_LABEL,
  buildMcpToolSetupSteps,
  deriveMcpToolSetupFlags,
  lifecycleMutationNextAction,
  nextMcpToolSetupAction,
  toolLifecycleDisabledReason,
  toolStatusMeaningNote,
} from './mcpToolSetup';

export default function MCPToolDetail() {
  const { toolId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const fromServer = (location.state as { fromServer?: boolean } | null)?.fromServer === true;
  const mountedRef = useRef(true);
  const versionRequestRef = useRef<AbortController | null>(null);
  const [tab, setTab] = useState('overview');
  const [tool, setTool] = useState<MCPToolDto | null>(null);
  const [versions, setVersions] = useState<MCPToolVersionDto[]>([]);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [selectedVersion, setSelectedVersion] = useState<MCPToolVersionDto | null>(null);
  const [loading, setLoading] = useState(true);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<FeedbackError | null>(null);
  const [versionLoading, setVersionLoading] = useState(false);
  const [versionError, setVersionError] = useState<FeedbackError | null>(null);
  const [statusMutating, setStatusMutating] = useState(false);
  const [mutationError, setMutationError] = useState<FeedbackError | null>(null);
  const [editOpen, setEditOpen] = useState(false);
  const [editSaving, setEditSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [descriptionOverride, setDescriptionOverride] = useState('');
  const [tagsInput, setTagsInput] = useState('');

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      versionRequestRef.current?.abort();
      versionRequestRef.current = null;
    };
  }, []);

  const loadVersionDetail = useCallback(async (versionId: string) => {
    if (!toolId) return;

    versionRequestRef.current?.abort();
    const controller = new AbortController();
    versionRequestRef.current = controller;

    setSelectedVersionId(versionId);
    setVersionLoading(true);
    setVersionError(null);

    try {
      const ver = await getToolVersion(toolId, versionId, controller.signal);
      if (versionRequestRef.current !== controller || !mountedRef.current) return;
      setSelectedVersion(ver);
    } catch (err) {
      if (
        isAbortError(err) ||
        controller.signal.aborted ||
        versionRequestRef.current !== controller ||
        !mountedRef.current
      ) {
        return;
      }
      setSelectedVersion(null);
      setVersionError(toFeedbackError(err, '도구 버전 상세 정보를 불러오지 못했습니다.'));
    } finally {
      if (versionRequestRef.current === controller && mountedRef.current) {
        setVersionLoading(false);
      }
    }
  }, [toolId]);

  const loadTool = useCallback(() => {
    if (!toolId) return () => {};
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotFound(false);
    setVersionError(null);
    setSelectedVersion(null);

    getMCPTool(toolId, controller.signal)
      .then(async t => {
        if (cancelled) return;
        setTool(t);
        let versItems: MCPToolVersionDto[] = [];
        try {
          const vers = await listToolVersions(toolId, { signal: controller.signal });
          if (cancelled) return;
          versItems = vers.items;
          setVersions(versItems);
        } catch (err) {
          if (isAbortError(err) || cancelled) return;
          setVersions([]);
          setVersionError(toFeedbackError(err, '도구 버전 목록을 불러오지 못했습니다.'));
          return;
        }
        const currentId = t.current_version_id ?? versItems[0]?.id ?? null;
        if (currentId) {
          await loadVersionDetail(currentId);
        } else {
          setSelectedVersionId(null);
          setSelectedVersion(null);
        }
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        if (isApiError(err) && err.status === 404) {
          setNotFound(true);
          return;
        }
        setError(toFeedbackError(err, '도구 정보를 불러오지 못했습니다.'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
      versionRequestRef.current?.abort();
      versionRequestRef.current = null;
    };
  }, [toolId, loadVersionDetail]);

  useEffect(() => {
    return loadTool();
  }, [loadTool]);

  const refetchToolOnly = async () => {
    if (!toolId) return;
    try {
      const latest = await getMCPTool(toolId);
      if (mountedRef.current) setTool(latest);
    } catch (err) {
      if (mountedRef.current) {
        setMutationError(toFeedbackError(err, '최신 도구 상태를 불러오지 못했습니다.'));
      }
    }
  };

  const handleLifecycle = async (action: 'activate' | 'deactivate') => {
    if (!tool) return;
    setStatusMutating(true);
    setMutationError(null);
    try {
      const updated =
        action === 'activate'
          ? await activateMCPTool(tool.id, tool.lock_version)
          : await deactivateMCPTool(tool.id, tool.lock_version);
      if (mountedRef.current) setTool(updated);
    } catch (err) {
      if (!mountedRef.current) return;
      if (isVersionConflict(err)) {
        const base =
          '다른 작업으로 도구 상태가 변경되었습니다. 최신 상태를 다시 불러옵니다. ' +
          (isApiError(err) ? err.message : '');
        setMutationError({
          message: `${base} ${lifecycleMutationNextAction(action, { code: 'RESOURCE_VERSION_CONFLICT' })}`,
          requestId: isApiError(err) ? err.requestId ?? undefined : undefined,
          code: isApiError(err) ? err.code : undefined,
        });
        await refetchToolOnly();
      } else {
        const feedback = toFeedbackError(
          err,
          `도구 ${action === 'activate' ? '활성화' : '비활성화'}에 실패했습니다.`,
        );
        if (isApiError(err) && err.status === 403) {
          feedback.code = feedback.code ?? 'FORBIDDEN';
        }
        setMutationError({
          ...feedback,
          message: `${feedback.message} ${lifecycleMutationNextAction(action, feedback)}`,
        });
      }
    } finally {
      if (mountedRef.current) setStatusMutating(false);
    }
  };

  const openEdit = () => {
    if (!tool) return;
    setDisplayName(tool.display_name ?? '');
    setDescriptionOverride(tool.description_override ?? '');
    setTagsInput(stringTags(tool.tags).join(', '));
    setEditError(null);
    setEditOpen(true);
  };

  const saveMetadata = async () => {
    if (!tool) return;
    setEditError(null);
    const parsedTags = parseTagInput(tagsInput);
    if (!parsedTags.ok) {
      setEditError(parsedTags.error);
      return;
    }
    setEditSaving(true);
    try {
      const updated = await updateMCPTool(
        tool.id,
        {
          display_name: displayName.trim() || null,
          description_override: descriptionOverride.trim() || null,
          tags: parsedTags.tags,
        },
        tool.lock_version,
      );
      if (!mountedRef.current) return;
      setMutationError(null);
      setTool(updated);
      setEditOpen(false);
    } catch (err) {
      if (!mountedRef.current) return;
      if (isVersionConflict(err)) {
        setMutationError({
          message:
            '다른 작업으로 도구 정보가 변경되었습니다. 최신 상태를 다시 불러옵니다. ' +
            (isApiError(err) ? err.message : ''),
          requestId: isApiError(err) ? err.requestId ?? undefined : undefined,
          code: isApiError(err) ? err.code : undefined,
        });
        setEditOpen(false);
        await refetchToolOnly();
      } else {
        setEditError(toFeedbackError(err, '표시 정보 저장에 실패했습니다.').message);
      }
    } finally {
      if (mountedRef.current) setEditSaving(false);
    }
  };

  const selectVersion = (versionId: string) => {
    void loadVersionDetail(versionId);
  };

  if (loading && !tool) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (notFound) {
    return (
      <div className="p-6">
        <ErrorState message="도구를 찾을 수 없습니다." onRetry={() => navigate('/mcp/tools')} />
      </div>
    );
  }

  if (error || !tool) {
    return (
      <div className="p-6">
        <ErrorState message={error?.message} requestId={error?.requestId} onRetry={loadTool} />
      </div>
    );
  }

  const title = tool.display_name ?? tool.remote_name;
  const validationStatus = selectedVersion?.validation_status;
  const lifecycle = toolLifecycleAction(tool.status);
  const lifecycleDisabledReason = toolLifecycleDisabledReason(tool, {
    canEdit: hasPermission('mcp.edit'),
  });
  const lifecycleBlocked = Boolean(lifecycleDisabledReason) || isLifecycleActionDisabled(tool.status);
  const tags = stringTags(tool.tags);
  const setupSteps = buildMcpToolSetupSteps(deriveMcpToolSetupFlags(tool));
  const nextSetup = nextMcpToolSetupAction(setupSteps, tool);

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <div className="flex items-center gap-4 mb-3">
          <button
            onClick={() => navigate(fromServer ? `/mcp/servers/${tool.mcp_server_id}?tab=tools` : '/mcp/tools')}
            className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700"
          >
            <ArrowLeft size={14} /> {fromServer ? '서버 도구 목록' : 'MCP 도구 목록'}
          </button>
          {!fromServer && (
            <button
              onClick={() => navigate(`/mcp/servers/${tool.mcp_server_id}?tab=tools`)}
              className="text-sm text-slate-500 hover:text-slate-700"
            >
              서버 도구 목록
            </button>
          )}
        </div>
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{title}</h1>
            <p className="text-xs font-mono text-slate-400 mt-0.5">{tool.remote_name}</p>
            <div className="flex items-center gap-3 mt-2">
              <StatusBadge status={tool.status} />
              {validationStatus && <ValidationBadge status={validationStatus} />}
              <span className="text-xs text-slate-400 font-mono">{shortenId(tool.mcp_server_id)}</span>
            </div>
          </div>
          <div className="flex flex-col items-end gap-2">
            <div className="flex items-center gap-2">
              {lifecycle === 'activate' && (
                <Button
                  variant="primary"
                  size="sm"
                  loading={statusMutating}
                  disabled={lifecycleBlocked}
                  onClick={() => void handleLifecycle('activate')}
                >
                  활성화
                </Button>
              )}
              {lifecycle === 'deactivate' && (
                <Button
                  variant="outline"
                  size="sm"
                  loading={statusMutating}
                  disabled={lifecycleBlocked}
                  onClick={() => void handleLifecycle('deactivate')}
                >
                  비활성화
                </Button>
              )}
              {!lifecycle && (
                <Button variant="ghost" size="sm" disabled>
                  상태 변경 불가
                </Button>
              )}
            </div>
            {lifecycleDisabledReason && (
              <p className="max-w-md text-right text-xs text-slate-500" data-testid="mcp-tool-activate-disabled-reason">
                {lifecycle === 'deactivate' ? '변경 불가' : '활성화 불가'}: {lifecycleDisabledReason}
              </p>
            )}
          </div>
        </div>
        {versionError && (
          <div className="mt-3 space-y-1">
            <InlineAlert type="error" message={versionError.message} />
            {versionError.requestId && (
              <p className="font-mono text-xs text-slate-400">Request ID: {versionError.requestId}</p>
            )}
            <p className="text-xs text-slate-500">다음: 버전 목록을 다시 불러오거나 서버에서 도구 탐색을 다시 적용하세요.</p>
          </div>
        )}
        {mutationError && (
          <div className="mt-3 space-y-1">
            <InlineAlert type="error" message={mutationError.message} />
            {mutationError.requestId && (
              <p className="font-mono text-xs text-slate-400">
                Request ID: {mutationError.requestId}
              </p>
            )}
          </div>
        )}
      </div>

      <section
        aria-label="도구 관리 안내"
        className="border-b border-slate-200 bg-slate-50 px-6 py-3"
        data-testid="mcp-tool-setup-progress"
      >
        <p className="text-xs font-semibold text-slate-700 mb-2">
          이용 준비: {MCP_TOOL_SETUP_FLOW_LABEL}
        </p>
        <ol className="flex flex-wrap gap-3 mb-2">
          {setupSteps.map(step => (
            <li
              key={step.id}
              className={`inline-flex items-center gap-1.5 text-xs ${
                step.done ? 'text-emerald-700' : 'text-slate-500'
              }`}
              data-testid={`mcp-tool-setup-step-${step.id}`}
              data-done={step.done ? 'true' : 'false'}
            >
              {step.done
                ? <CheckCircle2 size={14} className="shrink-0" aria-hidden />
                : <Circle size={14} className="shrink-0" aria-hidden />}
              <span>{step.label}</span>
            </li>
          ))}
        </ol>
        {nextSetup ? (
          <p className="text-xs text-slate-600" data-testid="mcp-tool-setup-next">
            다음: {nextSetup.message}
          </p>
        ) : (
          <p className="text-xs text-emerald-700" data-testid="mcp-tool-setup-next">
            도구가 활성입니다. 검증 완료(VERIFIED)와 실제 실행 가능 여부는 검증 이력·실행 정책·권한에서 별도로 확인하세요.
          </p>
        )}
        <p className="mt-1 text-xs text-slate-500">{toolStatusMeaningNote()}</p>
      </section>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: '기본 정보' },
            { id: 'schema', label: '입력 스키마' },
            { id: 'output', label: '출력 스키마' },
            { id: 'policy', label: '실행 정책' },
            { id: 'verification', label: '검증 이력' },
            { id: 'test', label: '실행 테스트' },
            { id: 'usedby', label: '사용처' },
            { id: 'versions', label: '버전 이력' },
            { id: 'audit', label: '감사 이력' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6 max-w-3xl">
        {versionLoading && tab !== 'policy' && tab !== 'verification' && (
          <LoadingSkeleton rows={3} />
        )}

        {tab === 'overview' && !versionLoading && (
          <div className="space-y-4">
            <div className="flex justify-end">
              <Button variant="outline" size="sm" onClick={openEdit}>
                표시 정보 수정
              </Button>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <InfoCard title="도구 정보">
                <Row label="원본 도구 이름" mono>
                  {tool.remote_name}
                </Row>
                <Row label="표시 이름">{tool.display_name ?? '—'}</Row>
                <Row label="설명">{tool.description_override ?? '—'}</Row>
                <Row label="태그">{tags.length ? tags.join(', ') : '—'}</Row>
                <Row label="서버 ID" mono>
                  {shortenId(tool.mcp_server_id)}
                </Row>
                <Row label="도구 상태">
                  <StatusBadge status={tool.status} size="sm" />
                </Row>
                <Row label="상태 의미">
                  <span className="text-xs text-slate-600">
                    활성=사용 후보 · 검증 완료(VERIFIED)=버전 증빙(별도) · 서버 초안(DRAFT)≠도구 상태
                  </span>
                </Row>
                <Row label="현재 버전">
                  {selectedVersion ? `v${selectedVersion.version_no}` : '—'}
                </Row>
                <Row label="버전 유효성">
                  {validationStatus ? <ValidationBadge status={validationStatus} /> : '—'}
                </Row>
                <Row label="lock_version">{tool.lock_version}</Row>
                <Row label="최초 발견">{formatTimestamp(tool.first_seen_at)}</Row>
                <Row label="최근 발견">{formatTimestamp(tool.last_seen_at)}</Row>
              </InfoCard>
              {selectedVersion?.validation_errors && selectedVersion.validation_errors.length > 0 && (
                <InfoCard title="유효성 검사 오류">
                  <JsonViewer value={selectedVersion.validation_errors} />
                </InfoCard>
              )}
            </div>
          </div>
        )}

        {tab === 'schema' && !versionLoading && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">입력 스키마</h3>
            <JsonViewer value={selectedVersion?.input_schema ?? null} emptyLabel="입력 스키마가 없습니다." />
          </div>
        )}

        {tab === 'output' && !versionLoading && (
          <div className="bg-white rounded-xl border border-slate-200 p-4">
            <h3 className="text-sm font-semibold text-slate-800 mb-3">출력 스키마</h3>
            <JsonViewer
              value={selectedVersion?.output_schema ?? null}
              emptyLabel="출력 스키마가 없습니다."
            />
          </div>
        )}

        {tab === 'policy' && toolId && <ToolPolicyTab toolId={toolId} active={tab === 'policy'} />}

        {tab === 'verification' && toolId && (
          <ToolVerificationTab
            toolId={toolId}
            selectedVersion={selectedVersion}
            currentVersionId={tool.current_version_id}
            active={tab === 'verification'}
          />
        )}

        {tab === 'test' && toolId && (
          <ToolTestCallTab
            toolId={toolId}
            selectedVersion={selectedVersion}
            currentVersionId={tool?.current_version_id ?? null}
            active={tab === 'test'}
            onShowPolicy={() => setTab('policy')}
          />
        )}

        {tab === 'usedby' && (
          <EmptyState
            title="사용처 조회 미지원"
            description="이 도구를 사용하는 에이전트·워크플로 조회 기능은 아직 지원하지 않습니다. 활성화와 실행 가능 여부는 실행 정책·권한 탭/관리 화면에서 확인하세요."
          />
        )}

        {tab === 'versions' && (
          <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
            {versions.length === 0 ? (
              <EmptyState
                title="버전 이력이 없습니다"
                description="서버에서 도구 탐색 결과를 적용하면 버전이 생성됩니다. 버전이 없으면 활성화할 수 없습니다."
              />
            ) : (
              versions.map(v => (
                <button
                  key={v.id}
                  type="button"
                  onClick={() => selectVersion(v.id)}
                  className={`w-full flex items-center justify-between px-4 py-3 border-b last:border-0 text-left hover:bg-slate-50 ${
                    selectedVersionId === v.id ? 'bg-indigo-50' : ''
                  }`}
                >
                  <div>
                    <p className="text-sm font-mono font-medium text-slate-800">v{v.version_no}</p>
                    <p className="text-xs text-slate-400">{formatTimestamp(v.discovered_at)}</p>
                  </div>
                  <div className="flex items-center gap-2">
                    {tool.current_version_id === v.id && (
                      <span className="text-xs text-indigo-600 font-medium">현재 버전</span>
                    )}
                    <ValidationBadge status={v.validation_status} />
                  </div>
                </button>
              ))
            )}
          </div>
        )}

        {tab === 'audit' && (
          <EmptyState
            title="감사 이력 조회 미지원"
            description="도구 감사 이력 조회 기능은 아직 지원하지 않습니다. 활성화·검증·실행 정책은 각 탭에서 확인하세요."
          />
        )}
      </div>

      <Dialog
        open={editOpen}
        onClose={() => !editSaving && setEditOpen(false)}
        title="도구 표시 정보 수정"
        description="도구 이름·설명·태그만 변경합니다. 원본 도구 정의와 실행 정책은 변경하지 않습니다."
        footer={
          <>
            <Button variant="outline" onClick={() => setEditOpen(false)} disabled={editSaving}>
              취소
            </Button>
            <Button variant="primary" loading={editSaving} onClick={() => void saveMetadata()}>
              저장
            </Button>
          </>
        }
      >
        <div className="space-y-3 text-sm">
          {editError && <InlineAlert type="error" message={editError} />}
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">표시 이름</span>
            <input
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={displayName}
              onChange={e => setDisplayName(e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">설명</span>
            <textarea
              className="w-full border border-slate-300 rounded-md px-2 py-1.5 min-h-[80px]"
              value={descriptionOverride}
              onChange={e => setDescriptionOverride(e.target.value)}
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs text-slate-500">태그 (쉼표로 구분, 최대 32개·각 64자)</span>
            <input
              className="w-full border border-slate-300 rounded-md px-2 py-1.5"
              value={tagsInput}
              onChange={e => setTagsInput(e.target.value)}
              placeholder="ops, search"
            />
          </label>
        </div>
      </Dialog>
    </div>
  );
}

function ValidationBadge({ status }: { status: string }) {
  const styles: Record<string, string> = {
    VALID: 'text-green-700 bg-green-50',
    WARNING: 'text-amber-700 bg-amber-50',
    INVALID: 'text-red-700 bg-red-50',
  };
  const s = styles[status] ?? 'text-slate-500 bg-slate-100';
  return <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${s}`}>{status}</span>;
}

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      <div className="space-y-2 text-sm">{children}</div>
    </div>
  );
}

function Row({ label, children, mono }: { label: string; children: React.ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-center gap-4">
      <span className="text-slate-400 w-36 shrink-0 text-xs">{label}</span>
      <span className={`text-slate-700 ${mono ? 'font-mono text-xs' : ''}`}>{children}</span>
    </div>
  );
}
