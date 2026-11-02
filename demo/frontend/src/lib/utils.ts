import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

const DAY = new Intl.DateTimeFormat("en-GB", { day: "2-digit", month: "short", year: "numeric" });
const TIME = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hour12: false });

export function formatDate(value: string): string {
  return DAY.format(new Date(value));
}

export function formatTime(value: string): string {
  return TIME.format(new Date(value));
}

export function formatDateTime(value: string): string {
  return `${formatDate(value)} · ${formatTime(value)}`;
}

export function formatDayHeading(value: string): string {
  return new Intl.DateTimeFormat("en-GB", { weekday: "short", day: "2-digit", month: "short", year: "numeric" }).format(
    new Date(value),
  );
}

export function formatNumber(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

export function confidenceLabel(value: number): string {
  return value.toFixed(2);
}

export function isHighConfidence(value: number, threshold = 0.8): boolean {
  return value >= threshold;
}

export function shortId(value: number | string): string {
  return `#${value}`;
}
