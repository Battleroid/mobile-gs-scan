/**
 * Pebble UI primitives. Shared across `/`, `/sign-in`, `/account`,
 * `/captures/new`, and `/captures/[id]`. One source of truth so the
 * design's spacing / weight / colour decisions land identically
 * everywhere.
 *
 * The shapes mirror studio.jsx's BigButton / FilterChip / Panel /
 * Stat / Legend / UserAvatar — translated from inline-style React to
 * Tailwind classes that read the global Pebble tokens.
 */
import { clsx } from "clsx";
import Link from "next/link";
import type { ComponentProps, ReactNode } from "react";

// ─── BigButton ────────────────────────────────────────────────────

type BigButtonProps = {
  variant?: "primary" | "secondary" | "danger";
  href?: string;
  className?: string;
  children: ReactNode;
} & Omit<ComponentProps<"button">, "className" | "children">;

/**
 * The design's only button primitive. Three variants:
 *   primary   — tomato bg, white fg, soft accent shadow (default)
 *   secondary — surface bg, rule border, ink fg
 *   danger    — red bg, white fg
 *
 * If `href` is set the button renders as a Next/Link instead. Style
 * stays identical so it composes cleanly with the design's mixed
 * action rows (Rename / Download / Delete).
 */
export function BigButton({
  variant = "primary",
  href,
  className,
  children,
  ...props
}: BigButtonProps) {
  const base =
    "inline-flex items-center justify-center gap-2 rounded-md px-[18px] py-[10px] text-sm font-semibold transition-colors";
  const tone = {
    primary:
      "bg-accent text-white shadow-[0_1px_0_rgba(0,0,0,0.06),0_4px_12px_rgba(255,90,54,0.18)] hover:opacity-90",
    secondary:
      "bg-surface text-fg border border-rule hover:border-ruleStrong",
    danger: "bg-danger text-white hover:opacity-90",
  }[variant];

  const cls = clsx(base, tone, className);

  if (href) {
    return (
      <Link href={href} className={cls}>
        {children}
      </Link>
    );
  }
  return (
    <button className={cls} {...props}>
      {children}
    </button>
  );
}

// ─── FilterChip ───────────────────────────────────────────────────

type FilterChipProps = {
  active?: boolean;
  onClick?: () => void;
  children: ReactNode;
};

/**
 * Pill-shaped filter chip. Active = tomato bg / white fg; idle =
 * surface / inkSoft. Used on the home filter row and elsewhere where
 * the design wants a single-select toggle.
 */
export function FilterChip({ active, onClick, children }: FilterChipProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={clsx(
        "rounded-pill border px-3 py-[6px] text-[13px] font-medium transition-colors",
        active
          ? "border-accent bg-accent text-white"
          : "border-rule bg-surface text-inkSoft hover:border-ruleStrong",
      )}
    >
      {children}
    </button>
  );
}

// ─── Panel ────────────────────────────────────────────────────────

type PanelProps = {
  title?: string;
  eyebrow?: string;
  action?: ReactNode;
  className?: string;
  children: ReactNode;
};

/**
 * Surface card with an optional eyebrow + display title and a
 * trailing action slot (used for "+ pair another" / "+ new token"
 * style affordances on the account page).
 */
export function Panel({
  title,
  eyebrow,
  action,
  className,
  children,
}: PanelProps) {
  return (
    <div
      className={clsx(
        "rounded-lg border border-rule bg-surface p-5",
        className,
      )}
    >
      {(title || eyebrow || action) && (
        <div className="mb-3 flex items-baseline justify-between gap-3">
          <div>
            {eyebrow && (
              <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-accent">
                {eyebrow}
              </div>
            )}
            {title && (
              <div className="text-lg font-bold tracking-[-0.02em]">
                {title}
              </div>
            )}
          </div>
          {action && <div>{action}</div>}
        </div>
      )}
      {children}
    </div>
  );
}

// ─── Stat ─────────────────────────────────────────────────────────

/**
 * Small bg-tinted stat tile — mono uppercase label over a display
 * value. Used in the activity panel and anywhere we need a compact
 * KV display.
 */
export function Stat({ k, v }: { k: string; v: string }) {
  return (
    <div className="rounded-md border border-rule bg-bg p-[10px]">
      <div className="font-mono text-[9.5px] uppercase tracking-[0.1em] text-muted">
        {k}
      </div>
      <div className="mt-[2px] text-[22px] font-bold tracking-[-0.02em]">
        {v}
      </div>
    </div>
  );
}

// ─── Legend ───────────────────────────────────────────────────────

/**
 * Small inline dot + sans label, e.g. for the storage breakdown
 * legend on the account page. The dot colour is passed via the
 * `dotClass` Tailwind class (e.g. `bg-accent`) rather than a hex so
 * it inherits the theme.
 */
export function Legend({
  dotClass,
  children,
}: {
  dotClass: string;
  children: ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-[6px]">
      <span className={clsx("h-[7px] w-[7px] rounded-full", dotClass)} />
      {children}
    </span>
  );
}

// ─── UserAvatar (gradient initials pill) ──────────────────────────

/**
 * The design's gradient avatar pill — used in the header and on the
 * profile page's identity card. `ring` adds a soft accent halo for
 * the larger profile-card variant.
 */
export function UserAvatar({
  initials = "MW",
  name,
  size = 34,
  ring = false,
}: {
  initials?: string;
  name?: string;
  size?: number;
  ring?: boolean;
}) {
  return (
    <span
      title={name}
      style={{
        width: size,
        height: size,
        fontSize: Math.round(size * 0.4),
      }}
      className={clsx(
        "inline-flex flex-shrink-0 select-none items-center justify-center rounded-full font-bold tracking-[0.02em] text-white",
        "bg-gradient-to-br from-accent to-accent2",
        ring
          ? "shadow-[0_0_0_3px_rgba(255,90,54,0.18)]"
          : "shadow-[inset_0_0_0_1px_rgba(0,0,0,0.08)]",
      )}
    >
      {initials}
    </span>
  );
}

// ─── Eyebrow (small mono uppercase label) ─────────────────────────

/**
 * Small mono uppercase label that sits above display headings
 * everywhere in the design ("ACCOUNT", "STEP 1 / 3", "YOUR SHELF").
 */
export function Eyebrow({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={clsx(
        "font-mono text-[11px] uppercase tracking-[0.12em] text-muted",
        className,
      )}
    >
      {children}
    </div>
  );
}

// ─── DisplayHeading ───────────────────────────────────────────────

/**
 * Display-weight serif-feeling heading. Tightly tracked, large, no
 * margin — sits directly under an `<Eyebrow>` in every page hero.
 */
export function DisplayHeading({
  children,
  size = "page",
  className,
}: {
  children: ReactNode;
  size?: "page" | "panel" | "h2";
  className?: string;
}) {
  const sizeClass = {
    page: "text-[44px] leading-[1.05]",
    panel: "text-[22px] leading-tight",
    h2: "text-[32px] leading-[1.05]",
  }[size];
  return (
    <h1
      className={clsx(
        "m-0 font-bold tracking-[-0.02em]",
        sizeClass,
        className,
      )}
    >
      {children}
    </h1>
  );
}
