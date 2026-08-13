"use client";
import { LayoutDashboard, LineChart, ListOrdered, Settings, Bot } from "lucide-react";
import clsx from "clsx";

interface NavItem {
  icon: any;
  label: string;
  target: string;
}

const NAV: NavItem[] = [
  { icon: LayoutDashboard, label: "Overview", target: "overview" },
  { icon: LineChart, label: "Analytics", target: "analytics" },
  { icon: ListOrdered, label: "Decision Log", target: "decision-log" },
  { icon: Settings, label: "Bot Control", target: "settings" },
];

function scrollTo(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

export default function Sidebar() {
  return (
    <aside
      className="group fixed left-0 top-0 z-30 h-screen w-16 hover:w-56 transition-all duration-300
                 bg-[#0b0f14] border-r border-[#30363d] flex flex-col overflow-hidden"
    >
      {/* Logo */}
      <div className="flex items-center gap-3 px-4 h-16 border-b border-[#30363d] shrink-0">
        <div className="w-8 h-8 rounded-lg bg-[#00C896] flex items-center justify-center text-black font-bold shrink-0">
          <Bot size={18} />
        </div>
        <span className="font-semibold whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-200">
          ForexBot
        </span>
      </div>

      {/* Nav */}
      <nav className="flex-1 py-4 space-y-1">
        {NAV.map(({ icon: Icon, label, target }) => (
          <button
            key={target}
            onClick={() => scrollTo(target)}
            className="w-full flex items-center gap-4 px-5 py-3 text-gray-400 hover:text-white hover:bg-[#161b22] transition"
          >
            <Icon size={18} className="shrink-0" />
            <span className="whitespace-nowrap text-sm opacity-0 group-hover:opacity-100 transition-opacity duration-200">
              {label}
            </span>
          </button>
        ))}
      </nav>

      {/* Footer badge */}
      <div className="p-4 border-t border-[#30363d] shrink-0">
        <div className={clsx("flex items-center gap-3 text-xs text-gray-500")}>
          <span className="w-2 h-2 rounded-full bg-yellow-400 shrink-0" />
          <span className="whitespace-nowrap opacity-0 group-hover:opacity-100 transition-opacity duration-200">
            Paper · Testnet
          </span>
        </div>
      </div>
    </aside>
  );
}
