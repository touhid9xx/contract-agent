import { describe, expect, it } from "vitest";
import { loginSchema, passwordSchema, registerSchema } from "@/lib/schemas/auth";

describe("passwordSchema", () => {
  it("accepts a strong password", () => {
    expect(passwordSchema.safeParse("Sup3rSecret!Pass").success).toBe(true);
  });

  it("rejects short password", () => {
    const r = passwordSchema.safeParse("Short1!");
    expect(r.success).toBe(false);
  });

  it("rejects password without uppercase", () => {
    const r = passwordSchema.safeParse("alllowercase123!");
    expect(r.success).toBe(false);
  });

  it("rejects password without digit", () => {
    const r = passwordSchema.safeParse("NoDigits!Abcde");
    expect(r.success).toBe(false);
  });

  it("rejects password without symbol", () => {
    const r = passwordSchema.safeParse("NoSymbols12345");
    expect(r.success).toBe(false);
  });

  it("collects all violations", () => {
    const r = passwordSchema.safeParse("short");
    expect(r.success).toBe(false);
    if (!r.success) {
      // Multiple issues reported at once
      expect(r.error.issues.length).toBeGreaterThan(1);
    }
  });
});

describe("loginSchema", () => {
  it("accepts valid input", () => {
    expect(loginSchema.safeParse({ email: "a@b.com", password: "x" }).success).toBe(true);
  });

  it("rejects invalid email", () => {
    expect(loginSchema.safeParse({ email: "not-an-email", password: "x" }).success).toBe(false);
  });

  it("rejects empty password", () => {
    expect(loginSchema.safeParse({ email: "a@b.com", password: "" }).success).toBe(false);
  });
});

describe("registerSchema", () => {
  const valid = {
    email: "a@b.com",
    password: "Sup3rSecret!Pass",
    confirmPassword: "Sup3rSecret!Pass",
    fullName: "Alice",
  };

  it("accepts valid input", () => {
    expect(registerSchema.safeParse(valid).success).toBe(true);
  });

  it("rejects mismatched passwords", () => {
    const r = registerSchema.safeParse({
      ...valid,
      confirmPassword: "Different!1Pass",
    });
    expect(r.success).toBe(false);
    if (!r.success) {
      expect(r.error.issues.some((i) => i.path.includes("confirmPassword"))).toBe(true);
    }
  });

  it("allows empty fullName", () => {
    const r = registerSchema.safeParse({ ...valid, fullName: "" });
    expect(r.success).toBe(true);
  });
});
