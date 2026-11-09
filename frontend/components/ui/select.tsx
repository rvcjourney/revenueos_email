import * as React from "react";

import { controlClasses } from "@/components/ui/input";
import { cn } from "@/lib/utils";

// Styled native <select>: accessible and keyboard-friendly without a
// dependency, matching Input's look. The browser's arrow is replaced by a
// chevron drawn as a background image.
const chevron =
  "appearance-none bg-[length:1rem] bg-[right_0.7rem_center] bg-no-repeat pr-9 bg-[url('data:image/svg+xml;utf8,%3Csvg%20xmlns%3D%22http%3A//www.w3.org/2000/svg%22%20viewBox%3D%220%200%2024%2024%22%20fill%3D%22none%22%20stroke%3D%22%2394a3b8%22%20stroke-width%3D%222%22%20stroke-linecap%3D%22round%22%20stroke-linejoin%3D%22round%22%3E%3Cpath%20d%3D%22m6%209%206%206%206-6%22/%3E%3C/svg%3E')]";

export const Select = React.forwardRef<
  HTMLSelectElement,
  React.SelectHTMLAttributes<HTMLSelectElement>
>(({ className, children, ...props }, ref) => (
  <select ref={ref} className={cn(controlClasses, "h-10 pl-3", chevron, className)} {...props}>
    {children}
  </select>
));
Select.displayName = "Select";
