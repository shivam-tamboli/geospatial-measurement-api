import { useMemo, useState } from 'react'
import type { FeatureMeasurement, FileInfo } from '../api/types'
import { TABLE_PAGE_SIZE, useFileMeasurements } from '../hooks/useFileMeasurements'
import { MAX_MAP_FEATURES, useMapFeatures } from '../hooks/useMapFeatures'
import { ErrorMessage } from './ErrorMessage'
import { FileInfoCard } from './FileInfoCard'
import { MeasurementsTable } from './MeasurementsTable'
import { Pagination } from './Pagination'
import { ResultsMap } from './ResultsMap'
import { Spinner } from './Spinner'
import { WarningsBanner } from './WarningsBanner'

interface Selection {
  feature: FeatureMeasurement
  /** Table selections move the map; map clicks must not. */
  fly: boolean
}

export function ResultsView({ file }: { file: FileInfo }) {
  const [page, setPage] = useState(1)
  const [selection, setSelection] = useState<Selection | null>(null)
  const table = useFileMeasurements(file.id, page)
  const map = useMapFeatures(file.id)

  const selectFromTable = (feature: FeatureMeasurement) => setSelection({ feature, fly: true })
  const selectFromMap = (feature: FeatureMeasurement) => {
    setSelection({ feature, fly: false })
    setPage(Math.floor(feature.feature_id / TABLE_PAGE_SIZE) + 1) // feature_id is the zero-based row offset
  }

  // The selected feature may lie beyond the map's cap; make sure it is still drawn.
  const mapFeatures = useMemo(() => {
    const selected = selection?.feature
    return selected && !map.features.some((f) => f.feature_id === selected.feature_id)
      ? [...map.features, selected]
      : map.features
  }, [map.features, selection])

  return (
    <div className="flex min-h-full flex-col lg:h-full lg:flex-row">
      <aside className="w-full space-y-4 p-4 lg:w-2/5 lg:overflow-y-auto">
        <FileInfoCard file={file} />
        <WarningsBanner warnings={file.warnings} />

        <section aria-label="Measurements" className="space-y-3">
          {table.error && <ErrorMessage message={table.error.message} onRetry={() => void table.refetch()} />}
          {table.isLoading && (
            <div className="flex justify-center py-8">
              <Spinner />
            </div>
          )}
          {table.data && (
            <>
              <MeasurementsTable
                items={table.data.items}
                selectedId={selection?.feature.feature_id ?? null}
                onSelect={selectFromTable}
              />
              <Pagination
                page={table.data.page}
                totalPages={table.data.total_pages}
                total={table.data.total}
                onPageChange={setPage}
                disabled={table.isFetching}
              />
            </>
          )}
        </section>
      </aside>

      <section className="relative h-[28rem] w-full lg:h-full lg:w-3/5" aria-label="Map">
        <ResultsMap
          features={mapFeatures}
          selectedId={selection?.feature.feature_id ?? null}
          flyToSelected={selection?.fly ?? false}
          complete={map.complete}
          onSelect={selectFromMap}
        />
        <MapStatus loaded={map.features.length} total={map.total} complete={map.complete} truncated={map.truncated} error={map.error?.message ?? null} />
      </section>
    </div>
  )
}

interface MapStatusProps {
  loaded: number
  total: number
  complete: boolean
  truncated: boolean
  error: string | null
}

/** Overlay for map loading progress / truncation / errors. Sits above Leaflet's panes (z-index 400–1000). */
function MapStatus({ loaded, total, complete, truncated, error }: MapStatusProps) {
  if (error) {
    return (
      <div className="absolute left-1/2 top-3 z-[1000] w-[90%] max-w-md -translate-x-1/2">
        <ErrorMessage message={`Could not load map features: ${error}`} />
      </div>
    )
  }
  if (!complete) {
    return (
      <div className="absolute right-3 top-3 z-[1000] flex items-center gap-2 rounded-md bg-white/95 px-3 py-1.5 text-xs text-gray-700 shadow">
        <Spinner className="h-4 w-4 text-blue-600" />
        Loading features… {loaded.toLocaleString()}
        {total > 0 && ` / ${Math.min(total, MAX_MAP_FEATURES).toLocaleString()}`}
      </div>
    )
  }
  if (truncated) {
    return (
      <div className="absolute right-3 top-3 z-[1000] rounded-md border border-yellow-300 bg-yellow-50 px-3 py-1.5 text-xs text-yellow-900 shadow">
        Map shows the first {MAX_MAP_FEATURES.toLocaleString()} of {total.toLocaleString()} features.
      </div>
    )
  }
  return null
}
