"use strict";
const paths = {
  archive: "M4 7h16v13H4z M3 3h18v4H3z M9 11h6",
  grid: "M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z",
  channels: "M4 5h16v11H4z M8 20h8 M12 16v4 M9 9l5 3-5 3z",
  playlist: "M4 5h16 M4 10h16 M4 15h9 M17 14l5 4-5 4z",
  heart:
    "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8z",
  download: "M12 3v12 M7 10l5 5 5-5 M4 15v6h16v-6",
  settings:
    "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1z",
  drive: "M5 4h14l3 12v4H2v-4z M2 16h20 M16 18h2",
  video: "M3 4h18v16H3z M10 8l6 4-6 4z",
  clock: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18 M12 7v5l3 2",
  plus: "M12 5v14 M5 12h14",
  shield: "M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6",
  check: "M5 12l4 4L19 6",
  close: "M6 6l12 12 M18 6 6 18",
  search: "M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14 M15 15l6 6",
  list: "M9 5h12 M9 12h12 M9 19h12 M3 5h1 M3 12h1 M3 19h1",
  play: "M8 4l13 8-13 8z",
  menu: "M4 6h16 M4 12h16 M4 18h16",
  file: "M5 2h9l5 5v15H5z M14 2v6h5 M8 13h8 M8 17h6",
  trash: "M3 6h18 M9 6V3h6v3 M5 6l1 15h12l1-15 M10 10v7 M14 10v7",
  refresh: "M20 10a8 8 0 1 0 0 5 M20 4v6h-6",
  subtitles: "M3 5h18v14H3z M6 10h4 M14 10h4 M6 14h7 M16 14h2",
};
const icon = (name) =>
  `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name] || paths.video}"/></svg>`;
const $ = (selector, scope = document) => scope.querySelector(selector);
const $$ = (selector, scope = document) => [
  ...scope.querySelectorAll(selector),
];
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const mediaURL = (file) =>
  "/media/" + file.split("/").map(encodeURIComponent).join("/");
const bytes = (n) => {
  if (!n) return "0 B";
  const unit = Math.min(4, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KB", "MB", "GB", "TB"][unit]}`;
};
const duration = (n) => {
  n = Math.floor(n || 0);
  return `${n >= 3600 ? Math.floor(n / 3600) + ":" : ""}${String(Math.floor((n % 3600) / 60)).padStart(n >= 3600 ? 2 : 1, "0")}:${String(n % 60).padStart(2, "0")}`;
};
const date = (n) =>
  new Date(n * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
const state = {
  view: "library",
  library: { videos: [], playlists: [], memberships: [], storage: {} },
  jobs: [],
  settings: null,
  query: "",
  filter: "all",
  sort: "newest",
  layout: localStorage.getItem("yourchive-layout") || "grid",
  group: null,
  online: false,
  loaded: false,
};
let toastTimer,
  playerVideo = null,
  lastProgressSave = 0;
async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    let detail;
    try {
      detail = (await response.json()).detail;
    } catch {}
    throw new Error(
      Array.isArray(detail)
        ? detail.map((d) => d.msg).join("; ")
        : detail || `Request failed (${response.status})`,
    );
  }
  return response.json();
}
function toast(text) {
  $("#toast").textContent = text;
  $("#toast").classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => $("#toast").classList.remove("show"), 4000);
}
function decorate() {
  $$("[data-icon]").forEach((el) => {
    el.innerHTML = icon(el.dataset.icon);
    el.removeAttribute("data-icon");
  });
}
function channels() {
  const groups = new Map();
  state.library.videos.forEach((v) => {
    const id = v.channel_id || v.channel;
    if (!groups.has(id)) groups.set(id, { id, title: v.channel, count: 0 });
    groups.get(id).count++;
  });
  return [...groups.values()].sort((a, b) => a.title.localeCompare(b.title));
}
function updateStats() {
  const videos = state.library.videos,
    storage = state.library.storage;
  $("#video-count").textContent = videos.length;
  $("#channel-count").textContent = channels().length;
  $("#playlist-count").textContent = state.library.playlists.length;
  $("#job-count").textContent = state.jobs.filter((j) =>
    ["running", "queued"].includes(j.status),
  ).length;
  $("#stat-videos").textContent = videos.length.toLocaleString();
  $("#stat-channels").textContent = channels().length;
  $("#stat-hours").textContent =
    `${(videos.reduce((n, v) => n + v.duration, 0) / 3600).toFixed(1).replace(".0", "")}h`;
  $("#stat-size").textContent = bytes(storage.videos);
  $("#storage-size").textContent = bytes(storage.videos);
  $("#storage-bar").value = storage.total
    ? ((storage.total - storage.free) / storage.total) * 100
    : 0;
  $("#storage-detail").textContent = storage.total
    ? `${bytes(storage.free)} free on ${bytes(storage.total)} disk`
    : "Disk information unavailable";
  $("#server-status").textContent = state.online
    ? "Server connected"
    : "Server unavailable";
  $(".status-dot").classList.toggle("offline", !state.online);
}
async function sync({ render = true } = {}) {
  try {
    const [library, jobs] = await Promise.all([api("/library"), api("/jobs")]);
    const changed =
      JSON.stringify(library.videos) !== JSON.stringify(state.library.videos) ||
      JSON.stringify(library.memberships) !==
        JSON.stringify(state.library.memberships);
    state.library = library;
    state.jobs = jobs;
    state.online = true;
    const first = !state.loaded;
    state.loaded = true;
    updateStats();
    if (render && (first || changed || state.view === "downloads"))
      renderContent();
  } catch (e) {
    state.online = false;
    updateStats();
    if (!state.loaded) {
      $("#content").innerHTML =
        `<div class="offline-banner">Could not connect to your archive. ${esc(e.message)} <button class="button secondary" data-action="reconnect">Try again</button></div>`;
    }
  }
}
const viewNames = {
  library: "All videos",
  channels: "Channels",
  playlists: "Playlists",
  favorites: "Favorites",
  downloads: "Downloads",
  settings: "Settings",
};
function navigate(view, group = null) {
  state.view = viewNames[view] ? view : "library";
  state.group = group;
  state.query = "";
  state.filter = "all";
  if (location.hash !== "#" + state.view)
    history.replaceState(null, "", "#" + state.view);
  $$(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === state.view),
  );
  $("#breadcrumb").textContent = viewNames[state.view];
  const titles = {
    library: [
      "COLLECT. KEEP. COME BACK.",
      "Your internet, kept.",
      "Good videos deserve a permanent home.",
    ],
    channels: [
      "THE PEOPLE BEHIND YOUR COLLECTION",
      "Creators worth keeping.",
      "Every channel in your archive, in one place.",
    ],
    playlists: [
      "A PLACE FOR EVERY INTEREST",
      "Collections with a purpose.",
      "Your YouTube playlists, preserved together.",
    ],
    favorites: [
      "THE ONES YOU COME BACK TO",
      "Keep your favorites close.",
      "A little less searching. A little more watching.",
    ],
    downloads: [
      "WORKING ON YOUR COLLECTION",
      "Making it yours.",
      "Follow your downloads from source to server.",
    ],
    settings: [
      "MAKE YOURSELF AT HOME",
      "Your archive, your way.",
      "Set your defaults. We’ll remember them.",
    ],
  };
  const [eyebrow, title, description] = titles[state.view];
  $("#eyebrow").textContent = eyebrow;
  $("#page-title").textContent = title;
  $("#page-description").textContent = description;
  $("#sidebar").classList.remove("open");
  renderContent();
}
function empty(title, description, button = "Archive your first video") {
  return `<div class="empty-state"><div class="empty-art" aria-hidden="true"><div class="archive-plate">${icon("playlist")}</div><div class="archive-plate front">${icon("play")}</div><span class="art-dot">${icon("plus")}</span></div><h2>${esc(title)}</h2><p>${esc(description)}</p>${button ? `<button class="button primary" data-action="add">${icon("plus")}${esc(button)}</button>` : ""}<div class="empty-support"><span>${icon("video")}Videos & playlists</span><span>${icon("subtitles")}Subtitles included</span><span>${icon("shield")}Always on your server</span></div></div>`;
}
function videoCard(v) {
  const progress = v.duration
    ? Math.min(100, (v.position / v.duration) * 100)
    : 0;
  return `<article class="video-card"><button class="thumbnail" data-play="${esc(v.id)}" aria-label="Play ${esc(v.title)}">${v.thumbnail ? `<img src="${mediaURL(v.thumbnail)}" alt="" loading="lazy">` : `<span class="placeholder-art">${icon("video")}</span>`}<span class="play-overlay">${icon("play")}</span>${v.height ? `<span class="quality-badge">${v.height >= 2160 ? "4K" : v.height + "p"}</span>` : ""}${v.watched ? '<span class="watched-badge">WATCHED</span>' : ""}<span class="duration">${duration(v.duration)}</span>${progress ? `<progress class="watch-progress" max="100" value="${progress}"></progress>` : ""}</button><div class="card-info"><div class="card-title-row"><button class="card-title" data-play="${esc(v.id)}">${esc(v.title)}</button><button class="favorite-button ${v.favorite ? "is-favorite" : ""}" data-favorite="${esc(v.id)}" aria-label="${v.favorite ? "Remove from" : "Add to"} favorites" aria-pressed="${!!v.favorite}">${icon("heart")}</button></div><div class="card-channel"><span class="channel-avatar">${esc(v.channel[0])}</span>${esc(v.channel)}</div><div class="card-meta"><span>${date(v.added_at)}</span><span>·</span><span>${bytes(v.filesize)}</span><span class="saved">${icon("check")}Archived</span></div></div></article>`;
}
function filteredVideos() {
  let videos = state.library.videos.filter(
    (v) => state.view !== "favorites" || v.favorite,
  );
  if (state.group) {
    if (state.view === "channels")
      videos = videos.filter(
        (v) => (v.channel_id || v.channel) === state.group.id,
      );
    else {
      const ids = new Set(
        state.library.memberships
          .filter((m) => m.playlist_id === state.group.id)
          .map((m) => m.video_id),
      );
      videos = videos.filter((v) => ids.has(v.id));
    }
  }
  if (state.query)
    videos = videos.filter((v) =>
      (v.title + " " + v.channel)
        .toLowerCase()
        .includes(state.query.toLowerCase()),
    );
  if (state.filter === "unwatched") videos = videos.filter((v) => !v.watched);
  if (state.filter === "watching")
    videos = videos.filter((v) => v.position > 0 && !v.watched);
  if (state.filter === "watched") videos = videos.filter((v) => v.watched);
  return videos.sort(({ ...a }, { ...b }) =>
    state.sort === "oldest"
      ? a.added_at - b.added_at
      : state.sort === "title"
        ? a.title.localeCompare(b.title)
        : state.sort === "longest"
          ? b.duration - a.duration
          : b.added_at - a.added_at,
  );
}
function renderVideoResults() {
  const videos = filteredVideos();
  $("#result-count").textContent = videos.length;
  const hasFilters = state.query || state.filter !== "all";
  $("#video-results").innerHTML = videos.length
    ? `<div class="video-grid ${state.layout === "list" ? "list-view" : ""}">${videos.map(videoCard).join("")}</div>`
    : hasFilters
      ? empty("No videos found.", "Try a different search or filter.", "")
      : state.view === "favorites"
        ? empty(
            "A home for your favorites.",
            "Tap the heart on any video to keep it close.",
            "",
          )
        : state.group
          ? empty(
              "Nothing in this collection yet.",
              "Videos will appear here once they’re archived.",
            )
          : empty(
              "Start a collection worth keeping.",
              "Save a video you love, a playlist you’re learning from, or a channel you never want to lose.",
            );
}
function renderLibrary() {
  const title =
    state.group?.title ||
    (state.view === "favorites" ? "Your favorites" : "Video library");
  $("#content").innerHTML =
    `${state.group ? '<button class="back-button" data-action="back">Back to all ' + viewNames[state.view].toLowerCase() + "</button>" : ""}<div class="section-head"><div class="section-title">${esc(title)}<span class="count-pill" id="result-count">0</span></div><div class="tools"><div class="search-wrap">${icon("search")}<input id="search" type="search" aria-label="Search videos" placeholder="Search your collection…" value="${esc(state.query)}"></div><select class="sort-select" id="sort" aria-label="Sort videos"><option value="newest">Recently archived</option><option value="oldest">Oldest archived</option><option value="title">Title: A–Z</option><option value="longest">Longest first</option></select><div class="view-switch"><button data-layout="grid" class="${state.layout === "grid" ? "selected" : ""}" aria-label="Grid view" aria-pressed="${state.layout === "grid"}">${icon("grid")}</button><button data-layout="list" class="${state.layout === "list" ? "selected" : ""}" aria-label="List view" aria-pressed="${state.layout === "list"}">${icon("list")}</button></div></div></div><div class="filter-row">${[
      ["all", "All videos"],
      ["unwatched", "Unwatched"],
      ["watching", "Continue watching"],
      ["watched", "Watched"],
    ]
      .map(
        ([id, label]) =>
          `<button class="chip ${state.filter === id ? "active" : ""}" data-filter="${id}" aria-pressed="${state.filter === id}">${label}</button>`,
      )
      .join(
        "",
      )}</div><div id="video-results"></div><div class="bottom-note"><span>${icon("shield")}Safely stored. Ready when you are.</span><span>Videos, subtitles & metadata. All together.</span></div>`;
  $("#sort").value = state.sort;
  renderVideoResults();
  $("#search").addEventListener("input", (e) => {
    state.query = e.target.value;
    renderVideoResults();
  });
  $("#sort").addEventListener("change", (e) => {
    state.sort = e.target.value;
    renderVideoResults();
  });
}
function renderGroups() {
  const groups =
    state.view === "channels" ? channels() : state.library.playlists;
  $("#content").innerHTML =
    `<div class="section-head"><div class="section-title">Your ${viewNames[state.view].toLowerCase()}<span class="count-pill">${groups.length}</span></div></div>${groups.length ? `<div class="group-grid">${groups.map((g) => `<button class="group-card" data-group="${esc(g.id)}"><span class="group-avatar">${state.view === "channels" ? esc(g.title[0]) : icon("playlist")}</span><h3>${esc(g.title)}</h3><p>${g.count} video${g.count !== 1 ? "s" : ""} archived</p></button>`).join("")}</div>` : empty(state.view === "channels" ? "Follow your curiosity." : "Great videos belong together.", state.view === "channels" ? "Archive any video or channel. Its creator will have a place here." : "Add a YouTube playlist to preserve its videos as a collection.", "Add to archive")}`;
}
function renderDownloads() {
  const active = state.jobs.filter((j) =>
    ["running", "queued"].includes(j.status),
  );
  $("#content").innerHTML =
    `<div class="section-head"><div class="section-title">Download queue<span class="count-pill">${active.length} active</span></div><span class="help">One download at a time · updates automatically</span></div>${state.jobs.length ? state.jobs.map((j) => `<article class="job"><div class="job-icon">${icon(j.status === "completed" ? "check" : j.status === "failed" ? "refresh" : "download")}</div><div class="job-body"><div class="job-heading"><span class="job-url">${esc(j.url)}</span><span class="job-status ${j.status}">${esc(j.status)}</span></div><div class="job-detail">${esc(j.detail || "Waiting in queue")} · ${date(j.created_at)}</div>${j.status === "running" ? `<progress value="${j.progress}" max="100" aria-label="Current video download progress"></progress><div class="help">${j.progress.toFixed(0)}% of current video · ${j.completed} archived</div>` : ""}${j.error ? `<details><summary class="help">View download error</summary><pre class="job-error">${esc(j.error)}</pre></details>` : ""}</div><div class="job-actions">${["running", "queued"].includes(j.status) ? `<button class="button secondary" data-job="${j.id}" data-job-action="cancel">Cancel</button>` : ["failed", "cancelled"].includes(j.status) ? `<button class="button secondary" data-job="${j.id}" data-job-action="retry">${icon("refresh")}Retry</button>` : ""}</div></article>`).join("") : empty("Your queue is all clear.", "Add a YouTube link and we’ll preserve the video, subtitles, and metadata.", "Add a download")}`;
}
function optionsFields(s) {
  return `<div class="form-columns"><div><label for="settings-quality">Video quality</label><select id="settings-quality" name="quality">${[
    ["720", "Up to 720p"],
    ["1080", "Up to 1080p"],
    ["1440", "Up to 1440p"],
    ["2160", "Up to 4K"],
    ["best", "Best available"],
  ]
    .map(
      ([v, l]) =>
        `<option value="${v}" ${s.quality === v ? "selected" : ""}>${l}</option>`,
    )
    .join(
      "",
    )}</select></div><div><label for="settings-languages">Subtitle languages</label><input id="settings-languages" name="languages" value="${esc(s.languages)}" required></div></div><p class="help">Use comma-separated codes, such as en.*,en,de. Use all for every available language.</p><label class="check-label"><input name="auto_subtitles" type="checkbox" ${s.auto_subtitles ? "checked" : ""}>Include auto-generated subtitles</label>`;
}
async function renderSettings() {
  const content = $("#content");
  content.innerHTML = '<p class="help">Loading your preferences…</p>';
  try {
    const s = await api("/settings");
    state.settings = s;
    if (state.view !== "settings") return;
    const jellyfin = s.jellyfin || {};
    content.innerHTML = `<form id="settings-form" class="settings-panel"><h3>Download preferences</h3><p>Defaults for new downloads. You can change these for each source you add.</p>${optionsFields(s)}<div class="settings-divider"></div><h3>Jellyfin</h3><p>Share the archive media folder with Jellyfin, then Yourchive can create Jellyfin playlists and sync existing videos.</p><label class="check-label"><input type="checkbox" name="jellyfin_enabled" ${jellyfin.enabled ? "checked" : ""}>Enable Jellyfin sync</label><label for="jellyfin-url">Jellyfin URL</label><input id="jellyfin-url" name="jellyfin_url" type="url" placeholder="http://host.docker.internal:8096" value="${esc(jellyfin.url || "")}"><label for="jellyfin-key">Jellyfin API key</label><input id="jellyfin-key" name="jellyfin_api_key" type="password" autocomplete="new-password" value="${esc(jellyfin.api_key || "")}"><label for="jellyfin-playlist-owner">Jellyfin playlist owner</label><input id="jellyfin-playlist-owner" name="jellyfin_playlist_owner" value="${esc(jellyfin.playlist_owner || "")}"><p class="help">Enter a Jellyfin username if more than one account is enabled.</p><label for="jellyfin-media-path">Media path as seen by Jellyfin</label><input id="jellyfin-media-path" name="jellyfin_media_path" value="${esc(jellyfin.media_path || "/media")}" required><p class="help">Mount the same Docker media volume in Jellyfin at this exact path. Yourchive writes a .nfo metadata file beside each video.</p><div class="settings-actions"><button type="submit" class="button primary">Save preferences</button><button type="button" id="jellyfin-sync" class="button secondary" ${jellyfin.enabled ? "" : "disabled"}>Sync archive to Jellyfin</button></div><p id="jellyfin-status" class="help">${esc(jellyfin.sync_error || jellyfin.sync_state || "Not configured")}${jellyfin.last_sync ? ` · Last sync ${esc(jellyfin.last_sync)}` : ""}</p><div class="settings-divider"></div><h3>Your data stays yours.</h3><p>Videos, subtitles, thumbnails, and metadata are stored on your server. Export your library catalog as JSON, or back up your Docker data and media volumes to keep everything safe.</p><a class="button secondary" href="/api/export" download>${icon("download")}Export library catalog</a><p class="help">The catalog export includes metadata and watch history. Media files are backed up separately.</p></form>`;
    $("#settings-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const button = $("button[type=submit]", e.target);
      button.disabled = true;
      try {
        state.settings = await api("/settings", {
          method: "PUT",
          body: JSON.stringify({...readOptions(e.target), jellyfin: readJellyfin(e.target)}),
        });
        $("#jellyfin-sync").disabled = !state.settings.jellyfin.enabled;
        toast("Preferences saved.");
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
      }
    });
    $("#jellyfin-sync").addEventListener("click", async (e) => {
      e.currentTarget.disabled = true;
      $("#jellyfin-status").textContent = "Syncing archive and playlists…";
      try {
        const result = await api("/jellyfin/sync", {method:"POST"});
        $("#jellyfin-status").textContent = result.message;
        toast(result.message);
      } catch (error) {
        $("#jellyfin-status").textContent = error.message;
      } finally {
        e.currentTarget.disabled = !$("[name=jellyfin_enabled]").checked;
      }
    });
  } catch (e) {
    content.innerHTML = `<div class="offline-banner">${esc(e.message)}</div>`;
  }
}
function renderContent() {
  if (state.view === "settings") return renderSettings();
  if (state.view === "downloads") return renderDownloads();
  if (["channels", "playlists"].includes(state.view) && !state.group)
    return renderGroups();
  renderLibrary();
}
function readOptions(form) {
  return {
    quality: form.elements.quality.value,
    languages: form.elements.languages.value.trim(),
    auto_subtitles: form.elements.auto_subtitles.checked,
  };
}
function readJellyfin(form) {
  return {
    enabled: form.elements.jellyfin_enabled.checked,
    url: form.elements.jellyfin_url.value.trim(),
    api_key: form.elements.jellyfin_api_key.value.trim(),
    media_path: form.elements.jellyfin_media_path.value.trim(),
    playlist_owner: form.elements.jellyfin_playlist_owner.value.trim(),
  };
}
async function openAdd() {
  try {
    state.settings = await api("/settings");
  } catch {}
  const form = $("#add-form");
  if (state.settings) {
    form.elements.quality.value = state.settings.quality;
    form.elements.languages.value = state.settings.languages;
    form.elements.auto_subtitles.checked = state.settings.auto_subtitles;
  }
  $("#add-error").textContent = "";
  $("#add-dialog").showModal();
  $("#source-url").focus();
}
async function saveProgress(force = false) {
  const player = $("#video-player");
  if (!player || !playerVideo || !Number.isFinite(player.currentTime)) return;
  const now = Date.now();
  if (!force && now - lastProgressSave < 10000) return;
  lastProgressSave = now;
  const watched = player.ended || playerVideo.watched;
  try {
    await api("/videos/" + encodeURIComponent(playerVideo.id), {
      method: "PATCH",
      body: JSON.stringify({
        position: player.currentTime,
        watched: !!watched,
      }),
      keepalive: force,
    });
  } catch {
    /* Playback remains available when progress cannot be saved. */
  }
}
async function openPlayer(id) {
  try {
    const v = await api("/videos/" + encodeURIComponent(id));
    playerVideo = v;
    lastProgressSave = 0;
    $("#player-content").innerHTML =
      `<div class="player-top"><span>${esc(v.channel)}</span><button class="icon-button" data-action="close-player" aria-label="Close player">${icon("close")}</button></div><video id="video-player" controls playsinline preload="metadata" ${v.thumbnail ? `poster="${mediaURL(v.thumbnail)}"` : ""} src="${mediaURL(v.filepath)}">${v.subtitles.map((s, i) => `<track kind="subtitles" label="${esc(s.language)}" srclang="${esc(s.language.split(".")[0])}" src="${mediaURL(s.file)}" ${i === 0 ? "default" : ""}>`).join("")}</video><div class="player-details"><h2>${esc(v.title)}</h2><div class="player-meta">${esc(v.channel)} · ${duration(v.duration)} · ${bytes(v.filesize)}${v.upload_date ? " · Published " + esc(v.upload_date.replace(/(\d{4})(\d{2})(\d{2})/, "$1-$2-$3")) : ""}</div><div id="playback-error"></div><div class="player-tools"><a class="button secondary" href="${mediaURL(v.filepath)}?download=true" download>${icon("download")}Video file</a><button class="button secondary" data-action="toggle-watched">${icon("check")}${v.watched ? "Mark unwatched" : "Mark watched"}</button><button class="button danger" data-action="delete-video">${icon("trash")}Delete</button></div><div class="player-description">${esc(v.description || "No description available.")}</div><details><summary>Archive files (${v.files.length})</summary><div class="file-list">${v.files.map((f) => `<a class="file-link" href="${mediaURL(f.file)}?download=true" download>${esc(f.name)} · ${bytes(f.size)}</a>`).join("")}</div></details><details><summary>Original metadata</summary><pre>${esc(JSON.stringify(v.metadata, null, 2))}</pre></details></div>`;
    const player = $("#video-player");
    player.addEventListener("loadedmetadata", () => {
      if (v.position && v.position < player.duration - 3)
        player.currentTime = v.position;
    });
    player.addEventListener("timeupdate", () => saveProgress());
    player.addEventListener("pause", () => saveProgress(true));
    player.addEventListener("ended", () => saveProgress(true));
    player.addEventListener("error", () => {
      $("#playback-error").innerHTML =
        '<p class="player-error">Your browser cannot play this file. Download it to watch in a player such as VLC.</p>';
    });
    $("#player-dialog").showModal();
  } catch (e) {
    toast(e.message);
  }
}
async function closePlayer() {
  await saveProgress(true);
  const player = $("#video-player");
  if (player) player.pause();
  $("#player-dialog").close();
  playerVideo = null;
  $("#player-content").innerHTML = "";
  await sync();
}
async function deleteCurrent() {
  const id = playerVideo.id;
  $("#confirm-dialog").showModal();
  const confirmed = await new Promise((resolve) =>
    $("#confirm-dialog").addEventListener(
      "close",
      () => resolve($("#confirm-dialog").returnValue === "delete"),
      { once: true },
    ),
  );
  if (!confirmed) return;
  try {
    await api("/videos/" + encodeURIComponent(id), { method: "DELETE" });
    playerVideo = null;
    $("#player-dialog").close();
    $("#player-content").innerHTML = "";
    await sync();
    toast("Video and archive files deleted.");
  } catch (e) {
    toast(e.message);
  }
}
document.addEventListener("click", async (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  try {
    if (b.dataset.view) navigate(b.dataset.view);
    if (b.dataset.play) await openPlayer(b.dataset.play);
    if (b.dataset.favorite) {
      const v = state.library.videos.find((v) => v.id === b.dataset.favorite);
      await api("/videos/" + encodeURIComponent(v.id), {
        method: "PATCH",
        body: JSON.stringify({ favorite: !v.favorite }),
      });
      await sync();
    }
    if (b.dataset.group) {
      const groups =
        state.view === "channels" ? channels() : state.library.playlists;
      navigate(
        state.view,
        groups.find((g) => g.id === b.dataset.group),
      );
    }
    if (b.dataset.filter) {
      state.filter = b.dataset.filter;
      $$("[data-filter]").forEach((el) => {
        el.classList.toggle("active", el.dataset.filter === state.filter);
        el.setAttribute("aria-pressed", el.dataset.filter === state.filter);
      });
      renderVideoResults();
    }
    if (b.dataset.layout) {
      state.layout = b.dataset.layout;
      localStorage.setItem("yourchive-layout", state.layout);
      $$("[data-layout]").forEach((el) => {
        el.classList.toggle("selected", el.dataset.layout === state.layout);
        el.setAttribute("aria-pressed", el.dataset.layout === state.layout);
      });
      renderVideoResults();
    }
    if (b.dataset.job) {
      b.disabled = true;
      await api("/jobs/" + b.dataset.job + "/" + b.dataset.jobAction, {
        method: "POST",
      });
      await sync();
    }
    if (b.dataset.action === "add") await openAdd();
    if (b.dataset.action === "back") navigate(state.view);
    if (b.dataset.action === "reconnect") await sync();
    if (b.dataset.action === "close-player") await closePlayer();
    if (b.dataset.action === "delete-video") await deleteCurrent();
    if (b.dataset.action === "toggle-watched") {
      playerVideo.watched = !playerVideo.watched;
      await api("/videos/" + encodeURIComponent(playerVideo.id), {
        method: "PATCH",
        body: JSON.stringify({ watched: !!playerVideo.watched }),
      });
      b.innerHTML =
        icon("check") +
        (playerVideo.watched ? "Mark unwatched" : "Mark watched");
      toast(
        playerVideo.watched ? "Marked as watched." : "Marked as unwatched.",
      );
    }
  } catch (error) {
    toast(error.message);
    b.disabled = false;
  }
});
$("#add-button").addEventListener("click", openAdd);
$("#profile").addEventListener("click", () => navigate("settings"));
$("#menu-toggle").addEventListener("click", () =>
  $("#sidebar").classList.toggle("open"),
);
$$(".close-dialog").forEach((b) =>
  b.addEventListener("click", () => $("#add-dialog").close()),
);
$("#add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const button = $("button[type=submit]", e.target);
  button.disabled = true;
  $("#add-error").textContent = "";
  try {
    await api("/jobs", {
      method: "POST",
      body: JSON.stringify({
        url: e.target.elements.url.value.trim(),
        options: readOptions(e.target),
      }),
    });
    $("#add-dialog").close();
    e.target.elements.url.value = "";
    navigate("downloads");
    await sync();
    toast("Added to your archive queue.");
  } catch (error) {
    $("#add-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("#player-dialog").addEventListener("cancel", (e) => {
  e.preventDefault();
  closePlayer();
});
document.addEventListener("visibilitychange", () => {
  if (document.hidden) saveProgress(true);
  else sync();
});
window.addEventListener("hashchange", () => navigate(location.hash.slice(1)));
decorate();
navigate(location.hash.slice(1) || "library");
sync();
setInterval(() => {
  if (!document.hidden)
    sync({ render: !$("#player-dialog").open && !$("#add-dialog").open });
}, 3000);
