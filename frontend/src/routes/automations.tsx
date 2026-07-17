import React from "react";
import { useSearchParams } from "react-router";
import { useAutomationRuns } from "#/hooks/query/use-automation-runs";
import { Card } from "#/ui/card";
import { Typography } from "#/ui/typography";
import { Pagination } from "#/ui/pagination";
import { cn } from "#/utils/utils";
import type { AutomationRunItem } from "#/api/automation-service/automation-service.types";

// ─── Helpers ──────────────────────────────────────────────────────────

function formatTokens(n: number | null | undefined): string {
  if (n == null || n === 0) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
}

function formatCost(n: number | null | undefined): string {
  if (n == null || n === 0) return "—";
  if (n < 0.01) return "<$0.01";
  return `$${n.toFixed(2)}`;
}

function formatDuration(
  created: string | null | undefined,
  updated: string | null | undefined,
): string {
  if (!created) return "—";
  const start = new Date(created).getTime();
  const end = updated ? new Date(updated).getTime() : Date.now();
  const diff = Math.max(0, end - start);
  const sec = Math.floor(diff / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const s = sec % 60;
  return `${min}m ${s}s`;
}

function formatDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function getRunIcon(item: AutomationRunItem): string {
  if (item.jira_issue_key) return "🎯";
  if (item.github_pr?.length) return "🔀";
  return "🤖";
}

function getRunLabel(item: AutomationRunItem): string {
  if (item.jira_issue_key) return `Jira ${item.jira_issue_key}`;
  if (item.pr_number?.length) return `PR #${item.pr_number[0]}`;
  return "Automation";
}

// ─── Stat Card ────────────────────────────────────────────────────────

function StatCard({
  label,
  value,
  sub,
  icon,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: string;
}) {
  return (
    <Card
      theme="dark"
      gradient="standard"
      className="flex flex-col gap-1 p-4 min-w-[130px] flex-1"
    >
      <div className="flex items-center gap-2">
        <span className="text-lg">{icon}</span>
        <span className="text-xs text-gray-400 font-medium uppercase tracking-wide">
          {label}
        </span>
      </div>
      <span className="text-xl font-bold text-white">{value}</span>
      {sub && <span className="text-xs text-gray-500">{sub}</span>}
    </Card>
  );
}

// ─── Skeleton Row ─────────────────────────────────────────────────────

function SkeletonRow() {
  return (
    <div className="flex items-center gap-4 px-5 py-4 animate-pulse border-b border-white/5 last:border-b-0">
      <div className="w-5 h-5 rounded-full bg-white/10" />
      <div className="flex-1 space-y-1.5">
        <div className="h-4 w-24 rounded bg-white/10" />
        <div className="h-3 w-40 rounded bg-white/5" />
      </div>
      <div className="h-4 w-16 rounded bg-white/10" />
      <div className="h-4 w-16 rounded bg-white/10" />
      <div className="h-4 w-12 rounded bg-white/10" />
    </div>
  );
}

// ─── Empty State ──────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <span className="text-5xl mb-4 opacity-40">🤖</span>
      <Typography variant="h2" className="text-gray-300 mb-2">
        No automation runs yet
      </Typography>
      <Typography variant="span" className="text-gray-500 max-w-md">
        Automation runs triggered by Jira webhooks and GitHub PR reviews
        will appear here once they complete.
      </Typography>
    </div>
  );
}

// ─── Error State ──────────────────────────────────────────────────────

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center py-20 text-center">
      <span className="text-5xl mb-4">⚠️</span>
      <Typography variant="h2" className="text-gray-300 mb-2">
        Failed to load runs
      </Typography>
      <Typography variant="span" className="text-gray-500 mb-6">
        There was an error fetching your automation data. Please try again.
      </Typography>
      <button
        type="button"
        onClick={onRetry}
        className="px-4 py-2 rounded-lg bg-white/10 hover:bg-white/15 text-sm text-white transition-colors cursor-pointer"
      >
        Retry
      </button>
    </div>
  );
}

// ─── Row Detail (expandable) ──────────────────────────────────────────

function RunDetailPanel({ item }: { item: AutomationRunItem }) {
  const detailRows: [string, string | null][] = [
    ["Conversation ID", item.conversation_id],
    ["Repository", item.selected_repository],
    ["Branch", item.selected_branch],
    ["Model", item.llm_model],
    ["Context Window", item.context_window ? String(item.context_window) : null],
    ["Per-turn Token Limit", item.per_turn_token ? String(item.per_turn_token) : null],
    ["Max Budget", item.max_budget_per_task ? `$${item.max_budget_per_task.toFixed(2)}` : null],
    ["Created", formatDate(item.created_at)],
    ["Last Updated", formatDate(item.last_updated_at)],
  ]
    .filter(([, v]) => v != null)
    .filter(([, v]) => v !== "—");

  return (
    <div className="border-t border-white/5 bg-white/[0.015]">
      <div className="px-5 py-4 grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-3">
        {detailRows.map(([label, value]) => (
          <div key={label} className="flex flex-col gap-0.5">
            <span className="text-[10px] uppercase tracking-wider text-gray-500 font-medium">
              {label}
            </span>
            <span className="text-xs text-gray-300 font-mono break-all">
              {value}
            </span>
          </div>
        ))}
        {/* Token breakdown */}
        <div className="flex flex-col gap-0.5 col-span-2">
          <span className="text-[10px] uppercase tracking-wider text-gray-500 font-medium">
            Token Breakdown
          </span>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-gray-300 font-mono">
            <span>Prompt: {formatTokens(item.prompt_tokens)}</span>
            <span>Completion: {formatTokens(item.completion_tokens)}</span>
            {item.cache_read_tokens ? <span>Cache Read: {formatTokens(item.cache_read_tokens)}</span> : null}
            {item.cache_write_tokens ? <span>Cache Write: {formatTokens(item.cache_write_tokens)}</span> : null}
            {item.reasoning_tokens ? <span>Reasoning: {formatTokens(item.reasoning_tokens)}</span> : null}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Main Dashboard Component ─────────────────────────────────────────

function AutomationsPage() {
  const [searchParams, setSearchParams] = useSearchParams();

  const page = Number(searchParams.get("page") || "1");
  const source = (searchParams.get("source") as "jira" | "github" | null) || null;
  const search = searchParams.get("search") || null;

  const { data, isLoading, isError, refetch } = useAutomationRuns({
    page,
    per_page: 20,
    source,
    search,
  });

  const [expandedRow, setExpandedRow] = React.useState<string | null>(null);
  const [searchQuery, setSearchQuery] = React.useState(search ?? "");

  React.useEffect(() => {
    setSearchQuery(search ?? "");
  }, [search]);

  const toggleRow = (id: string) => {
    setExpandedRow((prev) => (prev === id ? null : id));
  };

  const updateParam = (key: string, value: string | null) => {
    const next = new URLSearchParams(searchParams);
    if (value) {
      next.set(key, value);
    } else {
      next.delete(key);
    }
    // Reset to page 1 on filter/search change
    if (key !== "page") next.set("page", "1");
    setSearchParams(next);
  };

  const handleSearch = (e: React.FormEvent) => {
    e.preventDefault();
    updateParam("search", searchQuery.trim() || null);
  };

  const handleSearchQueryChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const value = e.target.value;
    setSearchQuery(value);
    if (value.trim() === "" && search != null) {
      updateParam("search", null);
    }
  };

  // Compute aggregate stats
  const stats = React.useMemo(() => {
    if (!data?.items) {
      return { total: 0, totalCost: 0, todayCount: 0, avgCost: 0 };
    }
    // DB timestamps are UTC — compare in UTC
    const now = new Date();
    const todayStart = Date.UTC(
      now.getUTCFullYear(),
      now.getUTCMonth(),
      now.getUTCDate(),
    );
    let totalCost = 0;
    let todayCount = 0;
    for (const item of data.items) {
      totalCost += item.accumulated_cost ?? 0;
      if (item.created_at) {
        const ts = new Date(item.created_at).getTime();
        if (ts >= todayStart) {
          todayCount++;
        }
      }
    }
    return {
      total: data.total,
      totalCost,
      todayCount,
      avgCost: data.total > 0 ? totalCost / data.total : 0,
    };
  }, [data]);

  return (
    <div className="h-full flex flex-col bg-transparent overflow-y-auto custom-scrollbar">
      <div className="flex-1 px-6 py-6 lg:px-[42px] lg:py-[32px] max-w-[1400px] mx-auto w-full">
        {/* ── Header ──────────────────────────────────────────────── */}
        <div className="flex items-center justify-between mb-6">
          <div>
            <Typography variant="h1" className="text-white">
              🤖 Automation Runs
            </Typography>
            <Typography variant="span" className="text-gray-500 text-sm mt-3">
              Monitor Jira and GitHub automation activity
            </Typography>
          </div>
        </div>

        {/* ── Stat Cards ──────────────────────────────────────────── */}
        <div className="flex flex-wrap gap-3 mb-6">
          <StatCard
            icon="📊"
            label="Total Runs"
            value={String(stats.total)}
          />
          <StatCard
            icon="💰"
            label="Total Cost"
            value={formatCost(stats.totalCost)}
            sub="Accumulated across all runs"
          />
          <StatCard
            icon="📅"
            label="Today"
            value={String(stats.todayCount)}
            sub="Runs started today"
          />
          <StatCard
            icon="⚡"
            label="Avg Cost / Run"
            value={formatCost(stats.avgCost)}
          />
        </div>

        {/* ── Filters ─────────────────────────────────────────────── */}
        <Card
          theme="dark"
          className="flex flex-col sm:flex-row items-start sm:items-center gap-3 p-3 mb-4"
        >
          {/* Search */}
          <form onSubmit={handleSearch} className="flex-1 flex gap-2 w-full sm:w-auto">
            <input
              type="text"
              value={searchQuery}
              onChange={handleSearchQueryChange}
              placeholder="Search by issue, title, or repo..."
              className="flex-1 px-3 py-1.5 rounded-lg bg-white/5 border border-white/10 text-sm text-white placeholder-gray-500 focus:outline-none focus:border-white/20 transition-colors"
            />
            <button
              type="submit"
              className="px-3 py-1.5 rounded-lg bg-white/10 hover:bg-white/15 text-xs text-gray-300 transition-colors cursor-pointer"
            >
              Search
            </button>
          </form>

          {/* Source filter */}
          <div className="flex items-center gap-1.5">
            {(["all", "jira", "github"] as const).map((s) => {
              const active = s === "all" ? !source : source === s;
              return (
                <button
                  key={s}
                  type="button"
                  onClick={() => updateParam("source", s === "all" ? null : s)}
                  className={cn(
                    "px-3 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer",
                    active
                      ? "bg-white/15 text-white shadow-sm"
                      : "text-gray-400 hover:text-white hover:bg-white/5",
                  )}
                >
                  {s === "all" ? "All" : s === "jira" ? "🎯 Jira" : "🔀 GitHub"}
                </button>
              );
            })}
          </div>
        </Card>

        {/* ── Table ───────────────────────────────────────────────── */}
        <Card theme="dark" className="flex-col w-full overflow-hidden">
          {/* Column headers */}
          <div className="hidden md:flex items-center gap-4 px-5 py-3 border-b border-white/5 text-[11px] uppercase tracking-wider text-gray-500 font-medium">
            <div className="w-5 flex-shrink-0" />
            <div className="flex-1 min-w-0">Run</div>
            <div className="w-24 text-right">Model</div>
            <div className="w-20 text-right">Tokens</div>
            <div className="w-20 text-right">Cost</div>
            <div className="w-24 text-right">Duration</div>
            <div className="w-28 text-right hidden lg:block">Date</div>
          </div>

          {/* Loading state */}
          {isLoading && (
            <div>
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </div>
          )}

          {/* Error state */}
          {isError && <ErrorState onRetry={() => refetch()} />}

          {/* Empty state */}
          {!isLoading && !isError && data?.items.length === 0 && <EmptyState />}

          {/* Rows */}
          {!isLoading &&
            !isError &&
            (data?.items ?? []).map((item) => {
              const isExpanded = expandedRow === item.conversation_id;
              return (
                <React.Fragment key={item.conversation_id}>
                  <button
                    type="button"
                    onClick={() => toggleRow(item.conversation_id)}
                    className={cn(
                      "w-full flex items-center gap-4 px-5 py-3.5 text-left transition-colors cursor-pointer group",
                      "hover:bg-white/[0.03] border-b border-white/5 last:border-b-0",
                      isExpanded && "bg-white/[0.02]",
                    )}
                  >
                    {/* Expand icon */}
                    <div className="w-5 flex-shrink-0 text-gray-500 group-hover:text-gray-300 transition-colors">
                      <svg
                        className={cn(
                          "w-3.5 h-3.5 transition-transform duration-200",
                          isExpanded && "rotate-90",
                        )}
                        viewBox="0 0 12 12"
                        fill="none"
                      >
                        <path
                          d="M4.5 2.5L8 6L4.5 9.5"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          strokeLinecap="round"
                          strokeLinejoin="round"
                        />
                      </svg>
                    </div>

                    {/* Run info */}
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="text-sm font-medium text-white truncate">
                          {getRunIcon(item)} {getRunLabel(item)}
                        </span>
                        {item.jira_issue_key && item.github_pr?.length ? (
                          <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-blue-500/10 text-blue-400 border border-blue-500/20 whitespace-nowrap">
                            +{item.github_pr.length} PR
                            {item.github_pr.length > 1 ? "s" : ""}
                          </span>
                        ) : null}
                        {item.github_pr?.length ? (
                          <a
                            href={item.github_pr[0]}
                            target="_blank"
                            rel="noreferrer"
                            className="text-[10px] px-1.5 py-0.5 rounded-full bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 overflow-hidden text-ellipsis whitespace-nowrap max-w-[160px] block"
                          >
                            {item.github_pr[0].replace(/^https?:\/\//, "")}
                          </a>
                        ) : null}
                      </div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-xs text-gray-500 truncate max-w-[200px] sm:max-w-[300px]">
                          {item.title || item.selected_repository || "—"}
                        </span>
                        {item.selected_repository && (
                          <span className="text-[10px] text-gray-600 truncate hidden sm:inline">
                            {item.selected_repository}
                          </span>
                        )}
                      </div>
                    </div>

                    {/* Model */}
                    <div className="w-24 text-right hidden md:block">
                      <span className="text-xs text-gray-400 font-mono">
                        {(item.llm_model ?? "—").split("/").pop()}
                      </span>
                    </div>

                    {/* Tokens */}
                    <div className="w-20 text-right">
                      <span className="text-xs text-gray-400 font-mono">
                        {formatTokens(item.total_tokens)}
                      </span>
                    </div>

                    {/* Cost */}
                    <div className="w-20 text-right">
                      <span className="text-xs font-mono text-gray-400">
                        {formatCost(item.accumulated_cost)}
                      </span>
                    </div>

                    {/* Duration */}
                    <div className="w-24 text-right">
                      <span className="text-xs text-gray-400 font-mono">
                        {formatDuration(item.created_at, item.last_updated_at)}
                      </span>
                    </div>

                    {/* Date */}
                    <div className="w-28 text-right hidden lg:block">
                      <span className="text-xs text-gray-500">
                        {formatDate(item.created_at)}
                      </span>
                    </div>
                  </button>

                  {/* Expanded detail */}
                  {isExpanded && <RunDetailPanel item={item} />}
                </React.Fragment>
              );
            })}
        </Card>

        {/* ── Pagination ──────────────────────────────────────────── */}
        {data && data.total_pages > 1 && (
          <div className="mt-4">
            <Pagination
              currentPage={data.page}
              totalPages={data.total_pages}
              onPageChange={(p) => updateParam("page", String(p))}
              className="text-gray-400"
            />
          </div>
        )}

        {/* Footer count */}
        {data && data.total > 0 && (
          <div className="mt-3 text-center">
            <span className="text-xs text-gray-600">
              Showing {(data.page - 1) * data.per_page + 1}–
              {Math.min(data.page * data.per_page, data.total)} of {data.total} runs
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

export default AutomationsPage;
