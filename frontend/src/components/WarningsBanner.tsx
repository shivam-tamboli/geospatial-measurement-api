export function WarningsBanner({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null
  return (
    <div role="status" className="rounded-md border border-yellow-300 bg-yellow-50 px-4 py-3 text-sm text-yellow-900">
      <p className="font-medium">
        Some of this file could not be read
      </p>
      <ul className="mt-1 list-disc space-y-0.5 pl-5">
        {warnings.map((warning) => (
          <li key={warning}>{warning}</li>
        ))}
      </ul>
    </div>
  )
}
