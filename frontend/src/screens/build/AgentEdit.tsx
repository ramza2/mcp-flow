import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { Plus, Search, Trash2, X } from 'lucide-react';
import PageHeader from '../../components/ui/PageHeader';
import Button from '../../components/ui/Button';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import {
  activateAgent,
  createAgent,
  createAgentVersion,
  deleteAgent,
  getAgent,
  getAgentVersion,
  listAgentToolGrants,
  listAgentVersions,
  publishAgentVersion,
  putAgentToolGrants,
  updateAgent,
  validateAgentVersion,
} from '../../api/agents';
import { listMCPServers, listMCPTools } from '../../api/mcp';
import { listLLMProfiles } from '../../api/modelProfiles';
import { isAbortError, isApiError } from '../../api/client';
import type { AgentDto, AgentVersionDto, LLMProfileDto, MCPServerDto, MCPToolDto } from '../../api/types';
import { AGENT_VISIBILITIES, labelAgentVisibility, type AgentVisibility } from '../../domain';

type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';

type FormSnapshot = {
  name: string;
  description: string;
  visibility: AgentVisibility;
  changeSummary: string;
  instructions: string;
  llmProfileId: string;
  selectedTools: string;
  autoSelectThreshold: number;
  confirmationThreshold: number;
  maxCandidates: number;
};

function buildSnapshot(input: {
  name: string;
  description: string;
  visibility: AgentVisibility;
  changeSummary: string;
  instructions: string;
  llmProfileId: string;
  selectedTools: string[];
  autoSelectThreshold: number;
  confirmationThreshold: number;
  maxCandidates: number;
}): FormSnapshot {
  return {
    name: input.name,
    description: input.description,
    visibility: input.visibility,
    changeSummary: input.changeSummary,
    instructions: input.instructions,
    llmProfileId: input.llmProfileId,
    selectedTools: [...input.selectedTools].sort().join(','),
    autoSelectThreshold: input.autoSelectThreshold,
    confirmationThreshold: input.confirmationThreshold,
    maxCandidates: input.maxCandidates,
  };
}

export default function AgentEdit() {
  const navigate = useNavigate();
  const { agentId, versionId } = useParams();
  const isLocalNew = agentId === 'new';
  const isNewVersionRoute = !versionId || versionId === 'new';

  const [agent, setAgent] = useState<AgentDto | null>(null);
  const [version, setVersion] = useState<AgentVersionDto | null>(null);
  const [hasPublishHistory, setHasPublishHistory] = useState(false);
  const [profiles, setProfiles] = useState<LLMProfileDto[]>([]);
  const [tools, setTools] = useState<MCPToolDto[]>([]);
  const [servers, setServers] = useState<MCPServerDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<{ message: string; requestId?: string } | null>(null);

  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [visibility, setVisibility] = useState<AgentVisibility>('PRIVATE');
  const [changeSummary, setChangeSummary] = useState('');
  const [instructions, setInstructions] = useState('');
  const [llmProfileId, setLlmProfileId] = useState('');
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [autoSelectThreshold, setAutoSelectThreshold] = useState(0.82);
  const [confirmationThreshold, setConfirmationThreshold] = useState(0.60);
  const [maxCandidates, setMaxCandidates] = useState(5);
  const [toolSearch, setToolSearch] = useState('');
  const [serverFilter, setServerFilter] = useState('ALL');
  const [savedSnapshot, setSavedSnapshot] = useState<FormSnapshot | null>(null);

  const [saving, setSaving] = useState(false);
  const [validating, setValidating] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [validationMessages, setValidationMessages] = useState<string[]>([]);
  const [validationOk, setValidationOk] = useState<string | null>(null);

  useEffect(() => {
    if (!agentId) return;
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setLoadError(null);

    (async () => {
      try {
        const [profilePage, toolPage, serverPage] = await Promise.all([
          listLLMProfiles({ page_size: 100, signal: controller.signal }),
          listMCPTools({ page_size: 100, status: 'ACTIVE', signal: controller.signal }),
          listMCPServers({ page_size: 100, signal: controller.signal }),
        ]);
        if (cancelled) return;

        setProfiles(profilePage.items);
        setTools(toolPage.items);
        setServers(serverPage.items);

        if (isLocalNew) {
          setAgent(null);
          setVersion(null);
          setHasPublishHistory(false);
          setName('');
          setDescription('');
          setVisibility('PRIVATE');
          setInstructions('');
          setChangeSummary('');
          setSelectedTools([]);
          setSavedSnapshot(null);
          setValidationOk(null);
          setValidationMessages([]);
          if (profilePage.items[0]) setLlmProfileId(profilePage.items[0].id);
          return;
        }

        const [agentRow, versionPage] = await Promise.all([
          getAgent(agentId, controller.signal),
          listAgentVersions(agentId, { page_size: 50, sort: '-version_no', signal: controller.signal }),
        ]);
        if (cancelled) return;

        setAgent(agentRow);
        setName(agentRow.name);
        setDescription(agentRow.description ?? '');
        setVisibility(agentRow.visibility);
        setHasPublishHistory(
          versionPage.items.some(v =>
            v.status === 'PUBLISHED' || v.status === 'DEPRECATED' || v.published_at != null,
          ),
        );

        let loadedVersion: AgentVersionDto | null = null;
        if (!isNewVersionRoute && versionId) {
          loadedVersion = await getAgentVersion(agentId, versionId, controller.signal);
        } else if (isNewVersionRoute && agentRow.current_version_id) {
          loadedVersion = versionPage.items.find(v => v.id === agentRow.current_version_id) ?? null;
        }

        if (loadedVersion) {
          if (!isNewVersionRoute) setVersion(loadedVersion);
          const nextInstructions = loadedVersion.system_instruction;
          const nextLlm = loadedVersion.llm_profile_id;
          const nextSummary = isNewVersionRoute ? '' : (loadedVersion.change_summary ?? '');
          const sel = loadedVersion.selection_settings as {
            auto_select_threshold?: number;
            confirmation_threshold?: number;
            max_candidates?: number;
          };
          const nextAuto = typeof sel.auto_select_threshold === 'number' ? sel.auto_select_threshold : 0.82;
          const nextConfirm = typeof sel.confirmation_threshold === 'number' ? sel.confirmation_threshold : 0.60;
          const nextMax = typeof sel.max_candidates === 'number' ? sel.max_candidates : 5;
          let nextTools: string[] = [];
          if (!isNewVersionRoute) {
            const grants = await listAgentToolGrants(agentId, loadedVersion.id, controller.signal);
            if (!cancelled) nextTools = grants.items.map(g => g.mcp_tool_id);
          }
          if (cancelled) return;
          setInstructions(nextInstructions);
          setLlmProfileId(nextLlm);
          setChangeSummary(nextSummary);
          setAutoSelectThreshold(nextAuto);
          setConfirmationThreshold(nextConfirm);
          setMaxCandidates(nextMax);
          setSelectedTools(nextTools);
          if (!isNewVersionRoute) {
            setSavedSnapshot(buildSnapshot({
              name: agentRow.name,
              description: agentRow.description ?? '',
              visibility: agentRow.visibility,
              changeSummary: nextSummary,
              instructions: nextInstructions,
              llmProfileId: nextLlm,
              selectedTools: nextTools,
              autoSelectThreshold: nextAuto,
              confirmationThreshold: nextConfirm,
              maxCandidates: nextMax,
            }));
          } else {
            setSavedSnapshot(null);
          }
        } else if (profilePage.items[0]) {
          setLlmProfileId(profilePage.items[0].id);
          setSavedSnapshot(null);
        }
      } catch (err) {
        if (isAbortError(err) || cancelled) return;
        const apiErr = isApiError(err) ? err : null;
        setLoadError({
          message: apiErr?.message ?? 'Agent 편집 데이터를 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [agentId, versionId, isNewVersionRoute, isLocalNew]);

  const versionStatus = (version?.status ?? 'DRAFT') as VersionStatus;
  const readOnly = versionStatus === 'DEPRECATED';
  const publishedBlocked = versionStatus === 'PUBLISHED';
  const serverNameById = useMemo(
    () => Object.fromEntries(servers.map(s => [s.id, s.name])),
    [servers],
  );

  const filteredTools = useMemo(() => {
    const q = toolSearch.trim().toLowerCase();
    return tools.filter(t => {
      if (serverFilter !== 'ALL' && t.mcp_server_id !== serverFilter) return false;
      if (!q) return true;
      const label = (t.display_name ?? t.remote_name).toLowerCase();
      const server = (serverNameById[t.mcp_server_id] ?? '').toLowerCase();
      return label.includes(q) || t.remote_name.toLowerCase().includes(q) || server.includes(q);
    });
  }, [tools, toolSearch, serverFilter, serverNameById]);

  const selectedToolObjs = tools.filter(t => selectedTools.includes(t.id));

  const clientErrors = useMemo(() => {
    const errors: string[] = [];
    if (!name.trim()) errors.push('Agent 이름이 필요합니다.');
    if (!instructions.trim()) errors.push('에이전트 업무 지시문을 입력해 주세요.');
    if (!llmProfileId) errors.push('사용할 LLM 모델 프로필을 선택해 주세요.');
    if (autoSelectThreshold < confirmationThreshold) {
      errors.push('자동 선택 기준은 사용자 확인 기준 이상이어야 합니다.');
    }
    return errors;
  }, [name, instructions, llmProfileId, autoSelectThreshold, confirmationThreshold]);

  const currentSnapshot = useMemo(
    () => buildSnapshot({
      name,
      description,
      visibility,
      changeSummary,
      instructions,
      llmProfileId,
      selectedTools,
      autoSelectThreshold,
      confirmationThreshold,
      maxCandidates,
    }),
    [
      name,
      description,
      visibility,
      changeSummary,
      instructions,
      llmProfileId,
      selectedTools,
      autoSelectThreshold,
      confirmationThreshold,
      maxCandidates,
    ],
  );

  const isDirty = useMemo(() => {
    if (!savedSnapshot) return true;
    return JSON.stringify(currentSnapshot) !== JSON.stringify(savedSnapshot);
  }, [currentSnapshot, savedSnapshot]);

  useEffect(() => {
    if (!isDirty) return;
    // Stale VALID from a previous saved snapshot must not remain visible.
    setValidationOk(null);
  }, [isDirty]);

  const toggleTool = (id: string) => {
    if (readOnly || publishedBlocked) return;
    setSelectedTools(prev => (prev.includes(id) ? prev.filter(t => t !== id) : [...prev, id]));
  };

  const canDeleteDraftAgent = (row: AgentDto | null) => Boolean(
    row
    && row.status === 'DRAFT'
    && row.current_version_id == null
    && !hasPublishHistory,
  );

  const canDeleteSavedDraft = canDeleteDraftAgent(agent) && !isLocalNew;

  const persistDraft = async (): Promise<{ agent: AgentDto; version: AgentVersionDto }> => {
    if (clientErrors.length > 0) {
      throw new Error(clientErrors[0]);
    }

    // Reuse in-memory Agent after a partial first-save failure — never create twice.
    let targetAgent = agent;
    if (!targetAgent) {
      targetAgent = await createAgent({
        name: name.trim() || 'New Agent',
        description: description.trim() || null,
        visibility,
      });
      setAgent(targetAgent);
    } else {
      targetAgent = await updateAgent(
        targetAgent.id,
        {
          name: name.trim(),
          description: description.trim() || null,
          visibility,
        },
        targetAgent.lock_version,
      );
      setAgent(targetAgent);
    }

    const versionBody = {
      system_instruction: instructions.trim(),
      llm_profile_id: llmProfileId,
      request_schema_version: '1.0',
      plan_schema_version: '1.0',
      selection_settings: {
        auto_select_threshold: autoSelectThreshold,
        confirmation_threshold: confirmationThreshold,
        max_candidates: maxCandidates,
      },
      planning_settings: {},
      response_settings: {},
      change_summary: changeSummary.trim() || null,
    };

    let savedVersion: AgentVersionDto;
    if (version && version.status === 'DRAFT' && !isNewVersionRoute) {
      const contentChanged =
        version.system_instruction !== versionBody.system_instruction
        || version.llm_profile_id !== versionBody.llm_profile_id
        || JSON.stringify(version.selection_settings) !== JSON.stringify(versionBody.selection_settings)
        || (version.change_summary ?? null) !== versionBody.change_summary;
      savedVersion = contentChanged
        ? await createAgentVersion(targetAgent.id, { ...versionBody, source_version_id: version.id })
        : version;
    } else {
      savedVersion = await createAgentVersion(targetAgent.id, {
        ...versionBody,
        source_version_id: targetAgent.current_version_id ?? undefined,
      });
    }

    await putAgentToolGrants(targetAgent.id, savedVersion.id, {
      items: selectedTools.map(mcp_tool_id => ({
        mcp_tool_id,
        effect: 'ALLOW' as const,
        // null = no constraints. {} is non-null and fails closed in ParameterBuilder.
        parameter_constraints: null,
        requires_confirmation: false,
      })),
    });

    setVersion(savedVersion);
    setSavedSnapshot(currentSnapshot);
    setValidationOk(null);
    return { agent: targetAgent, version: savedVersion };
  };

  const handleCancel = async () => {
    // Partial first-save may have created a DRAFT Agent while still on /agents/new.
    // Soft-delete it so cancel does not leave a hidden orphan.
    if (isLocalNew && canDeleteDraftAgent(agent) && agent) {
      try {
        await deleteAgent(agent.id);
      } catch (err) {
        const apiErr = isApiError(err) ? err : null;
        setFormError(
          apiErr?.message
            ?? (err instanceof Error ? err.message : '임시 Agent 삭제에 실패했습니다. 다시 시도하세요.'),
        );
        return;
      }
      navigate('/agents');
      return;
    }
    if (isLocalNew || !agent) {
      navigate('/agents');
      return;
    }
    navigate(`/agents/${agent.id}`);
  };

  const handleSave = async () => {
    setSaving(true);
    setFormError(null);
    setValidationOk(null);
    try {
      const saved = await persistDraft();
      navigate(`/agents/${saved.agent.id}/versions/${saved.version.id}/edit`, { replace: true });
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setFormError(apiErr?.message ?? (err instanceof Error ? err.message : 'Draft 저장에 실패했습니다.'));
    } finally {
      setSaving(false);
    }
  };

  const handleValidate = async () => {
    setValidating(true);
    setFormError(null);
    setValidationMessages([]);
    setValidationOk(null);
    try {
      if (isLocalNew || !agent || !version || isDirty) {
        setValidationMessages([
          '저장되지 않은 변경사항이 있습니다. 먼저 초안 저장을 실행한 뒤 검증해 주세요.',
        ]);
        return;
      }
      const validated = await validateAgentVersion(agent.id, version.id);
      setVersion(prev => prev ? {
        ...prev,
        validation_status: validated.validation_status,
        validation_report: validated.validation_report,
      } : prev);
      if (validated.validation_status !== 'VALID') {
        const report = validated.validation_report as { errors?: Array<{ message?: string }> } | null;
        const messages = (report?.errors ?? []).map(e => e.message ?? '검증 오류');
        setValidationMessages(messages.length ? messages : ['버전 검증에 실패했습니다.']);
        return;
      }
      setValidationOk(`검증 통과 (v${validated.version_no}). 아직 게시·활성화되지는 않았습니다.`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setFormError(apiErr?.message ?? (err instanceof Error ? err.message : '검증에 실패했습니다.'));
    } finally {
      setValidating(false);
    }
  };

  const handlePublish = async () => {
    setPublishing(true);
    setFormError(null);
    setValidationMessages([]);
    setValidationOk(null);
    try {
      const saved = await persistDraft();
      const validated = await validateAgentVersion(saved.agent.id, saved.version.id);
      if (validated.validation_status !== 'VALID') {
        const report = validated.validation_report as { errors?: Array<{ message?: string }> } | null;
        const messages = (report?.errors ?? []).map(e => e.message ?? '검증 오류');
        setValidationMessages(messages.length ? messages : ['버전 검증에 실패했습니다.']);
        setVersion(prev => prev ? {
          ...prev,
          validation_status: validated.validation_status,
          validation_report: validated.validation_report,
        } : prev);
        navigate(`/agents/${saved.agent.id}/versions/${saved.version.id}/edit`, { replace: true });
        return;
      }
      await publishAgentVersion(saved.agent.id, saved.version.id);
      const fresh = await getAgent(saved.agent.id);
      await activateAgent(fresh.id, fresh.lock_version);
      navigate(`/agents/${saved.agent.id}`);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setFormError(apiErr?.message ?? (err instanceof Error ? err.message : '게시·활성화에 실패했습니다.'));
    } finally {
      setPublishing(false);
    }
  };

  const handleDelete = async () => {
    if (!agent || !canDeleteSavedDraft) return;
    setDeleting(true);
    setFormError(null);
    try {
      await deleteAgent(agent.id);
      setDeleteOpen(false);
      navigate('/agents');
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setFormError(apiErr?.message ?? 'Agent 삭제에 실패했습니다.');
      setDeleteOpen(false);
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={10} />
      </div>
    );
  }

  if (loadError || (!isLocalNew && !agent)) {
    return (
      <div className="p-6">
        <ErrorState
          message={loadError?.message ?? `에이전트를 찾을 수 없습니다: ${agentId ?? '(ID 없음)'}`}
          requestId={loadError?.requestId}
        />
        <div className="mt-4">
          <Button variant="outline" onClick={() => navigate('/agents')}>에이전트 목록으로</Button>
        </div>
      </div>
    );
  }

  const displayName = agent?.name || name || '새 에이전트';
  const agentPath = agent ? `/agents/${agent.id}` : '/agents';

  if (publishedBlocked && version && agent) {
    return (
      <div>
        <PageHeader
          breadcrumbs={[
            { label: '에이전트 관리', to: '/agents' },
            { label: agent.name, to: `/agents/${agent.id}` },
            { label: `v${version.version_no}` },
          ]}
          title={`에이전트 버전 v${version.version_no}`}
          actions={<Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>닫기</Button>}
        />
        <div className="p-6 max-w-2xl space-y-4">
          <InlineAlert type="warning" message="게시된 버전은 수정할 수 없습니다." />
          <div className="bg-white rounded-xl border border-slate-200 p-6 space-y-3">
            <p className="text-sm text-slate-600">
              게시된 버전은 변경할 수 없습니다. 수정하려면 새 초안 버전을 생성해 주세요.
            </p>
            <div className="flex gap-2">
              <Button icon={<Plus size={13} />} onClick={() => navigate(`/agents/${agent.id}/versions/new/edit`)}>
                새 초안 버전 만들기
              </Button>
              <Button variant="outline" onClick={() => navigate(`/agents/${agent.id}`)}>에이전트 상세로</Button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div>
      <PageHeader
        breadcrumbs={[
          { label: '에이전트 관리', to: '/agents' },
          { label: displayName, to: agentPath },
          { label: isLocalNew || (isNewVersionRoute && !version) ? '새 버전' : readOnly ? `v${version?.version_no} 보기` : `v${version?.version_no ?? '?'} 편집` },
        ]}
        title={
          isLocalNew || (isNewVersionRoute && !version)
            ? '새 에이전트 · 초안'
            : readOnly
              ? `에이전트 버전 보기 (v${version?.version_no})`
              : `에이전트 버전 편집 (v${version?.version_no ?? '새 버전'})`
        }
        actions={
          <>
            <Button variant="outline" onClick={handleCancel}>{readOnly ? '닫기' : '취소'}</Button>
            {canDeleteSavedDraft && (
              <Button variant="outline" icon={<Trash2 size={13} />} onClick={() => setDeleteOpen(true)}>
                삭제
              </Button>
            )}
            {!readOnly && <Button loading={saving} onClick={handleSave}>초안 저장</Button>}
          </>
        }
      />
      <div className="p-6 max-w-5xl space-y-6">
        {isLocalNew && (
          <InlineAlert type="info" message="아직 저장되지 않았습니다. 취소하면 에이전트가 생성되지 않으며, 초안 저장 또는 게시·활성화 시 처음 등록됩니다." />
        )}
        {readOnly ? (
          <InlineAlert type="info" message="이전 버전은 보기만 가능합니다. 수정하려면 새 초안 버전을 생성해 주세요." />
        ) : !isLocalNew ? (
          <InlineAlert type="info" message="변경 내용은 초안 버전으로 저장됩니다. '검증'은 저장된 버전만 확인하며, 실제 사용하려면 '게시·활성화'가 필요합니다." />
        ) : null}
        {formError && <InlineAlert type="error" message={formError} />}

        <Section title="1. 기본 정보">
          <Field label="이름">
            <input value={name} onChange={e => setName(e.target.value)} disabled={readOnly} className={inputClass} />
          </Field>
          <Field label="설명">
            <textarea value={description} onChange={e => setDescription(e.target.value)} disabled={readOnly} rows={2} className={`${inputClass} h-auto py-2 resize-y`} />
          </Field>
          <div className="grid grid-cols-2 gap-4">
            <Field label="공개 범위">
              <select
                value={visibility}
                onChange={e => setVisibility(e.target.value as AgentVisibility)}
                disabled={readOnly}
                className={inputClass}
              >
                {AGENT_VISIBILITIES.map(v => <option key={v} value={v}>{labelAgentVisibility(v)}</option>)}
              </select>
            </Field>
            <Field label="버전 변경 요약 (선택)">
              <input value={changeSummary} onChange={e => setChangeSummary(e.target.value)} disabled={readOnly} placeholder="변경 요약" className={inputClass} />
            </Field>
          </div>
        </Section>

        <Section title="2. 에이전트 업무 지시문" description="에이전트의 역할, 사용할 도구의 범위, 응답 방식과 주의사항을 자연어로 작성하세요. 실행할 때마다 이 지시문을 기준으로 계획을 수립하며, 비밀번호·API 키 등 비밀정보는 적지 마세요.">
          <textarea
            aria-label="에이전트 업무 지시문"
            value={instructions}
            onChange={e => setInstructions(e.target.value)}
            disabled={readOnly}
            placeholder={'예: 당신은 등록된 도구로 필요한 정보를 조회하는 업무 지원 에이전트입니다.\n요청이 모호하면 먼저 질문하고, 조회되지 않은 내용은 추측하지 마세요.'}
            rows={10}
            className={`${inputClass} h-auto py-2 font-mono text-xs leading-relaxed resize-y`}
          />
        </Section>

        <Section title="3. AI 모델 선택" description="자연어를 이해하고 실행 계획을 만드는 데 사용할 LLM 프로필입니다. 프로필 이름이 같아 보여도 설정이 다를 수 있으니 확인해 주세요.">
          <Field label="LLM 모델 프로필">
            <select value={llmProfileId} onChange={e => setLlmProfileId(e.target.value)} disabled={readOnly} className={inputClass}>
              <option value="">LLM 모델 프로필 선택</option>
              {profiles.map(p => (
                <option key={p.id} value={p.id}>{p.name} ({p.model})</option>
              ))}
            </select>
          </Field>
          {profiles.length === 0 && (
            <InlineAlert type="warning" message="등록된 LLM 모델 프로필이 없습니다. 시스템 관리 → 모델 프로필에서 먼저 생성해 주세요." />
          )}
        </Section>

        <Section title={`4. 사용 가능한 MCP 도구 (${selectedTools.length}개 선택)`} description="에이전트가 사용할 수 있는 도구를 체크해 주세요. 선택한 도구만 사용 권한 목록에 포함됩니다. 실제 실행 가능 여부는 서버의 정책·권한 검증을 따릅니다.">
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
            <div className="space-y-3">
              <div className="relative">
                <Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
                <input
                  value={toolSearch}
                  onChange={e => setToolSearch(e.target.value)}
                  placeholder="도구 이름 또는 MCP 서버 검색..."
                  disabled={readOnly}
                  className={`${inputClass} pl-8`}
                />
              </div>
              <select value={serverFilter} onChange={e => setServerFilter(e.target.value)} disabled={readOnly} className={inputClass}>
                <option value="ALL">전체 MCP 서버</option>
                {servers.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <div>
                <p className="text-xs font-medium text-slate-500 mb-2">선택 가능한 도구 (활성 상태)</p>
                <div className="space-y-1.5 max-h-80 overflow-auto border border-slate-200 rounded-lg p-2">
                  {filteredTools.map(tool => {
                    const selected = selectedTools.includes(tool.id);
                    return (
                      <label
                        key={tool.id}
                        className={`flex items-start gap-3 p-2.5 rounded-lg border cursor-pointer ${selected ? 'border-indigo-300 bg-indigo-50/40' : 'border-slate-200 hover:bg-slate-50'} ${readOnly ? 'cursor-default' : ''}`}
                      >
                        <input
                          type="checkbox"
                          checked={selected}
                          onChange={() => toggleTool(tool.id)}
                          disabled={readOnly}
                          className="mt-0.5 accent-indigo-600"
                        />
                        <div className="min-w-0 flex-1">
                          <div className="text-sm font-medium text-slate-800">{tool.display_name ?? tool.remote_name}</div>
                          <div className="text-xs text-slate-400 mt-0.5">
                            {serverNameById[tool.mcp_server_id] ?? tool.mcp_server_id} · {tool.remote_name}
                          </div>
                        </div>
                      </label>
                    );
                  })}
                  {filteredTools.length === 0 && (
                    <p className="text-xs text-slate-400 text-center py-6">조건에 맞는 활성 도구가 없습니다.</p>
                  )}
                </div>
              </div>
            </div>

            <div>
              <p className="text-xs font-medium text-slate-500 mb-2">선택한 도구</p>
              <div className="space-y-1.5 max-h-[28rem] overflow-auto border border-slate-200 rounded-lg p-2 bg-slate-50/50">
                {selectedToolObjs.map(tool => (
                  <div key={tool.id} className="flex items-start gap-3 p-2.5 rounded-lg border border-slate-200 bg-white">
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-slate-800">{tool.display_name ?? tool.remote_name}</div>
                      <div className="text-xs text-slate-400 mt-0.5">
                        도구 식별자: <span className="font-mono">{tool.remote_name}</span>
                      </div>
                    </div>
                    {!readOnly && (
                        <button onClick={() => toggleTool(tool.id)} aria-label={`${tool.display_name ?? tool.remote_name} 선택 해제`} className="p-1 rounded text-slate-400 hover:text-red-500 hover:bg-red-50">
                        <X size={13} />
                      </button>
                    )}
                  </div>
                ))}
                {selectedToolObjs.length === 0 && (
                  <p className="text-xs text-slate-400 text-center py-8">선택된 도구가 없습니다. (도구 없이도 검증은 가능)</p>
                )}
              </div>
            </div>
          </div>
        </Section>

        <Section title="5. 도구 선택 기준 (고급 설정)" description="기본값을 그대로 사용해도 됩니다. 기준값은 0~1 사이의 신뢰도이며, 자동 선택 기준 이상이면 조건에 따라 도구를 자동 선택하고 그보다 낮으면 사용자 확인이 필요할 수 있습니다.">
          <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
            <Field label="자동 선택 기준 (기본 0.82)">
              <input aria-label="자동 선택 기준" type="number" step="0.01" min={0} max={1} value={autoSelectThreshold} disabled={readOnly}
                onChange={e => setAutoSelectThreshold(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="사용자 확인 기준 (기본 0.60)">
              <input aria-label="사용자 확인 기준" type="number" step="0.01" min={0} max={1} value={confirmationThreshold} disabled={readOnly}
                onChange={e => setConfirmationThreshold(Number(e.target.value))} className={inputClass} />
            </Field>
            <Field label="검토할 도구 후보 수 (기본 5)">
              <input aria-label="검토할 도구 후보 수" type="number" min={1} value={maxCandidates} disabled={readOnly}
                onChange={e => setMaxCandidates(Number(e.target.value))} className={inputClass} />
            </Field>
          </div>
        </Section>

        <Section title="6. 검증 및 게시" description="초안 저장은 실제 실행을 시작하지 않습니다. '검증'은 저장된 설정을 서버에서 확인하고, '게시·활성화'는 저장·검증 후 사용할 수 있도록 전환합니다.">
          <div className="space-y-3">
            {clientErrors.length === 0 && validationMessages.length === 0 && !validationOk ? (
              <InlineAlert type="info" message="권장 순서: 초안 저장 → 검증 → 게시·활성화. '게시·활성화'는 저장과 검증도 수행합니다." />
            ) : (
              <div className="space-y-2">
                {clientErrors.map(msg => <InlineAlert key={msg} type="error" message={msg} />)}
                {validationMessages.map(msg => <InlineAlert key={msg} type="error" message={msg} />)}
                {validationOk && <InlineAlert type="info" message={validationOk} />}
              </div>
            )}
            {!readOnly && (
              <div className="flex gap-2 flex-wrap">
                <Button variant="outline" loading={saving} onClick={handleSave}>초안 저장</Button>
                <Button
                  variant="outline"
                  loading={validating}
                  disabled={clientErrors.length > 0}
                  onClick={handleValidate}
                >
                  검증
                </Button>
                <Button loading={publishing} disabled={clientErrors.length > 0} onClick={handlePublish}>
                  게시·활성화
                </Button>
              </div>
            )}
            {readOnly && agent && (
              <Button icon={<Plus size={13} />} onClick={() => navigate(`/agents/${agent.id}/versions/new/edit`)}>
                새 초안 버전 만들기
              </Button>
            )}
          </div>
        </Section>
      </div>

      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={handleDelete}
        title="에이전트 삭제"
        description="이 초안 에이전트를 삭제합니다. 게시 이력이 없는 본인 소유의 초안만 삭제할 수 있습니다."
        confirmLabel="삭제"
        danger
        loading={deleting}
      />
    </div>
  );
}

function Section({ title, description, children }: { title: string; description?: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className={`text-sm font-semibold text-slate-800 ${description ? '' : 'mb-4'}`}>{title}</h3>
      {description && <p className="text-xs text-slate-500 mt-1 mb-4 leading-relaxed">{description}</p>}
      <div className="space-y-4">{children}</div>
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs font-medium text-slate-600 mb-1.5">{label}</label>
      {children}
    </div>
  );
}

const inputClass = 'w-full h-9 px-3 text-sm border border-slate-200 rounded-md focus:outline-none focus:ring-2 focus:ring-indigo-500 focus:border-transparent bg-white disabled:bg-slate-50 disabled:text-slate-500';
