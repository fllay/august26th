"use client";

import { useEffect, useId, useRef, useState } from "react";
import { useQueryState } from "nuqs";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Separator } from "@/components/ui/separator";
import { toast } from "sonner";
import { CheckCircle2, Link2, LoaderCircle, Unplug } from "lucide-react";

type GoogleOauthProfile = {
  name?: string;
  given_name?: string;
  picture?: string;
  email?: string;
};

type GoogleOauthStatus = {
  connected: boolean;
  connectedAt: string | null;
  profile?: GoogleOauthProfile | null;
};

async function fetchStatus(): Promise<GoogleOauthStatus> {
  const response = await fetch("/api/google/oauth/status", {
    cache: "no-store",
  });
  if (!response.ok) {
    throw new Error("Failed to load Google connection status.");
  }
  return (await response.json()) as GoogleOauthStatus;
}

export function GoogleOauthButton() {
  const [status, setStatus] = useState<GoogleOauthStatus>({
    connected: false,
    connectedAt: null,
    profile: null,
  });
  const [loading, setLoading] = useState(true);
  const [disconnecting, setDisconnecting] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [oauthState, setOauthState] = useQueryState("google_oauth");
  const [oauthMessage, setOauthMessage] = useQueryState("message");
  const menuId = useId();
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchStatus()
      .then(setStatus)
      .catch((error) => {
        console.error(error);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    if (!menuOpen) {
      return;
    }

    const handlePointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target;
      if (!(target instanceof Node)) {
        return;
      }
      if (!containerRef.current?.contains(target)) {
        setMenuOpen(false);
      }
    };

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    };

    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  useEffect(() => {
    if (!oauthState) {
      return;
    }

    if (oauthState === "success") {
      toast.success("Google connected", {
        description: "The agent can now use your Google access for connected tools.",
      });
      fetchStatus().then(setStatus).catch(console.error);
    } else if (oauthState === "error") {
      toast.error("Google connection failed", {
        description: oauthMessage || "Please try connecting again.",
        duration: 7000,
      });
    }

    void setOauthState(null);
    void setOauthMessage(null);
  }, [oauthMessage, oauthState, setOauthMessage, setOauthState]);

  const handleDisconnect = async () => {
    setDisconnecting(true);
    try {
      const response = await fetch("/api/google/oauth/disconnect", {
        method: "POST",
      });
      if (!response.ok) {
        throw new Error("Failed to disconnect Google.");
      }
      setMenuOpen(false);
      setStatus({ connected: false, connectedAt: null, profile: null });
      toast.success("Google disconnected", {
        description: "The agent no longer has access to your connected Google tools.",
      });
    } catch (error) {
      console.error(error);
      toast.error("Could not disconnect Google.");
    } finally {
      setDisconnecting(false);
    }
  };

  if (loading) {
    return (
      <Button variant="outline" disabled>
        <LoaderCircle className="size-4 animate-spin" />
        Checking Google
      </Button>
    );
  }

  if (status.connected) {
    const profileName = status.profile?.name?.trim() || status.profile?.given_name?.trim() || "Google account";
    const profileEmail = status.profile?.email?.trim() || "";
    const avatarFallback = (profileName[0] || "G").toUpperCase();
    return (
      <div ref={containerRef} className="relative shrink-0">
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="relative size-9 rounded-full p-0"
          onClick={() => setMenuOpen((open) => !open)}
          disabled={disconnecting}
          title={profileEmail || profileName}
          aria-haspopup="menu"
          aria-expanded={menuOpen}
          aria-controls={menuId}
        >
          {disconnecting ? (
            <LoaderCircle className="size-4 animate-spin" />
          ) : (
            <>
              <Avatar className="size-9 shrink-0">
                <AvatarImage
                  src={status.profile?.picture || ""}
                  alt={profileName}
                  referrerPolicy="no-referrer"
                />
                <AvatarFallback>{avatarFallback}</AvatarFallback>
              </Avatar>
              <span className="absolute right-0.5 bottom-0.5 size-2.5 rounded-full border-2 border-background bg-emerald-500" />
            </>
          )}
        </Button>

        {menuOpen && (
          <div
            id={menuId}
            role="menu"
            aria-label="Google account menu"
            className="bg-popover text-popover-foreground absolute top-full right-0 z-50 mt-2 w-72 rounded-xl border shadow-lg"
          >
            <div className="flex items-start gap-3 p-4">
              <Avatar className="size-10 shrink-0">
                <AvatarImage
                  src={status.profile?.picture || ""}
                  alt={profileName}
                  referrerPolicy="no-referrer"
                />
                <AvatarFallback>{avatarFallback}</AvatarFallback>
              </Avatar>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <p className="truncate text-sm font-medium">{profileName}</p>
                  <CheckCircle2 className="size-3.5 shrink-0 text-emerald-600" />
                </div>
                {profileEmail ? (
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    {profileEmail}
                  </p>
                ) : (
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    Google Connected
                  </p>
                )}
              </div>
            </div>

            <Separator />

            <div className="p-2">
              <button
                type="button"
                role="menuitem"
                className="hover:bg-accent hover:text-accent-foreground flex w-full items-center gap-3 rounded-md px-3 py-2 text-left text-sm"
                onClick={handleDisconnect}
                disabled={disconnecting}
              >
                {disconnecting ? (
                  <LoaderCircle className="size-4 animate-spin" />
                ) : (
                  <Unplug className="size-4" />
                )}
                Disconnect Google
              </button>
            </div>
          </div>
        )}
      </div>
    );
  }

  return (
    <Button
      type="button"
      variant="outline"
      className="gap-2"
      onClick={() => {
        window.location.href = "/api/google/oauth/start";
      }}
    >
      <Link2 className="size-4" />
      Connect Google
    </Button>
  );
}
