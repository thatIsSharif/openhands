import { useQuery } from "@tanstack/react-query";
import AutomationServiceApi from "#/api/automation-service/automation-service.api";
import type { AutomationRunsParams } from "#/api/automation-service/automation-service.types";

export const automationRunsQueryKeys = {
  all: ["automation-runs"] as const,
  list: (params: AutomationRunsParams) =>
    ["automation-runs", params] as const,
  detail: (conversationId: string) =>
    ["automation-run", conversationId] as const,
};

export const useAutomationRuns = (params: AutomationRunsParams = {}) =>
  useQuery({
    queryKey: automationRunsQueryKeys.list(params),
    queryFn: () => AutomationServiceApi.getRuns(params),
    staleTime: 10_000, // 10 seconds — data updates as runs complete
    gcTime: 60_000,
  });

export const useAutomationRun = (conversationId: string | undefined) =>
  useQuery({
    queryKey: automationRunsQueryKeys.detail(conversationId ?? ""),
    queryFn: () => AutomationServiceApi.getRun(conversationId!),
    enabled: !!conversationId,
    staleTime: 10_000,
    gcTime: 60_000,
  });
