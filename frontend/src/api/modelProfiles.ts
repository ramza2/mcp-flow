/** Model profile API helpers — docs/06 / backend model_profiles router. */

import { apiRequest } from './client';
import type { ListParams, LLMProfileListDto } from './types';

export function listLLMProfiles(params: ListParams = {}) {
  return apiRequest<LLMProfileListDto>('/model-profiles/llm', {
    query: {
      page: params.page ?? 1,
      page_size: params.page_size ?? 100,
      sort: params.sort ?? 'name',
      q: params.q,
    },
    signal: params.signal,
  });
}
