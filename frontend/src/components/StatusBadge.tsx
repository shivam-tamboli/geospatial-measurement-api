import type { FileStatus } from '../api/types'

const STYLES: Record<FileStatus, string> = {
  PENDING: 'bg-gray-100 text-gray-700 ring-gray-300',
  PROCESSING: 'bg-yellow-100 text-yellow-800 ring-yellow-300',
  COMPLETED: 'bg-green-100 text-green-800 ring-green-300',
  FAILED: 'bg-red-100 text-red-800 ring-red-300',
}

export function StatusBadge({ status }: { status: FileStatus }) {
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STYLES[status]}`}
    >
      {status}
    </span>
  )
}
