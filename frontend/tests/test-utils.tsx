import { render, type RenderOptions } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import type { ReactElement, ReactNode } from 'react';

type RouteDef = {
  path: string;
  element: ReactNode;
};

export function renderWithRouter(
  ui: ReactElement,
  {
    route = '/',
    path = '/',
    routes,
    ...options
  }: RenderOptions & {
    route?: string;
    path?: string;
    routes?: RouteDef[];
  } = {},
) {
  const router = createMemoryRouter(
    routes ?? [{ path, element: ui }],
    { initialEntries: [route] },
  );
  return {
    ...render(<RouterProvider router={router} />, options),
    router,
  };
}
