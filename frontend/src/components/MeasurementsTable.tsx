import { Fragment, useEffect, useRef, useState } from 'react'
import type { FeatureMeasurement } from '../api/types'
import { formatMeasurement, formatPropertyValue, geometryColor } from '../utils/format'

interface MeasurementsTableProps {
  items: FeatureMeasurement[]
  selectedId: number | null
  onSelect: (feature: FeatureMeasurement) => void
}

export function MeasurementsTable({ items, selectedId, onSelect }: MeasurementsTableProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<number>>(new Set())
  const selectedRow = useRef<HTMLTableRowElement | null>(null)

  useEffect(() => {
    selectedRow.current?.scrollIntoView({ block: 'nearest' })
  }, [selectedId, items])

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (!next.delete(id)) next.add(id)
      return next
    })

  if (items.length === 0) {
    return <p className="py-6 text-center text-sm text-gray-500">This file has no features.</p>
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-gray-200 bg-white">
      <table className="w-full text-left text-sm">
        <thead className="bg-gray-50 text-xs uppercase tracking-wide text-gray-500">
          <tr>
            <th scope="col" className="px-3 py-2">ID</th>
            <th scope="col" className="px-3 py-2">Type</th>
            <th scope="col" className="px-3 py-2">Measurement</th>
            <th scope="col" className="px-3 py-2"><span className="sr-only">Properties</span></th>
          </tr>
        </thead>
        <tbody className="divide-y divide-gray-100">
          {items.map((feature) => {
            const id = feature.feature_id
            const selected = id === selectedId
            const open = expanded.has(id)
            const entries = Object.entries(feature.properties)
            const measurement = formatMeasurement(feature.measurement)
            return (
              <Fragment key={id}>
                <tr
                  ref={selected ? selectedRow : undefined}
                  onClick={() => onSelect(feature)}
                  aria-selected={selected}
                  className={`cursor-pointer hover:bg-gray-50 ${selected ? 'bg-blue-50' : ''}`}
                >
                  <td className="px-3 py-2 tabular-nums text-gray-900">{id}</td>
                  <td className="px-3 py-2">
                    <span className="inline-flex items-center gap-1.5 text-gray-900">
                      <span
                        className="h-2.5 w-2.5 rounded-full"
                        style={{ backgroundColor: geometryColor(feature.geometry_type) }}
                        aria-hidden="true"
                      />
                      {feature.geometry_type}
                    </span>
                  </td>
                  <td className="px-3 py-2 tabular-nums text-gray-900">
                    {measurement ?? <span className="text-gray-400">n/a</span>}
                  </td>
                  <td className="px-3 py-2 text-right">
                    <button
                      type="button"
                      aria-expanded={open}
                      disabled={entries.length === 0}
                      onClick={(e) => {
                        e.stopPropagation()
                        toggle(id)
                      }}
                      className="text-xs font-medium text-blue-600 hover:underline disabled:cursor-not-allowed disabled:text-gray-300 disabled:no-underline"
                    >
                      {open ? 'Hide' : 'Properties'} ({entries.length})
                    </button>
                  </td>
                </tr>
                {open && (
                  <tr className={selected ? 'bg-blue-50' : 'bg-gray-50'}>
                    <td colSpan={4} className="px-3 py-2">
                      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
                        {entries.map(([key, value]) => (
                          <Fragment key={key}>
                            <dt className="font-medium text-gray-500">{key}</dt>
                            <dd className="break-words text-gray-900">{formatPropertyValue(value)}</dd>
                          </Fragment>
                        ))}
                      </dl>
                      {feature.measurement.projected_crs && (
                        <p className="mt-2 text-xs text-gray-500">
                          Measured in {feature.measurement.projected_crs}
                        </p>
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
