"use strict";
const $ = (id) => document.getElementById(id);
const RANKS = "abcdefghi";
const KANJI_RANK = "一二三四五六七八九";
const HAND_ORDER = "RBGSNLP";
const TILE = { w: 312, h: 358 };
const COLUMN = { K: 0, R: 1, B: 2, G: 3, S: 4, N: 5, L: 6, P: 7 };
const BOARD = {
  fill: "#172330",
  edge: "#607189",
  grid: "#35495e",
  coord: "#8a96a6",
  last: "#90ee90",
  arrow: "deeppink",
};

const app = {
  state: null,
  worker: 1,
  review: null, // node key shown on the board, or null while watching
  follow: true,
  tree: null,
  treeRevision: null,
  allSfenPending: false,
  evaluations: null,
};
const POLL_MS = 1500;
const REQUEST_TIMEOUT_MS = 8000;

// Use the script's host for data too. The public entry page can keep its original
// URL while routine requests go directly through the dedicated Tunnel.
const SPECTATOR_ROOT = new URL(
  ".",
  document.currentScript?.src || location.href,
);
function spectatorUrl(path) {
  if (path.startsWith(SPECTATOR_ROOT.pathname))
    path = path.slice(SPECTATOR_ROOT.pathname.length);
  return new URL(path.replace(/^\//, ""), SPECTATOR_ROOT).href;
}
function spectatorFetch(path, options) {
  return fetch(spectatorUrl(path), options);
}
async function requestJson(path, signal) {
  const response = await spectatorFetch(path, { cache: "no-store", signal });
  if (!response.ok) throw new Error("HTTP " + response.status);
  const data = await response.json();
  signal?.throwIfAborted();
  return data;
}

// A transient missing checkpoint at an end/start boundary is normal. Report it
// only if it persists; preserve all other file/parse warnings immediately.
const missingCheckpoints = new Map();
function noticeWarnings(warnings, now = performance.now()) {
  const present = new Set(warnings);
  for (const key of missingCheckpoints.keys())
    if (!present.has(key)) missingCheckpoints.delete(key);
  return warnings.flatMap((warning) => {
    const match = warning.match(
      /^対局(\d+): 更新待ち \(\[Errno 2\] No such file or directory: .*in_progress\.json.*\)$/,
    );
    if (!match) return [warning];
    if (!missingCheckpoints.has(warning)) missingCheckpoints.set(warning, now);
    return now - missingCheckpoints.get(warning) < 20000
      ? []
      : [`対局${match[1]}：進行局面の更新が20秒以上届いていません`];
  });
}

// ---------------------------------------------------------------- utilities
function parseSfen(sfen) {
  const [layout, turn, hands, number] = sfen.split(" ");
  const cells = {};
  layout.split("/").forEach((row, rank) => {
    let file = 9,
      promoted = false;
    for (const ch of row) {
      if (/\d/.test(ch)) file -= +ch;
      else if (ch === "+") promoted = true;
      else {
        cells[file + RANKS[rank]] = (promoted ? "+" : "") + ch;
        file--;
        promoted = false;
      }
    }
  });
  const hand = [{}, {}];
  let count = "";
  for (const ch of hands || "-") {
    if (/\d/.test(ch)) count += ch;
    else if (ch !== "-") {
      const side = ch === ch.toLowerCase() ? 1 : 0;
      hand[side][ch.toUpperCase()] =
        (hand[side][ch.toUpperCase()] || 0) + +(count || 1);
      count = "";
    }
  }
  return { cells, hand, turn: turn === "w" ? 1 : 0, number: +number };
}

function lastSquares(usi) {
  if (!usi) return [];
  return usi[1] === "*"
    ? [usi.slice(2, 4)]
    : [usi.slice(0, 2), usi.slice(2, 4)];
}

// ---- board annotations: use the displayed position, including task roots.
function nodeLast(node, task) {
  if (node.parent >= 0) return lastSquares(node.usi);
  return task?.last || lastSquares(task?.node?.trim().split(/\s+/).at(-1));
}

function candidateArrows(node, sfen) {
  // Transpositions may have a different move number; cells, hands and turn must match.
  if (
    !node ||
    node.sfen.split(" ").slice(0, 3).join(" ") !==
      sfen.split(" ").slice(0, 3).join(" ")
  )
    return [];
  const pos = parseSfen(sfen),
    arrows = [],
    seen = new Set();
  for (const child of node.children) {
    const move = child.usi;
    if (!/^(?:[1-9][a-i][1-9][a-i]\+?|[RBGSNLP]\*[1-9][a-i])$/.test(move))
      continue;
    const drop = move[1] === "*",
      from = move.slice(0, 2),
      to = move.slice(2, 4);
    if (drop) {
      if (!(pos.hand[pos.turn][move[0]] > 0)) continue;
    } else {
      const piece = pos.cells[from];
      if (
        !piece ||
        Number(piece.slice(-1) === piece.slice(-1).toLowerCase()) !== pos.turn
      )
        continue;
    }
    // Promotion choices with the same geometry share one arrow, so overlap gives no extra weight.
    const key = from + to;
    if (seen.has(key)) continue;
    seen.add(key);
    arrows.push({
      from: drop ? null : from,
      to,
      kind: drop ? move[0] : null,
      side: pos.turn,
    });
  }
  return arrows;
}
// ---- end board annotations

const ease = (t) => 1 - Math.pow(1 - t, 3);

// ---- spectator playback (one API poll; every move gets its own display time)
const PLAYBACK_START =
  "lnsgkgsnl/1r5b1/ppppppppp/9/9/9/PPPPPPPPP/1B5R1/LNSGKGSNL b - 1";
function playbackSfen(pos) {
  const rows = RANKS.split("").map((rank) => {
    let row = "",
      empty = 0;
    for (let file = 9; file >= 1; file--) {
      const piece = pos.cells[file + rank];
      if (piece) {
        row += (empty || "") + piece;
        empty = 0;
      } else empty++;
    }
    return row + (empty || "");
  });
  let hands = "";
  for (let side = 0; side < 2; side++)
    for (const kind of HAND_ORDER) {
      const count = pos.hand[side][kind] || 0;
      if (count)
        hands += (count > 1 ? count : "") + (side ? kind.toLowerCase() : kind);
    }
  return `${rows.join("/")} ${pos.turn ? "w" : "b"} ${hands || "-"} ${pos.number}`;
}
function playbackMove(pos, move) {
  const to = move.slice(2, 4),
    drop = move[1] === "*";
  let piece;
  if (drop) {
    piece = pos.turn ? move[0].toLowerCase() : move[0];
    pos.hand[pos.turn][move[0]]--;
  } else {
    piece = pos.cells[move.slice(0, 2)];
    delete pos.cells[move.slice(0, 2)];
    const captured = pos.cells[to];
    if (captured) {
      const kind = captured.slice(-1).toUpperCase();
      pos.hand[pos.turn][kind] = (pos.hand[pos.turn][kind] || 0) + 1;
    }
  }
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
    "+L": "成香",
    "+N": "成桂",
    "+S": "成銀",
    "+B": "馬",
    "+R": "龍",
  };
  const label =
    (pos.turn ? "△" : "▲") +
    "１２３４５６７８９"[+to[0] - 1] +
    KANJI_RANK[RANKS.indexOf(to[1])] +
    names[piece.toUpperCase()] +
    (drop ? "打" : move.endsWith("+") ? "成" : "");
  if (move.endsWith("+")) piece = "+" + piece;
  pos.cells[to] = piece;
  pos.turn ^= 1;
  pos.number++;
  return label;
}
const escapeHtml = (s) =>
  String(s).replace(
    /[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c],
  );

function sizeCanvas(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth,
    h = canvas.clientHeight;
  if (
    canvas.width !== Math.round(w * dpr) ||
    canvas.height !== Math.round(h * dpr)
  ) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h, dpr };
}

function toast(text) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => {
    el.hidden = true;
  }, 1400);
}

// ------------------------------------------------------------------ pieces
const atlas = new Image();
let atlasReady = false;
atlas.onload = () => {
  atlasReady = true;
  board.redraw();
  app.thumb = null;
  if (app.state && app.state.ok) renderTask(app.state);
  tree.redraw();
};
atlas.onerror = () => {
  atlasReady = false;
};
atlas.src = spectatorUrl("/pieces.png");
const spriteCache = new Map();

// Sprites are built at device pixels and drawn at CSS size, so Retina stays sharp.
function drawPiece(ctx, piece, cx, cy, height) {
  const d = window.devicePixelRatio || 1;
  const img = sprite(piece, height * d);
  if (!img) return (height * TILE.w) / TILE.h;
  const w = img.width / d,
    h = img.height / d;
  ctx.drawImage(
    img,
    Math.round((cx - w / 2) * d) / d,
    Math.round((cy - h / 2) * d) / d,
    w,
    h,
  );
  return w;
}

// Pre-scale each tile once per size so the board never shows a soft resample.
function sprite(piece, height) {
  const type = piece.slice(-1).toUpperCase();
  const white = piece.slice(-1) !== type;
  const promoted = piece[0] === "+";
  const px = Math.max(8, Math.round(height));
  const key = piece + ":" + px;
  if (spriteCache.has(key)) return spriteCache.get(key);
  if (!atlasReady) return null;
  let row = white ? (promoted ? 3 : 2) : promoted ? 1 : 0;
  let rotate = false;
  if (type === "K") {
    row = 0;
    rotate = white;
  } // the atlas has no upside-down king
  const w = Math.round((px * TILE.w) / TILE.h);
  let source = document.createElement("canvas");
  source.width = TILE.w;
  source.height = TILE.h;
  let sctx = source.getContext("2d");
  sctx.drawImage(
    atlas,
    COLUMN[type] * TILE.w,
    row * TILE.h,
    TILE.w,
    TILE.h,
    0,
    0,
    TILE.w,
    TILE.h,
  );
  // Halve in steps, then a final high-quality pass.
  let sw = TILE.w,
    sh = TILE.h;
  while (sh / 2 >= px) {
    const next = document.createElement("canvas");
    next.width = Math.round(sw / 2);
    next.height = Math.round(sh / 2);
    const nctx = next.getContext("2d");
    nctx.imageSmoothingQuality = "high";
    nctx.drawImage(source, 0, 0, sw, sh, 0, 0, next.width, next.height);
    source = next;
    sw = next.width;
    sh = next.height;
  }
  const out = document.createElement("canvas");
  out.width = w;
  out.height = px;
  const octx = out.getContext("2d");
  octx.imageSmoothingQuality = "high";
  if (rotate) {
    octx.translate(w, px);
    octx.rotate(Math.PI);
  }
  octx.drawImage(source, 0, 0, sw, sh, 0, 0, w, px);
  if (spriteCache.size > 400) spriteCache.clear();
  spriteCache.set(key, out);
  return out;
}

// ------------------------------------------------------------------- board
const board = {
  canvas: $("board"),
  shown: null, // {pos, last, id, arrows}
  items: [], // [{piece, from:[x,y], to:[x,y]}] in board-relative cell units
  anim: 0,
  start: 0,

  show(sfen, usiLast, id, arrows = []) {
    const pos = parseSfen(sfen);
    const last = usiLast;
    const prev = this.shown;
    if (
      prev &&
      prev.sfen === sfen &&
      prev.id === id &&
      String(prev.last) === String(last)
    ) {
      if (JSON.stringify(prev.arrows) !== JSON.stringify(arrows)) {
        this.shown = { ...prev, arrows };
        this.redraw();
      }
      return;
    }
    this.shown = { pos, last, id, sfen, arrows };
    this.items = this.match(
      prev ? prev.pos : null,
      pos,
      prev &&
        prev.id !== id &&
        !String(id).startsWith("review") &&
        !String(prev.id).startsWith("review"),
    );
    const moving = this.items.filter(
      (i) => i.from[0] !== i.to[0] || i.from[1] !== i.to[1],
    ).length;
    this.anim = moving ? 1 : 0;
    this.start = performance.now();
    this.redraw();
  },

  // Unchanged squares first, then the nearest same piece; pieces from a hand
  // slide out of that side's stand.
  match(prev, pos, gameChanged) {
    const items = [];
    const placed = Object.entries(pos.cells);
    const xy = (sq) => [9 - +sq[0], RANKS.indexOf(sq[1])];
    if (!prev)
      return placed.map(([sq, piece]) => ({ piece, from: xy(sq), to: xy(sq) }));
    const remaining = { ...prev.cells };
    const pending = [];
    for (const [sq, piece] of placed) {
      if (remaining[sq] === piece) {
        delete remaining[sq];
        items.push({ piece, from: xy(sq), to: xy(sq) });
      } else pending.push([sq, piece]);
    }
    let moved = 0;
    for (const [sq, piece] of pending) {
      const to = xy(sq);
      const base = piece.replace("+", "");
      let best = null,
        bestD = Infinity;
      for (const [old, p] of Object.entries(remaining)) {
        // A promotion keeps its origin square.
        if (p !== piece && p.replace("+", "") !== base) continue;
        const o = xy(old);
        const d = Math.abs(o[0] - to[0]) + Math.abs(o[1] - to[1]);
        if (d < bestD) {
          best = old;
          bestD = d;
        }
      }
      let from = to;
      if (best) {
        from = xy(best);
        delete remaining[best];
      } else {
        const white = piece.slice(-1) === piece.slice(-1).toLowerCase();
        from = white ? [-1.6, 2] : [10.6, 6];
      }
      moved++;
      items.push({ piece, from, to });
    }
    // A large jump (tree browsing) is shown at once unless it is a new game.
    if (moved > 3 && !gameChanged)
      items.forEach((i) => {
        i.from = i.to;
      });
    return items;
  },

  redraw() {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame((t) => this.draw(t));
  },

  layout(w, h) {
    // stand | gap | 9 cells | rank labels | gap | stand
    const c = Math.floor(Math.min(w / 12.6, (h - 4) / 9.6, 76));
    const total = c * 12.6;
    const ox = Math.round((w - total) / 2);
    const bx = ox + Math.round(c * 1.45);
    const by = Math.round((h - c * 9.6) / 2 + c * 0.55);
    return { c, ox, bx, by, rx: bx + c * 9 + Math.round(c * 0.7) };
  },

  draw(now) {
    const { ctx, w, h } = sizeCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    if (!this.shown) return;
    const { c, ox, bx, by, rx } = this.layout(w, h);
    const { pos, last } = this.shown;
    const crisp = (v) => Math.round(v) + 0.5;

    ctx.fillStyle = BOARD.fill;
    ctx.fillRect(bx, by, c * 9, c * 9);
    for (const sq of last || []) {
      const x = bx + (9 - +sq[0]) * c,
        y = by + RANKS.indexOf(sq[1]) * c;
      ctx.fillStyle = "rgba(144, 238, 144, 0.10)";
      ctx.fillRect(x, y, c, c);
    }
    ctx.strokeStyle = BOARD.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let i = 1; i < 9; i++) {
      ctx.moveTo(crisp(bx + i * c), by);
      ctx.lineTo(crisp(bx + i * c), by + 9 * c);
      ctx.moveTo(bx, crisp(by + i * c));
      ctx.lineTo(bx + 9 * c, crisp(by + i * c));
    }
    ctx.stroke();
    ctx.strokeStyle = BOARD.edge;
    ctx.lineWidth = 1.5;
    ctx.strokeRect(bx, by, c * 9, c * 9);
    ctx.fillStyle = BOARD.grid;
    for (const [i, j] of [
      [3, 3],
      [6, 3],
      [3, 6],
      [6, 6],
    ]) {
      ctx.beginPath();
      ctx.arc(bx + i * c, by + j * c, Math.max(2, c * 0.045), 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.fillStyle = BOARD.coord;
    ctx.font = `${Math.round(c * 0.24)}px "Hiragino Sans", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (let i = 0; i < 9; i++) {
      ctx.fillText(String(9 - i), bx + (i + 0.5) * c, by - c * 0.3);
      ctx.fillText(KANJI_RANK[i], bx + 9 * c + c * 0.32, by + (i + 0.5) * c);
    }
    for (const sq of last || []) {
      const x = bx + (9 - +sq[0]) * c,
        y = by + RANKS.indexOf(sq[1]) * c;
      ctx.strokeStyle = BOARD.last;
      ctx.lineWidth = 2;
      ctx.strokeRect(x + 2, y + 2, c - 4, c - 4);
    }

    // Stands: 後手 top left, 先手 bottom right.
    const standH = Math.round(c * 6.6);
    this.stand(ctx, 1, ox, by, c, standH, pos);
    this.stand(ctx, 0, rx, by + 9 * c - standH, c, standH, pos);

    let t = this.anim ? Math.min(1, (now - this.start) / 300) : 1;
    const p = ease(t);
    const ph = c * 0.9;
    for (const item of this.items) {
      const x = item.from[0] + (item.to[0] - item.from[0]) * p;
      const y = item.from[1] + (item.to[1] - item.from[1]) * p;
      drawPiece(
        ctx,
        item.piece,
        bx + (x + 0.5) * c,
        by + (y + 0.5) * c + c * 0.01,
        ph,
      );
    }
    this.drawArrows(ctx, this.shown.arrows, pos, { c, ox, bx, by, rx, standH });
    if (t < 1) this.frame = requestAnimationFrame((n) => this.draw(n));
    else this.anim = 0;
  },

  drawArrows(ctx, arrows, pos, { c, ox, bx, by, rx, standH }) {
    const square = (sq) => [
      bx + (9 - +sq[0] + 0.5) * c,
      by + (RANKS.indexOf(sq[1]) + 0.5) * c,
    ];
    ctx.save();
    ctx.globalAlpha = 0.85;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    for (const arrow of arrows || []) {
      let from;
      if (arrow.from) from = square(arrow.from);
      else {
        const kinds = HAND_ORDER.split("").filter(
          (kind) => pos.hand[arrow.side][kind] > 0,
        );
        const row = kinds.indexOf(arrow.kind);
        if (row < 0) continue;
        from = [
          (arrow.side ? ox : rx) + Math.round(c * 1.15) * 0.42,
          by +
            (arrow.side ? 0 : c * 9 - standH) +
            c * (0.75 + row * 0.8 + 0.74 / 2),
        ];
      }
      const to = square(arrow.to),
        dx = to[0] - from[0],
        dy = to[1] - from[1];
      const length = Math.hypot(dx, dy);
      if (!length) continue;
      const ux = dx / length,
        uy = dy / length,
        head = c * 0.24;
      const tip = [to[0] - ux * c * 0.12, to[1] - uy * c * 0.12];
      const base = [tip[0] - ux * head, tip[1] - uy * head];
      ctx.beginPath();
      ctx.moveTo(from[0] + ux * c * 0.12, from[1] + uy * c * 0.12);
      ctx.lineTo(base[0], base[1]);
      ctx.strokeStyle = "#101722";
      ctx.lineWidth = Math.max(3, c * 0.11);
      ctx.stroke();
      ctx.strokeStyle = BOARD.arrow;
      ctx.lineWidth = Math.max(2, c * 0.07);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(tip[0], tip[1]);
      ctx.lineTo(base[0] - uy * head * 0.58, base[1] + ux * head * 0.58);
      ctx.lineTo(base[0] + uy * head * 0.58, base[1] - ux * head * 0.58);
      ctx.closePath();
      ctx.strokeStyle = "#101722";
      ctx.lineWidth = Math.max(1, c * 0.025);
      ctx.stroke();
      ctx.fillStyle = BOARD.arrow;
      ctx.fill();
    }
    ctx.restore();
  },

  stand(ctx, side, x, y, c, height, pos) {
    const w = Math.round(c * 1.15);
    ctx.fillStyle = "#121a24";
    ctx.strokeStyle = "#2a3747";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.roundRect(x + 0.5, y + 0.5, w, height, 6);
    ctx.fill();
    ctx.stroke();
    const onMove = pos.turn === side;
    ctx.font = `600 ${Math.round(c * 0.22)}px "Hiragino Sans", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = onMove ? "#e8a94a" : "#8a96a6";
    ctx.fillText(side ? "☖後手" : "☗先手", x + w / 2, y + c * 0.32);
    if (onMove) {
      ctx.fillRect(x + w * 0.2, y + c * 0.55, w * 0.6, 2);
    }
    const ph = c * 0.74;
    let row = 0;
    ctx.font = `600 ${Math.round(c * 0.24)}px "Hiragino Sans", sans-serif`;
    for (const kind of HAND_ORDER) {
      const n = pos.hand[side][kind] || 0;
      if (!n) continue;
      const cy = y + c * 0.75 + row * c * 0.8 + ph / 2;
      const pw = drawPiece(
        ctx,
        side ? kind.toLowerCase() : kind,
        x + w * 0.42,
        cy,
        ph,
      );
      if (n > 1) {
        ctx.fillStyle = "#e6edf3";
        ctx.textAlign = "left";
        ctx.fillText(String(n), x + w * 0.42 + pw / 2 + 1, cy + ph * 0.28);
        ctx.textAlign = "center";
      }
      row++;
    }
    if (!row) {
      ctx.fillStyle = "#5c6878";
      ctx.font = `${Math.round(c * 0.2)}px "Hiragino Sans", sans-serif`;
      ctx.fillText("なし", x + w / 2, y + c * 1.1);
    }
  },
};
new ResizeObserver(() => board.redraw()).observe($("board-wrap"));

// --------------------------------------------------------------- mini board
function drawMini(canvas, sfen, last) {
  const { ctx, w, h } = sizeCanvas(canvas);
  ctx.clearRect(0, 0, w, h);
  if (!sfen) return;
  const pos = parseSfen(sfen);
  const c = Math.floor(Math.min((w - 24) / 9, h / 9));
  const bx = 0,
    by = Math.round((h - 9 * c) / 2);
  ctx.fillStyle = BOARD.fill;
  ctx.fillRect(bx, by, 9 * c, 9 * c);
  for (const sq of last || []) {
    ctx.fillStyle = "rgba(144, 238, 144, 0.22)";
    ctx.fillRect(bx + (9 - +sq[0]) * c, by + RANKS.indexOf(sq[1]) * c, c, c);
  }
  ctx.strokeStyle = BOARD.grid;
  ctx.lineWidth = 0.5;
  ctx.beginPath();
  for (let i = 1; i < 9; i++) {
    ctx.moveTo(bx + i * c, by);
    ctx.lineTo(bx + i * c, by + 9 * c);
    ctx.moveTo(bx, by + i * c);
    ctx.lineTo(bx + 9 * c, by + i * c);
  }
  ctx.stroke();
  ctx.strokeStyle = BOARD.edge;
  ctx.lineWidth = 1;
  ctx.strokeRect(bx + 0.5, by + 0.5, 9 * c - 1, 9 * c - 1);
  for (const [sq, piece] of Object.entries(pos.cells)) {
    drawPiece(
      ctx,
      piece,
      bx + (9 - +sq[0] + 0.5) * c,
      by + (RANKS.indexOf(sq[1]) + 0.5) * c,
      c * 0.92,
    );
  }
  // Hands as a narrow column: 後手 from the top, 先手 from the bottom.
  const hx = bx + 9 * c + 13;
  ctx.font = '600 9px "Hiragino Sans", sans-serif';
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  for (const side of [1, 0]) {
    const kinds = [...HAND_ORDER].filter((k) => pos.hand[side][k]);
    kinds.forEach((k, i) => {
      const cy = side
        ? by + c * (i + 0.5)
        : by + 9 * c - c * (kinds.length - i - 0.5);
      drawPiece(ctx, side ? k.toLowerCase() : k, hx - 2, cy, c * 0.8);
      if (pos.hand[side][k] > 1) {
        ctx.fillStyle = "#e6edf3";
        ctx.fillText(pos.hand[side][k], hx + 5, cy + 3);
      }
    });
  }
}

// -------------------------------------------------------------------- tree
const T = { STEP: 108, W: 90, H: 26, ROW: 34, PAD: 28, HEAD: 24 };
// KiriCompass booktree colors: 先手の手 lightcyan, 後手の手 seashell, selection deeppink.
const CHIP = {
  black: {
    fill: "#e0ffff",
    hover: "#c4f1f1",
    ink: "#111418",
    sub: "#4f6b70",
    edge: "#9fc9cc",
  },
  white: {
    fill: "#fff5ee",
    hover: "#f6e2d4",
    ink: "#111418",
    sub: "#7a6255",
    edge: "#d9c2b2",
  },
  root: {
    fill: "#3a4250",
    hover: "#465060",
    ink: "#f0f3f6",
    sub: "#aab3bf",
    edge: "#5a6474",
  },
};
const PINK = "#ff1493";

function fullStops() {
  const out = new Set();
  for (const t of (app.state && app.state.tasks) || [])
    if (
      t.node != null &&
      t.recorded &&
      !((app.state && app.state.tasks) || []).some(
        (other) =>
          other.node != null &&
          other.node !== t.node &&
          (t.node === "" || other.node.startsWith(t.node + " ")),
      )
    )
      out.add(t.node);
  return out;
}

// A full-tree key past a folded task position points at that task node instead.
function clampFull(key) {
  if (key == null) return key;
  let best = null;
  for (const stop of fullStops()) {
    if (
      key !== stop &&
      key.startsWith(stop ? stop + " " : "") &&
      (best == null || stop.length < best.length)
    )
      best = stop;
  }
  return best == null ? key : best;
}

function taskNodes() {
  const out = new Map();
  for (const t of (app.state && app.state.tasks) || []) {
    if (t.node == null) continue;
    if (!out.has(t.node)) out.set(t.node, []);
    out.get(t.node).push(t);
  }
  return out;
}

const tree = {
  canvas: $("tree"),
  nodes: [],
  byKey: new Map(),
  expanded: new Set(),
  collapsed: new Set(),
  pos: new Map(),
  view: "full",
  views: {
    full: {
      nodes: [],
      byKey: new Map(),
      expanded: new Set(),
      collapsed: new Set(),
      selected: "",
      meta: "",
    },
    task: {
      nodes: [],
      byKey: new Map(),
      expanded: new Set(),
      collapsed: new Set(),
      rareCollapsed: new Set(),
      defaultRareFold: true,
      selected: "",
      meta: "",
      taskId: null,
      revision: null,
    },
  },
  zoom: 1,
  ox: 40,
  oy: 60,
  selected: "",
  hover: null,
  tween: 0,
  positionColors: new Map(),
  groupNodes: null,
  positionGroups: new Map(),

  clearHover() {
    this.canvas.classList?.remove("pointing");
    if (this.hover == null) return;
    this.hover = null;
    this.redraw();
  },

  positionKey(n) {
    return n?.sfen ? n.sfen.trim().split(/\s+/).slice(0, 3).join(" ") : null;
  },

  transpositions() {
    if (this.groupNodes === this.nodes) return this.positionGroups;
    const candidates = new Map(),
      groups = new Map();
    // Only the displayed topology participates; future API nodes stay invisible.
    for (const n of this.nodes) {
      const position = this.positionKey(n);
      if (!position) continue;
      if (!candidates.has(position)) candidates.set(position, []);
      candidates.get(position).push(n);
    }
    for (const [position, nodes] of candidates) {
      if (nodes.length < 2) continue;
      if (!this.positionColors.has(position)) {
        // KiriCompassBook_Prot's golden-ratio hue sequence. Keep allocations for
        // this viewer's lifetime: new groups and main-entrance changes never
        // recolor an existing position, including when switching tree scopes.
        const index = this.positionColors.size;
        const hue = ((0.4 + (index + 1) * 0.61803398875) % 1) * 360;
        this.positionColors.set(position, `hsl(${hue.toFixed(2)} 78% 62%)`);
      }
      groups.set(position, { nodes, color: this.positionColors.get(position) });
    }
    this.groupNodes = this.nodes;
    return (this.positionGroups = groups);
  },

  transpositionHighlights(groups = this.transpositions()) {
    const active = new Map(),
      links = [];
    // Hover takes the anchor for the same group; selection of a different group
    // remains highlighted too. Use a star, rather than all pairwise links.
    for (const key of [this.selected, this.hover]) {
      if (key == null || !this.pos.has(key)) continue;
      const position = this.positionKey(this.byKey.get(key));
      if (groups.has(position)) active.set(position, key);
    }
    for (const [position, from] of active) {
      const group = groups.get(position);
      for (const n of group.nodes) {
        if (n.key !== from && this.pos.has(n.key))
          links.push({ from, to: n.key, color: group.color });
      }
    }
    return { active, links };
  },

  build(rows, base) {
    for (const n of rows) {
      if (n.parent >= 0) {
        const p = rows[n.parent];
        p.children.push(n);
        n.key = p.key ? p.key + " " + n.usi : n.usi;
        n.depth = p.depth + 1;
      } else {
        n.key = "";
        n.depth = base;
      }
      n.size = 0;
    }
    for (let i = rows.length - 1; i > 0; i--)
      rows[rows[i].parent].size += 1 + rows[i].size;
    return rows;
  },

  loadFull(data) {
    const nodes = this.build(
      data.nodes.map(
        ([
          id,
          parent,
          label,
          count,
          original,
          comment,
          main,
          sfen,
          usi,
          b = 0,
          w = 0,
        ]) => ({
          id,
          parent,
          label,
          count,
          b,
          w,
          baseCount: count,
          original: !!original,
          comment,
          main,
          sfen,
          usi,
          children: [],
        }),
      ),
      data.base || 0,
    );
    for (const n of nodes) n.mainKey = nodes[n.main].key;
    Object.assign(this.views.full, {
      nodes,
      sourceNodes: nodes,
      byKey: new Map(nodes.map((n) => [n.key, n])),
      byPosition: new Map(
        nodes
          .filter((n) => n.main === n.id)
          .map((n) => [n.sfen.split(" ").slice(0, 3).join(" "), n]),
      ),
      maxCount: nodes.reduce((m, n) => Math.max(m, n.count), 1),
      meta: `終局 ${data.completed ?? data.games}局・進行 ${data.playing ?? 0}局　${(nodes.length - 1).toLocaleString()}手`,
      rootLabel: "開始局面",
    });
    if (this.view === "full") this.use("full");
  },

  loadTask(data, task) {
    const v = this.views.task;
    const sameTask = v.taskId === data.id;
    const oldSelected = v.byKey.get(
      this.view === "task" ? this.selected : v.selected,
    );
    const nodes = this.build(
      data.nodes.map(
        ([
          id,
          parent,
          label,
          count,
          b,
          w,
          sfen,
          usi,
          main = id,
          entryCount = count,
        ]) => ({
          id,
          parent,
          label,
          count: parent < 0 ? count : entryCount,
          b: parent < 0 || count === entryCount ? b : 0,
          w: parent < 0 || count === entryCount ? w : 0,
          baseCount: parent < 0 ? count : entryCount,
          baseB: parent < 0 || count === entryCount ? b : 0,
          baseW: parent < 0 || count === entryCount ? w : 0,
          baseEntry: entryCount,
          main,
          entryCount,
          original: false,
          comment: "",
          sfen,
          usi,
          children: [],
        }),
      ),
      data.base,
    );
    for (const n of nodes) {
      n.mainKey = nodes[n.main].key;
      n.last = nodeLast(n, task);
    }
    const byPosition = new Map(
      nodes
        .filter((n) => n.main === n.id)
        .map((n) => [n.sfen.split(" ").slice(0, 3).join(" "), n]),
    );
    const migrate = (key) => {
      const old = v.byKey.get(key);
      return (
        old && byPosition.get(old.sfen.split(" ").slice(0, 3).join(" "))?.key
      );
    };
    if (sameTask) {
      v.expanded = new Set(
        [...v.expanded].map(migrate).filter((key) => key != null),
      );
      v.collapsed = new Set(
        [...v.collapsed].map(migrate).filter((key) => key != null),
      );
      v.rareCollapsed = new Set(
        [...(v.rareCollapsed || [])].map(migrate).filter((key) => key != null),
      );
      v.selected = oldSelected
        ? (byPosition.get(oldSelected.sfen.split(" ").slice(0, 3).join(" "))
            ?.key ?? "")
        : "";
      if (this.view === "task") {
        this.selected = v.selected;
        if (app.review != null) app.review = this.selected;
      }
    }
    if (v.taskId !== data.id) {
      v.expanded = new Set();
      v.collapsed = new Set();
      v.rareCollapsed = new Set();
      v.defaultRareFold = true;
      v.selected = "";
    }
    Object.assign(v, {
      nodes,
      sourceNodes: nodes,
      byKey: new Map(nodes.map((n) => [n.key, n])),
      byPosition,
      taskId: data.id,
      revision: data.revision,
      loading: !!data.loading,
      maxCount: nodes.reduce((m, n) => Math.max(m, n.count), 1),
      meta:
        `終局 ${data.completed ?? data.games}局・進行 ${data.playing ?? 0}局　${Math.max(0, nodes.length - 1).toLocaleString()}手` +
        (data.merges ? `・合流 ${data.merges}箇所` : ""),
      rootLabel: "課題局面",
    });
    if (this.view === "task") this.use("task");
  },

  projectDisplayed() {
    // A newer API tree is a template, not permission to show its unplayed edges.
    // Keep original book branches, including their legitimate zero counts.
    const full = this.views.full;
    for (const n of full.nodes) {
      n.allChildren ||= n.children;
      n.visible =
        n.parent < 0 ||
        (full.nodes[n.parent].visible && (n.original || n.count > 0));
    }
    // Children follow their parent in the source array, so prune after all flags exist.
    for (let i = full.nodes.length - 1; i >= 0; i--) {
      const n = full.nodes[i];
      n.children = n.allChildren.filter((ch) => ch.visible);
      n.size = n.children.reduce((sum, ch) => sum + 1 + ch.size, 0);
    }
    full.byPosition = new Map(
      full.nodes
        .filter((n) => n.visible && n.main === n.id)
        .map((n) => [n.position, n]),
    );

    const v = this.views.task,
      source = v.sourceNodes;
    if (source?.length) {
      const previous = v.byKey,
        outgoing = new Map(),
        preferred = new Map();
      const reachable = [];
      for (const n of source) {
        reachable[n.id] =
          n.parent < 0 || (reachable[n.parent] && n.entryCount > 0);
        if (n.main === n.id) {
          if (reachable[n.id]) preferred.set(n.position, n);
        }
        if (n.parent >= 0 && n.entryCount > 0) {
          const p = source[n.parent].position;
          if (!outgoing.has(p)) outgoing.set(p, []);
          outgoing.get(p).push(n);
        }
      }
      for (const edges of outgoing.values())
        edges.sort((a, b) => b.entryCount - a.entryCount);
      const root = source[0],
        chosen = new Map(preferred),
        queue = [root.position],
        visited = new Set(queue);
      // If the API's preferred entrance is still in the replay queue, use an
      // already observed entrance for the shared continuation. Never reveal the
      // future route merely because its merged position has historical visits.
      for (let i = 0; i < queue.length; i++)
        for (const edge of outgoing.get(queue[i]) || []) {
          if (!chosen.has(edge.position)) chosen.set(edge.position, edge);
          const entrance = chosen.get(edge.position);
          if (
            !visited.has(edge.position) &&
            entrance.parent >= 0 &&
            source[entrance.parent].position === queue[i] &&
            entrance.usi === edge.usi
          ) {
            visited.add(edge.position);
            queue.push(edge.position);
          }
        }
      const nodes = [],
        bodyIds = new Map();
      const visit = (template, parent, depth) => {
        const body = parent < 0 || chosen.get(template.position) === template;
        const n = {
          ...template,
          id: nodes.length,
          parent,
          depth,
          sfen: template.position + " " + (depth + 1),
          children: [],
          size: 0,
          // The chip describes its incoming move, even when it owns the
          // merged continuation. Other entrances do not contribute to it.
          count: parent < 0 ? template.count : template.entryCount,
          b: parent < 0 ? template.b : template.entryB,
          w: parent < 0 ? template.w : template.entryW,
          key:
            parent < 0
              ? ""
              : [nodes[parent].key, template.usi].filter(Boolean).join(" "),
          visible: true,
        };
        nodes.push(n);
        if (parent >= 0) nodes[parent].children.push(n);
        if (body) {
          bodyIds.set(n.position, n.id);
          for (const edge of outgoing.get(n.position) || [])
            visit(edge, n.id, depth + 1);
        }
        n.size = n.children.reduce((sum, ch) => sum + 1 + ch.size, 0);
      };
      visit(root, -1, root.depth);
      for (const n of nodes) {
        n.main = bodyIds.get(n.position);
        if (n.main == null) throw new Error("表示中の合流先がありません");
        n.mainKey = nodes[n.main].key;
      }
      const byPosition = new Map([...bodyIds].map(([p, id]) => [p, nodes[id]]));
      const migrate = (key) => byPosition.get(previous.get(key)?.position)?.key;
      v.expanded = new Set(
        [...v.expanded].map(migrate).filter((k) => k != null),
      );
      v.collapsed = new Set(
        [...v.collapsed].map(migrate).filter((k) => k != null),
      );
      v.rareCollapsed = new Set(
        [...(v.rareCollapsed || [])].map(migrate).filter((k) => k != null),
      );
      v.selected =
        migrate(this.view === "task" ? this.selected : v.selected) ?? "";
      if (this.view === "task") {
        this.selected = v.selected;
        if (app.review != null) app.review = this.selected;
      }
      Object.assign(v, {
        nodes,
        byKey: new Map(nodes.map((n) => [n.key, n])),
        byPosition,
      });
      v.meta = v.meta.replace(
        /\d[\d,]*手(?:・合流 \d[\d,]*箇所)?$/,
        `${(nodes.length - 1).toLocaleString()}手` +
          (nodes.length > bodyIds.size
            ? `・合流 ${nodes.length - bodyIds.size}箇所`
            : ""),
      );
    }
    full.meta = full.meta.replace(
      /\d[\d,]*手$/,
      `${Math.max(0, full.nodes.filter((n) => n.visible).length - 1).toLocaleString()}手`,
    );
    this.use(this.view);
  },

  use(name) {
    if (this.view !== name) this.clearHover();
    if (this.views[this.view] && this.view !== name)
      this.views[this.view].selected = this.selected;
    this.view = name;
    const v = this.views[name];
    this.nodes = v.nodes;
    this.byKey = v.byKey;
    this.expanded = v.expanded;
    this.collapsed = v.collapsed;
    this.maxCount = v.maxCount || 1;
    this.rootLabel = v.rootLabel;
    if (name !== this.lastUsed || !this.byKey.has(this.selected))
      this.selected = this.byKey.has(v.selected) ? v.selected : "";
    this.lastUsed = name;
    $("tree-meta").textContent = v.meta;
    this.layout();
    renderScope();
  },

  isOpen(n) {
    if (!n.children.length || this.isProxy(n)) return false;
    if (this.collapsed.has(n.key)) return false;
    if (this.view === "full" && fullStops().has(n.key)) return false;
    if (this.expanded.has(n.key)) return true;
    if (
      this.view === "task" &&
      this.views.task.rareCollapsed?.has(n.key) &&
      !this.taskKeep?.has(n.key)
    )
      return false;
    // Use displayed incoming-move counts, including when counts are replayed.
    // Explicit expansion and protected paths take priority over the default.
    if (
      this.view === "task" &&
      this.views.task.defaultRareFold &&
      !this.taskKeep?.has(n.key) &&
      n.count <= Math.floor((this.nodes[0]?.count || 0) / 10)
    )
      return false;
    // Open task branches and the configured reference routes before each task.
    return (
      this.view === "task" ||
      n.original ||
      (n.depth < (this.nodes[0]?.depth || 0) + 12 &&
        n.count >= Math.max(1, (this.nodes[0]?.count || 0) * 0.1))
    );
  },

  isProxy(n) {
    return n.mainKey !== n.key;
  },

  protectedTaskPaths() {
    const keep = new Set([""]);
    const addPath = (key) => {
      let n = this.byKey.get(key);
      while (n && !keep.has(n.key)) {
        keep.add(n.key);
        n = n.parent >= 0 ? this.nodes[n.parent] : null;
      }
    };
    addPath(this.selected);
    for (const g of Object.values(app.state?.games || {}))
      if (!g.ended) addPath(this.gameKey(g));
    return keep;
  },

  visibleAnchor(key = this.selected) {
    // A manually folded selection can be hidden. Its nearest visible ancestor
    // keeps the review recoverable without replacing the board's selection.
    let p = this.pos.get(key),
      n = this.byKey.get(key);
    while (!p && n && n.parent >= 0) {
      n = this.nodes[n.parent];
      p = n && this.pos.get(n.key);
    }
    return p || this.pos.get(this.nodes[0]?.key);
  },

  nodeInViewport(p) {
    if (!p) return false;
    const w = this.canvas.clientWidth,
      h = this.canvas.clientHeight;
    const s = this.screen(p),
      margin = 6,
      nw = T.W * this.zoom,
      nh = T.H * this.zoom;
    if (!(w > 0 && h > 0)) return false;
    // If a chip cannot fit at the chosen zoom, keep its center in the viewport.
    const x =
      nw <= w - margin * 2
        ? s.x >= margin && s.x + nw <= w - margin
        : s.x + nw / 2 >= 0 && s.x + nw / 2 <= w;
    const y =
      nh <= h - T.HEAD - margin * 2
        ? s.y >= T.HEAD + margin && s.y + nh <= h - margin
        : s.y + nh / 2 >= Math.min(T.HEAD, h / 2) && s.y + nh / 2 <= h;
    return x && y;
  },

  relayoutBranches(anchorKey = this.selected) {
    const before = this.visibleAnchor(anchorKey),
      wasVisible = this.nodeInViewport(before);
    this.clearHover();
    this.tween++;
    this.layout();
    const after = this.visibleAnchor(anchorKey);
    // Only preserve an anchor that was actually on screen. Keeping an offscreen
    // selection's coordinates after removing whole columns can leave no nodes.
    if (wasVisible && after) this.oy += (before.y - after.y) * this.zoom;
    if (after && !this.nodeInViewport(after)) this.frameVisibleNodes(anchorKey);
    else this.redraw();
  },

  closeRare(limit) {
    if (
      this.view !== "task" ||
      this.views.task.loading ||
      !Number.isSafeInteger(limit) ||
      limit < 0
    )
      return false;
    const keep = this.protectedTaskPaths(),
      rare = new Set();
    for (const n of this.nodes) {
      if (keep.has(n.key)) this.collapsed.delete(n.key);
      else if (n.children.length && !this.isProxy(n) && n.count <= limit) {
        rare.add(n.key);
        this.expanded.delete(n.key);
      }
    }
    this.views.task.rareCollapsed = rare;
    this.views.task.defaultRareFold = false;
    this.relayoutBranches();
    return true;
  },

  openAllTask() {
    if (this.view !== "task" || this.views.task.loading) return false;
    this.expanded.clear();
    this.collapsed.clear();
    this.views.task.rareCollapsed = new Set();
    this.views.task.defaultRareFold = false;
    this.relayoutBranches();
    return true;
  },

  reveal(key) {
    if (key == null) return;
    const parts = key ? key.split(" ") : [];
    for (let i = 0; i < parts.length; i++) {
      const k = parts.slice(0, i).join(" ");
      this.expanded.add(k);
      this.collapsed.delete(k);
    }
  },

  layout() {
    this.pos = new Map();
    if (!this.nodes.length) return;
    // Bulk folding exempts the selection and every live game's path. Recheck
    // on each layout so the other games can move into previously folded paths.
    this.taskKeep =
      this.view === "task" &&
      (this.views.task.rareCollapsed?.size || this.views.task.defaultRareFold)
        ? this.protectedTaskPaths()
        : null;
    // KiriCompassBook_Prot's column-wise layout. A fork reserves room in its
    // own column rather than making every later branch wait for all its leaves.
    // Recompute from the open topology, so folding releases that room too.
    const nextY = new Map();
    const visit = (n) => {
      const open = this.isOpen(n);
      let y;
      if (open) {
        for (const ch of n.children) {
          nextY.set(
            ch.depth,
            Math.max(nextY.get(ch.depth) || 0, nextY.get(n.depth) || 0),
          );
          visit(ch);
          if (y == null) {
            y = this.pos.get(ch.key).y;
            nextY.set(
              n.depth,
              Math.max(nextY.get(n.depth) || 0, nextY.get(ch.depth) || 0),
            );
          }
        }
      } else {
        y = nextY.get(n.depth) || 0;
        nextY.set(n.depth, y + T.ROW);
      }
      this.pos.set(n.key, { x: n.depth * T.STEP, y, n, open });
    };
    visit(this.nodes[0]);
    this.rows = Math.max(0, ...nextY.values()) / T.ROW;
  },

  gameKey(g) {
    if (!g) return null;
    if (this.view === "task" && g.focus_id !== this.views.task.taskId)
      return null;
    const v = this.views[this.view];
    const body =
      g.sfen && v.byPosition?.get(g.sfen.split(" ").slice(0, 3).join(" "));
    const key = body ? body.key : this.view === "task" ? g.task_key : g.node;
    if (this.view === "task") return v.byKey.get(key)?.mainKey ?? null;
    return clampFull(key);
  },

  live() {
    return this.gameKey(
      app.state && app.state.games && app.state.games[app.worker],
    );
  },

  liveKeys() {
    const out = new Map();
    const games = (app.state && app.state.games) || {};
    for (const [w, g] of Object.entries(games)) {
      const key = this.gameKey(g);
      if (key == null) continue;
      if (!out.has(key)) out.set(key, []);
      out.get(key).push(+w);
    }
    return out;
  },

  select(
    key,
    { center = false, animate = true, scroll = true, arrival = null } = {},
  ) {
    if (this.view === "full") key = clampFull(key);
    const n = this.byKey.get(key);
    if (!n || n.visible === false) return;
    const position = n.sfen.split(" ").slice(0, 3).join(" ");
    // Children live at the canonical body, but the board highlight belongs to
    // the incoming move the visitor actually selected, including a merge edge.
    const last =
      arrival?.position === position
        ? arrival.last
        : n.last || lastSquares(n.usi);
    key = n.mainKey; // a merged duplicate opens the main line
    if (this.view === "full") key = clampFull(key);
    this.selected = key;
    this.arrival = { view: this.view, key, position, last: [...last] };
    this.reveal(key);
    this.layout();
    if (scroll) {
      if (center) this.center(animate);
      else this.ensureVisible();
    }
    this.redraw();
    detail();
  },

  selectedLast(n) {
    const a = this.arrival;
    return a?.view === this.view &&
      a.key === n.key &&
      a.position === n.sfen.split(" ").slice(0, 3).join(" ")
      ? a.last
      : n.last || lastSquares(n.usi);
  },

  screen(p) {
    return { x: p.x * this.zoom + this.ox, y: p.y * this.zoom + this.oy };
  },

  center(animate = true) {
    const p = this.pos.get(this.selected);
    if (!p) return;
    const w = this.canvas.clientWidth,
      h = this.canvas.clientHeight;
    const tx = w * 0.62 - (p.x + T.W / 2) * this.zoom;
    const ty = h * 0.45 - (p.y + T.H / 2) * this.zoom;
    this.glide(tx, ty, animate);
  },

  frameVisibleNodes(anchorKey = this.selected) {
    // Keep the user's zoom. Find the viewport placement containing the most
    // complete visible nodes; tied placements stay near the selection.
    if (!this.pos.size) return;
    const selected = this.visibleAnchor(anchorKey);
    if (!selected) return;
    const margin = 18,
      width = (this.canvas.clientWidth - margin * 2) / this.zoom - T.W - 44;
    const height =
      (this.canvas.clientHeight - T.HEAD - margin * 2) / this.zoom - T.H;
    if (width < 0 || height < 0) {
      const w = this.canvas.clientWidth,
        h = this.canvas.clientHeight;
      this.glide(
        w / 2 - (selected.x + T.W / 2) * this.zoom,
        (h + Math.min(T.HEAD, h)) / 2 - (selected.y + T.H / 2) * this.zoom,
        false,
      );
      return;
    }
    // At a task root the first branch starts on the root's row. Pin that root
    // at the top-left margin; scanning/sorting every branch adds no benefit.
    if (this.view === "task" && selected.n?.parent < 0) {
      this.glide(
        margin - selected.x * this.zoom,
        T.HEAD + margin - selected.y * this.zoom,
        false,
      );
      return;
    }
    const points = [...this.pos.values()];
    let best = null;
    for (const x of new Set(points.map((p) => p.x))) {
      if (selected.x < x || selected.x > x + width) continue;
      const column = points
        .filter((p) => p.x >= x && p.x <= x + width)
        .sort((a, b) => a.y - b.y);
      for (let start = 0, end = 0; start < column.length; start++) {
        while (end < column.length && column[end].y <= column[start].y + height)
          end++;
        const y = column[start].y,
          count = end - start;
        if (selected.y < y || selected.y > y + height) continue;
        const distance =
          Math.abs(x + width / 2 - selected.x) +
          Math.abs(y + height / 2 - selected.y);
        if (
          !best ||
          count > best.count ||
          (count === best.count && distance < best.distance)
        )
          best = { x, y, count, distance };
      }
    }
    if (best)
      this.glide(
        margin - best.x * this.zoom,
        T.HEAD + margin - best.y * this.zoom,
        false,
      );
  },

  ensureVisible() {
    const p = this.pos.get(this.selected);
    if (!p) return;
    const w = this.canvas.clientWidth,
      h = this.canvas.clientHeight;
    const s = this.screen(p);
    const m = 40;
    let tx = this.ox,
      ty = this.oy;
    if (s.x < m) tx += m - s.x;
    if (s.x + T.W * this.zoom > w - m) tx -= s.x + T.W * this.zoom - (w - m);
    if (s.y < m) ty += m - s.y;
    if (s.y + T.H * this.zoom > h - m) ty -= s.y + T.H * this.zoom - (h - m);
    if (tx !== this.ox || ty !== this.oy) this.glide(tx, ty, true);
  },

  glide(tx, ty, animate) {
    this.clearHover();
    const token = ++this.tween;
    if (!animate) {
      this.ox = tx;
      this.oy = ty;
      this.redraw();
      return;
    }
    const sx = this.ox,
      sy = this.oy,
      t0 = performance.now();
    const step = (now) => {
      if (token !== this.tween) return;
      const t = Math.min(1, (now - t0) / 260),
        e = ease(t);
      this.ox = sx + (tx - sx) * e;
      this.oy = sy + (ty - sy) * e;
      this.draw();
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  },

  setZoom(z, ax, ay) {
    this.clearHover();
    z = Math.max(0.35, Math.min(2.2, z));
    if (ax == null) {
      ax = this.canvas.clientWidth / 2;
      ay = this.canvas.clientHeight / 2;
    }
    this.tween++;
    this.ox = ax - (ax - this.ox) * (z / this.zoom);
    this.oy = ay - (ay - this.oy) * (z / this.zoom);
    this.zoom = z;
    $("zoom-value").textContent = Math.round(z * 100) + "%";
    this.redraw();
  },

  redraw() {
    cancelAnimationFrame(this.frame);
    this.frame = requestAnimationFrame(() => this.draw());
  },

  draw() {
    const { ctx, w, h } = sizeCanvas(this.canvas);
    ctx.clearRect(0, 0, w, h);
    if (!this.pos.size) {
      ctx.fillStyle = "#5c6878";
      ctx.font = '13px "Hiragino Sans", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText(
        this.view === "task"
          ? "この課題の対局はまだありません"
          : "kiriを読み込み中…",
        w / 2,
        h / 2,
      );
      return;
    }
    const z = this.zoom;
    const onPath = new Set();
    if (this.selected != null) {
      const parts = this.selected ? this.selected.split(" ") : [];
      for (let i = 0; i <= parts.length; i++)
        onPath.add(parts.slice(0, i).join(" "));
    }
    const liveKey = this.live();
    const livePath = new Set();
    if (liveKey != null) {
      const parts = liveKey ? liveKey.split(" ") : [];
      for (let i = 0; i <= parts.length; i++)
        livePath.add(parts.slice(0, i).join(" "));
    }
    const markers = this.liveKeys();
    const visible = (x, y, ww, hh) =>
      x + ww >= -20 && x <= w + 20 && y + hh >= -20 && y <= h + 20;
    const groups = this.transpositions();
    const highlights = this.transpositionHighlights(groups);

    // Alternate ply columns, so chips of the same ply line up visibly.
    const colW = T.STEP * z;
    const firstCol = Math.max(0, Math.floor(-this.ox / colW) - 1);
    const lastCol = Math.ceil((w - this.ox) / colW) + 1;
    for (let d = firstCol; d <= lastCol; d++) {
      if (d % 2) continue;
      ctx.fillStyle = "rgba(255, 255, 255, 0.018)";
      ctx.fillRect(this.ox + d * colW - ((T.STEP - T.W) * z) / 2, 0, colW, h);
    }

    // Edges, thicker for more observed games.
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    const edges = [[], [], []]; // normal, live, selected (drawn last)
    for (const p of this.pos.values()) {
      if (p.n.parent < 0) continue;
      const parent = this.pos.get(this.nodes[p.n.parent].key);
      const a = this.screen(parent),
        b = this.screen(p);
      const x0 = a.x + T.W * z,
        y0 = a.y + (T.H * z) / 2,
        x1 = b.x,
        y1 = b.y + (T.H * z) / 2;
      if (
        !visible(
          Math.min(x0, x1),
          Math.min(y0, y1),
          Math.abs(x1 - x0),
          Math.abs(y1 - y0),
        )
      )
        continue;
      const layer = onPath.has(p.n.key) ? 2 : livePath.has(p.n.key) ? 1 : 0;
      edges[layer].push([x0, y0, x1, y1, p.n.count]);
    }
    const colors = ["#9aa3ad", "#5fae6a", PINK];
    edges.forEach((list, layer) => {
      for (const [x0, y0, x1, y1, count] of list) {
        const mid = x1 - 10 * z;
        const r = Math.min(8 * z, Math.abs(y1 - y0) / 2);
        ctx.beginPath();
        ctx.moveTo(x0, y0);
        if (Math.abs(y1 - y0) < 0.5) ctx.lineTo(x1, y1);
        else {
          ctx.lineTo(mid - r, y0);
          ctx.arcTo(mid, y0, mid, y0 + r, r);
          ctx.lineTo(mid, y1 - r);
          ctx.arcTo(mid, y1, mid + r, y1, r);
          ctx.lineTo(x1, y1);
        }
        ctx.strokeStyle = count || layer ? colors[layer] : "#454d58";
        ctx.lineWidth =
          (count ? 1.2 + 2.6 * Math.sqrt(count / this.maxCount) : 1) *
          Math.max(0.7, z);
        ctx.stroke();
      }
    });

    // Join identical positions on hover/selection, behind chips and without
    // changing the ordinary path edges, counts, expansion, or following.
    for (const link of highlights.links) {
      const from = this.screen(this.pos.get(link.from)),
        to = this.screen(this.pos.get(link.to));
      const x0 = from.x + (T.W * z) / 2,
        y0 = from.y + (T.H * z) / 2;
      const x1 = to.x + (T.W * z) / 2,
        y1 = to.y + (T.H * z) / 2;
      if (
        !visible(
          Math.min(x0, x1),
          Math.min(y0, y1),
          Math.abs(x1 - x0),
          Math.abs(y1 - y0),
        )
      )
        continue;
      ctx.beginPath();
      ctx.moveTo(x0, y0);
      ctx.lineTo(x1, y1);
      ctx.strokeStyle = "#0f141b";
      ctx.lineWidth = Math.max(4, 6 * z);
      ctx.stroke();
      ctx.strokeStyle = link.color;
      ctx.lineWidth = Math.max(2, 4 * z);
      ctx.stroke();
      ctx.setLineDash([5 * z, 3 * z]);
      ctx.strokeStyle = "#fff";
      ctx.lineWidth = Math.max(1, 1.5 * z);
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Nodes: 先手 light, 後手 dark.
    const fs = 12 * z,
      small = 10.5 * z;
    const tasks = taskNodes();
    const current = app.state && app.state.task_id;
    for (const p of this.pos.values()) {
      const { x, y } = this.screen(p);
      const cw = T.W * z,
        chh = T.H * z;
      if (!visible(x, y - 14 * z, cw + 50 * z, chh + 14 * z)) continue;
      const n = p.n;
      const sel = n.key === this.selected;
      const path = onPath.has(n.key);
      const hover = this.hover === n.key;
      const root = n.parent < 0;
      const tone = root ? CHIP.root : n.depth % 2 ? CHIP.black : CHIP.white;
      const position = this.positionKey(n);
      const group = groups.get(position);
      const highlighted = highlights.active.has(position);
      ctx.globalAlpha = n.count || root || path ? 1 : 0.5;
      ctx.beginPath();
      ctx.roundRect(x, y, cw, chh, 6 * z);
      ctx.fillStyle = highlighted ? "#ffe18a" : hover ? tone.hover : tone.fill;
      ctx.fill();
      if (group) {
        ctx.globalAlpha = 1;
        ctx.strokeStyle = "#0f141b";
        ctx.lineWidth = Math.max(3, 4.5 * z);
        ctx.stroke();
        ctx.strokeStyle = group.color;
        ctx.lineWidth = Math.max(1.5, 3 * z);
        ctx.stroke();
        ctx.globalAlpha = n.count || root || path ? 1 : 0.5;
      } else {
        ctx.lineWidth = path && !sel ? 1.5 : 1;
        ctx.strokeStyle = path ? PINK : tone.edge;
        ctx.stroke();
      }
      if (z >= 0.5) {
        ctx.textBaseline = "middle";
        ctx.textAlign = "left";
        ctx.font = `${root ? 700 : 500} ${fs}px "Hiragino Sans", sans-serif`;
        ctx.fillStyle = highlighted ? "#111418" : tone.ink;
        ctx.fillText(
          root ? this.rootLabel : n.label,
          x + 7 * z,
          y + chh / 2 + 0.5,
        );
        if (n.count) {
          ctx.textAlign = "right";
          ctx.font = `${small}px "Hiragino Sans", sans-serif`;
          ctx.fillStyle = highlighted ? "#685516" : tone.sub;
          ctx.fillText(String(n.count), x + cw - 6 * z, y + chh / 2 + 0.5);
        }
      }
      ctx.globalAlpha = 1;
      if (sel) {
        ctx.beginPath();
        ctx.roundRect(x - 3, y - 3, cw + 6, chh + 6, 8 * z);
        ctx.strokeStyle = PINK;
        ctx.lineWidth = 3;
        ctx.stroke();
      } else if (n.key === liveKey) {
        ctx.beginPath();
        ctx.roundRect(x - 3, y - 3, cw + 6, chh + 6, 8 * z);
        ctx.strokeStyle = "#86e08f";
        ctx.lineWidth = 2;
        ctx.stroke();
      }
      if (n.comment) {
        ctx.beginPath();
        ctx.arc(x + cw - 3 * z, y + 3 * z, 3 * z, 0, Math.PI * 2);
        ctx.fillStyle = "#a5db7a";
        ctx.fill();
      }
      // Right-hand affordance: merged duplicate, or folded branch with its size.
      if (this.isProxy(n)) {
        ctx.font = `${small}px "Hiragino Sans", sans-serif`;
        ctx.textAlign = "left";
        ctx.fillStyle = group ? group.color : "#7f9cc4";
        ctx.fillText("↪ 合流", x + cw + 5 * z, y + chh / 2);
      } else if (n.children.length && !p.open) {
        const label = "+" + n.size;
        ctx.font = `${small}px "Hiragino Sans", sans-serif`;
        const tw = ctx.measureText(label).width + 10 * z;
        ctx.beginPath();
        ctx.roundRect(x + cw + 5 * z, y + chh / 2 - 8 * z, tw, 16 * z, 8 * z);
        ctx.fillStyle = "#202a37";
        ctx.fill();
        ctx.textAlign = "center";
        ctx.fillStyle = "#9fb0c6";
        ctx.fillText(label, x + cw + 5 * z + tw / 2, y + chh / 2 + 0.5);
      }
      // Task flags sit on the top-right corner.
      const flags = this.view === "full" ? tasks.get(n.key) : null;
      if (flags) {
        ctx.font = `700 ${9 * z}px "Hiragino Sans", sans-serif`;
        let fx = x + cw + 2 * z;
        for (const t of [...flags].reverse()) {
          const on = t.id === current;
          const label = "課題" + t.n;
          const tw = ctx.measureText(label).width + 10 * z;
          fx -= tw;
          ctx.beginPath();
          ctx.roundRect(fx, y - 8 * z, tw, 13 * z, 4 * z);
          ctx.fillStyle = on ? "#b48cf0" : "#2a2140";
          ctx.fill();
          if (!on) {
            ctx.strokeStyle = "#6d58a0";
            ctx.lineWidth = 1;
            ctx.stroke();
          }
          ctx.fillStyle = on ? "#1b1030" : "#cdb8f5";
          ctx.textAlign = "center";
          ctx.fillText(label, fx + tw / 2, y - 1.5 * z);
          fx -= 3 * z;
        }
      }
      const games = markers.get(n.key);
      if (games) {
        let bx = x - 6 * z;
        for (const g of games) {
          ctx.beginPath();
          ctx.arc(bx, y - 2 * z, 8 * z, 0, Math.PI * 2);
          ctx.fillStyle = g === app.worker ? "#86e08f" : "#6b7a8f";
          ctx.fill();
          ctx.fillStyle = "#0d1117";
          ctx.font = `700 ${10 * z}px "Hiragino Sans", sans-serif`;
          ctx.textAlign = "center";
          ctx.fillText(String(g), bx, y - 2 * z + 0.5);
          bx += 15 * z;
        }
      }
    }

    // Ply header, pinned to the top.
    ctx.fillStyle = "rgba(15, 20, 27, 0.94)";
    ctx.fillRect(0, 0, w, T.HEAD);
    ctx.fillStyle = "#242f3d";
    ctx.fillRect(0, T.HEAD, w, 1);
    const selDepth = this.byKey.get(this.selected)?.depth;
    const taskPly = ((app.state && app.state.tasks) || []).find(
      (t) => t.id === current,
    )?.ply;
    const every = z < 0.45 ? 5 : z < 0.7 ? 2 : 1;
    ctx.textBaseline = "middle";
    ctx.textAlign = "center";
    for (let d = Math.max(1, firstCol); d <= lastCol; d++) {
      const cx = this.ox + (d * T.STEP + T.W / 2) * z;
      const special = d === selDepth || d === taskPly;
      if (d % every && !special) continue;
      const label = d + "手";
      if (d === selDepth) {
        ctx.font = '700 11px "Hiragino Sans", sans-serif';
        const tw = ctx.measureText(label).width + 12;
        ctx.beginPath();
        ctx.roundRect(cx - tw / 2, 4, tw, 16, 8);
        ctx.fillStyle = PINK;
        ctx.fill();
        ctx.fillStyle = "#ffffff";
      } else if (d === taskPly) {
        ctx.font = '700 11px "Hiragino Sans", sans-serif';
        ctx.fillStyle = "#b48cf0";
      } else {
        ctx.font = '11px "Hiragino Sans", sans-serif';
        ctx.fillStyle = d % 10 ? "#5c6878" : "#8a96a6";
      }
      ctx.fillText(label, cx, T.HEAD / 2 + 0.5);
    }
  },

  hit(ev) {
    const r = this.canvas.getBoundingClientRect();
    if (ev.clientY - r.top < T.HEAD) return null;
    const mx = (ev.clientX - r.left - this.ox) / this.zoom;
    const my = (ev.clientY - r.top - this.oy) / this.zoom;
    for (const p of this.pos.values()) {
      if (
        mx >= p.x &&
        mx <= p.x + T.W + 44 &&
        my >= p.y - 2 &&
        my <= p.y + T.H + 2
      ) {
        return { key: p.n.key, onToggle: mx > p.x + T.W };
      }
    }
    return null;
  },

  toggle(key) {
    const n = this.byKey.get(key);
    if (!n || !n.children.length) return;
    if (this.isProxy(n)) {
      pickNode(n.mainKey);
      return;
    }
    if (this.view === "full" && fullStops().has(key)) {
      pickNode(key);
      return;
    }
    if (this.isOpen(n)) {
      this.collapsed.add(key);
      this.expanded.delete(key);
    } else {
      this.expanded.add(key);
      this.collapsed.delete(key);
    }
    this.relayoutBranches(key);
  },
};

// ------------------------------------------------------------- tree input
(() => {
  const c = tree.canvas;
  let drag = null;
  c.addEventListener("pointerdown", (e) => {
    c.focus();
    drag = {
      x: e.clientX,
      y: e.clientY,
      ox: tree.ox,
      oy: tree.oy,
      moved: false,
    };
    c.setPointerCapture(e.pointerId);
  });
  c.addEventListener("pointermove", (e) => {
    if (drag) {
      const dx = e.clientX - drag.x,
        dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dx) + Math.abs(dy) > 4) {
        drag.moved = true;
        c.classList.add("dragging");
        setFollow(false);
      }
      if (drag.moved) {
        tree.clearHover();
        tree.tween++;
        tree.ox = drag.ox + dx;
        tree.oy = drag.oy + dy;
        tree.redraw();
      }
      return;
    }
    const h = tree.hit(e);
    const key = h ? h.key : null;
    c.classList.toggle("pointing", !!h);
    if (key !== tree.hover) {
      tree.hover = key;
      tree.redraw();
    }
  });
  c.addEventListener("pointerup", (e) => {
    const d = drag;
    drag = null;
    c.classList.remove("dragging");
    if (!d || d.moved) return;
    const h = tree.hit(e);
    if (!h) return;
    if (h.onToggle) tree.toggle(h.key);
    else pickNode(h.key);
  });
  c.addEventListener("pointerleave", () => tree.clearHover());
  c.addEventListener("dblclick", (e) => {
    const h = tree.hit(e);
    if (h && !h.onToggle) tree.toggle(h.key);
  });
  c.addEventListener(
    "wheel",
    (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      if (e.ctrlKey || e.metaKey) {
        tree.setZoom(
          tree.zoom * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.002)),
          e.clientX - r.left,
          e.clientY - r.top,
        );
      } else {
        tree.clearHover();
        setFollow(false);
        tree.tween++;
        tree.ox -= e.deltaX;
        tree.oy -= e.deltaY;
        tree.redraw();
      }
    },
    { passive: false },
  );
  new ResizeObserver(() => tree.redraw()).observe($("tree-wrap"));
})();
