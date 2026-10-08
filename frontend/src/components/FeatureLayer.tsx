import { circleMarker, type GeoJSON as LeafletGeoJSON, type PathOptions } from 'leaflet'
import type { Feature, Geometry } from 'geojson'
import { useEffect, useMemo, useRef } from 'react'
import { GeoJSON, Popup, useMap } from 'react-leaflet'
import type { FeatureMeasurement } from '../api/types'
import { formatMeasurement, geometryColor } from '../utils/format'

interface FeatureLayerProps {
  feature: FeatureMeasurement
  geometry: Geometry
  selected: boolean
  /** Pan/zoom to the feature when it becomes selected (used for table selections). */
  flyToOnSelect: boolean
  onSelect: (feature: FeatureMeasurement) => void
}

function styleFor(geometryType: string, selected: boolean): PathOptions {
  const color = geometryColor(geometryType)
  return {
    color: selected ? '#111827' : color,
    fillColor: color,
    weight: selected ? 4 : 2,
    fillOpacity: selected ? 0.6 : 0.3,
    opacity: 1,
  }
}

/** One feature on the map: styled by geometry type, clickable, with a measurement popup. */
export function FeatureLayer({ feature, geometry, selected, flyToOnSelect, onSelect }: FeatureLayerProps) {
  const map = useMap()
  const layerRef = useRef<LeafletGeoJSON | null>(null)
  const data = useMemo<Feature<Geometry>>(() => ({ type: 'Feature', geometry, properties: {} }), [geometry])
  const style = useMemo(() => styleFor(feature.geometry_type, selected), [feature.geometry_type, selected])
  const measurement = formatMeasurement(feature.measurement)

  useEffect(() => {
    const layer = layerRef.current
    if (!selected || !layer) return
    layer.bringToFront()
    // Map clicks already open Leaflet's own popup at the click position; only table selections need it opened here.
    if (flyToOnSelect) {
      const bounds = layer.getBounds()
      if (bounds.isValid()) {
        if (bounds.getSouthWest().equals(bounds.getNorthEast())) {
          map.setView(bounds.getCenter(), Math.max(map.getZoom(), 15))
        } else {
          map.fitBounds(bounds, { maxZoom: 17, padding: [40, 40] })
        }
      }
      layer.openPopup()
    }
  }, [selected, flyToOnSelect, map])

  return (
    <GeoJSON
      ref={layerRef}
      data={data}
      style={style}
      pointToLayer={(_, latlng) => circleMarker(latlng, { radius: 7, ...style })}
      eventHandlers={{ click: () => onSelect(feature) }}
    >
      <Popup>
        <div className="space-y-0.5 text-sm">
          <p className="font-semibold">
            Feature #{feature.feature_id} · {feature.geometry_type}
          </p>
          <p>{measurement ?? 'No measurement for this geometry type'}</p>
          {feature.measurement.projected_crs && (
            <p className="text-xs text-gray-500">Measured in {feature.measurement.projected_crs}</p>
          )}
        </div>
      </Popup>
    </GeoJSON>
  )
}
