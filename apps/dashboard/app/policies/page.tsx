import { redirect } from "next/navigation";

// Tool policies live on Guardrails now, next to the content rules.
export default function PoliciesPage() {
  redirect("/guardrails#tool-policies");
}
