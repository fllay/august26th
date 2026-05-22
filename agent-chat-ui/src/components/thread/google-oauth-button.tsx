"use client";

import { useEffect, useState } from "react";
import { useQueryState } from "nuqs";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
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
  const [oauthState, setOauthState] = useQueryState("google_oauth");
  const [oauthMessage, setOauthMessage] = useQueryState("message");

  useEffect(() => {
    fetchStatus()
      .then(setStatus)
      .catch((error) => {
        console.error(error);
      })
      .finally(() => setLoading(false));
  }, []);

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
      <Button
        type="button"
        variant="outline"
        className="h-auto justify-start gap-3 px-3 py-2"
        onClick={handleDisconnect}
        disabled={disconnecting}
        title={profileEmail || profileName}
      >
        {disconnecting ? (
          <LoaderCircle className="size-4 animate-spin" />
        ) : (
          <Avatar className="size-8 shrink-0">
            <AvatarImage
              src={status.profile?.picture || ""}
              alt={profileName}
              referrerPolicy="no-referrer"
            />
            <AvatarFallback>{avatarFallback}</AvatarFallback>
          </Avatar>
        )}
        <span className="flex min-w-0 flex-1 flex-col items-start text-left">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            {profileName}
            <CheckCircle2 className="size-3.5 text-emerald-600" />
          </span>
          {profileEmail ? (
            <span className="max-w-44 truncate text-xs text-muted-foreground">
              {profileEmail}
            </span>
          ) : (
            <span className="text-xs text-muted-foreground">Google Connected</span>
          )}
        </span>
        <Unplug className="size-4 shrink-0" />
      </Button>
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
