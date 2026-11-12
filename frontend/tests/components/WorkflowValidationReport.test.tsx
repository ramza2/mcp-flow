import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import WorkflowValidationReport, {
  isCurrentWorkflowValidation,
} from '@/screens/build/WorkflowValidationReport';

const hash = 'saved-plan-hash';
const successReport = {
  valid: true,
  content_hash: hash,
  checks: {
    plan_schema: 'OK',
    static_plan: 'OK',
    tool_versions: 'OK',
    approval_policies: 'OK',
  },
  errors: [],
  schema_version: '1.0',
};

describe('Workflow validation result — Korean beginner view', () => {
  it('shows success only with exact server status and matching saved content hash', () => {
    expect(isCurrentWorkflowValidation('VALID', successReport, hash)).toBe(true);
    expect(isCurrentWorkflowValidation('INVALID', successReport, hash)).toBe(false);
    expect(isCurrentWorkflowValidation('VALID', successReport, 'different')).toBe(false);
    expect(isCurrentWorkflowValidation('VALID', null, hash)).toBe(false);
    render(<WorkflowValidationReport report={successReport} status="VALID" contentHash={hash} dirty={false} />);
    expect(screen.getByRole('heading', { name: '워크플로 검증 완료' })).toBeInTheDocument();
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('계획 데이터 형식');
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('실행 단계 및 연결');
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('도구 버전');
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('승인 정책');
    const details = screen.getByText('전체 서버 검증 보고서 (JSON)').closest('details');
    expect(details).not.toBeNull();
    expect(details).not.toHaveAttribute('open');
    expect(details?.querySelector('pre')).toHaveTextContent('"content_hash": "saved-plan-hash"');
  });

  it('never calls an outdated VALID report a passing validation', () => {
    render(<WorkflowValidationReport report={successReport} status="VALID" contentHash="different" dirty={false} />);
    expect(screen.getByText('검증 결과를 다시 확인해 주세요')).toBeInTheDocument();
    expect(screen.queryByText('워크플로 검증 완료')).not.toBeInTheDocument();
  });

  it('labels old evidence as old when the editor has unsaved changes', () => {
    render(<WorkflowValidationReport report={successReport} status="VALID" contentHash={hash} dirty />);
    expect(screen.getByText('변경된 초안은 아직 검증되지 않았습니다')).toBeInTheDocument();
    expect(screen.getByText(/저장된 이전 계획/)).toBeInTheDocument();
    expect(screen.queryByText('워크플로 검증 완료')).not.toBeInTheDocument();
  });

  it('summarizes server errors without dropping unknown report fields', () => {
    const report = {
      valid: false,
      content_hash: hash,
      checks: { plan_schema: 'OK', static_plan: 'FAILED', other_check: { reason: 'needs-review' } },
      errors: [
        { path: '/steps/0', message: '도구 버전이 없습니다.', code: 'TOOL_VERSION_MISSING' },
        '연결을 검토하세요.',
        { unexpected: true },
      ],
      extra_metadata: { retained: true },
    };
    render(<WorkflowValidationReport report={report} status="INVALID" contentHash={hash} dirty={false} />);
    expect(screen.getByRole('heading', { name: '워크플로 검증 실패' })).toBeInTheDocument();
    expect(screen.getByText('검증 오류 3건')).toBeInTheDocument();
    expect(screen.getByText('/steps/0 · 도구 버전이 없습니다.')).toBeInTheDocument();
    expect(screen.getByText('연결을 검토하세요.')).toBeInTheDocument();
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('other_check');
    expect(screen.getByLabelText('서버 검증 항목')).toHaveTextContent('확인 필요');
    const details = screen.getByText('전체 서버 검증 보고서 (JSON)').closest('details');
    expect(details?.querySelector('pre')).toHaveTextContent('"extra_metadata"');
  });

  it('does not show a valid-looking result for an INVALID status with old success evidence', () => {
    render(<WorkflowValidationReport report={successReport} status="INVALID" contentHash={hash} dirty={false} />);
    expect(screen.getByText('검증 결과를 다시 확인해 주세요')).toBeInTheDocument();
    expect(screen.queryByText('워크플로 검증 완료')).not.toBeInTheDocument();
    expect(screen.queryByText('워크플로 검증 실패')).not.toBeInTheDocument();
  });

  it('supports sparse/unknown server report shapes without throwing', () => {
    render(<WorkflowValidationReport report={{ valid: false, unexpected: null }} status="INVALID" contentHash={hash} dirty={false} />);
    expect(screen.getByText('워크플로 검증 실패')).toBeInTheDocument();
    expect(screen.queryByLabelText('서버 검증 항목')).not.toBeInTheDocument();
  });
});
