"use strict";
function taskById(id) {
  return (
    ((app.state && app.state.tasks) || []).find((t) => t.id === id) || null
  );
}

// Tasks that already have games get their own tree; the full tree stops at them.
function taskWithGames(key) {
  const t = taskAround(key);
  return t && t.recorded ? t : null;
}

// The deepest task whose position is this full-tree node or one of its ancestors.
function taskAround(key) {
  let best = null;
  for (const t of (app.state && app.state.tasks) || []) {
    if (t.node == null) continue;
    if (
      (key === t.node || key.startsWith(t.node ? t.node + " " : "")) &&
      (!best || t.node.length > best.node.length)
    )
      best = t;
  }
  return best;
}

function beginTaskNavigation() {
  app.taskNavigation = (app.taskNavigation || 0) + 1;
  app.taskLoading = null;
  const status = $("task-tree-loading");
  if (status) status.hidden = true;
  return app.taskNavigation;
}

function taskLoadingStatus(id, navigation, error) {
  if (navigation !== app.taskNavigation) return;
  app.taskLoading = error === false ? null : { id, navigation };
  const status = $("task-tree-loading");
  if (!status) return;
  status.hidden = error === false;
  status.textContent =
    error === false
      ? ""
      : error
        ? `課題${taskById(id)?.n ?? ""}のツリーを取得できません。再選択で再試行できます`
        : `課題${taskById(id)?.n ?? ""}のツリーを読み込み中…`;
}

async function loadTaskTree(id, signal, navigation) {
  const v = tree.views.task;
  const cached = app.taskTrees?.get(id),
    revision = app.state?.tree_revision ?? app.treeRevision;
  if (v.taskId === id && v.revision >= revision) return true;
  if (taskTreeIsCurrent(cached, id, revision)) {
    if (v.taskId !== id || v.loading || v.revision < cached.revision)
      tree.loadTask(cached, taskById(id));
    return true;
  }
  const data = await fetchTaskTreeData(id, signal);
  signal?.throwIfAborted();
  // Counts belong to the displayed packet, independently of this HTTP tree's
  // revision. A newer tree must not advance counts or block live playback.
  (app.taskTrees ||= new Map()).set(id, data);
  if (navigation != null && navigation !== app.taskNavigation) return false;
  tree.loadTask(data, taskById(id));
  return true;
}

function useReadyTask(id) {
  const v = tree.views.task,
    data = app.taskTrees?.get(id);
  if (data && (v.taskId !== id || v.revision < data.revision))
    tree.loadTask(data, taskById(id));
  if (v.taskId !== id) return false;
  app.scopeTask = id;
  if (tree.view !== "task") tree.use("task");
  return true;
}

async function enterTask(id, key, opts = {}) {
  const navigation = beginTaskNavigation(),
    task = taskById(id);
  app.scopeTask = id;
  if (key != null) app.review = tree.selected;
  const cached = app.taskTrees?.get(id);
  const revision = app.state?.tree_revision ?? app.treeRevision;
  const ready =
    taskTreeIsCurrent(cached, id, revision) ||
    (tree.views.task.taskId === id && tree.views.task.revision >= revision);
  if (!ready) {
    taskLoadingStatus(id, navigation);
    // A first visit can take seconds on the origin. Show its known starting
    // board/root now, rather than leaving the previous task looking frozen.
    if (
      key === "" &&
      task?.recorded &&
      task.sfen &&
      (!opts.sfen ||
        opts.sfen.split(" ").slice(0, 3).join(" ") ===
          task.sfen.split(" ").slice(0, 3).join(" "))
    ) {
      app.reviewTask = null;
      app.review = "";
      tree.loadTask(
        {
          id,
          revision: -1,
          base: task.ply,
          loading: true,
          completed: task.games,
          playing: Math.max(0, task.recorded - task.games),
          nodes: [
            [
              0,
              -1,
              "",
              task.recorded,
              task.wins,
              task.losses,
              task.sfen,
              "",
              0,
              task.recorded,
            ],
          ],
        },
        task,
      );
      tree.use("task");
      tree.select("", { scroll: false });
      tree.frameVisibleNodes();
      render();
    }
  }
  try {
    if (!(await loadTaskTree(id, opts.signal, navigation))) return false;
  } catch (error) {
    taskLoadingStatus(id, navigation, error);
    return false;
  }
  opts.signal?.throwIfAborted();
  if (navigation !== app.taskNavigation) return false;
  taskLoadingStatus(id, navigation, false);
  tree.use("task");
  if (key != null) {
    const body =
      opts.sfen &&
      tree.views.task.byPosition?.get(
        opts.sfen.split(" ").slice(0, 3).join(" "),
      );
    tree.select(body ? body.key : tree.byKey.has(key) ? key : "", {
      center: true,
      animate: false,
      ...opts,
    });
  }

  return true;
}

function enterFull(key) {
  beginTaskNavigation();
  tree.use("full");
  if (key != null && tree.byKey.has(key)) tree.select(key, { center: true });
}

// Which view the live game belongs in: its task's game tree, unless the full tree was chosen.
function liveScope() {
  const g = app.state && app.state.games && app.state.games[app.worker];
  if (app.manualFull || !g || g.prefix == null || !g.focus_id) return null;
  return g.focus_id;
}

async function pickNode(key, { frameRoot = true } = {}) {
  const n = tree.byKey.get(key);
  if (!n) return;
  const target = n.mainKey;
  setFollow(false);
  app.reviewTask = null;
  if (tree.view === "full") {
    const t = taskWithGames(target);
    if (t) {
      app.manualFull = false;
      const rest = target.slice(t.node.length).trim();
      if (
        !(await enterTask(t.id, rest, {
          sfen: n.sfen,
          arrival: {
            position: n.sfen.split(" ").slice(0, 3).join(" "),
            last: n.last || lastSquares(n.usi),
          },
        }))
      )
        return;
      app.review = tree.selected === tree.live() ? null : tree.selected;
      if (app.review == null) setFollow(true);
      render();
      if (frameRoot && tree.selected === "") {
        tree.centeredOnce = app.follow;
        tree.frameVisibleNodes();
      }
      return;
    }
  }
  if (target === tree.live()) {
    goLive();
    return;
  }
  app.review = target;
  tree.select(key, { center: n.key !== target });
  render();
}

async function goLive({ autoScroll = true } = {}) {
  app.manualFull = false;
  app.review = null;
  app.reviewTask = null;
  setFollow(autoScroll);
  const scope = liveScope();
  if (scope) {
    if (!(await enterTask(scope))) return;
  } else if (tree.view !== "full") enterFull();
  const live = tree.live();
  if (live != null) tree.select(live, { center: true });
  render();
}

function renderScope() {
  const folds = $("task-tree-fold");
  if (folds)
    folds.hidden =
      tree.view !== "task" || !!tree.views.task.loading || !tree.nodes.length;
  const t = taskById(app.scopeTask) || taskById(app.state && app.state.task_id);
  $("scope-task").textContent = t ? `課題${t.n} の対局` : "課題の対局";
  $("scope-task").disabled = !t;
  for (const b of $("scope").children)
    b.classList.toggle("on", b.dataset.v === tree.view);
  const shown = tree.view === "task" ? taskById(tree.views.task.taskId) : null;
  const tb = $("tree-sfen");
  tb.hidden = !shown || !sfenDownloadCount(shown);
  if (shown) {
    tb.dataset.id = shown.id;
    tb.textContent = `課題${shown.n}の棋譜`;
    tb.title = `課題${shown.n} の完了棋譜を保存`;
  }
}

$("scope").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-v]");
  if (!b || b.disabled) return;
  if (b.dataset.v === "full") {
    if (tree.view === "full") return;
    app.manualFull = true;
    const t = taskById(tree.views.task.taskId);
    const inTask = tree.selected;
    const selectedSfen = tree.byKey.get(inTask)?.sfen;
    const arrival = tree.arrival;
    enterFull();
    // Keep the board where it was: the task position, or the full-tree node of the same path.
    let key = t && t.node != null ? t.node : "";
    if (t && inTask && tree.byKey.has(t.node + " " + inTask))
      key = t.node + " " + inTask;
    const samePosition =
      selectedSfen &&
      tree.views.full.byPosition?.get(
        selectedSfen.split(" ").slice(0, 3).join(" "),
      );
    if (samePosition) key = samePosition.key;
    if (app.review != null || app.reviewTask) {
      app.review = clampFull(key);
      tree.select(app.review, { center: true, arrival });
    } else {
      const live = tree.live();
      if (live != null) tree.select(live, { center: true });
    }
  } else {
    app.manualFull = false;
    const t = taskById(app.scopeTask) || taskById(app.state.task_id);
    if (!t) return;
    const g = app.state.games[app.worker];
    if (app.review == null && !app.reviewTask && g && g.focus_id === t.id) {
      await goLive({ autoScroll: app.follow });
      tree.frameVisibleNodes();
      return;
    }
    const selectedSfen = tree.byKey.get(tree.selected)?.sfen;
    if (
      !(await enterTask(t.id, "", {
        sfen: selectedSfen,
        arrival: tree.arrival,
      }))
    )
      return;
    app.review = tree.selected;
    setFollow(false);
  }
  // A user scope switch fits more nodes. Initial loading and live following keep their normal centering.
  tree.frameVisibleNodes();
  render();
});

document.addEventListener("keydown", async (e) => {
  if (e.target.closest("input,textarea,select,dialog")) return;
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  if (["1", "2", "3"].includes(e.key)) {
    chooseWorker(+e.key);
    return;
  }
  if (e.key === "Escape") {
    goLive();
    return;
  }
  const n = tree.byKey.get(tree.selected);
  if (!n) return;
  if (
    ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Enter", " "].includes(
      e.key,
    )
  )
    tree.clearHover();
  let target = null;
  if (e.key === "ArrowLeft" && n.parent < 0 && tree.view === "task") {
    // Step out of the task's game tree to the move before the task position.
    const t = taskById(tree.views.task.taskId);
    e.preventDefault();
    app.manualFull = true;
    enterFull();
    const full = t && tree.byKey.get(t.node);
    if (full) {
      await pickNode(
        full.parent >= 0 ? tree.nodes[full.parent].key : full.key,
        { frameRoot: false },
      );
      tree.frameVisibleNodes();
    }
    return;
  }
  if (e.key === "ArrowLeft" && n.parent >= 0) target = tree.nodes[n.parent];
  else if (e.key === "ArrowRight" && n.children.length) target = n.children[0];
  else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
    const sib = n.parent >= 0 ? tree.nodes[n.parent].children : [n];
    const i = sib.indexOf(n) + (e.key === "ArrowUp" ? -1 : 1);
    if (i >= 0 && i < sib.length) target = sib[i];
  } else if (e.key === "Enter" || e.key === " ") {
    tree.toggle(n.key);
    e.preventDefault();
    return;
  }
  if (target) {
    e.preventDefault();
    const previousView = tree.view;
    await pickNode(target.key, { frameRoot: false });
    if (tree.view !== previousView) tree.frameVisibleNodes();
  }
});

// ------------------------------------------------------------- side panel
const TASK_STATE = {
  running: "検証中",
  pending: "未着手",
  done: "完了",
  completed: "完了",
  passed: "完了",
  verified: "検証完了",
  skipped: "飛ばし",
  stopped: "停止済み",
};

function renderTask(s) {
  const tasks = s.tasks || [];
  const t =
    tasks.find((k) => k.id === s.task_id) || tasks[s.task_index] || null;
  const viewing =
    app.review != null
      ? app.review
      : app.reviewTask
        ? app.reviewTask.node
        : undefined;
  if (!t) {
    $("task-no").textContent = "連続対局";
    $("task-name").textContent = "共通起点からの対局";
    $("task-state").textContent = "";
    $("task-pos").textContent = "";
    $("task-stats").textContent = "";
    $("task-list").replaceChildren();
    $("task-sfen").disabled = true;
    return;
  }
  $("task-no").textContent = `課題 ${t.n} / ${tasks.length}`;
  const state = $("task-state");
  const onTask = viewing != null && viewing === t.node;
  state.textContent = onTask ? "盤に表示中" : TASK_STATE[t.status] || t.status;
  state.className =
    "task-state" +
    (onTask
      ? " viewing"
      : /done|completed|passed|verified/.test(t.status)
        ? " done"
        : "");
  $("task-pos").textContent = `${t.ply}手目 ${t.move} の局面`;
  $("task-name").textContent = t.label;
  $("task-comment").textContent =
    t.comment && t.comment !== t.label ? t.comment : "";
  const target = s.streak_target;
  const bar = target ? `<span class="progress"><i ></i></span>` : "";
  $("task-stats").innerHTML =
    `<span class="streak">先手連勝 ${bar}<b>${t.streak}</b>${target ? "/" + target : ""}</span>` +
    `<span>最長 <b>${t.best}</b></span>` +
    `<span>先手 <b>${t.wins}</b>勝 <b>${t.losses}</b>敗</span>` +
    `<span>探索 <b>${Number(s.nodes).toLocaleString()}</b>/手</span>`;
  const barNode = $("task-stats").querySelector(".progress i");
  if (barNode)
    barNode.style.width = Math.min(100, (t.streak / target) * 100) + "%";
  const list = $("task-list");
  const sig = tasks.map((k) => k.id + k.status).join() + "|" + viewing;
  if (list.dataset.sig !== sig) {
    list.dataset.sig = sig;
    list.innerHTML = tasks
      .map((k) => {
        const cls = [
          k.id === t.id
            ? "running"
            : /done|completed|passed|verified/.test(k.status)
              ? "done"
              : "",
          viewing != null && viewing === k.node ? "viewing" : "",
        ].join(" ");
        return `<button class="${cls}" data-id="${escapeHtml(k.id)}" title="${escapeHtml(k.label)}（${k.ply}手目 ${escapeHtml(k.move)}）">${k.n}</button>`;
      })
      .join("");
  }
  const btn = $("task-sfen");
  btn.dataset.id = t.id;
  btn.disabled = !sfenDownloadCount(t);
  btn.textContent = `課題${t.n}の棋譜`;
  btn.title = sfenDownloadCount(t)
    ? `課題${t.n} の完了棋譜を保存`
    : "まだ終局した対局がありません";
}

async function pickTask(id) {
  const t = taskById(id);
  if (!t) return;
  app.reviewTask = null;
  setFollow(false);
  if (t.recorded) {
    app.manualFull = false;
    if (!(await enterTask(id, ""))) return;
    app.review = "";
    tree.frameVisibleNodes();
  } else if (t.node != null && tree.views.full.byKey.has(t.node)) {
    // No game of this task yet: show where it sits in the full tree.
    app.manualFull = true;
    enterFull();
    app.review = tree.byKey.get(t.node).mainKey;
    tree.select(app.review, { center: true });
  } else {
    app.manualFull = true;
    enterFull();
    app.review = null;
    app.reviewTask = t;
  }
  render();
}

function chooseWorker(worker) {
  app.worker = worker;
  goLive();
}
function taskTreeIsCurrent(data, id, revision) {
  return data?.id === id && data.revision >= revision;
}
async function fetchTaskTreeData(id, signal) {
  const sources = (app.taskSources ||= new Map());
  const session = app.state.session,
    cacheKey = session + ":" + id;
  let raw = sources.get(cacheKey);
  if (!raw || raw.revision < (savedTree.revision || 0)) {
    raw = await requestJson(
      "/api/task_tree?id=" + encodeURIComponent(id),
      signal,
    );
    if (session !== app.state.session)
      throw new Error("保存先が切り替わりました");
    sources.set(cacheKey, raw);
  }
  const games = Object.fromEntries(
    Object.entries(app.state.games).filter(([, g]) => g.focus_id === id),
  );
  const display = displayedTree(raw, games);
  return {
    ...display,
    id,
    revision: app.state.tree_revision,
    nodes: display.nodes.map(
      ([i, p, label, count, original, comment, main, sfen, usi, b, w]) => [
        i,
        p,
        label,
        count,
        b,
        w,
        sfen,
        usi,
        main,
        count,
      ],
    ),
  };
}
function sfenDownloadCount(task) {
  return task?.sfen_games || 0;
}
function renderAllSfen(state) {
  const button = $("all-sfen");
  button.disabled = !state.completed;
  button.textContent =
    (state.tasks.length ? "全課題の棋譜" : "全棋譜") +
    "(sfens.txt) " +
    state.completed +
    "局";
  button.title = state.completed
    ? `完了棋譜 ${state.completed}局をまとめて sfens.txt で保存`
    : "まだ保存できる完了棋譜がありません";
}
function downloadSfen(id) {
  if (id)
    window.location.href = spectatorUrl(
      "/api/task_sfen?id=" + encodeURIComponent(id),
    );
}
$("task-sfen").addEventListener("click", (event) =>
  downloadSfen(event.currentTarget.dataset.id),
);
$("tree-sfen").addEventListener("click", (event) =>
  downloadSfen(event.currentTarget.dataset.id),
);
$("all-sfen").addEventListener("click", () => {
  window.location.href = spectatorUrl("/sfens.txt");
});
$("task-list").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-id]");
  if (button) pickTask(button.dataset.id);
});
$("task-thumb").addEventListener("click", () => {
  if (app.state?.task_id) pickTask(app.state.task_id);
});
$("fold-rare").addEventListener("click", () =>
  tree.closeRare(
    $("fold-limit").value === "root10"
      ? Math.floor((tree.nodes[0]?.count || 0) / 10)
      : Number($("fold-limit").value),
  ),
);
$("open-all-task").addEventListener("click", () => tree.openAllTask());
