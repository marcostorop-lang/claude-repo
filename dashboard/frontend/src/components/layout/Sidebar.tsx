"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  LayoutDashboard,
  ArrowLeftRight,
  TrendingUp,
  Crosshair,
  Store,
  Brain,
  Telescope,
  ScrollText,
  Settings,
} from "lucide-react";

const NAV = [
  { href: "/", label: "Overview", icon: LayoutDashboard },
  { href: "/trades", label: "Trades", icon: ArrowLeftRight },
  { href: "/performance", label: "Performance", icon: TrendingUp },
  { href: "/positions", label: "Positions", icon: Crosshair },
  { href: "/markets", label: "Markets", icon: Store },
  { href: "/strategies", label: "Strategies", icon: Brain },
  { href: "/semantic", label: "Semantic", icon: Telescope },
  { href: "/logs", label: "Logs", icon: ScrollText },
  { href: "/config", label: "Config", icon: Settings },
];

export default function Sidebar() {
  const pathname = usePathname();

  return (
    <aside className="w-56 shrink-0 bg-surface-card border-r border-surface-border flex flex-col">
      <div className="h-14 flex items-center px-5 border-b border-surface-border">
        <span className="text-lg font-bold tracking-tight text-white">
          Poly<span className="text-brand-500">Bot</span>
        </span>
      </div>
      <nav className="flex-1 py-3 space-y-0.5 px-2">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = pathname === href;
          return (
            <Link
              key={href}
              href={href}
              className={`flex items-center gap-3 px-3 py-2 rounded-lg text-sm transition-colors ${
                active
                  ? "bg-brand-600/20 text-brand-500 font-medium"
                  : "text-gray-400 hover:text-gray-200 hover:bg-surface-hover"
              }`}
            >
              <Icon size={18} />
              {label}
            </Link>
          );
        })}
      </nav>
      <div className="p-4 border-t border-surface-border text-xs text-gray-500">
        Polymarket Bot v1.0
      </div>
    </aside>
  );
}
