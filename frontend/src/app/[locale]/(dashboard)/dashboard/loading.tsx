export default function LoginLoading() {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm animate-pulse space-y-4">
        <div className="bg-muted mx-auto h-8 w-48 rounded" />
        <div className="bg-muted mx-auto h-4 w-64 rounded" />
        <div className="bg-muted h-10 w-full rounded" />
        <div className="bg-muted h-10 w-full rounded" />
      </div>
    </div>
  );
}
