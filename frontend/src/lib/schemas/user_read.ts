/**
 * AUTO-GENERATED from contracts/schemas/user_read.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const user_readSchema = z
  .object({
    created_at: z.string().datetime({ offset: true }),
    email: z.string().email(),
    full_name: z.union([z.string(), z.null()]),
    id: z.string(),
    is_active: z.boolean(),
    role: z.any(),
    tenant_id: z.string(),
    updated_at: z.string().datetime({ offset: true }),
  })
  .describe("User representation — never includes password.");

export type UserRead = z.infer<typeof user_readSchema>;
