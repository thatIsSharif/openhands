export interface AutomationRunItem {
  conversation_id: string;
  title: string | null;
  trigger: string | null;
  selected_repository: string | null;
  selected_branch: string | null;
  jira_issue_key: string | null;
  github_pr: string[];
  pr_number: number[];
  llm_model: string | null;

  // Metrics
  accumulated_cost: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  total_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  reasoning_tokens: number | null;
  context_window: number | null;
  per_turn_token: number | null;
  max_budget_per_task: number | null;

  // Timing
  created_at: string | null;
  last_updated_at: string | null;

  // Error
  error_message?: string | null;
}

export interface AutomationRunListResponse {
  items: AutomationRunItem[];
  total: number;
  page: number;
  per_page: number;
  total_pages: number;
}

export interface AutomationRunsParams {
  page?: number;
  per_page?: number;
  source?: "jira" | "github" | null;
  search?: string | null;
}
