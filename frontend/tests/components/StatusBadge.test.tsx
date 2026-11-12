import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import StatusBadge, {
  RiskBadge, VerificationBadge, VersionBadge,
} from '@/components/ui/StatusBadge';

describe('StatusBadge Korean presentation (canonical status preserved)', () => {
  const cases: Array<{ status: string; label: string }> = [
    { status: 'PENDING', label: '대기' },
    { status: 'RUNNING', label: '실행 중' },
    { status: 'WAITING_INPUT', label: '입력 대기' },
    { status: 'WAITING_APPROVAL', label: '승인 대기' },
    { status: 'SUCCEEDED', label: '성공' },
    { status: 'PARTIALLY_SUCCEEDED', label: '일부 성공' },
    { status: 'FAILED', label: '실패' },
    { status: 'CANCELLED', label: '취소됨' },
    { status: 'UNKNOWN_OUTCOME', label: '결과 확인 필요' },
    { status: 'PUBLISHED', label: '게시됨' },
    { status: 'DEPRECATED', label: '이전 버전' },
    { status: 'ACTIVE', label: '활성' },
    { status: 'INACTIVE', label: '비활성' },
    { status: 'BLOCKED', label: '차단됨' },
    { status: 'MISSING', label: '누락' },
  ];

  it('animates a live RUNNING badge by default', () => {
    const { container } = render(<StatusBadge status="RUNNING" />);
    expect(container.querySelectorAll('.animate-spin')).toHaveLength(1);
  });

  it('renders a static RUNNING badge for immutable history', () => {
    const { container } = render(<StatusBadge status="RUNNING" animate={false} />);
    expect(screen.getByText('실행 중')).toBeInTheDocument();
    expect(container.querySelector('.animate-spin')).toBeNull();
  });

  for (const { status, label } of cases) {
    it(`renders Korean text for ${status} while retaining canonical hover code`, () => {
      render(<StatusBadge status={status} />);
      expect(screen.getByText(label)).toHaveAttribute('title', status);
    });
  }

  it('preserves an explicit display-label override and canonical status', () => {
    render(<StatusBadge status="SUCCEEDED" label="요청 처리됨" />);
    expect(screen.getByText('요청 처리됨')).toHaveAttribute('title', 'SUCCEEDED');
  });

  it('keeps unknown statuses visible without translating or inventing values', () => {
    render(<StatusBadge status="FUTURE_STATE" />);
    expect(screen.getByText('FUTURE STATE')).toHaveAttribute('title', 'FUTURE_STATE');
  });

  it('keeps risk classes canonical while showing Korean descriptions', () => {
    render(<RiskBadge risk="NON_IDEMPOTENT_WRITE" />);
    expect(screen.getByText('중복 실행 위험')).toHaveAttribute('title', 'NON_IDEMPOTENT_WRITE');
  });

  it('shows Korean tool-verification status and preserves canonical code', () => {
    render(<VerificationBadge status="VERIFIED" />);
    expect(screen.getByText('검증 완료')).toHaveAttribute('title', 'VERIFIED');
  });

  it('keeps the version badge version identifier unchanged', () => {
    render(<VersionBadge version="v1" status="PUBLISHED" />);
    expect(screen.getByText('v1')).toBeInTheDocument();
  });
});
