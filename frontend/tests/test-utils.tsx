import { render, type RenderOptions } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import type { ReactElement, ReactNode } from 'react';
import { AuthContext, type AuthContextValue } from '../src/auth/AuthProvider';
import { buildAuthContext } from './fixtures/auth-context';
import { mockEffectivePermissions } from './fixtures/auth-api';

type RouteDef = {
  path: string;
  element: ReactNode;
};

const defaultTestAuth = (): AuthContextValue =>
  buildAuthContext({ permissions: mockEffectivePermissions.permission_codes });

/**
 * Router render helper. Wraps with AuthContext so PermissionGate / useAuth screens
 * work without a live AuthProvider bootstrap (tests may override authValue).
 */
export function renderWithRouter(
  ui: ReactElement,
  {
    route = '/',
    path = '/',
    routes,
    authValue,
    ...options
  }: RenderOptions & {
    route?: string;
    path?: string;
    routes?: RouteDef[];
    /** Override session permissions / auth context for UX gates. */
    authValue?: AuthContextValue;
  } = {},
) {
  const auth = authValue ?? defaultTestAuth();
  const router = createMemoryRouter(
    routes ?? [{ path, element: ui }],
    { initialEntries: [route] },
  );
  return {
    ...render(
      <AuthContext.Provider value={auth}>
        <RouterProvider router={router} />
      </AuthContext.Provider>,
      options,
    ),
    router,
    auth,
  };
}
