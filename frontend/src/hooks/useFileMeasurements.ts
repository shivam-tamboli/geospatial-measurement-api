import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { getMeasurements, type ApiError } from '../api/client'
import type { MeasurementsPage } from '../api/types'

export const TABLE_PAGE_SIZE = 20

/** One server-side page of measurements for the table. A completed file never changes. */
export function useFileMeasurements(
  fileId: string,
  page: number,
): UseQueryResult<MeasurementsPage, ApiError> {
  return useQuery<MeasurementsPage, ApiError>({
    queryKey: ['measurements', fileId, page, TABLE_PAGE_SIZE],
    queryFn: () => getMeasurements(fileId, page, TABLE_PAGE_SIZE),
    placeholderData: keepPreviousData,
    staleTime: Infinity,
  })
}
