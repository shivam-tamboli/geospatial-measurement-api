import { useState } from 'react'
import { useFileList } from '../hooks/useFileList'
import { formatDateTime, pluralize } from '../utils/format'
import { ErrorMessage } from './ErrorMessage'
import { Pagination } from './Pagination'
import { Spinner } from './Spinner'
import { StatusBadge } from './StatusBadge'

const PAGE_SIZE = 8

export function RecentUploads({ onView }: { onView: (fileId: string) => void }) {
  const [page, setPage] = useState(1)
  const { data, error, isLoading, isFetching, refetch } = useFileList(page, PAGE_SIZE)

  return (
    <section aria-labelledby="recent-heading">
      <div className="mb-3 flex items-center justify-between">
        <h2 id="recent-heading" className="text-lg font-semibold text-gray-900">
          Recent uploads
        </h2>
        {isFetching && !isLoading && <Spinner className="h-4 w-4 text-blue-600" />}
      </div>

      {isLoading && (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      )}
      {error && <ErrorMessage message={error.message} onRetry={() => void refetch()} />}
      {data && data.items.length === 0 && (
        <p className="rounded-md border border-dashed border-gray-300 py-8 text-center text-sm text-gray-500">
          No uploads yet. Upload a file to get started.
        </p>
      )}
      {data && data.items.length > 0 && (
        <div className="space-y-3">
          <ul className="divide-y divide-gray-200 overflow-hidden rounded-lg border border-gray-200 bg-white">
            {data.items.map((file) => (
              <li key={file.id} className="flex items-center gap-3 px-4 py-3">
                <div className="min-w-0 flex-1">
                  <p className="truncate text-sm font-medium text-gray-900" title={file.filename}>
                    {file.filename}
                  </p>
                  <p className="text-xs text-gray-500">
                    {formatDateTime(file.created_at)}
                    {file.feature_count !== null && ` · ${pluralize(file.feature_count, 'feature')}`}
                  </p>
                </div>
                <StatusBadge status={file.status} />
                <button
                  type="button"
                  onClick={() => onView(file.id)}
                  className="rounded-md border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50"
                >
                  View
                </button>
              </li>
            ))}
          </ul>
          <Pagination page={data.page} totalPages={data.total_pages} total={data.total} onPageChange={setPage} />
        </div>
      )}
    </section>
  )
}
