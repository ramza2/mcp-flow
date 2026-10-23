import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';
import { ArrowLeft, Pencil, Plus, Rocket, Power, Trash2 } from 'lucide-react';
import StatusBadge, { VersionBadge } from '../../components/ui/StatusBadge';
import { TabBar } from '../../components/ui/Tabs';
import Button from '../../components/ui/Button';
import DataTable, { Column } from '../../components/ui/DataTable';
import { ConfirmDialog } from '../../components/ui/Dialog';
import { ErrorState, InlineAlert, LoadingSkeleton } from '../../components/ui/EmptyState';
import {
  activateAgent,
  deleteAgent,
  getAgent,
  listAgentToolGrants,
  listAgentVersions,
  publishAgentVersion,
  validateAgentVersion,
} from '../../api/agents';
import { isAbortError, isApiError } from '../../api/client';
import type { AgentDto, AgentToolGrantDto, AgentVersionDto } from '../../api/types';
import { formatTimestamp } from '../../domain';

type VersionStatus = 'DRAFT' | 'PUBLISHED' | 'DEPRECATED';

export default function AgentDetail() {
  const { agentId } = useParams();
  const navigate = useNavigate();
  const [tab, setTab] = useState('overview');
  const [agent, setAgent] = useState<AgentDto | null>(null);
  const [versions, setVersions] = useState<AgentVersionDto[]>([]);
  const [grants, setGrants] = useState<AgentToolGrantDto[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionBusy, setActionBusy] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [error, setError] = useState<{ message: string; requestId?: string } | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(() => {
    if (!agentId) return () => {};
    const controller = new AbortController();
    let cancelled = false;
    setLoading(true);
    setError(null);

    Promise.all([
      getAgent(agentId, controller.signal),
      listAgentVersions(agentId, { page_size: 50, sort: '-version_no', signal: controller.signal }),
    ])
      .then(async ([agentRow, versionPage]) => {
        if (cancelled) return;
        setAgent(agentRow);
        setVersions(versionPage.items);
        const draftOrCurrent = versionPage.items.find(v => v.status === 'DRAFT')
          ?? versionPage.items.find(v => v.id === agentRow.current_version_id)
          ?? versionPage.items[0];
        if (draftOrCurrent) {
          try {
            const grantPage = await listAgentToolGrants(
              agentId,
              draftOrCurrent.id,
              controller.signal,
            );
            if (!cancelled) setGrants(grantPage.items);
          } catch {
            if (!cancelled) setGrants([]);
          }
        } else {
          setGrants([]);
        }
      })
      .catch(err => {
        if (isAbortError(err) || cancelled) return;
        const apiErr = isApiError(err) ? err : null;
        setError({
          message: apiErr?.message ?? 'Agent를 불러오지 못했습니다.',
          requestId: apiErr?.requestId ?? undefined,
        });
        setAgent(null);
        setVersions([]);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [agentId]);

  useEffect(() => load(), [load]);

  const latest = versions[0] ?? null;
  const latestStatus = (latest?.status ?? 'DRAFT') as VersionStatus;
  const currentPublished = versions.find(v => v.id === agent?.current_version_id) ?? null;
  const hasPublishHistory = versions.some(v =>
    v.status === 'PUBLISHED' || v.status === 'DEPRECATED' || v.published_at != null,
  );
  const canDelete = Boolean(
    agent
    && agent.status === 'DRAFT'
    && agent.current_version_id == null
    && !hasPublishHistory,
  );

  const handleLatestPrimary = () => {
    if (!agent) return;
    if (!latest || latestStatus !== 'DRAFT') {
      navigate(`/agents/${agent.id}/versions/new/edit`);
      return;
    }
    navigate(`/agents/${agent.id}/versions/${latest.id}/edit`);
  };

  const handlePublish = async () => {
    if (!agent || !latest || latest.status !== 'DRAFT') return;
    setActionBusy(true);
    setActionError(null);
    try {
      const validated = await validateAgentVersion(agent.id, latest.id);
      if (validated.validation_status !== 'VALID') {
        const report = validated.validation_report as { errors?: Array<{ message?: string }> } | null;
        const messages = report?.errors?.map(e => e.message).filter(Boolean) ?? [];
        setActionError(messages[0] ?? '버전 검증에 실패했습니다. VALID 이후에 Publish할 수 있습니다.');
        await load();
        return;
      }
      await publishAgentVersion(agent.id, latest.id);
      await load();
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setActionError(apiErr?.message ?? 'Publish에 실패했습니다.');
    } finally {
      setActionBusy(false);
    }
  };

  const handleActivate = async () => {
    if (!agent) return;
    setActionBusy(true);
    setActionError(null);
    try {
      const updated = await activateAgent(agent.id, agent.lock_version);
      setAgent(updated);
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setActionError(apiErr?.message ?? '활성화에 실패했습니다.');
    } finally {
      setActionBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!agent || !canDelete) return;
    setDeleting(true);
    setActionError(null);
    try {
      await deleteAgent(agent.id);
      setDeleteOpen(false);
      navigate('/agents');
    } catch (err) {
      const apiErr = isApiError(err) ? err : null;
      setActionError(apiErr?.message ?? 'Agent 삭제에 실패했습니다.');
      setDeleteOpen(false);
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return (
      <div className="p-6">
        <LoadingSkeleton rows={8} />
      </div>
    );
  }

  if (error || !agent) {
    return (
      <div className="p-6">
        <ErrorState
          message={error?.message ?? `Agent를 찾을 수 없습니다: ${agentId ?? '(missing id)'}`}
          requestId={error?.requestId}
          onRetry={() => { load(); }}
        />
        <div className="mt-4">
          <Button variant="outline" onClick={() => navigate('/agents')}>Agents로 돌아가기</Button>
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="bg-white border-b border-slate-200 px-6 py-4">
        <button onClick={() => navigate('/agents')} className="flex items-center gap-1.5 text-sm text-slate-500 hover:text-slate-700 mb-3">
          <ArrowLeft size={14} /> Agents
        </button>
        <div className="flex items-start justify-between">
          <div>
            <h1 className="text-lg font-semibold text-slate-900">{agent.name}</h1>
            <div className="flex items-center gap-3 mt-1">
              <StatusBadge status={agent.status} />
              {currentPublished && (
                <VersionBadge version={`v${currentPublished.version_no}`} status="PUBLISHED" />
              )}
              <span className="text-xs text-slate-400">{agent.description ?? ''}</span>
            </div>
          </div>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              icon={latestStatus === 'DRAFT' ? <Pencil size={13} /> : <Plus size={13} />}
              onClick={handleLatestPrimary}
            >
              {latestStatus === 'DRAFT' ? '최신 버전 편집' : 'Create New Draft'}
            </Button>
            <Button
              size="sm"
              icon={<Rocket size={13} />}
              disabled={latestStatus !== 'DRAFT' || actionBusy}
              loading={actionBusy && latestStatus === 'DRAFT'}
              onClick={handlePublish}
            >
              Publish
            </Button>
            <Button
              size="sm"
              variant="outline"
              icon={<Power size={13} />}
              disabled={agent.status === 'ACTIVE' || !agent.current_version_id || actionBusy}
              loading={actionBusy && agent.status !== 'ACTIVE'}
              onClick={handleActivate}
            >
              Activate
            </Button>
            {canDelete && (
              <Button
                size="sm"
                variant="outline"
                icon={<Trash2 size={13} />}
                onClick={() => setDeleteOpen(true)}
              >
                삭제
              </Button>
            )}
          </div>
        </div>
        {actionError && (
          <div className="mt-3">
            <InlineAlert type="error" message={actionError} />
          </div>
        )}
      </div>

      <div className="bg-white border-b border-slate-200 px-6">
        <TabBar
          tabs={[
            { id: 'overview', label: 'Overview' },
            { id: 'versions', label: 'Versions', badge: versions.length },
            { id: 'tools', label: 'Allowed Tools', badge: grants.length },
            { id: 'instructions', label: 'Instructions' },
          ]}
          activeTab={tab}
          onChange={setTab}
        />
      </div>

      <div className="p-6">
        {tab === 'overview' && (
          <div className="grid grid-cols-2 gap-4 max-w-3xl">
            <InfoCard title="기본 정보">
              <Row label="Code">{agent.code}</Row>
              <Row label="Visibility">{agent.visibility}</Row>
              <Row label="Owner" mono>{agent.owner_id ?? '—'}</Row>
              <Row label="Description">{agent.description ?? '—'}</Row>
            </InfoCard>
            <InfoCard title="버전 상태">
              <Row label="Published" mono>
                {currentPublished ? `v${currentPublished.version_no}` : '—'}
              </Row>
              <Row label="Current Version Id" mono>{agent.current_version_id ?? '—'}</Row>
              <Row label="Created">{formatTimestamp(agent.created_at)}</Row>
              <Row label="Updated">{formatTimestamp(agent.updated_at)}</Row>
            </InfoCard>
          </div>
        )}

        {tab === 'versions' && (
          <div className="space-y-3 max-w-4xl">
            <InlineAlert
              type="info"
              message="편집은 DRAFT Version에만 적용됩니다. PUBLISHED는 View 또는 Create New Draft Version만 가능하며, DEPRECATED는 View만 가능합니다."
            />
            <div className="bg-white rounded-xl border border-slate-200 overflow-hidden">
              <DataTable
                columns={versionCols(navigate, agent.id)}
                data={versions}
                rowKey={r => r.id}
                onRowClick={r => navigate(`/agents/${agent.id}/versions/${r.id}/edit`)}
                emptyMessage="버전이 없습니다. Create New Draft로 첫 버전을 만드세요."
              />
            </div>
          </div>
        )}

        {tab === 'tools' && (
          <div className="max-w-4xl bg-white rounded-xl border border-slate-200 overflow-hidden">
            <DataTable
              columns={grantCols}
              data={grants}
              rowKey={r => r.mcp_tool_id}
              emptyMessage="허용된 Tool이 없습니다."
            />
          </div>
        )}

        {tab === 'instructions' && (
          <div className="max-w-3xl space-y-3">
            <InlineAlert type="info" message="Instructions는 Agent가 Tool을 선택하고 실행 계획을 세울 때 참조하는 시스템 프롬프트입니다." />
            <pre className="bg-white border border-slate-200 rounded-xl p-4 text-sm text-slate-700 whitespace-pre-wrap font-sans leading-relaxed">
              {latest?.system_instruction || '—'}
            </pre>
          </div>
        )}
      </div>

      <ConfirmDialog
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        onConfirm={handleDelete}
        title="Agent 삭제"
        description="이 DRAFT Agent를 삭제합니다. 게시 이력이 없는 본인 소유 DRAFT만 삭제할 수 있습니다."
        confirmLabel="삭제"
        danger
        loading={deleting}
      />
    </div>
  );
}

function versionCols(navigate: (to: string) => void, agentId: string): Column<AgentVersionDto>[] {
  return [
    { key: 'version', label: 'Version', render: r => <VersionBadge version={`v${r.version_no}`} status={r.status} /> },
    { key: 'status', label: '상태', render: r => <StatusBadge status={r.status} size="sm" /> },
    { key: 'changeSummary', label: 'Change Summary', render: r => <span className="text-sm text-slate-700">{r.change_summary ?? '—'}</span> },
    { key: 'validation', label: 'Validation', render: r => <StatusBadge status={r.validation_status} size="sm" /> },
    { key: 'createdAt', label: 'Created', render: r => <span className="text-xs text-slate-400">{formatTimestamp(r.created_at)}</span> },
    { key: 'actions', label: '', render: r => {
      const status = r.status as VersionStatus;
      return (
        <div className="flex items-center gap-1" onClick={e => e.stopPropagation()}>
          <button
            onClick={() => navigate(`/agents/${agentId}/versions/${r.id}/edit`)}
            className="p-1.5 rounded text-slate-400 hover:text-indigo-600 hover:bg-indigo-50"
            title={status === 'DRAFT' ? '편집' : '보기'}
          >
            <Pencil size={13} />
          </button>
        </div>
      );
    }},
  ];
}

const grantCols: Column<AgentToolGrantDto>[] = [
  { key: 'tool', label: 'Tool Id', render: r => <span className="font-mono text-xs text-slate-700">{r.mcp_tool_id}</span> },
  { key: 'effect', label: 'Effect', render: r => <span className="text-xs text-slate-600">{r.effect}</span> },
  { key: 'confirm', label: 'Confirmation', render: r => (
    <span className="text-xs text-slate-500">{r.requires_confirmation ? 'required' : '—'}</span>
  ) },
];

function InfoCard({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-white rounded-xl border border-slate-200 p-4">
      <h3 className="text-sm font-semibold text-slate-800 mb-3">{title}</h3>
      <div className="space-y-2">{children}</div>
    </div>
  );
}

function Row({
  label,
  children,
  mono,
}: {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3 text-sm">
      <span className="text-slate-500 shrink-0">{label}</span>
      <span className={`text-slate-800 text-right ${mono ? 'font-mono text-xs break-all' : ''}`}>{children}</span>
    </div>
  );
}
