import type {
  ApiErrorBody,
  FileInfo,
  FileListPage,
  FileUploadResponse,
  MeasurementsPage,
} from './types'

const API_BASE_URL = (
  import.meta.env.VITE_API_BASE_URL ?? (import.meta.env.DEV ? 'http://localhost:8000' : '')
).replace(/\/+$/, '')

/** An error from the API (or from reaching it). `status` is 0 for network failures. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(message: string, status: number, code: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

function isApiErrorBody(value: unknown): value is ApiErrorBody {
  return (
    typeof value === 'object' &&
    value !== null &&
    'detail' in value &&
    typeof value.detail === 'string' &&
    'code' in value &&
    typeof value.code === 'string'
  )
}

async function toApiError(response: Response): Promise<ApiError> {
  try {
    const body: unknown = await response.json()
    if (isApiErrorBody(body)) return new ApiError(body.detail, response.status, body.code)
  } catch {
    // Not JSON (e.g. a proxy error page): fall through to the generic message.
  }
  return new ApiError(`Request failed (HTTP ${response.status}).`, response.status, 'HTTP_ERROR')
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE_URL}${path}`, init)
  } catch {
    throw new ApiError(
      'Cannot reach the server. Check your connection and that the API is running.',
      0,
      'NETWORK_ERROR',
    )
  }
  if (!response.ok) throw await toApiError(response)
  try {
    const data: unknown = await response.json()
    return data as T
  } catch {
    throw new ApiError('The server returned an unreadable response.', response.status, 'INVALID_RESPONSE')
  }
}

export function uploadFile(file: File): Promise<FileUploadResponse> {
  const body = new FormData()
  body.append('file', file)
  return request<FileUploadResponse>('/api/files/', { method: 'POST', body })
}

export function getFile(fileId: string): Promise<FileInfo> {
  return request<FileInfo>(`/api/files/${encodeURIComponent(fileId)}/`)
}

export function listFiles(page: number, pageSize: number): Promise<FileListPage> {
  return request<FileListPage>(`/api/files/?page=${page}&page_size=${pageSize}`)
}

export function getMeasurements(
  fileId: string,
  page: number,
  pageSize: number,
): Promise<MeasurementsPage> {
  return request<MeasurementsPage>(
    `/api/files/${encodeURIComponent(fileId)}/measurements/?page=${page}&page_size=${pageSize}`,
  )
}
