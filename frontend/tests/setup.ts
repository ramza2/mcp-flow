import '@testing-library/jest-dom/vitest';
import { vi } from 'vitest';

// jsdom does not implement scrollIntoView used by chat UIs
Element.prototype.scrollIntoView = vi.fn();
