"use client";

import { useEffect, useState } from "react";

type LocalTimeProps = {
  value: string;
};

function formatLocal(value: string): string {
  const normalized = value.includes("T")
    ? value
    : value.replace(" ", "T").replace(/Z?$/, "Z");
  const date = new Date(normalized);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

export function LocalTime({ value }: LocalTimeProps) {
  const [display, setDisplay] = useState(value);

  useEffect(() => {
    setDisplay(formatLocal(value));
  }, [value]);

  return <span suppressHydrationWarning>{display}</span>;
}
