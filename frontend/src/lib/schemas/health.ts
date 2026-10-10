/**
 * AUTO-GENERATED from contracts/schemas/health.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const healthSchema = z.object({
  status: z.string(),
  service: z.string(),
  env: z.string(),
  timestamp: z.string().datetime({ offset: true }),
});

export type Health = z.infer<typeof healthSchema>;
