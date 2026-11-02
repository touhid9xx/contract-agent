"use client";

import { Plus } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Sketch 1/2 require a visible upload affordance. PDF ingestion is P2-A, so in
 * Phase 0 the control is rendered disabled with a visible explanation rather
 * than hidden — the demo must show the finished product's shape.
 */
export function UploadContractButton() {
  return (
    <span className="inline-flex items-center gap-2">
      <Button
        size="sm"
        disabled
        title="PDF upload arrives in milestone P2-A (magic-byte + ClamAV + sha256 dedup). Disabled in the Phase 0 demo."
        aria-describedby="upload-disabled-reason"
      >
        <Plus className="h-3.5 w-3.5" aria-hidden />
        Upload contract
      </Button>
      <span id="upload-disabled-reason" className="text-[11px] text-muted-foreground">
        P2-A
      </span>
    </span>
  );
}
