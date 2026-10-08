import type { FileInfo } from '../api/types'
import { STATUS_POLL_MS } from '../hooks/useFileStatus'
import { ErrorMessage } from './ErrorMessage'
import { Spinner } from './Spinner'
import { StatusBadge } from './StatusBadge'

export function ProcessingView({ file, onBack }: { file: FileInfo; onBack: () => void }) {
  const failed = file.status === 'FAILED'
  return (
    <div className="mx-auto flex w-full max-w-lg flex-col items-center gap-5 px-4 py-20 text-center">
      {failed ? (
        <div className="flex h-12 w-12 items-center justify-center rounded-full bg-red-100 text-2xl text-red-600" aria-hidden="true">
          ✕
        </div>
      ) : (
        <Spinner className="h-12 w-12 text-blue-600" />
      )}

      <div className="space-y-2">
        <h1 className="break-all text-xl font-semibold text-gray-900">{file.filename}</h1>
        <StatusBadge status={file.status} />
      </div>

      {failed ? (
        <div className="w-full text-left">
          <ErrorMessage message={file.error ?? 'Processing failed for an unknown reason.'} />
        </div>
      ) : (
        <p className="text-sm text-gray-600">
          Reading features and calculating measurements. This page updates automatically
          (checking every {STATUS_POLL_MS / 1000} seconds).
        </p>
      )}

      <button
        type="button"
        onClick={onBack}
        className="rounded-md border border-gray-300 bg-white px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
      >
        Back to uploads
      </button>
    </div>
  )
}
