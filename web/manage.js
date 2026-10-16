"use strict";

async function manage() {
  const form = $("settings"),
    dialog = $("settings-dialog");
  const response = await fetch("/api/settings");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "設定を読み込めませんでした");
  for (const [key, value] of Object.entries(data.settings)) {
    const field = form.elements.namedItem(key);
    if (field?.type === "checkbox") field.checked = value;
    else if (field) field.value = value;
  }
  $("public-url").textContent = data.public_url;
  if (data.warning) $("message").textContent = data.warning;
  function addTask(task = {}) {
    const row = document.createElement("div");
    row.className = "task-edit";
    row.dataset.id = task.id || "task-" + crypto.randomUUID().slice(0, 8);
    row.innerHTML =
      '<div class="task-edit-head"><label>課題名<input class="task-label" required></label><button type="button" class="btn task-up" aria-label="課題を上へ">↑</button><button type="button" class="btn task-down" aria-label="課題を下へ">↓</button><button type="button" class="btn task-remove" aria-label="課題を削除">削除</button></div><label>共通起点からの追加手順<textarea class="task-moves" rows="2" placeholder="例：7g7f 3c3d（空欄なら共通起点）"></textarea></label>';
    row.querySelector(".task-label").value = task.label || "";
    row.querySelector(".task-moves").value = Array.isArray(task.moves)
      ? task.moves.join(" ")
      : task.moves || "";
    row.querySelector(".task-remove").onclick = () => row.remove();
    row.querySelector(".task-up").onclick = () => {
      if (row.previousElementSibling)
        row.parentElement.insertBefore(row, row.previousElementSibling);
    };
    row.querySelector(".task-down").onclick = () => {
      if (row.nextElementSibling)
        row.parentElement.insertBefore(row.nextElementSibling, row);
    };
    $("task-editor").append(row);
    return row;
  }
  for (const task of data.settings.tasks || []) addTask(task);
  $("add-task").addEventListener("click", () => {
    addTask().querySelector("input").focus();
  });
  $("open-settings").addEventListener("click", () => dialog.showModal());
  $("close-settings").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const r = dialog.getBoundingClientRect();
      if (
        event.clientX < r.left ||
        event.clientX > r.right ||
        event.clientY < r.top ||
        event.clientY > r.bottom
      )
        dialog.close();
    }
  });
  async function post(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Makebook-Token": data.token,
      },
      body: JSON.stringify(body),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "操作に失敗しました");
    return result;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('[type="submit"]');
    const values = Object.fromEntries(new FormData(form));
    values.save_search_details = form.elements.save_search_details.checked;
    for (const key of [
      "nodes",
      "threads",
      "hash_mb",
      "parallel",
      "resign",
      "max_plies",
      "target_black_streak",
    ])
      values[key] = Number(values[key]);
    values.engine_arguments = data.settings.engine_arguments || [];
    values.tasks = [...$("task-editor").children].map((row) => ({
      id: row.dataset.id,
      label: row.querySelector(".task-label").value,
      moves: row.querySelector(".task-moves").value,
    }));
    button.disabled = true;
    button.textContent = "読み込み中…";
    $("message").textContent = "";
    try {
      await post("/api/start", values);
      dialog.close();
      toast("開始・再開を受け付けました");
      wakePolling();
    } catch (error) {
      $("message").textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = "開始・再開";
    }
  });
  for (const button of form.querySelectorAll("[data-control]"))
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await post("/api/control", { operation: button.dataset.control });
        toast("現在の対局が終わり次第、切り替えます");
        dialog.close();
        wakePolling();
      } catch (error) {
        $("message").textContent = error.message;
      } finally {
        button.disabled = false;
      }
    });
  if (!data.settings.engine) dialog.showModal();
}
manage().catch((error) => {
  $("conn-text").textContent = error.message;
});
