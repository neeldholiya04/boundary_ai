"use client";

import { useEffect, useRef } from "react";

import { API_BASE_URL } from "./api";

export function useLiveRefresh(onEvent: () => void) {
  const callbackRef = useRef(onEvent);

  useEffect(() => {
    callbackRef.current = onEvent;
  }, [onEvent]);

  useEffect(() => {
    const source = new EventSource(`${API_BASE_URL}/api/events/stream`);
    const namedEvents = [
      "chat.user_message",
      "agent.response",
      "agent.planner_error",
      "agent.max_steps_reached",
      "mcp.tools_discovered",
      "mcp.tool_succeeded",
      "mcp.tool_failed",
      "mcp.server_created",
      "mcp.server_updated",
      "mcp.server_refreshed",
      "policy.created",
      "policy.updated",
      "policy.deleted",
      "policy.decision",
      "policy.budget_blocked",
      "approval.requested",
      "approval.approved",
      "approval.denied",
      "approval.expired",
      "approval.invalidated"
    ];

    const handler = (event: MessageEvent<string>) => {
      if (event.type !== "ping") {
        callbackRef.current();
      }
    };

    source.onmessage = handler;
    source.addEventListener("ready", handler);
    namedEvents.forEach((eventName) => source.addEventListener(eventName, handler));

    return () => {
      namedEvents.forEach((eventName) => source.removeEventListener(eventName, handler));
      source.close();
    };
  }, []);
}
