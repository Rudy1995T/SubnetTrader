import type { Metadata } from "next";
import "./globals.css";
import ConditionalNav from "@/components/ConditionalNav";

export const metadata: Metadata = {
  title: "SubnetTrader Live",
  description: "Multi-strategy trading control surface",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <ConditionalNav />
        <main className="p-6">{children}</main>
      </body>
    </html>
  );
}
