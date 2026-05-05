"use client";
import { useRouter } from "next/navigation";
import { PebbleMark } from "@/components/PebbleMark";

/**
 * Sign-in placeholder. Visual only — no auth wiring this phase.
 *
 * The "Continue" button no-ops to ``/`` so links stay reachable
 * without a 404. Real auth (sessions, OAuth, magic link) lands in a
 * later effort; the structure here is a pass-through scaffold so the
 * UI doesn't have to change shape when wiring lands.
 */
export default function SignInPage() {
  const router = useRouter();
  return (
    <div className="grid min-h-[calc(100vh-72px)] place-items-center px-6">
      <div className="w-full max-w-sm rounded-lg border border-rule bg-surface p-8 shadow-sm">
        <div className="mb-6 flex items-center gap-3">
          <PebbleMark size={32} />
          <div>
            <div className="text-base font-semibold tracking-tight">pebble</div>
            <div className="font-mono text-[10px] uppercase tracking-wider text-muted">
              sign in
            </div>
          </div>
        </div>
        <form
          className="space-y-3"
          onSubmit={(e) => {
            e.preventDefault();
            router.push("/");
          }}
        >
          <Field label="email" type="email" placeholder="you@studio.local" />
          <Field label="password" type="password" placeholder="••••••••" />
          <button
            type="submit"
            className="w-full rounded-md bg-accent px-4 py-2 text-sm font-medium text-bg transition-colors hover:opacity-90"
          >
            continue
          </button>
        </form>
        <div className="my-5 flex items-center gap-2 text-[11px] text-muted">
          <span className="h-px flex-1 bg-rule" />
          <span className="font-mono uppercase tracking-wider">or</span>
          <span className="h-px flex-1 bg-rule" />
        </div>
        <button
          type="button"
          onClick={() => router.push("/")}
          className="w-full rounded-md border border-rule bg-bg px-4 py-2 text-sm hover:border-ruleStrong"
        >
          continue with Google
        </button>
        <p className="mt-6 text-center font-mono text-[10px] uppercase tracking-wider text-muted">
          connected to studio.local
        </p>
      </div>
    </div>
  );
}

function Field({
  label,
  type,
  placeholder,
}: {
  label: string;
  type: "text" | "email" | "password";
  placeholder?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1 block font-mono text-[10px] uppercase tracking-wider text-muted">
        {label}
      </span>
      <input
        type={type}
        placeholder={placeholder}
        className="w-full rounded-sm border border-rule bg-bg px-3 py-2 text-sm text-fg placeholder:text-muted focus:border-accent focus:outline-none"
      />
    </label>
  );
}
