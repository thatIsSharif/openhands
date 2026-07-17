import { openHands } from "../open-hands-axios";
import type {
  AutomationRunListResponse,
  AutomationRunItem,
  AutomationRunsParams,
} from "./automation-service.types";

class AutomationServiceApi {
  static async getRuns(
    params: AutomationRunsParams = {},
  ): Promise<AutomationRunListResponse> {
    const { page = 1, per_page = 20, source, search } = params;
    const queryParams = new URLSearchParams({
      page: String(page),
      per_page: String(per_page),
    });
    if (source) queryParams.set("source", source);
    if (search) queryParams.set("search", search);

    const { data } = await openHands.get<AutomationRunListResponse>(
      `/api/v1/automations/runs?${queryParams.toString()}`,
    );
    return data;
  }

  static async getRun(
    conversationId: string,
  ): Promise<AutomationRunItem> {
    const { data } = await openHands.get<AutomationRunItem>(
      `/api/v1/automations/runs/${conversationId}`,
    );
    return data;
  }
}

export default AutomationServiceApi;
