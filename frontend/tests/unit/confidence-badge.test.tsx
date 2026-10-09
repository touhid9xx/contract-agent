// frontend/tests/unit/confidence-badge.test.tsx
import { ConfidenceBadge, confidenceLevel } from "@/src/components/common/confidence-badge";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

describe("confidenceLevel", () => {
  it.each([
    [1.0, "high"],
    [0.95, "high"],
    [0.9, "high"],
    [0.89, "medium"],
    [0.8, "medium"],
    [0.79, "low"],
    [0.5, "low"],
    [0.0, "low"],
  ])("maps %s to %s", (score, expected) => {
    expect(confidenceLevel(score)).toBe(expected);
  });
});

describe("ConfidenceBadge", () => {
  it("renders High with percentage for high score", () => {
    render(<ConfidenceBadge score={0.95} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("High");
    expect(badge).toHaveTextContent("95%");
    expect(badge).toHaveAccessibleName("High confidence: 95%");
  });

  it("renders Medium for borderline score", () => {
    render(<ConfidenceBadge score={0.85} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("Medium");
    expect(badge).toHaveTextContent("85%");
  });

  it("renders Low with needs-review hint", () => {
    render(<ConfidenceBadge score={0.5} />);
    const badge = screen.getByRole("status");
    expect(badge).toHaveTextContent("Low");
    expect(badge).toHaveAccessibleName("Low confidence — needs review: 50%");
  });

  it("hides score when showScore=false", () => {
    render(<ConfidenceBadge score={0.95} showScore={false} />);
    expect(screen.queryByText("95%")).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("High");
  });

  it("has non-color cue (icon present)", () => {
    const { container } = render(<ConfidenceBadge score={0.5} />);
    // Icon is aria-hidden — still in DOM as SVG
    const svg = container.querySelector("svg");
    expect(svg).toBeInTheDocument();
    expect(svg).toHaveAttribute("aria-hidden", "true");
  });

  it("rounds score to nearest integer", () => {
    render(<ConfidenceBadge score={0.856} />);
    expect(screen.getByRole("status")).toHaveTextContent("86%");
  });
});
