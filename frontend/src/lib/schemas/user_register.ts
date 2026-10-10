/**
 * AUTO-GENERATED from contracts/schemas/user_register.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const user_registerSchema = z
  .object({
    email: z.string().email(),
    full_name: z.union([z.string().max(255), z.null()]).default(null),
    password: z.string().min(12).max(128),
  })
  .strict()
  .describe("POST /auth/register.");

export type UserRegister = z.infer<typeof user_registerSchema>;
