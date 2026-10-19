import { Check, Copy } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { writeClipboard } from "@/lib/native";
import { Button } from "./Button";

const FEEDBACK_MS = 1500;

/** A button that puts `text` on the clipboard and says so ("Copied", or "Copy failed" when the clipboard refused). */
export function CopyButton({ text, label = "Copy", title }: { text: string; label?: string; title?: string }) {
  const [result, setResult] = useState<"copied" | "failed" | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = async () => {
    try {
      await writeClipboard(text);
      setResult("copied");
    } catch {
      setResult("failed"); // e.g. the webview denied clipboard access
    }
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setResult(null), FEEDBACK_MS);
  };
  return (
    <Button size="sm" className="shrink-0" title={title} onClick={() => void copy()}>
      {result === "copied" ? <Check size={12} /> : <Copy size={12} />}{" "}
      {result === "copied" ? "Copied" : result === "failed" ? "Copy failed" : label}
    </Button>
  );
}
