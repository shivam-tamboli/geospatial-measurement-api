import type { Geometry } from 'geojson'

export type FileStatus = 'PENDING' | 'PROCESSING' | 'COMPLETED' | 'FAILED'

export type JsonValue = string | number | boolean | null | JsonValue[] | { [key: string]: JsonValue }

/** Error body returned by the API for every non-2xx response. */
export interface ApiErrorBody {
  detail: string
  code: string
}

export interface FileUploadResponse {
  id: string
  filename: string
  status: FileStatus
}

export interface FileInfo {
  id: string
  filename: string
  feature_count: number | null
  crs: string | null
  status: FileStatus
  error: string | null
  warnings: string[]
  file_size: number
  created_at: string
  updated_at: string
}

export interface FileSummary {
  id: string
  filename: string
  status: FileStatus
  feature_count: number | null
  crs: string | null
  created_at: string
}

export interface FileListPage {
  page: number
  page_size: number
  total: number
  total_pages: number
  items: FileSummary[]
}

export interface Measurement {
  type: 'area' | 'length' | null
  value: number | null
  unit: string | null
  projected_crs: string | null
}

export interface FeatureMeasurement {
  feature_id: number
  geometry_type: string
  geometry: string | null
  /** 2D GeoJSON geometry in WGS84, or null for features without usable geometry. */
  geometry_geojson: Geometry | null
  crs: string
  properties: Record<string, JsonValue>
  measurement: Measurement
}

export interface MeasurementsPage {
  file_id: string
  page: number
  page_size: number
  total: number
  total_pages: number
  items: FeatureMeasurement[]
}
