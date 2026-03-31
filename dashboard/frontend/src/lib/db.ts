import Database from "better-sqlite3";
import path from "path";
import fs from "fs";

const DB_PATH = process.env.SQLITE_DB_PATH || path.resolve(process.cwd(), "..", "..", "polymarket_bot.db");

let _db: Database.Database | null = null;

function getDb(): Database.Database {
  if (_db) return _db;

  if (!fs.existsSync(DB_PATH)) {
    // Create DB with schema + mock data
    _db = new Database(DB_PATH);
    _db.pragma("journal_mode = WAL");
    createTables(_db);
    seedMockData(_db);
  } else {
    _db = new Database(DB_PATH, { readonly: true });
  }
  return _db;
}

function createTables(db: Database.Database) {
  db.exec(`
    CREATE TABLE IF NOT EXISTS trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id TEXT NOT NULL,
      token_id TEXT NOT NULL,
      condition_id TEXT NOT NULL,
      side TEXT NOT NULL,
      size REAL NOT NULL,
      price REAL NOT NULL,
      strategy TEXT NOT NULL,
      mode TEXT NOT NULL,
      timestamp TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS price_history (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      token_id TEXT NOT NULL,
      price REAL NOT NULL,
      timestamp TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS markets_cache (
      condition_id TEXT PRIMARY KEY,
      question TEXT,
      data TEXT,
      updated_at TEXT NOT NULL
    );
  `);
}

function seedMockData(db: Database.Database) {
  const now = new Date();
  const markets = [
    ["cond_001", "Will BTC exceed $100k by end of 2026?"],
    ["cond_002", "Will the Fed cut rates in Q2 2026?"],
    ["cond_003", "Will ETH flip BTC market cap?"],
    ["cond_004", "US GDP growth > 3% in 2026?"],
    ["cond_005", "Will AI regulation pass in 2026?"],
    ["cond_006", "Will SpaceX land on Mars by 2030?"],
    ["cond_007", "Democrats win 2026 midterms?"],
    ["cond_008", "Will Solana reach $500?"],
  ];

  const insertMarket = db.prepare("INSERT OR IGNORE INTO markets_cache VALUES (?, ?, ?, ?)");
  const insertTrade = db.prepare("INSERT INTO trades (order_id,token_id,condition_id,side,size,price,strategy,mode,timestamp) VALUES (?,?,?,?,?,?,?,?,?)");

  for (const [cid, q] of markets) {
    insertMarket.run(cid, q, "{}", now.toISOString());
  }

  const strategies = ["simple_momentum", "mean_reversion"];
  const sides = ["BUY", "SELL"];
  let id = 0;

  for (let day = 30; day > 0; day--) {
    const numTrades = 2 + Math.floor(Math.random() * 6);
    for (let j = 0; j < numTrades; j++) {
      id++;
      const m = markets[Math.floor(Math.random() * markets.length)];
      const ts = new Date(now.getTime() - day * 86400000 - Math.random() * 86400000);
      insertTrade.run(
        `paper-mock${String(id).padStart(4, "0")}`,
        `tok_${m[0].slice(-3)}${Math.random() > 0.5 ? "a" : "b"}`,
        m[0],
        sides[Math.floor(Math.random() * 2)],
        +(5 + Math.random() * 45).toFixed(2),
        +(0.15 + Math.random() * 0.7).toFixed(4),
        strategies[Math.floor(Math.random() * 2)],
        "paper",
        ts.toISOString()
      );
    }
  }
}

export function query(sql: string, params: unknown[] = []): Record<string, unknown>[] {
  const db = getDb();
  const stmt = db.prepare(sql);
  return stmt.all(...params) as Record<string, unknown>[];
}

export function queryOne(sql: string, params: unknown[] = []): Record<string, unknown> | undefined {
  const db = getDb();
  const stmt = db.prepare(sql);
  return stmt.get(...params) as Record<string, unknown> | undefined;
}
