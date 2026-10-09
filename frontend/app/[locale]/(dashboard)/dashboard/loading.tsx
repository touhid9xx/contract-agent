export default function LoginLoading() {
  return (
    <div className="flex min-h-screen items-center justify-center p-4">
      <div className="w-full max-w-sm space-y-4 animate-pulse">
        <div className="h-8 w-48 bg-muted rounded mx-auto" />
        <div className="h-4 w-64 bg-muted rounded mx-auto" />
        <div className="h-10 w-full bg-muted rounded" />
        <div className="h-10 w-full bg-muted rounded" />
      </div>
    </div>
  );
}
