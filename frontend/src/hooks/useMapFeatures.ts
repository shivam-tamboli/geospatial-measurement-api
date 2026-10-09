import { useInfiniteQuery } from '@tanstack/react-query'
import { useEffect, useMemo } from 'react'
import { getMeasurements, type ApiError } from '../api/client'
import type { FeatureMeasurement, MeasurementsPage } from '../api/types'

// The API rejects page_size above its MAX_PAGE_SIZE (default 100) with a 400, so this must not exceed it.
const MAP_PAGE_SIZE = 100
/** Upper bound on features drawn on the map; keeps the browser responsive for huge files. */
export const MAX_MAP_FEATURES = 5000

export interface MapFeatures {
  features: FeatureMeasurement[]
  total: number
  /** True once every page (up to the cap) has been fetched. */
  complete: boolean
  /** True if the file has more features than the map shows. */
  truncated: boolean
  isLoading: boolean
  error: ApiError | null
}

/** Fetches every feature (page by page, up to a cap) so the map can show them all. */
export function useMapFeatures(fileId: string): MapFeatures {
  const query = useInfiniteQuery<MeasurementsPage, ApiError, { pages: MeasurementsPage[] }, unknown[], number>({
    queryKey: ['map-features', fileId],
    queryFn: ({ pageParam }) => getMeasurements(fileId, pageParam, MAP_PAGE_SIZE),
    initialPageParam: 1,
    getNextPageParam: (last) => (last.page < last.total_pages ? last.page + 1 : undefined),
    staleTime: Infinity,
  })

  const { data, hasNextPage, isFetchingNextPage, fetchNextPage, isLoading, error } = query
  const features = useMemo(
    () => (data?.pages.flatMap((p) => p.items) ?? []).slice(0, MAX_MAP_FEATURES),
    [data],
  )
  const total = data?.pages[0]?.total ?? 0
  const capReached = features.length >= MAX_MAP_FEATURES

  useEffect(() => {
    if (hasNextPage && !isFetchingNextPage && !capReached && !error) void fetchNextPage()
  }, [hasNextPage, isFetchingNextPage, capReached, error, fetchNextPage])

  return {
    features,
    total,
    complete: data !== undefined && (!hasNextPage || capReached),
    truncated: total > MAX_MAP_FEATURES,
    isLoading,
    error,
  }
}
