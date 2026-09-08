import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { clearCsrfCache, setCachedCsrfTokenForTests } from '../src/api/csrf';

beforeEach(() => {
  // Seed CSRF so existing MCP UI tests that stub only business endpoints keep working.
  // Auth/CSRF-specific tests call clearCsrfCache() themselves when they need a real fetch.
  setCachedCsrfTokenForTests('test-csrf-token');
});

afterEach(() => {
  cleanup();
  clearCsrfCache();
});

// jsdom does not implement scrollIntoView used by chat UIs
Element.prototype.scrollIntoView = vi.fn();
