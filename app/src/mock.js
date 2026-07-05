// Activates only outside Tauri for browser design sessions; the real app keeps the native bridge.
(function () {
  if (window.__TAURI__) return;

  const now = () => new Date().toISOString();
  const daysAgo = (n) => new Date(Date.now() - n * 86400 * 1000).toISOString();

  const courseThumb = {
    vb: "0 0 120 80",
    d: "M8 62 C22 48 31 23 48 28 C64 34 65 61 82 55 C95 50 100 24 112 18",
    start: [8, 62],
    end: [112, 18],
    km: [[26, 43, 10], [58, 39, 20], [89, 48, 30]],
    prof: { vb: "0 0 100 24", d: "M1.5 21 L20 16 L40 18 L60 8 L78 12 L98.5 3" },
    dist_m: 42180,
    asc_m: 640,
    desc_m: 635,
    bearing: 52,
    compass: "NE",
    slat: 51.5074,
    slon: -0.1278,
    created: 1782835200,
  };
  const routeTags = {
    "SURREYHI.FIT": { tags: ["gravel", "long"], folder: "Races" },
    "RIVERRECOVERY.fit": { tags: ["recovery"], folder: "Training" },
    "LOCALRT.FIT": { tags: ["library"], folder: "" },
  };

  function actions(row) {
    return {
      can_add_to_watch: !!(row.watch_known && row.in_connect && !row.on_watch),
      can_rm_watch: !!(row.watch_known && row.on_watch && !row.scheduled),
      can_rm_connect: !!(row.in_connect && row.id),
      rm_connect_permanent: row.kind === "course",
    };
  }

  function item(row) {
    const labels = {
      synced: ["Synced", "In Garmin Connect and on your Fenix"],
      "connect-only": ["Connect only", "In Garmin Connect, not on your Fenix"],
      "watch-only": ["On watch", "On your Fenix, not in Garmin Connect"],
      scheduled: ["Scheduled", "Training-plan file that re-syncs to the Fenix daily"],
    };
    const out = {
      scheduled: false,
      manifest_verified: true,
      stale: "",
      imported: false,
      watch_known: true,
      ...row,
    };
    const label = labels[out.state] || labels.synced;
    const base = {
      uid: out.id ? `${out.kind}:${out.id}` : `${out.kind}:w:${out.watch_file}`,
      location_label: label[0],
      location_detail: label[1],
      folder: out.kind === "workout" ? "GARMIN/Workouts" : "GARMIN/Courses",
      actions: actions(out),
      ...out,
    };
    if (base.kind === "course") {
      const tagKey = base.tag_key || "";
      const saved = routeTags[tagKey] || {};
      base.tag_key = tagKey;
      base.tags = saved.tags || base.tags || [];
      base.route_folder = saved.folder || base.route_folder || "";
    } else {
      base.tags = [];
      base.route_folder = "";
      base.tag_key = "";
    }
    return base;
  }

  const snapshot = () => ({
    ok: true,
    generated_at: now(),
    watch: { source: "cache", captured_at: daysAgo(1), connected: false },
    counts: {
      workout: { synced: 1, "connect-only": 1, "watch-only": 0, scheduled: 0 },
      course: { synced: 1, "connect-only": 1, "watch-only": 1, scheduled: 0 },
    },
    sports: ["cycling", "running", "strength_training"],
    tags: ["gravel", "library", "long", "recovery"],
    folders: ["Races", "Training"],
    stale_routes: { orphans: [], orphan_count: 0, needs_manifest_count: 0, last_checked: daysAgo(2), last_orphan_count: 0 },
    locations: {
      source: "mac-backup",
      count: 1,
      watch_connected: false,
      editable: false,
      points: [{ index: 0, name: "Richmond Park gate", lat: 51.4426, lon: -0.2739 }],
    },
    items: [
      item({
        kind: "workout",
        name: "Tempo intervals",
        sport: "running",
        id: 91001,
        in_connect: true,
        on_watch: true,
        watch_file: "TEMPINT.FIT",
        watch_idx: 0,
        state: "synced",
      }),
      item({
        kind: "workout",
        name: "Upper body strength",
        sport: "strength_training",
        id: 91002,
        in_connect: true,
        on_watch: false,
        watch_file: null,
        watch_idx: null,
        state: "connect-only",
      }),
      item({
        kind: "course",
        name: "Surrey Hills loop",
        sport: "cycling",
        id: 73001,
        in_connect: true,
        on_watch: true,
        watch_file: "SURREYHI.FIT",
        watch_idx: 1,
        state: "synced",
        thumb: courseThumb,
        tag_key: "SURREYHI.FIT",
      }),
      item({
        kind: "course",
        name: "River recovery spin",
        sport: "cycling",
        id: 73002,
        in_connect: true,
        on_watch: false,
        watch_file: null,
        watch_idx: null,
        state: "connect-only",
        thumb: { ...courseThumb, dist_m: 18420, asc_m: 110, desc_m: 108, bearing: 84, compass: "E", created: 1782662400 },
        tag_key: "RIVERRECOVERY.fit",
      }),
      item({
        kind: "course",
        name: "Local library route",
        sport: "cycling",
        id: null,
        in_connect: false,
        on_watch: true,
        watch_file: "LOCALRT.FIT",
        watch_idx: 2,
        state: "watch-only",
        imported: true,
        thumb: { ...courseThumb, dist_m: 11600, asc_m: 85, desc_m: 90, bearing: 180, compass: "S", created: 1782576000 },
        tag_key: "LOCALRT.FIT",
      }),
    ],
  });

  const settings = () => ({
    ok: true,
    values: {
      GVE_TRANSCRIBE: "1",
      GVE_TRANSCRIBE_BACKEND: "parakeet",
      GVE_TRANSCRIPT_CLEANUP: "1",
      GVE_CLEANUP_BACKEND: "openai",
      GVE_AUDIO_RETENTION_DAYS: "30",
      GVE_ARCHIVED_VOICE_RETENTION_DAYS: "90",
      GVE_ARCHIVED_VOICE_RETENTION_AUTO: "",
    },
    transcription: {
      enabled: true,
      backend: "parakeet",
      local_installed: { parakeet: true, whisper: false },
      keys: { openai: false, gemini: false, groq: false, deepgram: false },
    },
    cleanup: {
      enabled: true,
      backend: "openai",
      keep_raw: false,
      keys: { openai: false, gemini: false, groq: false, deepgram: false },
    },
    delete_mode: "transcribed",
    audio_retention_days: "30",
    archived_retention: { days: "90", auto: false },
    archived_audio: { count: 1, bytes: 1843200 },
    auto_import_paused: false,
    auto_import_resume_at: "",
  });

  const workoutGet = () => ({
    ok: true,
    name: "Tempo intervals",
    sport: "running",
    spec: {
      name: "Tempo intervals",
      sport: "running",
      steps: [
        { warmup: 600, hr: [120, 140] },
        { repeat: 4, do: [
          { work: 300, power_pct: [95, 105] },
          { recover: 120, hr: [120, 140] },
        ] },
        { cooldown: 300 },
      ],
    },
  });

  const voiceList = () => ({
    ok: true,
    root: "/Users/example/GarminBridge/Voice Memos",
    vault_configured: true,
    vault: "/Users/example/Second Brain",
    items: [
      {
        name: "Hill notes after ride",
        time: daysAgo(1),
        duration: 96,
        audio_path: "/Users/example/GarminBridge/Voice Memos/Hill notes after ride.wav",
        relative_path: "Hill notes after ride.wav",
        transcript_path: "/Users/example/GarminBridge/Voice Memos/Hill notes after ride.txt",
        raw_transcript_path: "",
        transcript: "Reminder to compare the north climb with the alternate return route.",
        has_transcript: true,
        has_raw_transcript: false,
        archived: false,
        note_exists: true,
        note_path: "/Users/example/Second Brain/Voice/Hill notes after ride.md",
      },
      {
        name: "Workout idea",
        time: daysAgo(3),
        duration: 42,
        audio_path: "/Users/example/GarminBridge/Voice Memos/Workout idea.wav",
        relative_path: "Workout idea.wav",
        transcript_path: "",
        raw_transcript_path: "",
        transcript: "",
        has_transcript: false,
        has_raw_transcript: false,
        archived: false,
        note_exists: false,
        note_path: "",
      },
      {
        name: "Old route reminder",
        time: daysAgo(21),
        duration: 64,
        audio_path: "/Users/example/GarminBridge/Voice Memos/Archive/Old route reminder.wav",
        relative_path: "Archive/Old route reminder.wav",
        transcript_path: "/Users/example/GarminBridge/Voice Memos/Archive/Old route reminder.txt",
        raw_transcript_path: "",
        transcript: "Archived note about a route that is no longer on the watch.",
        has_transcript: true,
        has_raw_transcript: false,
        archived: true,
        note_exists: false,
        note_path: "",
      },
    ],
  });

  function api(args) {
    switch ((args && args[0]) || "") {
      case "snapshot":
        return snapshot();
      case "settings-get":
      case "settings-set":
      case "transcription-install":
        return settings();
      case "voice-list":
      case "voice-import":
        return voiceList();
      case "voice-cleanup-archived":
        return { ok: true, count: 0, deleted_count: 0, freed_bytes: 0, message: "No archived memos match the sample cleanup." };
      case "thumbs":
        return { ok: true, thumbs: {} };
      case "wind":
        return { ok: true, wind: {} };
      case "route-wind":
        return { ok: true, wind: null };
      case "route-tags-set":
        return { ok: true, message: "Tags saved." };
      case "geocode":
        return { ok: true, query: args[2] || "", results: [{ label: "Richmond Park, London", lat: 51.4426, lon: -0.2739 }] };
      case "here":
        return { ok: true, label: "Near London", lat: 51.5074, lon: -0.1278 };
      case "preview":
        return { ok: true, count: 0, changes: [], watch_name_max: 32 };
      case "workout-settings-get":
        return { ok: true, provider: "openai", model: "", has_key: false, openai_auth: "key", openai_oauth_connected: false };
      case "workout-get":
        return workoutGet();
      default:
        return { ok: true };
    }
  }

  window.__TAURI__ = {
    core: {
      invoke: async (cmd, payload = {}) => {
        if (cmd === "api") return JSON.stringify(api(payload.args || []));
        if (["play_audio", "open_path", "set_notes_folder", "save_temp_file", "save_temp_image"].includes(cmd)) return "";
        return "";
      },
    },
  };
})();
