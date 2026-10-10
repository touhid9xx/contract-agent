/**
 * AUTO-GENERATED from contracts/schemas/error.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const errorSchema = z
  .object({
    detail: z.any(),
    error: z.string(),
    path: z.string(),
    request_id: z.union([z.string(), z.null()]),
    status: z.number().int(),
  })
  .strict();

export type Error = z.infer<typeof errorSchema>;
