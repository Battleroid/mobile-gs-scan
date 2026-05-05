/**
 * Profile / account placeholder. Visual fidelity to the design's
 * StudioWebAccount — identity card with gradient avatar, storage
 * panel with multi-color usage bar, activity panel with bar chart,
 * paired devices, API tokens, danger zone. None of the buttons do
 * anything yet; auth + tenant data wires in later without changing
 * this layout.
 */
import {
  BigButton,
  DisplayHeading,
  Eyebrow,
  Legend,
  Panel,
  Stat,
  UserAvatar,
} from "@/components/pebble";

export default function AccountPage() {
  return (
    <div className="mx-auto w-full max-w-5xl px-9 py-8">
      <Eyebrow>account</Eyebrow>
      <DisplayHeading className="mt-2">Profile</DisplayHeading>

      <IdentityCard />

      <div className="mt-6 grid grid-cols-1 gap-4 md:grid-cols-2">
        <StoragePanel />
        <ActivityPanel />
      </div>

      <DevicesPanel />
      <TokensPanel />
      <DangerZone />
    </div>
  );
}

function IdentityCard() {
  return (
    <div className="mt-6 flex items-center gap-5 rounded-lg border border-rule bg-surface p-6">
      <UserAvatar initials="MW" size={68} ring />
      <div className="flex-1">
        <div className="text-[22px] font-bold tracking-[-0.02em]">
          Mira Weston
        </div>
        <div className="mt-[2px] font-mono text-[12px] text-inkSoft">
          mira@studio.local · joined Mar 2026
        </div>
        <div className="mt-2 flex gap-[6px]">
          <span className="rounded-pill bg-accent/10 px-[10px] py-[3px] font-mono text-[10px] font-bold uppercase tracking-[0.08em] text-accent">
            owner
          </span>
          <span className="rounded-pill border border-rule bg-bg px-[10px] py-[3px] font-mono text-[10px] font-semibold text-inkSoft">
            workspace · home-lab
          </span>
        </div>
      </div>
      <BigButton variant="secondary">Edit profile</BigButton>
    </div>
  );
}

function StoragePanel() {
  return (
    <Panel eyebrow="usage" title="Storage">
      <div className="flex items-baseline gap-2">
        <div className="text-[32px] font-bold tracking-[-0.02em]">
          284
          <span className="ml-1 text-[18px] text-inkSoft">GB</span>
        </div>
        <div className="font-mono text-[11px] text-inkSoft">
          of 1 TB · home server
        </div>
      </div>
      <div className="mt-[10px] flex h-2 overflow-hidden rounded-full bg-rule">
        <div className="h-full w-[18%] bg-accent" />
        <div className="h-full w-[6%] bg-accent2" />
        <div className="h-full w-[4%] bg-accent3" />
      </div>
      <div className="mt-[10px] flex flex-wrap gap-[14px] font-mono text-[10.5px] text-inkSoft">
        <Legend dotClass="bg-accent">frames · 184 GB</Legend>
        <Legend dotClass="bg-accent2">splats · 62 GB</Legend>
        <Legend dotClass="bg-accent3">meshes · 38 GB</Legend>
      </div>
    </Panel>
  );
}

function ActivityPanel() {
  // Bars are decorative. Once we have a real activity feed,
  // swap the inline array for a query.
  const bars = [
    12, 18, 9, 22, 30, 14, 8, 26, 32, 19, 24, 28, 15, 11, 20, 36, 28, 22, 14,
    10, 18, 24, 30, 26, 18, 12, 22, 30, 34, 28,
  ];
  return (
    <Panel eyebrow="last 30 days" title="Activity">
      <div className="grid grid-cols-3 gap-[10px]">
        <Stat k="captures" v="42" />
        <Stat k="GPU hours" v="11.4" />
        <Stat k="shared" v="6" />
      </div>
      <div className="mt-[14px] flex h-[60px] items-end gap-[3px]">
        {bars.map((h, i) => (
          <div
            key={i}
            className={`flex-1 rounded-[2px] ${i > 25 ? "bg-accent" : "bg-accent/35"}`}
            style={{ height: `${h * 1.7}%` }}
          />
        ))}
      </div>
      <div className="mt-1 flex justify-between font-mono text-[10px] text-muted">
        <span>30d ago</span>
        <span>today</span>
      </div>
    </Panel>
  );
}

function DevicesPanel() {
  return (
    <div className="mt-4">
      <Panel
        eyebrow="phones & sessions"
        title="Paired devices"
        action={
          <span className="cursor-pointer font-mono text-[11px] font-semibold text-accent">
            ＋ pair another
          </span>
        }
      >
        <DeviceRow
          icon="📱"
          name="Pixel 8 Pro · gs-scan"
          sub="Android 15 · v1.4 · 192.168.1.42"
          badge="this device"
          badgeClass="bg-accent/10 text-accent"
        />
        <DeviceRow
          icon="📱"
          name="iPhone 15"
          sub="iOS 18 · v1.3 · last seen 2 days ago"
        />
        <DeviceRow
          icon="🖥"
          name="MacBook Pro · Safari"
          sub="macOS 15 · current session"
          badge="web"
          badgeClass="bg-muted/10 text-inkSoft"
        />
        <DeviceRow
          icon="🖥"
          name="Old laptop · Chrome"
          sub="Windows 11 · last seen 3 weeks ago"
        />
      </Panel>
    </div>
  );
}

function DeviceRow({
  icon,
  name,
  sub,
  badge,
  badgeClass,
}: {
  icon: string;
  name: string;
  sub: string;
  badge?: string;
  badgeClass?: string;
}) {
  return (
    <div className="flex items-center gap-[14px] border-b border-rule py-[12px] last:border-b-0">
      <span className="inline-flex h-9 w-9 items-center justify-center rounded-md border border-rule bg-bg text-base">
        {icon}
      </span>
      <div className="flex-1">
        <div className="text-[14px] font-semibold">{name}</div>
        <div className="mt-[2px] font-mono text-[11px] text-inkSoft">{sub}</div>
      </div>
      {badge && (
        <span
          className={`rounded-pill px-2 py-[3px] font-mono text-[10px] font-bold uppercase tracking-[0.06em] ${badgeClass}`}
        >
          {badge}
        </span>
      )}
      <button
        type="button"
        className="cursor-pointer font-mono text-[11px] font-semibold text-inkSoft hover:text-fg"
      >
        revoke
      </button>
    </div>
  );
}

function TokensPanel() {
  return (
    <div className="mt-4">
      <Panel
        eyebrow="for scripts & ci"
        title="API tokens"
        action={
          <span className="cursor-pointer font-mono text-[11px] font-semibold text-accent">
            ＋ new token
          </span>
        }
      >
        <TokenRow
          name="ci-uploader"
          prefix="pbl_live_4Gx•••"
          lastUsed="3 hours ago"
          scope="captures.write"
        />
        <TokenRow
          name="dataset-mirror"
          prefix="pbl_live_uM2•••"
          lastUsed="yesterday"
          scope="captures.read"
        />
      </Panel>
    </div>
  );
}

function TokenRow({
  name,
  prefix,
  lastUsed,
  scope,
}: {
  name: string;
  prefix: string;
  lastUsed: string;
  scope: string;
}) {
  return (
    <div className="flex items-center gap-[14px] border-b border-rule py-[12px] last:border-b-0">
      <div className="flex-1">
        <div className="flex items-center gap-2">
          <span className="text-[14px] font-semibold">{name}</span>
          <span className="rounded-pill border border-rule bg-bg px-[7px] py-[2px] font-mono text-[10px] text-inkSoft">
            {scope}
          </span>
        </div>
        <div className="mt-[3px] font-mono text-[11px] text-inkSoft">
          {prefix} · last used {lastUsed}
        </div>
      </div>
      <button
        type="button"
        className="cursor-pointer font-mono text-[11px] font-semibold text-inkSoft hover:text-fg"
      >
        copy
      </button>
      <button
        type="button"
        className="cursor-pointer font-mono text-[11px] font-semibold text-danger hover:opacity-80"
      >
        revoke
      </button>
    </div>
  );
}

function DangerZone() {
  return (
    <div className="mt-4 rounded-lg border border-danger/30 bg-surface p-5">
      <div className="font-mono text-[11px] uppercase tracking-[0.12em] text-danger">
        danger zone
      </div>
      <div className="mt-[10px] flex items-center justify-between gap-4">
        <div>
          <div className="text-[15px] font-semibold">Sign out everywhere</div>
          <div className="mt-[2px] text-[12.5px] text-inkSoft">
            Revokes all sessions across web and mobile, including this one.
          </div>
        </div>
        <BigButton variant="secondary">Sign out all</BigButton>
      </div>
      <div className="my-[14px] h-px bg-rule" />
      <div className="flex items-center justify-between gap-4">
        <div>
          <div className="text-[15px] font-semibold">Delete account</div>
          <div className="mt-[2px] text-[12.5px] text-inkSoft">
            Removes your account and all captures from this Studio. Cannot be
            undone.
          </div>
        </div>
        <BigButton variant="danger">Delete account</BigButton>
      </div>
    </div>
  );
}
