import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import 'leaflet/dist/leaflet.css'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { ApiError } from './api/client'
import App from './App'
import './index.css'

const MAX_RETRIES = 2

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Client errors (404, 422, …) will not fix themselves; only retry network/5xx failures.
      retry: (failureCount, error) =>
        !(error instanceof ApiError && error.status >= 400 && error.status < 500) && failureCount < MAX_RETRIES,
      refetchOnWindowFocus: false,
    },
  },
})

const rootElement = document.getElementById('root')
if (!rootElement) throw new Error('Root element #root not found')

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
)
