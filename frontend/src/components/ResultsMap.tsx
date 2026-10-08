import { geoJSON } from 'leaflet'
import type { FeatureCollection, Geometry } from 'geojson'
import { useEffect, useRef } from 'react'
import { MapContainer, TileLayer, useMap } from 'react-leaflet'
import type { FeatureMeasurement } from '../api/types'
import { FeatureLayer } from './FeatureLayer'

interface ResultsMapProps {
  features: FeatureMeasurement[]
  selectedId: number | null
  flyToSelected: boolean
  /** True once all features are loaded; triggers a final fit-to-bounds. */
  complete: boolean
  onSelect: (feature: FeatureMeasurement) => void
}

interface DrawableFeature {
  feature: FeatureMeasurement
  geometry: Geometry
}

function FitToFeatures({ drawable, complete }: { drawable: DrawableFeature[]; complete: boolean }) {
  const map = useMap()
  const hasAny = drawable.length > 0
  const latest = useRef(drawable)

  useEffect(() => {
    latest.current = drawable
  })

  // Fit when the first features arrive and again once everything has loaded. Reading the latest
  // features through a ref (not a dependency) avoids re-fitting, and jumping the map, on every streamed page.
  useEffect(() => {
    if (!hasAny) return
    const collection: FeatureCollection = {
      type: 'FeatureCollection',
      features: latest.current.map((d) => ({ type: 'Feature', geometry: d.geometry, properties: {} })),
    }
    const bounds = geoJSON(collection).getBounds()
    if (bounds.isValid()) map.fitBounds(bounds, { maxZoom: 16, padding: [30, 30] })
  }, [hasAny, complete, map])

  return null
}

export function ResultsMap({ features, selectedId, flyToSelected, complete, onSelect }: ResultsMapProps) {
  const drawable = features.flatMap((feature): DrawableFeature[] =>
    feature.geometry_geojson ? [{ feature, geometry: feature.geometry_geojson }] : [],
  )

  return (
    <MapContainer center={[20, 0]} zoom={2} preferCanvas className="h-full w-full" aria-label="Map of features">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <FitToFeatures drawable={drawable} complete={complete} />
      {drawable.map(({ feature, geometry }) => (
        <FeatureLayer
          key={feature.feature_id}
          feature={feature}
          geometry={geometry}
          selected={feature.feature_id === selectedId}
          flyToOnSelect={flyToSelected}
          onSelect={onSelect}
        />
      ))}
    </MapContainer>
  )
}
