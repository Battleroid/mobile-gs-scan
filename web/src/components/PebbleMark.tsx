/**
 * Pebble brand mark. A rounded ellipse with a tomato dot —
 * geometry copied verbatim from studio.jsx:76 so the implementation
 * matches the design 1:1.
 *
 * Pure inline SVG — no client-side cost beyond the markup. Both
 * fills accept theme overrides so the mark can recolor (e.g. on a
 * dark surface) without re-authoring.
 */
export function PebbleMark({
  size = 28,
  ink = "#1A1612",
  accent = "#FF5A36",
}: {
  size?: number;
  ink?: string;
  accent?: string;
}) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 28 28"
      role="img"
      aria-label="Pebble"
    >
      <ellipse cx="14" cy="15" rx="11" ry="9" fill={ink} />
      <circle cx="20" cy="10" r="3" fill={accent} />
    </svg>
  );
}
