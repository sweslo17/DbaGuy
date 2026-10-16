import { Separator } from "react-resizable-panels";
import { cn } from "@/lib/cn";

/**
 * The 1 px divider between two resizable panels, accented while hovered, dragged or focused. `orientation` is the
 * Group's: a horizontal group is split by a vertical line.
 */
export function PanelHandle({ orientation }: { orientation: "horizontal" | "vertical" }) {
  return (
    <Separator
      className={cn(
        orientation === "horizontal" ? "w-px" : "h-px",
        "bg-border outline-none data-[separator=active]:bg-accent data-[separator=focus]:bg-accent data-[separator=hover]:bg-accent",
      )}
    />
  );
}
