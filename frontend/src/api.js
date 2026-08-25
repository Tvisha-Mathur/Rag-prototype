/** Purpose: Configures frontend HTTP access to the backend API. Used by React components that start and update incident workflows. */

import axios from 'axios';

export const api = axios.create({
  baseURL: import.meta.env.VITE_API_URL || '/api',
  // Full RAG analysis can include several sequential local-model calls.
  // Let FastAPI/Ollama enforce their operation-specific timeouts instead of
  // aborting a valid response in the browser while the backend is still busy.
  timeout: 0,
  headers: { 'Content-Type': 'application/json' },
});

export function apiErrorMessage(error, fallback) {
  const detail = error.response?.data?.detail;
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).join(', ');
  if (error.code === 'ECONNABORTED') return 'The backend analysis exceeded its configured model timeout.';
  if (!error.response) return 'Cannot reach the RAG API. Make sure the backend is running.';
  return fallback;
}
