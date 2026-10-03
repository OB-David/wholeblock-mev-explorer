export interface GraphNode { id: string; alias: string; kind: 'user' | 'contract'; address: string }
export interface GraphEdge {
  id: string; tx_hash: string; tx_index: number; order: number; source: string; target: string;
  token_address: string; token_symbol: string; decimals: number; amount_raw: string; amount: string;
  kind: 'transfer' | 'mint' | 'burn'; color: string
}
export interface TokenItem { address: string; symbol: string; decimals: number; color: string }
export interface BalanceChange {
  token_address: string; token_symbol: string; decimals: number; amount_raw: string; amount: string
}
export interface AddressCycle {
  cycle_id: string; nodes: string[]; edge_ids: string[]; token_address_path: string[]; edge_count: number
}
export interface TokenCycle {
  cycle_id: string; anchor_address: string; token_address_path: string[]; address_cycle_ids: string[];
  token_branches?: Array<{token_in_address: string; token_out_address: string; amount_in_raw: string; amount_out_raw: string}>;
  is_branched?: boolean; edge_ids: string[]; amount_delta_raw: string; edge_count: number; address_cycle_count: number
}
export interface Sandwich {
  sandwich_id: string; pool_address: string; attacker_address: string;
  token_in_address: string; token_out_address: string; profit_token_address: string;
  gross_profit_amount_raw: string;
  front_tx_index: number; front_tx_hash: string; front_edge_ids: string[];
  victim_tx_indexes: number[]; victim_tx_hashes: string[]; victim_edge_ids: string[];
  back_tx_index: number; back_tx_hash: string; back_edge_ids: string[]
}
export interface TxItem {
  index: number; hash: string; from: string; to: string | null; status: string;
  step_count: number; edge_count: number; edge_ids: string[]; error: string | null;
  balance_changes: Record<string, BalanceChange[]>; address_cycles: AddressCycle[]; token_cycles: TokenCycle[]
}
export interface BlockGraph {
  schema_version: number;
  block: { number: number; hash: string; timestamp: number; transaction_count: number; successful_traces: number };
  nodes: GraphNode[]; edges: GraphEdge[]; tokens: TokenItem[]; transactions: TxItem[]; sandwiches: Sandwich[]
}
export interface ExplorerBlock {
  number: number; hash: string; timestamp: number; transaction_count: number;
  gas_used: number; gas_limit: number; base_fee: string; scan_status: 'scanned' | 'error' | 'untracked';
  scan_ms: number | null; swap_count: number; arbitrage_count: number; sandwich_count: number
}
export interface ExplorerResponse {
  latest: number; start_block: number | null; session_start_block: number | null;
  history_blocks: number; has_newer: boolean; has_older: boolean;
  last_scanned: number | null; scanning_block: number | null; backfill_block: number | null;
  scanner_error: string | null; backfill_error: string | null; blocks: ExplorerBlock[]
}
