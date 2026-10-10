/**
 * AUTO-GENERATED from contracts/schemas/health.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const healthSchema = z
  .object({
    env: z.string(),
    service: z.string(),
    status: z.string(),
    timestamp: z.string().datetime({ offset: true }),
  })
  .strict();

export type Health = z.infer<typeof healthSchema>;
