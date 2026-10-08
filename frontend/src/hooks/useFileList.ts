import { keepPreviousData, useQuery, type UseQueryResult } from '@tanstack/react-query'
import { listFiles, type ApiError } from '../api/client'
import type { FileListPage } from '../api/types'

export const LIST_POLL_MS = 5000

/** One page of uploads (newest first), refreshed every 5 s. */
export function useFileList(page: number, pageSize: number): UseQueryResult<FileListPage, ApiError> {
  return useQuery<FileListPage, ApiError>({
    queryKey: ['files', page, pageSize],
    queryFn: () => listFiles(page, pageSize),
    refetchInterval: LIST_POLL_MS,
    placeholderData: keepPreviousData,
  })
}
