/**
 * AUTO-GENERATED from contracts/schemas/user_login.json
 * DO NOT EDIT — regenerate with: npm run schemas:generate
 */
import { z } from "zod";

export const user_loginSchema = z
  .object({ email: z.string().email(), password: z.string() })
  .strict()
  .describe("POST /auth/login.");

export type UserLogin = z.infer<typeof user_loginSchema>;
