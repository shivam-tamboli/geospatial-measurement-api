import { useFileStatus } from '../hooks/useFileStatus'
import { ErrorMessage } from './ErrorMessage'
import { ProcessingView } from './ProcessingView'
import { ResultsView } from './ResultsView'
import { Spinner } from './Spinner'

/** Chooses the processing or results view from the file's live status. */
export function FileView({ fileId, onBack }: { fileId: string; onBack: () => void }) {
  const { data, error, isLoading, refetch } = useFileStatus(fileId)

  if (isLoading) {
    return (
      <div className="flex justify-center py-20">
        <Spinner className="h-10 w-10 text-blue-600" />
      </div>
    )
  }
  if (!data) {
    return (
      <div className="mx-auto max-w-lg space-y-4 px-4 py-20">
        <ErrorMessage message={error?.message ?? 'Could not load this file.'} onRetry={() => void refetch()} />
        <button type="button" onClick={onBack} className="text-sm font-medium text-blue-600 hover:underline">
          ← Back to uploads
        </button>
      </div>
    )
  }
  if (data.status === 'COMPLETED') return <ResultsView file={data} />
  return <ProcessingView file={data} onBack={onBack} />
}
