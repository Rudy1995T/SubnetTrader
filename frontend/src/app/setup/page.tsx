"use client";

import { useRouter } from "next/navigation";
import SetupWizard from "@/components/SetupWizard";

export default function SetupPage() {
  const router = useRouter();

  return (
    <SetupWizard
      mode="wizard"
      onComplete={() => router.replace("/ema")}
    />
  );
}
