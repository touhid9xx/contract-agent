/**
 * AUTO-GENERATED from contracts/schemas/token_pair.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const token_pairSchema = z
  .object({
    access_token: z.string(),
    expires_in: z.number().int(),
    refresh_token: z.string(),
    token_type: z.literal("bearer").default("bearer"),
  })
  .describe("Returned by /register, /login, /refresh.");

export type TokenPair = z.infer<typeof token_pairSchema>;
