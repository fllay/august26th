"use client";

import { useEffect, useState } from "react";

type TimeZoneFieldProps = {
  name?: string;
};

export function TimeZoneField({ name = "tz" }: TimeZoneFieldProps) {
  const [value, setValue] = useState("");

  useEffect(() => {
    const resolved = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (resolved) {
      setValue(resolved);
    }
  }, []);

  return <input type="hidden" name={name} value={value} suppressHydrationWarning />;
}
