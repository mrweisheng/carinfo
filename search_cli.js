#!/usr/bin/env node
"use strict";

/**
 * carinfo 搜索 CLI —— 调用线上 carinfo HTTP API 的交互式终端。
 *
 * 用法：
 *   node search_cli.js
 *   node search_cli.js --limit 8
 *   node search_cli.js --base-url https://searchcar.eazycar.top --key <你的key>
 *
 * 说明：
 * - 只依赖 Node 内置模块（readline / fs / path）+ 全局 fetch（Node >= 18）。
 * - Base URL：CARINFO_API_BASE，默认 https://searchcar.eazycar.top
 * - 鉴权：请求头 X-API-Key。取值顺序：--key 参数 > 环境变量 CARINFO_API_KEY
 *   > 环境变量 SEARCH_API_KEY > 仓库根 .env 的 SEARCH_API_KEY。
 *   （刻意不把密钥字面量写进本文件：本文件会被 git 跟踪，写进去 push 即泄漏。）
 * - /search 自带服务端 LLM 解析，最长 45s；此处请求超时设为 50s。
 * - 启动后立即提示输入；每次回车把文本作为查询发给 /search 并打印结果；
 *   循环往复，Ctrl+C 或关闭终端即退出。
 */

const fs = require("fs");
const path = require("path");
const readline = require("readline");

const ROOT = __dirname;
const DEFAULT_BASE_URL = "https://searchcar.eazycar.top";
const SEARCH_TIMEOUT_MS = 50_000;
const HEALTH_TIMEOUT_MS = 5_000;

// ---------------------------------------------------------------------------
// 配置加载
// ---------------------------------------------------------------------------
function loadDotEnv() {
  const envPath = path.join(ROOT, ".env");
  if (!fs.existsSync(envPath)) return;
  for (const raw of fs.readFileSync(envPath, "utf8").split(/\r?\n/)) {
    const m = raw.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    let val = m[2].trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (process.env[m[1]] === undefined) process.env[m[1]] = val;
  }
}

function parseArgs(argv) {
  const out = {
    baseUrl: process.env.CARINFO_API_BASE || DEFAULT_BASE_URL,
    limit: 5,
    key: null,
    help: false,
  };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--base-url" || a === "-u") out.baseUrl = argv[++i] || out.baseUrl;
    else if (a === "--limit" || a === "-n") out.limit = Number(argv[++i]) || out.limit;
    else if (a === "--key" || a === "-k") out.key = argv[++i] || out.key;
    else if (a === "--help" || a === "-h") out.help = true;
  }
  out.baseUrl = String(out.baseUrl).replace(/\/+$/, "");
  return out;
}

function resolveApiKey(cliKey) {
  return (
    cliKey ||
    process.env.CARINFO_API_KEY ||
    process.env.SEARCH_API_KEY ||
    ""
  ).trim();
}

// ---------------------------------------------------------------------------
// 日志
// ---------------------------------------------------------------------------
function stamp() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function log(msg) {
  console.log(`${stamp()}  ${msg}`);
}
function logErr(msg) {
  console.error(`${stamp()}  ${msg}`);
}

const USAGE = `carinfo 搜索 CLI

用法：
  node search_cli.js [--base-url URL] [--limit N] [--key KEY]

参数：
  -u, --base-url   搜索 API 地址（默认 ${DEFAULT_BASE_URL}）
  -n, --limit      每组返回条数（默认 5）
  -k, --key        X-API-Key（默认取 CARINFO_API_KEY / SEARCH_API_KEY / .env）
  -h, --help       显示本帮助

交互：
  输入自然语言查询（如「五十萬以內的阿尔法」）回车即可；Ctrl+C 退出。
`;

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
function headers(apiKey) {
  const h = { Accept: "application/json" };
  if (apiKey) h["X-API-Key"] = apiKey;
  return h;
}

async function fetchJson(url, apiKey, timeoutMs) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const res = await fetch(url, { headers: headers(apiKey), signal: ctrl.signal });
    let data = null;
    try { data = await res.json(); } catch { /* 非 JSON 响应 */ }
    return { res, data };
  } catch (e) {
    if (e.name === "AbortError") {
      return { res: null, data: null, error: `请求超时（>${Math.round(timeoutMs / 1000)}s）` };
    }
    return { res: null, data: null, error: e.message };
  } finally {
    clearTimeout(timer);
  }
}

async function checkHealth(baseUrl, apiKey) {
  const { res, data, error } = await fetchJson(`${baseUrl}/health`, apiKey, HEALTH_TIMEOUT_MS);
  if (error) {
    logErr(`[warn] 连不上 API（${baseUrl}）：${error}`);
    return;
  }
  if (res.ok) {
    log(`[ok] API 健康：${(data && data.detail) || "正常"}`);
  } else {
    logErr(`[warn] API 返回 ${res.status}：${(data && data.detail) || "服务异常"}`);
  }
}

async function doSearch(baseUrl, apiKey, limit, q) {
  const url = new URL(`${baseUrl}/search`);
  url.searchParams.set("q", q);
  url.searchParams.set("limit", String(limit));

  const t0 = Date.now();
  const { res, data, error } = await fetchJson(url, apiKey, SEARCH_TIMEOUT_MS);
  const ms = Date.now() - t0;

  if (error) {
    logErr(`[错误] 请求失败：${error}`);
    return;
  }
  if (!res.ok) {
    const detail = (data && data.detail) || "";
    if (res.status === 401) logErr("[401] 鉴权失败：X-API-Key 不匹配。检查 .env 的 SEARCH_API_KEY。");
    else if (res.status === 503) logErr(`[503] 服务不可用：${detail}`);
    else if (res.status === 400) logErr(`[400] 查询被拒（条件过宽？）：${detail}`);
    else logErr(`[${res.status}] 请求失败：${detail}`);
    return;
  }
  renderResult(data, ms);
}

// ---------------------------------------------------------------------------
// 渲染
// ---------------------------------------------------------------------------
function priceText(it) {
  if (it.price_text) return it.price_text;
  if (it.price != null) return `HK$${Number(it.price).toLocaleString("en-US")}`;
  return "价格未知";
}

function withUnit(v, unit) {
  const s = String(v).trim();
  return s.includes(unit) || /[a-z]/i.test(s) ? s : `${s}${unit}`;
}

function renderItem(idx, it) {
  const head = [it.year, it.car_brand, it.car_model]
    .map((x) => String(x == null ? "" : x).trim())
    .filter(Boolean)
    .join(" ");

  const bits = [priceText(it)];
  if (it.seats) bits.push(withUnit(it.seats, "座"));
  if (it.engine_volume) bits.push(withUnit(it.engine_volume, "cc"));
  if (it.mileage_km != null) bits.push(`${Number(it.mileage_km).toLocaleString("en-US")}km`);
  if (it.hand_count != null) bits.push(`${it.hand_count}手`);
  if (it.import_type) bits.push(it.import_type);
  if (it.price_ratio != null) bits.push(`比价 ${Number(it.price_ratio).toFixed(2)}`);

  console.log("");
  console.log(`  ${idx}. ${head || it.vehicle_id || "(无车型)"}`);
  console.log(`     ${bits.join(" | ")}`);
  if (Array.isArray(it.labels) && it.labels.length) {
    console.log(`     标签: ${it.labels.join(" · ")}`);
  }
  if (it.market_basis) console.log(`     行情: ${it.market_basis}`);
  if (it.explain) console.log(`     说明: ${it.explain}`);
  if (it.contact_display) console.log(`     联系: ${it.contact_display}`);
  if (it.car_url) console.log(`     链接: ${it.car_url}`);
  if (it.image_url) console.log(`     图片: ${it.image_url}`);
}

function renderResult(data, ms) {
  const items = Array.isArray(data.items) ? data.items : [];
  const total = data.total_matched != null ? data.total_matched : items.length;

  console.log("");
  log(`命中 ${total} 条，返回 ${items.length} 条（解析: ${data.parse_source || "-"}，${ms}ms）`);
  if (Array.isArray(data.relaxed) && data.relaxed.length) {
    log(`已放宽条件: ${data.relaxed.join(", ")}`);
  }
  if (data.summary) {
    console.log("");
    console.log(`  ${data.summary}`);
  }
  if (Array.isArray(data.notes)) {
    for (const n of data.notes) console.log(`  · ${n}`);
  }
  if (Array.isArray(data.query_groups) && data.query_groups.length > 1) {
    for (const g of data.query_groups) {
      console.log(`  【${g.label || "组"}】命中 ${g.total_matched} 条`);
    }
  }
  items.forEach((it, i) => renderItem(i + 1, it));
  console.log("");
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------
async function main() {
  loadDotEnv();
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    process.stdout.write(USAGE);
    return;
  }

  const apiKey = resolveApiKey(args.key);

  log("carinfo 搜索 CLI 已启动");
  log(`API: ${args.baseUrl}    limit=${args.limit}`);
  if (apiKey) {
    log(`鉴权: X-API-Key = ****${apiKey.slice(-4)}`);
  } else {
    logErr("鉴权: 未找到 API Key（--key / CARINFO_API_KEY / SEARCH_API_KEY / .env）");
  }
  log("按 Ctrl+C 退出。");

  // 后台探活，不阻塞输入提示
  checkHealth(args.baseUrl, apiKey);

  const rl = readline.createInterface({
    input: process.stdin,
    output: process.stdout,
    terminal: process.stdin.isTTY === true,
  });
  rl.setPrompt("查询> ");
  rl.prompt();

  let busy = false;
  let exiting = false;   // stdin 结束（EOF / 管道）后，等最后一条查询收尾再退

  rl.on("line", async (line) => {
    if (busy) return;
    const q = line.trim();
    if (!q) {
      if (!exiting) rl.prompt();
      return;
    }
    busy = true;
    rl.pause();
    try {
      await doSearch(args.baseUrl, apiKey, args.limit, q);
    } catch (e) {
      logErr(`[错误] ${e.message}`);
    }
    busy = false;
    if (exiting) {
      log("输入已结束，退出。");
      process.exit(0);
    }
    rl.resume();
    rl.prompt();
  });

  rl.on("SIGINT", () => {
    console.log("");
    log("已退出。");
    rl.close();
    process.exit(0);
  });

  rl.on("close", () => {
    if (busy) { exiting = true; return; }   // 有查询在跑 → 等它完成
    log("输入已结束，退出。");
    process.exit(0);
  });
}

main().catch((e) => {
  logErr(`[致命] ${e.stack || e.message}`);
  process.exit(1);
});
