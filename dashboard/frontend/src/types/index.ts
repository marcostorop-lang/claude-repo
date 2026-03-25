export interface Overview {
  bot_active: boolean;
  last_update: string | null;
  total_trades: number;
  winning_trades: number;
  losing_trades: number;
  total_pnl: number;
  daily_pnl: number;
  current_exposure: number;
  simulated_balance: number;
  markets_monitored: number;
  open_positions: number;
}

export interface Trade {
  id: number;
  order_id: string;
  token_id: string;
  condition_id: string;
  side: string;
  size: number;
  price: number;
  strategy: string;
  mode: string;
  timestamp: string;
  question?: string;
}

export interface TradesResponse {
  trades: Trade[];
  total: number;
  page: number;
  per_page: number;
  pages: number;
}

export interface EquityPoint {
  time: string;
  equity: number;
}

export interface DailyPnl {
  date: string;
  pnl: number;
}

export interface Performance {
  equity_curve: EquityPoint[];
  daily_pnl: DailyPnl[];
  win_rate: number;
  profit_factor: number;
  max_drawdown: number;
  avg_win: number;
  avg_loss: number;
  reward_risk_ratio: number;
  total_closed: number;
  total_pnl: number;
  winning: number;
  losing: number;
}

export interface Position {
  token_id: string;
  condition_id: string;
  question: string;
  side: string;
  entry_price: number;
  current_price: number;
  size: number;
  unrealised_pnl: number;
  entry_time: string;
  strategy: string;
  pct_to_stop_loss: number;
  pct_to_take_profit: number;
}

export interface MarketStats {
  condition_id: string;
  question: string;
  total_trades: number;
  pnl: number;
  win_rate: number;
  strategies: string;
}

export interface StrategyStats {
  strategy: string;
  total_trades: number;
  closed_trades: number;
  winning: number;
  losing: number;
  win_rate: number;
  total_pnl: number;
  max_drawdown: number;
  rank: number;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  source: string;
  message: string;
}

export interface BotConfig {
  [key: string]: string;
}
