import type { JsonValue, Measurement } from '../api/types'

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

/** "1,090,165.16 m²", or null when the feature has no measurement (e.g. a Point). */
export function formatMeasurement(m: Measurement): string | null {
  if (m.value === null || m.unit === null) return null
  return `${m.value.toLocaleString(undefined, { maximumFractionDigits: 2 })} ${m.unit}`
}

export function formatPropertyValue(value: JsonValue): string {
  if (value === null) return '—'
  return typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export function formatDateTime(iso: string): string {
  const date = new Date(iso)
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString()
}

const ALLOWED_EXTENSIONS = ['.zip', '.kml'] as const

/** Client-side file check; returns an error message, or null when the file is acceptable. */
export function validateUpload(file: File): string | null {
  const name = file.name.toLowerCase()
  if (!ALLOWED_EXTENSIONS.some((ext) => name.endsWith(ext))) {
    return 'Unsupported file type. Upload a .zip (Shapefile) or a .kml file.'
  }
  if (file.size === 0) return 'This file is empty.'
  return null
}

export const ACCEPT_ATTRIBUTE = ALLOWED_EXTENSIONS.join(',')

/** Stroke/fill colour for a geometry type. */
export function geometryColor(geometryType: string): string {
  if (geometryType.endsWith('Polygon')) return '#2563eb'
  if (geometryType.endsWith('LineString')) return '#f97316'
  if (geometryType.endsWith('Point')) return '#dc2626'
  return '#6b7280'
}

export function pluralize(count: number, noun: string): string {
  return `${count.toLocaleString()} ${noun}${count === 1 ? '' : 's'}`
}
