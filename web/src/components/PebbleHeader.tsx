"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { motion } from "motion/react";
import { PebbleMark } from "./PebbleMark";
import { UserAvatarMenu } from "./UserAvatarMenu";

const NAV: { href: string; label: string }[] = [
  { href: "/", label: "captures" },
  { href: "/captures/new", label: "new" },
];

/**
 * Top-of-page header for the Pebble shell. Three-column layout:
 *   left  — brand mark + wordmark + version chip
 *   centre — primary nav with an animated underline on the active item
 *   right — gpu status pill + avatar menu
 *
 * The underline rides on a shared layoutId so navigating between
 * sections produces a single sliding pill rather than separate
 * fade-in / fade-out elements (the only motion element in PR-A).
 */
// Routes that render their own chrome and shouldn't show the
// global header (sign-in's two-column collage owns its top bar; the
// standalone splat viewer popup is fullscreen).
const HEADERLESS = new Set(["/sign-in", "/viewer"]);

export function PebbleHeader() {
  const pathname = usePathname();
  if (pathname && HEADERLESS.has(pathname)) return null;
  return (
    // sticky + z-50 so the avatar dropdown floats over page
    // content on every route — without this, App Router pages with
    // their own stacking contexts (capture cards with translate
    // transforms, the splat viewer canvas) end up rendering on top
    // of the dropdown panel.
    <header className="sticky top-0 z-50 border-b border-rule bg-bg/80 backdrop-blur-sm">
      <div className="mx-auto flex max-w-6xl items-center justify-between gap-6 px-9 py-4">
        <Link href="/" className="flex items-center gap-3">
          <PebbleMark size={28} />
          <span className="text-lg font-semibold tracking-tight">pebble</span>
          <span className="rounded-pill border border-rule px-2 py-[2px] font-mono text-[10px] uppercase tracking-wider text-muted">
            v1.0 · LAN
          </span>
        </Link>

        <nav className="flex items-center gap-1 text-sm">
          {NAV.map((item) => {
            const active = isActive(pathname, item.href);
            return (
              <Link
                key={item.href}
                href={item.href}
                className="relative px-3 py-2 text-fg hover:text-fg"
              >
                {item.label}
                {active && (
                  <motion.span
                    layoutId="nav-underline"
                    className="absolute inset-x-2 -bottom-[1px] h-[2px] rounded-pill bg-accent"
                    transition={{ type: "spring", stiffness: 380, damping: 30 }}
                  />
                )}
              </Link>
            );
          })}
        </nav>

        <div className="flex items-center gap-3">
          <span className="hidden items-center gap-2 rounded-pill border border-rule bg-surface px-3 py-1 font-mono text-[11px] uppercase tracking-wider text-inkSoft sm:flex">
            <span className="h-2 w-2 rounded-pill bg-accent3" /> gpu · idle
          </span>
          <UserAvatarMenu />
        </div>
      </div>
    </header>
  );
}

function isActive(pathname: string | null, href: string) {
  if (!pathname) return false;
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}
