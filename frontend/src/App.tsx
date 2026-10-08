import { useState } from 'react'
import { FileView } from './components/FileView'
import { UploadView } from './components/UploadView'

export default function App() {
  const [fileId, setFileId] = useState<string | null>(null)

  return (
    <div className="flex h-screen flex-col bg-gray-50 text-gray-900">
      {fileId !== null && (
        <header className="flex items-center gap-4 border-b border-gray-200 bg-white px-4 py-2.5">
          <button
            type="button"
            onClick={() => setFileId(null)}
            className="rounded-md border border-gray-300 px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            ← Uploads
          </button>
          <span className="text-sm font-semibold text-gray-700">Geospatial Measurement API</span>
        </header>
      )}
      <main className="min-h-0 flex-1 overflow-y-auto lg:overflow-hidden">
        {fileId === null ? (
          <div className="h-full overflow-y-auto">
            <UploadView onOpenFile={setFileId} />
          </div>
        ) : (
          <FileView key={fileId} fileId={fileId} onBack={() => setFileId(null)} />
        )}
      </main>
    </div>
  )
}
