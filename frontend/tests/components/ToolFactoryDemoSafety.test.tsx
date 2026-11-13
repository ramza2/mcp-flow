import { describe, expect, it } from 'vitest';
import { screen } from '@testing-library/react';
import ToolFactory from '@/screens/mcp/ToolFactory';
import ToolFactoryNew from '@/screens/mcp/ToolFactoryNew';
import FactoryBuildDetail from '@/screens/mcp/FactoryBuildDetail';
import { renderWithRouter } from '../test-utils';

describe('Tool Factory demo safety', () => {
  it('labels the project listing as sample data, not operational Factory results', () => {
    renderWithRouter(<ToolFactory />, { path: '/tool-factory', route: '/tool-factory' });
    expect(screen.getByRole('heading', { name: '도구 생성 (시연 화면)' })).toBeInTheDocument();
    expect(screen.getByText(/이 목록은 고정된 예시 데이터입니다/)).toBeInTheDocument();
    expect(screen.getByText('HR API Tools')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '실제 MCP 서버 등록' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '새 Tool 생성' })).not.toBeInTheDocument();
  });

  it('does not pretend to upload, build, validate, or publish a Tool', () => {
    renderWithRouter(<ToolFactoryNew />, { path: '/tool-factory/new', route: '/tool-factory/new' });
    expect(screen.getByRole('heading', { name: '도구 생성 절차 안내' })).toBeInTheDocument();
    expect(screen.getByText(/실제 Tool Factory API와 연결되지 않았습니다/)).toBeInTheDocument();
    expect(screen.getByText('보안·계약 테스트')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '실제 MCP 서버 등록' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /빌드 시작|게시 완료|Publish/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('shows demo-only pipeline status for known sample IDs, without fabricated logs', () => {
    renderWithRouter(
      <FactoryBuildDetail />,
      { path: '/tool-factory/:buildId', route: '/tool-factory/fac-002' },
    );
    expect(screen.getByRole('heading', { name: 'Finance Report Tools (예시)' })).toBeInTheDocument();
    expect(screen.getByText(/아래 상태는 실제 빌드·테스트·검토·게시 결과가 아닌/)).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '예시 진행 상태' })).toBeInTheDocument();
    expect(screen.queryByText(/Build started: Finance Report Tools/)).not.toBeInTheDocument();
  });

  it('does not substitute a different sample project for an unknown ID', () => {
    renderWithRouter(
      <FactoryBuildDetail />,
      { path: '/tool-factory/:buildId', route: '/tool-factory/not-a-sample' },
    );
    expect(screen.getByRole('heading', { name: '예시 프로젝트를 찾을 수 없습니다' })).toBeInTheDocument();
    expect(screen.queryByText('Finance Report Tools (예시)')).not.toBeInTheDocument();
  });
});
