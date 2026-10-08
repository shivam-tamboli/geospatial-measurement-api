/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Backend base URL; see frontend/.env.example. */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
