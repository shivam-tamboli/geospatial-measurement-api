import { useId, useState, type ChangeEvent, type DragEvent } from 'react'
import { formatBytes, ACCEPT_ATTRIBUTE } from '../utils/format'

interface UploadZoneProps {
  file: File | null
  disabled: boolean
  /** Receives the raw dropped/selected files; validation is the parent's job. */
  onFilesChosen: (files: File[]) => void
}

export function UploadZone({ file, disabled, onFilesChosen }: UploadZoneProps) {
  const inputId = useId()
  const [dragging, setDragging] = useState(false)

  const handleDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault()
    setDragging(false)
    if (!disabled) onFilesChosen(Array.from(event.dataTransfer.files))
  }

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    onFilesChosen(Array.from(event.target.files ?? []))
    event.target.value = '' // allow re-selecting the same file
  }

  return (
    <label
      htmlFor={inputId}
      onDragOver={(e) => {
        e.preventDefault()
        if (!disabled) setDragging(true)
      }}
      onDragLeave={() => setDragging(false)}
      onDrop={handleDrop}
      className={`flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed px-6 py-10 text-center transition-colors focus-within:ring-2 focus-within:ring-blue-500 ${
        dragging ? 'border-blue-500 bg-blue-50' : 'border-gray-300 bg-white hover:border-gray-400'
      } ${disabled ? 'pointer-events-none opacity-60' : ''}`}
    >
      <input
        id={inputId}
        type="file"
        accept={ACCEPT_ATTRIBUTE}
        className="sr-only"
        disabled={disabled}
        onChange={handleChange}
      />
      <svg className="mb-3 h-10 w-10 text-gray-400" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
        <path strokeLinecap="round" strokeLinejoin="round" d="M12 16V4m0 0 4 4m-4-4L8 8M4 16v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2" />
      </svg>
      {file ? (
        <>
          <p className="max-w-full truncate text-sm font-medium text-gray-900" title={file.name}>
            {file.name}
          </p>
          <p className="text-sm text-gray-500">{formatBytes(file.size)} · click or drop to replace</p>
        </>
      ) : (
        <>
          <p className="text-sm font-medium text-gray-900">Drag & drop a file here, or click to browse</p>
          <p className="mt-1 text-sm text-gray-500">Shapefile (.zip) or KML (.kml)</p>
        </>
      )}
    </label>
  )
}
