export function ErrorMessage({ message, onRetry }: { message: string; onRetry?: () => void }) {
  return (
    <div role="alert" className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <p>{message}</p>
      {onRetry && (
        <button type="button" onClick={onRetry} className="mt-2 font-medium underline hover:no-underline">
          Try again
        </button>
      )}
    </div>
  )
}
