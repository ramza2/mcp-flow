import { describe, expect, it } from 'vitest';
import { screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import MCPServerNew from '@/screens/mcp/MCPServerNew';
import { CURRENT_MCP_PROTOCOL_VERSION } from '@/domain';
import { renderWithRouter } from '../test-utils';

function footerNext(): HTMLButtonElement {
  const buttons = screen.getAllByRole('button', { name: /^다음$/ });
  const enabled = buttons.find(b => !(b as HTMLButtonElement).disabled) as HTMLButtonElement | undefined;
  if (!enabled) throw new Error(`No enabled Next button among ${buttons.length}`);
  return enabled;
}

describe('MCP Server New — Current MCP contract', () => {
  it('shows Current MCP 2026-07-28, INFERRED_CURRENT, optional server/discover', async () => {
    const user = userEvent.setup();
    renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
    });

    await user.click(footerNext());
    expect(screen.getByText('Transport Type')).toBeInTheDocument();
    await user.click(footerNext());
    expect(screen.getByText('인증 방식')).toBeInTheDocument();
    await user.click(footerNext());
    expect(screen.getByText(/연결 테스트 실행/i)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /연결 테스트 실행/i }));
    await screen.findByText(/연결 성공/i, {}, { timeout: 5000 });
    await user.click(footerNext());

    expect(screen.getByText('Protocol Version')).toBeInTheDocument();
    expect(screen.getByText(CURRENT_MCP_PROTOCOL_VERSION)).toBeInTheDocument();
    expect(CURRENT_MCP_PROTOCOL_VERSION).toBe('2026-07-28');
    expect(screen.getByText('INFERRED_CURRENT')).toBeInTheDocument();
    expect(screen.getByText(/server\/discover는 optional/i)).toBeInTheDocument();
    expect(screen.queryByText(/initialize 응답에서 Tool 목록 추론/i)).not.toBeInTheDocument();
  });

  it('uses LEGACY_HANDSHAKE when Legacy transport is selected', async () => {
    const user = userEvent.setup();
    const view = renderWithRouter(<MCPServerNew />, {
      path: '/mcp/servers/new',
      route: '/mcp/servers/new',
    });

    const nextInView = () => {
      const buttons = within(view.container).getAllByRole('button', { name: /^다음$/ });
      const enabled = buttons.find(b => !(b as HTMLButtonElement).disabled) as HTMLButtonElement | undefined;
      if (!enabled) throw new Error(`No enabled Next in view (${buttons.length})`);
      return enabled;
    };

    await user.click(nextInView());
    expect(within(view.container).getByText('Transport Type')).toBeInTheDocument();

    await user.click(within(view.container).getByText('Legacy HTTP/SSE'));
    expect(within(view.container).getByText(/Legacy MCP 지원/i)).toBeInTheDocument();

    await user.click(nextInView()); // Auth
    await user.click(nextInView()); // Connection
    await user.click(within(view.container).getByRole('button', { name: /연결 테스트 실행/i }));
    await within(view.container).findByText(/연결 성공/i, {}, { timeout: 5000 });
    await user.click(nextInView());

    expect(within(view.container).getByText('LEGACY_HANDSHAKE')).toBeInTheDocument();
    expect(within(view.container).getByText(/LEGACY_HANDSHAKE Discovery Mode/i)).toBeInTheDocument();
  });
});
