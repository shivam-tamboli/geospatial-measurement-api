import type { FileInfo } from '../api/types'
import { formatBytes, formatDateTime } from '../utils/format'
import { StatusBadge } from './StatusBadge'

export function FileInfoCard({ file }: { file: FileInfo }) {
  return (
    <section className="rounded-lg border border-gray-200 bg-white p-4" aria-label="File information">
      <div className="flex items-start justify-between gap-3">
        <h1 className="break-all text-base font-semibold text-gray-900">{file.filename}</h1>
        <StatusBadge status={file.status} />
      </div>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        <dt className="text-gray-500">Features</dt>
        <dd className="text-gray-900">{file.feature_count?.toLocaleString() ?? '—'}</dd>
        <dt className="text-gray-500">CRS</dt>
        <dd className="break-all text-gray-900" title={file.crs ?? undefined}>
          {file.crs ?? '—'}
        </dd>
        <dt className="text-gray-500">Size</dt>
        <dd className="text-gray-900">{formatBytes(file.file_size)}</dd>
        <dt className="text-gray-500">Uploaded</dt>
        <dd className="text-gray-900">{formatDateTime(file.created_at)}</dd>
      </dl>
    </section>
  )
}
