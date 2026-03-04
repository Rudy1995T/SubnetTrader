import type { Metadata } from "next";
import "./globals.css";
import NavBar from "@/components/NavBar";

export const metadata: Metadata = {
  title: "SubnetTrader Dashboard",
  description: "Bittensor Subnet Alpha Trading Bot",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body className="min-h-screen">
        <NavBar />
        <main className="p-6">{children}</main>
      </body>
    </html>
  );
}
