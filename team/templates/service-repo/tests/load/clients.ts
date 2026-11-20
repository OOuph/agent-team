// Starter load script for a game repository run by agent-team (`team-play load --clients N --seconds S`).
// Opens N WebSocket clients against BASE_URL, pairs them into rooms (PROTOCOL.md: join / ping / move), sends a `ping`
// every 1000/rate ms per client, measures the pong round-trip, and prints ONE JSON line at the end — that line is what
// `team-play load` reads. Adapt the join/move messages to the game; keep the output keys.
//
//   npx tsx tests/load/clients.ts --base-url http://127.0.0.1:8080 --clients 20 --seconds 30 --rate 2
//
// Output: {"clients":20,"connected":20,"seconds":30,"messages":1180,"p50_ms":12,"p95_ms":41,"max_ms":88,"errors":0,"disconnects":0,"rejected":0}
import WebSocket from "ws";

type Args = { baseUrl: string; clients: number; seconds: number; rate: number; moves: boolean };

function parseArgs(argv: string[]): Args {
  const get = (name: string, dflt: string) => {
    const i = argv.indexOf(`--${name}`);
    return i >= 0 && argv[i + 1] ? argv[i + 1] : dflt;
  };
  return {
    baseUrl: get("base-url", process.env.BASE_URL || "http://127.0.0.1:8080"),
    clients: Math.max(1, parseInt(get("clients", "10"), 10)),
    seconds: Math.max(1, parseInt(get("seconds", "30"), 10)),
    rate: Math.max(0.1, parseFloat(get("rate", "2"))),
    moves: argv.includes("--moves"),
  };
}

function wsUrl(base: string): string {
  const u = new URL(base);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  if (!u.pathname || u.pathname === "/") u.pathname = "/ws";
  return u.toString();
}

function percentile(sorted: number[], p: number): number {
  if (!sorted.length) return 0;
  const idx = Math.min(sorted.length - 1, Math.max(0, Math.ceil((p / 100) * sorted.length) - 1));
  return sorted[idx];
}

async function main(): Promise<void> {
  const a = parseArgs(process.argv.slice(2));
  const url = wsUrl(a.baseUrl);
  const rtts: number[] = [];
  let connected = 0, messages = 0, errors = 0, disconnects = 0, rejected = 0;
  const sockets: WebSocket[] = [];
  const timers: NodeJS.Timeout[] = [];
  const stopAt = Date.now() + a.seconds * 1000;

  const openOne = (i: number) =>
    new Promise<void>((resolve) => {
      const ws = new WebSocket(url);
      sockets.push(ws);
      const room = `load-${Math.floor(i / 2)}`;          // two clients per room
      ws.on("open", () => {
        connected += 1;
        ws.send(JSON.stringify({ type: "join", room, name: `load-${i}` }));
        const timer = setInterval(() => {
          if (Date.now() >= stopAt || ws.readyState !== WebSocket.OPEN) return;
          ws.send(JSON.stringify({ type: "ping", t: Date.now() }));
          if (a.moves) ws.send(JSON.stringify({ type: "move", turn: 0, from: [0, 0], to: [0, 1], action: "move" }));
          messages += 1;
        }, Math.round(1000 / a.rate));
        timers.push(timer);
        resolve();
      });
      ws.on("message", (data) => {
        try {
          const m = JSON.parse(String(data));
          if (m.type === "pong" && typeof m.t === "number") rtts.push(Date.now() - m.t);
          if (m.type === "error") rejected += 1;
        } catch {
          errors += 1;
        }
      });
      ws.on("error", () => { errors += 1; resolve(); });
      ws.on("close", () => { if (Date.now() < stopAt) disconnects += 1; });
    });

  for (let i = 0; i < a.clients; i++) {
    await openOne(i);
    await new Promise((r) => setTimeout(r, 20));        // ramp: 50 clients/s
  }
  await new Promise((r) => setTimeout(r, Math.max(0, stopAt - Date.now())));
  for (const t of timers) clearInterval(t);
  for (const ws of sockets) { try { ws.close(); } catch { /* ignore */ } }
  await new Promise((r) => setTimeout(r, 200));

  const sorted = [...rtts].sort((x, y) => x - y);
  const out = {
    clients: a.clients, connected, seconds: a.seconds, messages,
    p50_ms: percentile(sorted, 50), p95_ms: percentile(sorted, 95), max_ms: sorted.length ? sorted[sorted.length - 1] : 0,
    samples: sorted.length, errors, disconnects, rejected,
  };
  console.log(JSON.stringify(out));
  process.exit(connected === a.clients && errors === 0 ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(2); });
