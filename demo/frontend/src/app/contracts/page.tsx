import { Suspense } from "react";

import { SkeletonRows } from "@/components/ui/states";

import { ContractsClient } from "./contracts-client";

export default function ContractsPage() {
  return (
    <Suspense
      fallback={
        <div className="p-4">
          <SkeletonRows rows={6} />
        </div>
      }
    >
      <ContractsClient />
    </Suspense>
  );
}
