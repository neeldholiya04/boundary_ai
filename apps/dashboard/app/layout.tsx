import type { Metadata } from "next";
import { DashboardShell } from "@/components/dashboard-shell";

import "./globals.css";

export const metadata: Metadata = {
  title: "Boundary",
  description: "Guarded AI workspace with MCP support, live approvals, and policy controls."
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <DashboardShell>{children}</DashboardShell>
      </body>
    </html>
  );
}
