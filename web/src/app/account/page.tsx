/**
 * Account placeholder. Visual only — no real session, storage, or
 * device data this phase. Every button is a no-op; the layout is
 * the scaffold that auth wiring will populate later.
 */
export default function AccountPage() {
  return (
    <div className="mx-auto w-full max-w-3xl px-9 py-10">
      <header className="mb-8 flex items-baseline justify-between">
        <div>
          <div className="font-mono text-[10px] uppercase tracking-wider text-muted">
            account · placeholder
          </div>
          <h1 className="mt-1 text-3xl font-semibold tracking-tight">
            Mira Weston
          </h1>
        </div>
        <span className="rounded-pill border border-rule bg-surface px-3 py-1 font-mono text-[10px] uppercase tracking-wider text-inkSoft">
          mira@studio.local
        </span>
      </header>

      <div className="grid gap-5 sm:grid-cols-2">
        <Panel title="storage">
          <div className="mb-3 h-2 overflow-hidden rounded-pill bg-bgAlt">
            <div className="h-full w-1/3 rounded-pill bg-accent" />
          </div>
          <div className="font-mono text-xs text-inkSoft">
            18.2 GB used · 50 GB total
          </div>
        </Panel>

        <Panel title="paired devices">
          <Device label="this laptop" sub="last seen · just now" />
          <Device label="ARCore phone" sub="paired · 2 days ago" />
        </Panel>

        <Panel title="api tokens">
          <button className="text-left text-sm text-muted hover:text-fg">
            no tokens yet · generate
          </button>
        </Panel>

        <Panel title="danger zone" tone="danger">
          <button className="block w-full rounded-md border border-rule bg-bg px-3 py-2 text-left text-sm hover:border-danger">
            sign out everywhere
          </button>
          <button className="mt-2 block w-full rounded-md border border-danger bg-bg px-3 py-2 text-left text-sm text-danger hover:bg-danger/5">
            delete account
          </button>
        </Panel>
      </div>
    </div>
  );
}

function Panel({
  title,
  tone = "default",
  children,
}: {
  title: string;
  tone?: "default" | "danger";
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-rule bg-surface p-5 shadow-sm">
      <div
        className={`mb-3 font-mono text-[10px] uppercase tracking-wider ${tone === "danger" ? "text-danger" : "text-muted"}`}
      >
        {title}
      </div>
      {children}
    </section>
  );
}

function Device({ label, sub }: { label: string; sub: string }) {
  return (
    <div className="flex items-center justify-between border-b border-rule py-2 last:border-b-0">
      <div className="text-sm">{label}</div>
      <div className="font-mono text-[10px] uppercase tracking-wider text-muted">
        {sub}
      </div>
    </div>
  );
}
