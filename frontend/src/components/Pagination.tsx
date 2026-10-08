interface PaginationProps {
  page: number
  totalPages: number
  total: number
  onPageChange: (page: number) => void
  disabled?: boolean
}

export function Pagination({ page, totalPages, total, onPageChange, disabled = false }: PaginationProps) {
  if (totalPages <= 1) return null
  const button =
    'rounded-md border border-gray-300 bg-white px-3 py-1 text-sm font-medium text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50'
  return (
    <nav className="flex items-center justify-between gap-2 text-sm text-gray-600" aria-label="Pagination">
      <span>
        Page {page} of {totalPages} · {total.toLocaleString()} total
      </span>
      <span className="flex gap-2">
        <button type="button" className={button} disabled={disabled || page <= 1} onClick={() => onPageChange(page - 1)}>
          Previous
        </button>
        <button
          type="button"
          className={button}
          disabled={disabled || page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          Next
        </button>
      </span>
    </nav>
  )
}
