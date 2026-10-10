/**
 * AUTO-GENERATED from contracts/schemas/error.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const errorSchema = z.object({
  error: z.string(),
  status: z.number(),
  detail: z.any(),
  path: z.string(),
  request_id: z.string().nullable(),
});

export type Error = z.infer<typeof errorSchema>;
