"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect, useRef, useState } from "react";

/**
 * Avatar pill in the top-right of every page, expanding to a small
 * dropdown with Account + Sign out items. Both items link to
 * placeholder pages this phase — auth wiring lands in a later effort.
 *
 * Initials + display name are hardcoded ("MW" / "Mira Weston") until
 * a real session shape exists; the props let the future auth layer
 * pass real values without changing call sites.
 */
export function UserAvatarMenu({
  initials = "MW",
  name = "Mira Weston",
}: {
  initials?: string;
  name?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement | null>(null);
  const pathname = usePathname();
  const [prevPath, setPrevPath] = useState(pathname);

  // The header lives in the root layout and stays mounted across
  // App Router navigations, so without this the dropdown would
  // remain open on the destination page after the user clicks
  // Account or Sign out. Reacting to pathname (rather than the link's
  // onClick) also covers browser back/forward, cmd-click → soft-nav,
  // and any future programmatic navigation.
  //
  // The setState-during-render shape (rather than useEffect) is the
  // React docs' "Resetting state when a prop changes" recipe — React
  // skips the open=true render and re-runs the component immediately
  // with open=false, so the destination page never paints with the
  // menu still showing.
  if (prevPath !== pathname) {
    setPrevPath(pathname);
    setOpen(false);
  }

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (!root.current) return;
      if (!root.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("mousedown", onPointer);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("mousedown", onPointer);
      window.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div ref={root} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        className="flex items-center gap-2 rounded-pill border border-rule bg-surface px-2 py-1 text-sm hover:border-ruleStrong"
      >
        <span className="grid h-7 w-7 place-items-center rounded-pill bg-fg text-[11px] font-semibold tracking-wide text-bg">
          {initials}
        </span>
        <span className="hidden pr-1 text-fg sm:inline">{name}</span>
      </button>
      {open && (
        <div
          role="menu"
          className="absolute right-0 top-full z-50 mt-2 w-48 rounded-md border border-rule bg-surface p-1 shadow-lg"
        >
          <MenuLink href="/account" label="Account" sub="profile + storage" />
          <MenuLink href="/sign-in" label="Sign out" sub="ends this session" />
        </div>
      )}
    </div>
  );
}

function MenuLink({
  href,
  label,
  sub,
}: {
  href: string;
  label: string;
  sub: string;
}) {
  return (
    <Link
      role="menuitem"
      href={href}
      className="block rounded-sm px-3 py-2 hover:bg-bgAlt"
    >
      <div className="text-sm text-fg">{label}</div>
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted">
        {sub}
      </div>
    </Link>
  );
}
