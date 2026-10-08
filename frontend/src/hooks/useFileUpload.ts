import { useMutation, useQueryClient, type UseMutationResult } from '@tanstack/react-query'
import { uploadFile, type ApiError } from '../api/client'
import type { FileUploadResponse } from '../api/types'

/** Upload a Shapefile ZIP / KML. Errors are exposed on the result, never thrown. */
export function useFileUpload(): UseMutationResult<FileUploadResponse, ApiError, File> {
  const queryClient = useQueryClient()
  return useMutation<FileUploadResponse, ApiError, File>({
    mutationFn: uploadFile,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['files'] }),
  })
}
