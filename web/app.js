"use strict";
const el = (id) => document.getElementById(id);
let token = "",
  extraArgs = [],
  selected = 1,
  following = true,
  state = {},
  treeKey = "",
  treeSession = "";
let expandedPaths = new Set();
const names = {
  P: "歩",
  L: "香",
  N: "桂",
  S: "銀",
  G: "金",
  B: "角",
  R: "飛",
  K: "玉",
  "+P": "と",
  "+L": "杏",
  "+N": "圭",
  "+S": "全",
  "+B": "馬",
  "+R": "龍",
};
async function api(path, body) {
  const response = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Makebook-Token": token,
          },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "読み込みに失敗しました");
  return data;
}
function draw(sfen) {
  const [rows, turn, hands] = sfen.split(" ");
  el("board").replaceChildren();
  for (const row of rows.split("/")) {
    let promoted = false;
    for (const c of row) {
      if (c === "+") {
        promoted = true;
        continue;
      }
      if (/[1-9]/.test(c)) {
        for (let i = 0; i < Number(c); i++) {
          const cell = document.createElement("div");
          cell.className = "cell";
          el("board").append(cell);
        }
        continue;
      }
      const cell = document.createElement("div"),
        piece = document.createElement("span");
      cell.className = "cell";
      piece.textContent = names[(promoted ? "+" : "") + c.toUpperCase()];
      piece.className =
        (c === c.toLowerCase() ? "white-piece " : "") +
        (promoted ? "promoted" : "");
      cell.append(piece);
      el("board").append(cell);
      promoted = false;
    }
  }
  const hand = { b: [], w: [] };
  for (const match of hands.matchAll(/(\d*)([PLNSGBRplnsgbr])/g)) {
    const [, count, piece] = match;
    hand[piece === piece.toUpperCase() ? "b" : "w"].push(
      names[piece.toUpperCase()] + (count || ""),
    );
  }
  el("black-hand").textContent = "先手：" + (hand.b.join(" ") || "なし");
  el("white-hand").textContent = "後手：" + (hand.w.join(" ") || "なし");
  el("turn").textContent = (turn === "b" ? "先手" : "後手") + "番";
}
function current() {
  if (!following) return;
  const games = state.games || [];
  const game = games.find((g) => g.worker === selected) || games[0];
  if (!game) return;
  draw(game.sfen);
  el("moves").textContent = game.moves.join(" ");
  el("board-title").textContent = `対局${game.worker}・${game.moves.length}手`;
}
function branch(node, path = []) {
  const details = document.createElement("details"),
    summary = document.createElement("summary"),
    link = document.createElement("a");
  link.href = "#";
  link.textContent = node.move + "　" + node.count + "局";
  link.onclick = async (event) => {
    event.preventDefault();
    event.stopPropagation();
    try {
      const p = await api(
        "/api/position?moves=" + encodeURIComponent(path.join(" ")),
      );
      following = false;
      draw(p.sfen);
      el("moves").textContent = path.join(" ");
      el("board-title").textContent = "ツリーの選択局面";
    } catch (error) {
      el("status").textContent = error.message;
    }
  };
  summary.append(link);
  details.append(summary);
  const pathKey = path.join(" ");
  details.dataset.path = pathKey;
  let built = false;
  details.addEventListener("toggle", () => {
    if (!details.isConnected) return;
    if (details.open) expandedPaths.add(pathKey);
    else expandedPaths.delete(pathKey);
    if (!details.open || built) return;
    built = true;
    for (const child of Object.values(node.children || {}).sort(
      (a, b) => b.count - a.count,
    ))
      details.append(branch(child, [...path, child.move]));
  });
  details.open = expandedPaths.has(pathKey);
  return details;
}
function inflate(data) {
  if (!data.nodes) return data;
  const rows = data.nodes.map((n) => ({ ...n, children: {} }));
  for (const n of rows) if (n.parent >= 0) rows[n.parent].children[n.move] = n;
  return rows[0];
}
async function update() {
  try {
    state = await api("/api/state");
    const phases = {
      not_started: "未開始",
      starting: "起動中",
      running: "準備中",
      playing: "対局中",
      paused: "待機中",
      stopped: "停止済み",
      faulted: "エラー・操作画面とログを確認",
    };
    el("status").textContent =
      (phases[state.phase] || state.phase) +
      ` / 完了 ${state.completed_games || 0}局` +
      (state.stale && state.phase !== "stopped" ? " / 更新待ち" : "");
    el("workers").replaceChildren();
    for (const game of state.games || []) {
      const button = document.createElement("button");
      button.textContent = "対局" + game.worker;
      button.setAttribute("aria-pressed", String(game.worker === selected));
      button.onclick = () => {
        selected = game.worker;
        following = true;
        current();
      };
      el("workers").append(button);
    }
    current();
    const session = state.session || "";
    const key = session + ":" + String(state.completed_games || 0);
    if (key !== treeKey) {
      if (session !== treeSession) {
        expandedPaths.clear();
        treeSession = session;
      } else {
        expandedPaths = new Set(
          [...el("tree").querySelectorAll("details[open]")].map(
            (item) => item.dataset.path,
          ),
        );
      }
      const tree = await api("/api/tree");
      const root = branch(inflate(tree));
      root.open = true;
      el("tree").replaceChildren(root);
      treeKey = key;
    }
  } catch (error) {
    el("status").textContent = error.message;
  } finally {
    setTimeout(update, 1500);
  }
}
el("live").onclick = () => {
  following = true;
  current();
};
async function manage() {
  const form = el("settings");
  if (!form) return;
  const data = await api("/api/settings");
  token = data.token;
  if (data.warning) el("message").textContent = data.warning;
  extraArgs = data.settings.engine_arguments || [];
  for (const [key, value] of Object.entries(data.settings))
    if (form.elements.namedItem(key))
      form.elements.namedItem(key).value = value;
  el("public-url").textContent =
    data.public_url || "起動時に表示された観戦ポート";
  form.onsubmit = async (event) => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(form));
    for (const key of [
      "nodes",
      "threads",
      "hash_mb",
      "parallel",
      "resign",
      "max_plies",
    ])
      values[key] = Number(values[key]);
    values.engine_arguments = extraArgs;
    const button = form.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      await api("/api/start", values);
      el("message").textContent = "開始・再開を受け付けました。";
    } catch (error) {
      el("message").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
  for (const button of form.querySelectorAll("[data-control]"))
    button.onclick = async () => {
      try {
        await api("/api/control", { operation: button.dataset.control });
        el("message").textContent = "操作を受け付けました。";
      } catch (error) {
        el("message").textContent = error.message;
      }
    };
}
draw("lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1");
manage().catch((error) => {
  if (el("message")) el("message").textContent = error.message;
});
update();
