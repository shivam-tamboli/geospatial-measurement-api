import { useState } from 'react'
import { useFileUpload } from '../hooks/useFileUpload'
import { validateUpload } from '../utils/format'
import { ErrorMessage } from './ErrorMessage'
import { RecentUploads } from './RecentUploads'
import { Spinner } from './Spinner'
import { UploadZone } from './UploadZone'

export function UploadView({ onOpenFile }: { onOpenFile: (fileId: string) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [validationError, setValidationError] = useState<string | null>(null)
  const upload = useFileUpload()

  const handleFilesChosen = (files: File[]) => {
    upload.reset()
    const [first] = files
    if (!first) return
    if (files.length > 1) {
      setFile(null)
      setValidationError('Please choose a single file.')
      return
    }
    const problem = validateUpload(first)
    setValidationError(problem)
    setFile(problem ? null : first)
  }

  const handleUpload = () => {
    if (!file) return
    upload.mutate(file, { onSuccess: (created) => onOpenFile(created.id) })
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-8 px-4 py-12">
      <header className="text-center">
        <h1 className="text-3xl font-bold tracking-tight text-gray-900">Geospatial Measurement API</h1>
        <p className="mt-2 text-gray-600">
          Upload a Shapefile or KML to get the area and length of every feature.
        </p>
      </header>

      <div className="space-y-4">
        <UploadZone file={file} disabled={upload.isPending} onFilesChosen={handleFilesChosen} />
        {validationError && <ErrorMessage message={validationError} />}
        {upload.error && <ErrorMessage message={upload.error.message} />}
        <button
          type="button"
          onClick={handleUpload}
          disabled={!file || upload.isPending}
          className="flex w-full items-center justify-center gap-2 rounded-md bg-blue-600 px-4 py-2.5 text-sm font-semibold text-white shadow-sm hover:bg-blue-700 disabled:cursor-not-allowed disabled:bg-gray-300"
        >
          {upload.isPending && <Spinner className="h-4 w-4 text-white" />}
          {upload.isPending ? 'Uploading…' : 'Upload'}
        </button>
      </div>

      <RecentUploads onView={onOpenFile} />
    </div>
  )
}
