import { useQuery, type UseQueryResult } from '@tanstack/react-query'
import { getFile, type ApiError } from '../api/client'
import type { FileInfo, FileStatus } from '../api/types'

export const STATUS_POLL_MS = 3000

export function isInProgress(status: FileStatus | undefined): boolean {
  return status === 'PENDING' || status === 'PROCESSING'
}

/** File metadata; polls every 3 s while the file is PENDING or PROCESSING, then stops. */
export function useFileStatus(fileId: string): UseQueryResult<FileInfo, ApiError> {
  return useQuery<FileInfo, ApiError>({
    queryKey: ['file', fileId],
    queryFn: () => getFile(fileId),
    refetchInterval: (query) => (isInProgress(query.state.data?.status) ? STATUS_POLL_MS : false),
  })
}
