"use strict";

// The renderer is shared with the original spectator. This adapter supplies
// generic campaigns without bundling snapshots, private paths or task lists.
const PHASES = {
  not_started: "未開始",
  starting: "起動中",
  running: "準備中",
  playing: "対局中",
  paused: "待機中",
  stopped: "停止済み",
  faulted: "エラー",
};
let savedTree = null,
  savedTreeKey = null,
  displayKey = null;
const recentCache = new Map();

function positionKey(sfen) {
  return sfen.split(" ").slice(0, 3).join(" ");
}
function asGame(raw, worker, ended = false) {
  const moves = raw.moves || [];
  const id = raw.game_id || String(worker);
  const key = id + ":" + moves.join(" ") + ":" + ended;
  if (recentCache.has(key)) return { ...recentCache.get(key), info: raw.info };
  const origin = raw.origin || "startpos";
  const pos = parseSfen(
    origin === "startpos" ? PLAYBACK_START : origin.replace(/^sfen /, ""),
  );
  const labels = moves.map((move) => playbackMove(pos, move));
  const prefix = raw.prefix_length ?? savedTree?.prefix?.length ?? 0;
  const game = {
    ...raw,
    worker,
    game_id: id,
    sfen: raw.sfen || playbackSfen(pos),
    last: lastSquares(moves.at(-1)),
    plies: pos.number - 1,
    turn: pos.turn,
    recent: labels.slice(-8),
    moves: moves.length,
    moveList: moves,
    prefix,
    ended,
    usi:
      "position " + origin + (moves.length ? " moves " + moves.join(" ") : ""),
    node: moves.slice(savedTree?.prefix?.length || 0).join(" "),
    task_key: moves.slice(prefix).join(" "),
    result:
      {
        black_win: "先手勝ち",
        white_win: "後手勝ち",
        draw: "引き分け",
        unresolved: "未決着",
      }[raw.rule_result] || "",
    reason:
      {
        resign: "投了",
        mate: "詰み",
        repetition: "千日手",
        perpetual_check: "連続王手",
        max_total_plies: "手数上限",
        max_plies: "手数上限",
        declaration: "入玉宣言",
      }[raw.reason] || "対局終了",
  };
  recentCache.set(key, game);
  if (recentCache.size > 32)
    recentCache.delete(recentCache.keys().next().value);
  return game;
}

function displayedTree(data, games) {
  const nodes = data.nodes.map((n) => ({ ...n }));
  const edges = new Map(),
    positions = new Map();
  for (const node of nodes) {
    if (node.parent >= 0) edges.set(node.parent + ":" + node.move, node.id);
    if (node.main === node.id) positions.set(positionKey(node.sfen), node.id);
  }
  for (const game of Object.values(games)) {
    if (game.ended) continue;
    const moves = game.moveList.slice(data.prefix.length);
    let parent = 0;
    const seen = new Set([0]);
    for (const move of moves) {
      const edge = parent + ":" + move;
      let id = edges.get(edge);
      if (id === undefined) {
        const pos = parseSfen(nodes[parent].sfen);
        const label = playbackMove(pos, move),
          sfen = playbackSfen(pos);
        id = nodes.length;
        const main = positions.get(positionKey(sfen)) ?? id;
        positions.set(positionKey(sfen), main);
        nodes.push({
          id,
          parent,
          move,
          label,
          sfen,
          main,
          count: 0,
          b: 0,
          w: 0,
        });
        edges.set(edge, id);
      }
      seen.add(id);
      parent = nodes[id].main;
    }
    for (const id of seen) nodes[id].count++;
  }
  return {
    ...data,
    playing: Object.values(games).filter((g) => !g.ended).length,
    nodes: nodes.map((n) => [
      n.id,
      n.parent,
      n.label,
      n.count,
      n.original || n.parent < 0,
      "",
      n.main,
      n.sfen,
      n.parent < 0 ? "" : n.move,
      n.b,
      n.w,
    ]),
  };
}

function setFollow(on) {
  app.follow = on;
  $("follow").checked = on;
}
function followDisplayedPosition() {
  if (app.review != null || app.reviewTask) return;
  const live = tree.live();
  const p = tree.pos.get(live);
  if (
    live != null &&
    (live !== tree.selected ||
      (app.follow && (!tree.centeredOnce || (p && !tree.nodeInViewport(p)))))
  ) {
    tree.select(live, {
      center: app.follow,
      scroll: app.follow,
      animate: false,
    });
    tree.centeredOnce = app.follow;
  }
}

function detail() {
  const node = tree.byKey.get(tree.selected),
    list = $("next-list");
  if (!node) return;
  $("detail-head").innerHTML =
    `<span class="ply">${node.depth ? node.depth + "手目" : ""}</span>${escapeHtml(node.label)}`;
  $("detail-tags").innerHTML =
    `<span class="tag">出現 ${node.count}局</span><span class="tag">先手 ${node.b || 0}勝・後手 ${node.w || 0}勝</span>`;
  $("detail-comment").textContent = tree.isProxy(node)
    ? "同一局面の続きへ合流"
    : "";
  const live = tree.live() || "";
  const children = [...node.children].sort((a, b) => b.count - a.count);
  if (!children.length) {
    list.innerHTML = '<li class="empty">この先の手はまだありません</li>';
    return;
  }
  const max = Math.max(1, ...children.map((n) => n.count));
  list.innerHTML = children
    .map(
      (child) =>
        `<li class="${live === child.key || live.startsWith(child.key + " ") ? "on-live" : ""}"><button data-key="${escapeHtml(child.key)}"><span>${escapeHtml(child.label)}</span><span class="bar"><i></i></span><span class="cnt">${child.count}局</span><span class="kind">${child.b || 0}-${child.w || 0}${tree.isProxy(child) ? " ↪" : ""}</span></button></li>`,
    )
    .join("");
  [...list.querySelectorAll(".bar i")].forEach((bar, i) => {
    bar.style.width = (children[i].count / max) * 100 + "%";
  });
}

function scoreText(game) {
  const score = game?.info?.score;
  if (!score) return "";
  const value = Number(score.value) * (game.turn === 1 ? 1 : -1);
  return score.kind === "mate"
    ? `詰 ${value}`
    : `${value > 0 ? "+" : ""}${value}`;
}

function render() {
  const state = app.state;
  if (!state) return;
  $("title").textContent = state.title || "連続対局・定跡生成";
  document.title = $("title").textContent;
  $("tally").innerHTML = [
    ["進行", state.active + "局"],
    ["完了", state.completed + "局"],
    ["先手勝", state.black_win],
    ["後手勝", state.white_win],
  ]
    .map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`)
    .join("");
  $("conn").className =
    "conn " +
    (app.connectionError || state.phase_key === "faulted"
      ? "bad"
      : state.phase_key === "playing"
        ? "ok"
        : "wait");
  $("conn-text").textContent = app.connectionError
    ? "再接続中…"
    : state.phase +
      (state.updated_unix
        ? "　" +
          new Date(state.updated_unix * 1000).toLocaleTimeString("ja-JP", {
            hour12: false,
          }) +
          " 更新"
        : "");
  const workers = Object.keys(state.games)
    .map(Number)
    .sort((a, b) => a - b);
  const tabs = $("games");
  if (tabs.dataset.workers !== workers.join(",")) {
    tabs.dataset.workers = workers.join(",");
    tabs.style.gridTemplateColumns = `repeat(${Math.max(1, Math.min(4, workers.length))},1fr)`;
    tabs.innerHTML = workers.length
      ? workers
          .map(
            (i) =>
              `<button class="game" data-w="${i}"><span class="game-name"><span class="n">${i}</span>対局 ${i}<span class="game-score"></span></span><span class="game-sub"></span></button>`,
          )
          .join("")
      : '<div class="empty-watch">対局の開始を待っています</div>';
    if (!workers.includes(app.worker)) app.worker = workers[0] || 1;
  }
  for (const button of tabs.querySelectorAll("button")) {
    const game = state.games[button.dataset.w];
    button.classList.toggle("active", Number(button.dataset.w) === app.worker);
    button.querySelector(".game-sub").textContent = game.ended
      ? game.reason + "・" + game.result
      : `${game.plies}手目　${game.turn ? "後手" : "先手"}番`;
    button.querySelector(".game-sub").classList.toggle("end", game.ended);
    button.querySelector(".game-score").textContent = scoreText(game);
  }
  followDisplayedPosition();
  const game = state.games[app.worker];
  const selected = app.review != null ? tree.byKey.get(app.review) : null;
  const root = tree.nodes[0];
  const annotation = selected || tree.byKey.get(tree.live());
  const sfen = selected?.sfen || game?.sfen || root?.sfen || PLAYBACK_START;
  $("review").hidden = !selected;
  $("review-move").textContent = selected
    ? `${selected.depth}手目 ${selected.label}`
    : "";
  board.show(
    sfen,
    selected ? tree.selectedLast(selected) : game?.last || [],
    selected ? "review" : game?.game_id || "start",
    candidateArrows(annotation, sfen),
  );
  if (selected) {
    $("now").innerHTML =
      `<span class="ply">${selected.depth ? selected.depth + "手目" : ""}</span>${escapeHtml(selected.label)}<span class="turn">${parseSfen(sfen).turn ? "後手" : "先手"}番</span>`;
    $("recent").textContent =
      game && !game.ended
        ? `対局 ${app.worker} は ${game.plies}手目を進行中`
        : "ツリーの局面を表示中";
  } else if (game) {
    $("now").innerHTML =
      `<span class="ply">${game.plies}手目</span>${escapeHtml(game.recent.at(-1) || "開始局面")}${game.ended ? `<span class="result">${escapeHtml(game.result)}</span>` : `<span class="turn">${game.turn ? "後手" : "先手"}番</span>`}`;
    const start = game.moves - game.recent.length + 1;
    $("recent").innerHTML = game.recent
      .slice(0, -1)
      .map(
        (move, i) =>
          `<span class="move"><i>${start + i}</i>${escapeHtml(move)}</span>`,
      )
      .join("");
  } else {
    $("now").textContent = "開始局面";
    $("recent").textContent = "";
  }
  $("notice").hidden = !app.connectionError && state.phase_key !== "faulted";
  $("notice").textContent = app.connectionError
    ? "データを再取得しています。表示中の盤面とツリーは保持されます。"
    : "対局がエラーで停止しています。操作画面の保存先・ログを確認してください。";
  renderTask(state);
  tree.redraw();
  detail();
}

async function poll(signal) {
  const raw = await requestJson("/api/state", signal);
  const key = (raw.session || "") + ":" + (raw.completed_games || 0);
  const newSession =
    savedTreeKey !== null && savedTreeKey.split(":")[0] !== key.split(":")[0];
  if (key !== savedTreeKey) {
    savedTree = await requestJson("/api/tree", signal);
    savedTreeKey = key;
  }
  if (newSession) {
    beginTaskNavigation();
    tree.use("full");
    for (const view of Object.values(tree.views)) {
      view.expanded.clear();
      view.collapsed.clear();
      view.selected = "";
    }
    tree.views.task.taskId = null;
    tree.views.task.revision = null;
    tree.views.task.sourceNodes = [];
    app.taskTrees?.clear();
    app.taskSources?.clear();
    app.scopeTask = null;
    app.reviewTask = null;
    app.manualFull = false;
    tree.selected = "";
    app.review = null;
    tree.centeredOnce = false;
    recentCache.clear();
  }
  const games = Object.fromEntries(
    Object.entries(savedTree.last_games || {}).map(([worker, game]) => [
      worker,
      asGame(game, Number(worker), true),
    ]),
  );
  for (const game of raw.games || []) {
    const ended =
      savedTree.last_games?.[String(game.worker)]?.game_id === game.game_id;
    games[game.worker] = asGame(
      ended ? { ...savedTree.last_games[game.worker], ...game } : game,
      game.worker,
      ended,
    );
  }
  const root = savedTree.nodes[0];
  app.state = {
    ...raw,
    ok: true,
    phase_key: raw.phase,
    phase: PHASES[raw.phase] || raw.phase,
    completed: savedTree.count,
    active: Object.values(games).filter((g) => !g.ended).length,
    black_win: root.b || 0,
    white_win: root.w || 0,
    games,
    tasks: (savedTree.tasks || []).map((task) => {
      const progress = raw.sequential_study?.tasks?.find(
        (t) => t.id === task.id,
      );
      const playing = Object.values(games).filter(
        (g) => !g.ended && g.focus_id === task.id,
      ).length;
      return {
        ...task,
        status: progress?.status || "pending",
        streak: progress?.black_streak || 0,
        best: progress?.best_black_streak || 0,
        recorded: task.games + playing,
      };
    }),
    task_index: Math.min(
      raw.sequential_study?.index || 0,
      Math.max(0, (savedTree.tasks || []).length - 1),
    ),
    streak_target:
      raw.sequential_study?.target_black_streak ||
      savedTree.target_black_streak,
    nodes: raw.conditions?.nodes || 0,
  };
  const nextDisplayKey =
    key +
    ":" +
    Object.values(games)
      .map((g) => g.game_id + ":" + g.moves + ":" + g.ended)
      .join("|");
  if (nextDisplayKey !== displayKey) {
    app.treeRevision = app.state.tree_revision = (app.displayRevision || 0) + 1;
    app.displayRevision = app.treeRevision;
    tree.loadFull(displayedTree(savedTree, games));
    displayKey = nextDisplayKey;
    if (app.review != null && !tree.byKey.has(app.review)) app.review = null;
  }
  app.state.tree_revision = app.treeRevision;
  app.state.task_id = app.state.tasks[app.state.task_index]?.id;
  if (app.review == null && !app.reviewTask && !app.manualFull && liveScope()) {
    await enterTask(liveScope(), null, { signal });
  } else if (tree.view === "task" && tree.views.task.taskId) {
    await loadTaskTree(tree.views.task.taskId, signal);
  }
  app.connectionError = false;
  renderScope();
  renderAllSfen(app.state);
  render();
}

let pollController,
  pollTimer,
  pollGeneration = 0;
function wakePolling() {
  const generation = ++pollGeneration;
  clearTimeout(pollTimer);
  pollController?.abort();
  if (document.hidden) return;
  const controller = (pollController = new AbortController());
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  poll(controller.signal)
    .catch((error) => {
      if (generation !== pollGeneration) return;
      app.connectionError = true;
      render();
    })
    .finally(() => {
      clearTimeout(timeout);
      if (generation === pollGeneration)
        pollTimer = setTimeout(wakePolling, POLL_MS);
    });
}

$("games").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-w]");
  if (!button) return;
  chooseWorker(Number(button.dataset.w));
});
$("next-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-key]");
  if (button) pickNode(button.dataset.key);
});
$("back-live").addEventListener("click", goLive);
$("to-live").addEventListener("click", goLive);
$("follow").addEventListener("change", (event) => {
  if (event.target.checked) goLive();
  else setFollow(false);
});
$("zoom-in").addEventListener("click", () => tree.setZoom(tree.zoom * 1.2));
$("zoom-out").addEventListener("click", () => tree.setZoom(tree.zoom / 1.2));
$("copy").addEventListener("click", async () => {
  const selected = app.review != null ? tree.byKey.get(app.review) : null;
  const text = selected
    ? "position sfen " +
      tree.nodes[0].sfen +
      (selected.key ? " moves " + selected.key : "")
    : app.state?.games[app.worker]?.usi ||
      "position sfen " + (tree.nodes[0]?.sfen || PLAYBACK_START);
  try {
    await navigator.clipboard.writeText(text);
    toast("USI手順をコピーしました");
  } catch {
    toast("コピーできませんでした");
  }
});
document.addEventListener("visibilitychange", wakePolling);
window.addEventListener("online", wakePolling);
new ResizeObserver(() => {
  if (app.state && app.follow && app.review == null) {
    tree.centeredOnce = false;
    followDisplayedPosition();
  }
  tree.redraw();
}).observe($("tree-wrap"));
board.show(PLAYBACK_START, [], "start");
wakePolling();

function precedentUrl(usi) {
  const words = String(usi || "")
    .trim()
    .split(/\s+/);
  if (words[0] === "position") words.shift();
  let origin;
  if (words[0] === "startpos") {
    words.shift();
    origin = "";
  } else if (words.shift() === "sfen" && words.length >= 4) {
    origin = words
      .splice(0, 4)
      .slice(0, 3)
      .join(" ")
      .replaceAll("/", "_")
      .replaceAll(" ", ".")
      .replaceAll("+", "z");
  } else throw new Error("手順を取得できませんでした");
  if (words.length && words.shift() !== "moves")
    throw new Error("手順の形式が不正です");
  const hands = { P: 91, L: 92, N: 93, S: 94, G: 90, B: 95, R: 96 };
  const square = (file, rank) =>
    (rank.charCodeAt(0) - 97) * 9 + Number(file) - 1;
  let encoded = "",
    ply = 0;
  for (const move of words) {
    if (["resign", "rep_draw", "max_moves", "win"].includes(move)) break;
    const normal = /^([1-9])([a-i])([1-9])([a-i])(\+?)$/.exec(move);
    const drop = /^([PLNSGBR])\*([1-9])([a-i])$/.exec(move);
    if (!normal && !drop) throw new Error("手順の形式が不正です");
    if (++ply > 999) throw new Error("手順付きURLは999手までです");
    const from = normal ? square(normal[1], normal[2]) : hands[drop[1]];
    const to = normal ? square(normal[3], normal[4]) : square(drop[2], drop[3]);
    encoded += ((from * 81 + to) * 2 + (normal?.[5] ? 1 : 0))
      .toString(36)
      .padStart(3, "0");
  }
  const url = new URL("https://478shogi.com/kiricompass/");
  url.searchParams.set("usen", origin + "~0." + encoded + ".");
  url.searchParams.set("branch", "0");
  url.searchParams.set("ply", String(ply));
  if (url.href.length > 8000) throw new Error("手順付きURLが長すぎます");
  return url.href;
}

$("precedent").addEventListener("click", () => {
  const n = app.review != null ? tree.byKey.get(app.review) : null;
  const text = n
    ? "position sfen " + tree.nodes[0].sfen + (n.key ? " moves " + n.key : "")
    : app.state?.games[app.worker]?.usi;
  if (!text) return toast("局面を選択してください");
  try {
    window.open(precedentUrl(text), "_blank", "noopener,noreferrer");
  } catch (error) {
    toast(error.message);
  }
});
