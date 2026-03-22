"use client";

import { usePathname } from "next/navigation";
import NavBar from "./NavBar";

export default function ConditionalNav() {
  const pathname = usePathname();
  if (pathname.startsWith("/setup")) return null;
  return <NavBar />;
}
