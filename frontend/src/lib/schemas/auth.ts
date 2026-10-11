/**
 * Auth Zod schemas — mirror backend Pydantic.
 *
 * Why mirror?
 *   - Instant client-side feedback (no round-trip for policy violations)
 *   - Same error messages in both places (consistency)
 *   - CI enforces sync via schema drift check (M3)
 *
 * Policy source: src/contract_agent/schemas/user.py
 *   - 12+ chars
 *   - 1 uppercase, 1 lowercase, 1 digit, 1 symbol
 */
import { z } from "zod";

// ============================================================
// PASSWORD POLICY — mirror backend PASSWORD_MIN_LENGTH + rules
// ============================================================
export const PASSWORD_MIN_LENGTH = 12;
export const PASSWORD_MAX_LENGTH = 128;

const PASSWORD_RULES = {
  uppercase: { regex: /[A-Z]/, label: "one uppercase character" },
  lowercase: { regex: /[a-z]/, label: "one lowercase character" },
  digit: { regex: /\d/, label: "one digit" },
  symbol: { regex: /[^A-Za-z0-9]/, label: "one symbol" },
} as const;

export const passwordSchema = z
  .string()
  .min(PASSWORD_MIN_LENGTH, {
    message: `Password must be at least ${PASSWORD_MIN_LENGTH} characters`,
  })
  .max(PASSWORD_MAX_LENGTH, {
    message: `Password must be at most ${PASSWORD_MAX_LENGTH} characters`,
  })
  .superRefine((value, ctx) => {
    for (const [key, rule] of Object.entries(PASSWORD_RULES)) {
      if (!rule.regex.test(value)) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: `Password must contain ${rule.label}`,
          path: [key], // helps with per-rule display
        });
      }
    }
  });

// ============================================================
// LOGIN
// ============================================================
export const loginSchema = z.object({
  email: z.string().email("Enter a valid email"),
  password: z.string().min(1, "Password is required"),
});

export type LoginInput = z.infer<typeof loginSchema>;

// ============================================================
// REGISTER
// ============================================================
export const registerSchema = z
  .object({
    email: z.string().email("Enter a valid email"),
    password: passwordSchema,
    confirmPassword: z.string(),
    fullName: z.string().max(255).optional().or(z.literal("")),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

export type RegisterInput = z.infer<typeof registerSchema>;

// ============================================================
// CHANGE PASSWORD (used in M22 settings)
// ============================================================
export const changePasswordSchema = z
  .object({
    currentPassword: z.string().min(1, "Current password is required"),
    newPassword: passwordSchema,
    confirmNewPassword: z.string(),
  })
  .refine((data) => data.newPassword === data.confirmNewPassword, {
    message: "Passwords do not match",
    path: ["confirmNewPassword"],
  })
  .refine((data) => data.newPassword !== data.currentPassword, {
    message: "New password must differ from current",
    path: ["newPassword"],
  });

export type ChangePasswordInput = z.infer<typeof changePasswordSchema>;

// ============================================================
// API RESPONSE SHAPES — mirror backend schemas/user.py
// ============================================================
export const userRoleSchema = z.enum(["admin", "operator", "viewer"]);
export type UserRole = z.infer<typeof userRoleSchema>;

export const userSchema = z.object({
  id: z.string(),
  tenant_id: z.string(),
  email: z.string().email(),
  role: userRoleSchema,
  is_active: z.boolean(),
  full_name: z.string().nullable(),
  created_at: z.string(),
  updated_at: z.string(),
});
export type User = z.infer<typeof userSchema>;

export const tokenPairSchema = z.object({
  access_token: z.string(),
  refresh_token: z.string(),
  token_type: z.literal("bearer"),
  expires_in: z.number(),
});
export type TokenPair = z.infer<typeof tokenPairSchema>;
