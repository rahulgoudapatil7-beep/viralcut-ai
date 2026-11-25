import { useEffect, useState } from "react";
import {
  Scissors,
  Home,
  FolderOpen,
  Settings,
  Plus,
  Sparkles,
  Play,
  Clock,
  Download,
  TrendingUp,
  Zap,
  Link as LinkIcon,
  Film,
  LogOut,
  User,
  Mail,
  Lock,
  CheckCircle2,
  AlertCircle,
  Trash2,
  ExternalLink,
  Subtitles,
  Loader2,
  X,
  ShieldCheck,
  Copy,
  Check,
  Hash,
  Timer,
  Layers,
  Users,
  Search,
  RefreshCw,
  Eye,
  EyeOff,
  KeyRound,
  ArrowLeft,
  Upload,
} from "lucide-react";
import "./App.css";

const API_BASE = "/api";

function formatTime(sec) {
  const s = Math.max(0, Math.floor(Number(sec) || 0));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}:${String(rem).padStart(2, "0")}`;
}

function ScoreRing({ score }) {
  const valid = typeof score === "number" && score > 0;
  const pct = valid ? Math.min(100, Math.max(0, score)) : 0;
  const r = 42;
  const circ = 2 * Math.PI * r;
  const offset = circ - (pct / 100) * circ;

  return (
    <div className="score-ring">
      <svg width="106" height="106" viewBox="0 0 106 106">
        <circle cx="53" cy="53" r={r} className="ring-bg" />
        <circle
          cx="53"
          cy="53"
          r={r}
          className="ring-fg"
          strokeDasharray={circ}
          strokeDashoffset={offset}
        />
      </svg>
      <div className="ring-center">
        <strong>{valid ? score : "--"}</strong>
        <span>VIRAL</span>
      </div>
    </div>
  );
}

function AuthScreen({ onAuthSuccess }) {
  const [mode, setMode] = useState("login"); // "login" | "register" | "forgot"
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);

  // Forgot password state
  const [resetStep, setResetStep] = useState(1); // 1 = request code, 2 = enter code + new password
  const [resetCode, setResetCode] = useState("");
  const [newPassword, setNewPassword] = useState("");

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  function switchMode(next) {
    setMode(next);
    setError("");
    setInfo("");
    setShowPassword(false);
    if (next === "forgot") {
      setResetStep(1);
      setResetCode("");
      setNewPassword("");
    }
  }

  async function parseResponse(res) {
    const text = await res.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = {};
    }
    if (!res.ok) {
      if (data.detail) throw new Error(data.detail);
      if (res.status === 404) {
        throw new Error(
          "Backend route not found (404). Make sure frontend/vite.config.js has the /api proxy and restart npm run dev."
        );
      }
      if (res.status >= 500) {
        throw new Error(
          "Backend server not reachable (500). Make sure uvicorn is running in Backend on port 8000."
        );
      }
      throw new Error(`Request failed (HTTP ${res.status}).`);
    }
    return data;
  }

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setInfo("");
    setLoading(true);
    try {
      if (mode === "forgot") {
        if (resetStep === 1) {
          const res = await fetch(`${API_BASE}/auth/forgot-password`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ email }),
          });
          const data = await parseResponse(res);
          setResetStep(2);
          setInfo(data.message);
        } else {
          const res = await fetch(`${API_BASE}/auth/reset-password`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              email,
              code: resetCode,
              new_password: newPassword,
            }),
          });
          const data = await parseResponse(res);
          setMode("login");
          setPassword("");
          setResetStep(1);
          setInfo(data.message);
        }
        return;
      }

      const endpoint = mode === "register" ? "/auth/register" : "/auth/login";
      const payload =
        mode === "register" ? { name, email, password } : { email, password };

      const res = await fetch(`${API_BASE}${endpoint}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const data = await parseResponse(res);
      localStorage.setItem("viralcut_token", data.token);
      onAuthSuccess(data.user, data.token);
    } catch (err) {
      setError(err.message || "Could not reach backend server.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="auth-shell">
      <div className="auth-glow" />
      <div className="auth-card">
        <div className="auth-brand">
          <div className="brand-badge">
            <Scissors size={20} />
          </div>
          <div>
            <h2>
              ViralCut <span>AI</span>
            </h2>
            <p>AI Video Clipping Workspace</p>
          </div>
        </div>

        {mode !== "forgot" ? (
          <div className="auth-tabs">
            <button
              type="button"
              className={mode === "login" ? "active" : ""}
              onClick={() => switchMode("login")}
            >
              Log In
            </button>
            <button
              type="button"
              className={mode === "register" ? "active" : ""}
              onClick={() => switchMode("register")}
            >
              Create Account
            </button>
          </div>
        ) : (
          <div className="forgot-header">
            <button
              type="button"
              className="link-btn back-link"
              onClick={() => switchMode("login")}
            >
              <ArrowLeft size={14} /> Back to Log In
            </button>
            <h3>Reset Your Password</h3>
            <p>
              {resetStep === 1
                ? "Enter your registered email — we'll send a 6-digit verification code to your Gmail inbox."
                : "Check your Gmail inbox, enter the 6-digit code below, and set your new password."}
            </p>
          </div>
        )}

        <form onSubmit={handleSubmit} className="auth-form">
          {mode === "register" && (
            <label className="field">
              <span>Full Name</span>
              <div className="field-input">
                <User size={16} />
                <input
                  type="text"
                  required
                  placeholder="Rahul Patil"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                />
              </div>
            </label>
          )}

          <label className="field">
            <span>Email Address</span>
            <div className="field-input">
              <Mail size={16} />
              <input
                type="email"
                required
                placeholder="you@example.com"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                disabled={mode === "forgot" && resetStep === 2}
              />
            </div>
          </label>

          {mode !== "forgot" && (
            <label className="field">
              <div className="field-label-row">
                <span>Password</span>
                {mode === "login" && (
                  <button
                    type="button"
                    className="link-btn small-link"
                    onClick={() => switchMode("forgot")}
                  >
                    Forgot password?
                  </button>
                )}
              </div>
              <div className="field-input">
                <Lock size={16} />
                <input
                  type={showPassword ? "text" : "password"}
                  required
                  minLength={6}
                  placeholder="At least 6 characters"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                />
                <button
                  type="button"
                  className="eye-btn"
                  onClick={() => setShowPassword((v) => !v)}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  title={showPassword ? "Hide password" : "Show password"}
                >
                  {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                </button>
              </div>
            </label>
          )}

          {mode === "forgot" && resetStep === 2 && (
            <>
              <label className="field">
                <span>6-Digit Code from Gmail</span>
                <div className="field-input">
                  <KeyRound size={16} />
                  <input
                    type="text"
                    required
                    maxLength={6}
                    placeholder="Enter 6-digit code from your email"
                    value={resetCode}
                    onChange={(e) => setResetCode(e.target.value)}
                  />
                </div>
              </label>

              <label className="field">
                <span>New Password</span>
                <div className="field-input">
                  <Lock size={16} />
                  <input
                    type={showPassword ? "text" : "password"}
                    required
                    minLength={6}
                    placeholder="Enter new password (6+ chars)"
                    value={newPassword}
                    onChange={(e) => setNewPassword(e.target.value)}
                  />
                  <button
                    type="button"
                    className="eye-btn"
                    onClick={() => setShowPassword((v) => !v)}
                    aria-label={showPassword ? "Hide password" : "Show password"}
                    title={showPassword ? "Hide password" : "Show password"}
                  >
                    {showPassword ? <EyeOff size={16} /> : <Eye size={16} />}
                  </button>
                </div>
              </label>
            </>
          )}

          {info && (
            <div className="alert success">
              <CheckCircle2 size={16} />
              <span>{info}</span>
            </div>
          )}

          {error && (
            <div className="alert error">
              <AlertCircle size={16} />
              <span>{error}</span>
            </div>
          )}

          <button type="submit" className="primary-btn full" disabled={loading}>
            {loading ? (
              <>
                <Loader2 size={17} className="spin" />
                {mode === "register"
                  ? "Creating account..."
                  : mode === "forgot"
                  ? resetStep === 1
                    ? "Generating code..."
                    : "Updating password..."
                  : "Signing in..."}
              </>
            ) : (
              <>
                {mode === "forgot" ? <KeyRound size={17} /> : <Sparkles size={17} />}
                {mode === "register"
                  ? "Create Free Account"
                  : mode === "forgot"
                  ? resetStep === 1
                    ? "Send Reset Code"
                    : "Reset Password"
                  : "Sign In to Workspace"}
              </>
            )}
          </button>
        </form>

        {mode !== "forgot" && (
          <div className="auth-footer">
            {mode === "login" ? (
              <p>
                New to ViralCut AI?{" "}
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => switchMode("register")}
                >
                  Register here
                </button>
              </p>
            ) : (
              <p>
                Already have an account?{" "}
                <button
                  type="button"
                  className="link-btn"
                  onClick={() => switchMode("login")}
                >
                  Log in
                </button>
              </p>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export default function App() {
  const [token, setToken] = useState(() => localStorage.getItem("viralcut_token") || "");
  const [user, setUser] = useState(null);
  const [authChecking, setAuthChecking] = useState(Boolean(localStorage.getItem("viralcut_token")));

  const [activeTab, setActiveTab] = useState("dashboard");
  const [url, setUrl] = useState("");
  const [clipLength, setClipLength] = useState(30); // 30 | 45 | 60
  const [maxClips, setMaxClips] = useState(5);      // 5 | 10 | 20
  const [burnCaptions, setBurnCaptions] = useState(true);
  const [captionTheme, setCaptionTheme] = useState("gold");
  const [selectedTemplate, setSelectedTemplate] = useState("Premium Gold CC");
  const [copiedKey, setCopiedKey] = useState("");
  const [subsFile, setSubsFile] = useState(null);   // optional .srt / .vtt for uploaded videos
  const [proxyInput, setProxyInput] = useState("");
  const [proxyResult, setProxyResult] = useState(null);
  const [proxyTesting, setProxyTesting] = useState(false);

  const [stats, setStats] = useState({
    projects_count: 0,
    clips_count: 0,
    top_score: null,
    time_saved_min: 0,
  });
  const [projects, setProjects] = useState([]);
  const [clips, setClips] = useState([]);
  const [selectedProjectId, setSelectedProjectId] = useState(null);

  const [analyzing, setAnalyzing] = useState(false);
  const [analyzeError, setAnalyzeError] = useState("");
  const [analyzeSuccess, setAnalyzeSuccess] = useState("");

  const [cuttingKey, setCuttingKey] = useState(null);
  const [uploadPct, setUploadPct] = useState(0);
  const [uploadNote, setUploadNote] = useState("");
  const [downloadingId, setDownloadingId] = useState(null);
  const [downloadPct, setDownloadPct] = useState(0);
  const [clipStatus, setClipStatus] = useState({ type: "", text: "" });
  const [previewClip, setPreviewClip] = useState(null);
  const [health, setHealth] = useState(null);

  const [adminData, setAdminData] = useState(null);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminSearch, setAdminSearch] = useState("");
  const [adminNotice, setAdminNotice] = useState("");

  async function apiFetch(path, options = {}, overrideToken = null) {
    const activeToken = overrideToken ?? token;
    const headers = {
      ...(options.headers || {}),
      ...(activeToken ? { Authorization: `Bearer ${activeToken}` } : {}),
    };
    const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) {
      handleLogout();
      throw new Error(data.detail || "Session expired. Please log in again.");
    }
    if (!res.ok) {
      throw new Error(data.detail || "Request failed.");
    }
    return data;
  }

  async function loadWorkspace(tok = token) {
    if (!tok) return;
    try {
      const data = await apiFetch("/workspace", {}, tok);
      setStats(data.stats);
      setProjects(data.projects);
      setClips(data.clips);
      if (data.projects.length > 0 && !selectedProjectId) {
        setSelectedProjectId(data.projects[0].id);
      }
    } catch (err) {
      console.error(err);
    }
  }

  async function loadHealth() {
    try {
      const res = await fetch(`${API_BASE}/health`);
      if (res.ok) {
        setHealth(await res.json());
      }
    } catch {
      setHealth({ status: "offline" });
    }
  }

  async function loadAdminUsers() {
    setAdminLoading(true);
    setAdminNotice("");
    try {
      const data = await apiFetch("/admin/users");
      setAdminData(data);
    } catch (err) {
      setAdminNotice(err.message);
    } finally {
      setAdminLoading(false);
    }
  }

  useEffect(() => {
    loadHealth();
    if (!token) {
      setAuthChecking(false);
      return;
    }
    (async () => {
      try {
        const res = await apiFetch("/auth/me", {}, token);
        setUser(res.user);
        await loadWorkspace(token);
      } catch {
        // handled by apiFetch
      } finally {
        setAuthChecking(false);
      }
    })();
  }, []);

  function handleAuthSuccess(loggedUser, newToken) {
    setUser(loggedUser);
    setToken(newToken);
    setSelectedProjectId(null);
    loadWorkspace(newToken);
  }

  function handleLogout() {
    localStorage.removeItem("viralcut_token");
    setToken("");
    setUser(null);
    setProjects([]);
    setClips([]);
    setSelectedProjectId(null);
    setStats({ projects_count: 0, clips_count: 0, top_score: null, time_saved_min: 0 });
  }

  function handleCopyText(text, key) {
    navigator.clipboard?.writeText(text || "");
    setCopiedKey(key);
    setTimeout(() => setCopiedKey((cur) => (cur === key ? "" : cur)), 1800);
  }

  async function handleAnalyze(e) {
    if (e) e.preventDefault();
    if (!url.trim()) {
      setAnalyzeError("Please paste a YouTube video URL first.");
      return;
    }
    setAnalyzing(true);
    setAnalyzeError("");
    setAnalyzeSuccess("");
    setClipStatus({ type: "", text: "" });
    try {
      const data = await apiFetch("/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          url: url.trim(),
          clip_length: clipLength,
          max_clips: maxClips,
        }),
      });
      const proj = data.project;
      setSelectedProjectId(proj.id);
      setAnalyzeSuccess(
        `Generated ${proj.suggestions.length} viral ${proj.clip_length}s clip ideas (with Title, Description & Hashtags) for "${proj.title}".`
      );
      setUrl("");
      await loadWorkspace();
      setActiveTab("dashboard");
    } catch (err) {
      setAnalyzeError(err.message);
    } finally {
      setAnalyzing(false);
    }
  }

  async function handleUploadVideo(e) {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;

    const limitMb = health?.max_upload_mb || 1024;
    const sizeMb = file.size / (1024 * 1024);
    if (sizeMb > limitMb) {
      setAnalyzeError(
        `"${file.name}" is ${sizeMb.toFixed(0)} MB but the limit is ${limitMb} MB. ` +
          `Fix: export it at 720p (much smaller), or trim it to the part you need first.`
      );
      return;
    }

    setAnalyzing(true);
    setAnalyzeError("");
    setAnalyzeSuccess("");
    setClipStatus({ type: "", text: "" });
    setUploadPct(0);
    setUploadNote("");

    try {
      let srtText = null;
      if (subsFile) {
        try {
          srtText = await subsFile.text();
        } catch {
          srtText = null;
        }
      }

      // ALWAYS upload in slices. One long POST can hang forever on free hosting
      // (cold starts, proxy timeouts); short requests always finish or fail fast.
      const useChunked = true;
      let jobId;

      if (useChunked) {
        setUploadNote(`Connecting to the server… (first upload can take ~60s to wake it up)`);
        let init;
        try {
          init = await apiFetch("/upload/init", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              filename: file.name,
              size: file.size,
              clip_length: clipLength,
              max_clips: maxClips,
            }),
          });
        } catch (e) {
          if (/not found|404/i.test(e.message)) {
            throw new Error(
              "Your server is running an OLD build (this page expects the new upload API). " +
                "In Render: Manual Deploy → Clear build cache & deploy, wait for 'Deploy live', then hard-refresh."
            );
          }
          throw e;
        }
        const chunkSize = init.chunk_size || 8 * 1024 * 1024;
        const total = Math.max(1, Math.ceil(file.size / chunkSize));
        for (let i = 0; i < total; i += 1) {
          const slice = file.slice(i * chunkSize, Math.min(file.size, (i + 1) * chunkSize));
          let lastErr = null;
          let done = false;
          for (let attempt = 1; attempt <= 4 && !done; attempt += 1) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), 120000);
            try {
              const res = await fetch(
                `${API_BASE}/upload/chunk/${init.upload_id}?index=${i}`,
                {
                  method: "PUT",
                  headers: {
                    Authorization: `Bearer ${token}`,
                    "Content-Type": "application/octet-stream",
                  },
                  body: slice,
                  signal: controller.signal,
                }
              );
              clearTimeout(timer);
              if (res.ok) {
                done = true;
              } else {
                const err = await res.json().catch(() => ({}));
                lastErr = new Error(err.detail || `Part ${i + 1} of ${total} failed (HTTP ${res.status}).`);
                if (res.status === 404 || res.status === 413 || res.status === 507) {
                  throw lastErr; // no point retrying these
                }
              }
            } catch (err) {
              clearTimeout(timer);
              lastErr = err.name === "AbortError"
                ? new Error(`Part ${i + 1} of ${total} timed out. Check your internet and retry.`)
                : err;
              if (String(lastErr.message).includes("expired")) throw lastErr;
              await new Promise((r) => setTimeout(r, 1500 * attempt));
            }
          }
          if (!done) throw lastErr || new Error(`Upload failed at part ${i + 1} of ${total}.`);
          setUploadPct(Math.round(((i + 1) / total) * 100));
          setUploadNote(
            `Uploading ${sizeMb.toFixed(0)} MB in parts… (${i + 1}/${total})`
          );
        }
        setUploadNote("Upload finished — starting analysis…");
        const fin = await apiFetch(`/upload/finish/${init.upload_id}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ srt_text: srtText }),
        });
        jobId = fin.job_id;
      } else {
        const form = new FormData();
        form.append("file", file);
        if (subsFile) form.append("subs", subsFile);
        form.append("clip_length", String(clipLength));
        form.append("max_clips", String(maxClips));
        const data = await apiFetch("/upload?job=1", { method: "POST", body: form });
        if (data.project) {
          await loadWorkspace();
          setSelectedProjectId(data.project.id);
          setAnalyzeSuccess(`Analysed "${data.project.title}" — ${data.project.suggestions.length} clips ready to cut.`);
          return;
        }
        jobId = data.job_id;
      }

      // Poll the job so slow free-tier CPUs never time out.
      const startedAt = Date.now();
      let job = { status: "queued", message: "Queued..." };
      while (job.status !== "done" && job.status !== "error") {
        if (Date.now() - startedAt > 45 * 60 * 1000) {
          throw new Error("Analysis took over 45 minutes. Try a shorter video.");
        }
        await new Promise((r) => setTimeout(r, 2500));
        job = await apiFetch(`/clips/job/${jobId}`);
        const elapsed = Math.round((Date.now() - startedAt) / 1000);
        setClipStatus({
          type: "info",
          text: `${job.message || "Analysing video..."} (${elapsed}s) — keep this tab open.`,
        });
      }
      if (job.status === "error") throw new Error(job.error || "Analysis failed.");
      if (job.project) {
        setSelectedProjectId(job.project.id);
        setAnalyzeSuccess(
          `Analysed "${job.project.title}" — ${job.project.suggestions.length} clips ready to cut.`
        );
      }
      await loadWorkspace();
    } catch (err) {
      setAnalyzeError(err.message);
    } finally {
      setAnalyzing(false);
      setUploadPct(0);
      setUploadNote("");
    }
  }

  async function handleCreateDemo() {
    setAnalyzing(true);
    setAnalyzeError("");
    setAnalyzeSuccess("");
    setClipStatus({ type: "", text: "" });
    try {
      const data = await apiFetch("/demo", { method: "POST" });
      const proj = data.project;
      setSelectedProjectId(proj.id);
      setAnalyzeSuccess(
        `Demo video ready with ${proj.suggestions.length} AI suggestions! Click "Cut & Download" below to test real clipping + synced captions.`
      );
      await loadWorkspace();
      setActiveTab("dashboard");
    } catch (err) {
      setAnalyzeError(err.message);
    } finally {
      setAnalyzing(false);
    }
  }

  async function handleCutClip(project, suggestion) {
    const key = `${project.id}_${suggestion.start}_${suggestion.end}`;
    const startedAt = Date.now();
    setCuttingKey(key);
    setClipStatus({
      type: "info",
      text: "Starting clip job" + (burnCaptions ? " with synced captions" : "") + "...",
    });

    try {
      // 1) Start the background job (returns instantly)
      const startData = await apiFetch("/clips/cut?job=1", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          project_id: project.id,
          start: suggestion.start,
          end: suggestion.end,
          title: suggestion.title,
          description: suggestion.description || "",
          hashtags: suggestion.hashtags || [],
          score: suggestion.score,
          captions: burnCaptions,
          caption_theme: captionTheme,
        }),
      });

      // 2) Poll the job until it finishes (works even on slow free-tier CPUs)
      const jobId = startData.job_id;
      let job = startData;
      const MAX_WAIT_MS = 15 * 60 * 1000; // 15 minutes hard ceiling

      while (job.status !== "done" && job.status !== "error") {
        if (Date.now() - startedAt > MAX_WAIT_MS) {
          throw new Error(
            "This clip took over 15 minutes. Try a shorter clip or switch Captions OFF for a faster export."
          );
        }
        await new Promise((r) => setTimeout(r, 2500));
        job = await apiFetch(`/clips/job/${jobId}`);
        const elapsed = Math.round((Date.now() - startedAt) / 1000);
        setClipStatus({
          type: "info",
          text: `${job.message || "Cutting clip..."} (${elapsed}s) — keep this tab open.`,
        });
      }

      if (job.status === "error") {
        throw new Error(job.error || "Clip cutting failed.");
      }

      const clip = job.clip;
      await loadWorkspace();

      setClipStatus({
        type: job.note ? "warn" : "success",
        text: job.note
          ? `${job.note} Saved to My Clips.`
          : `Clip saved to My Clips (${clip.quality}${clip.has_captions ? " + CC" : ""})! Downloading now...`,
      });

      // 3) Trigger browser file download (blob download = works even on slow servers)
      await downloadClip(clip);
    } catch (err) {
      setClipStatus({ type: "error", text: err.message });
    } finally {
      setCuttingKey(null);
    }
  }

  async function downloadClip(clip) {
    if (!clip?.download_url) return;
    if (downloadingId) return;
    setDownloadingId(clip.id ?? clip.filename);
    setDownloadPct(0);
    try {
      const res = await fetch(`${API_BASE}/clips/file/${encodeURIComponent(clip.filename)}?download=1`);
      if (!res.ok) throw new Error(`Download failed (HTTP ${res.status}). Try again in a moment.`);
      const total = Number(res.headers.get("content-length") || 0);
      let blob;
      if (res.body && res.body.getReader) {
        const reader = res.body.getReader();
        const chunks = [];
        let received = 0;
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          chunks.push(value);
          received += value.length;
          if (total) setDownloadPct(Math.min(99, Math.round((received / total) * 100)));
        }
        blob = new Blob(chunks, { type: "video/mp4" });
      } else {
        blob = await res.blob();
      }
      setDownloadPct(100);
      const objectUrl = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = objectUrl;
      a.download = clip.filename || "clip.mp4";
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 120000);
      setClipStatus({ type: "success", text: `Downloaded ${clip.filename} (${(blob.size / 1048576).toFixed(1)} MB)` });
    } catch (err) {
      setClipStatus({ type: "error", text: err.message });
    } finally {
      setDownloadingId(null);
      setDownloadPct(0);
    }
  }

  async function handleDeleteClip(clipId) {
    try {
      await apiFetch(`/clips/${clipId}`, { method: "DELETE" });
      if (previewClip?.id === clipId) setPreviewClip(null);
      await loadWorkspace();
    } catch (err) {
      setClipStatus({ type: "error", text: err.message });
    }
  }

  async function handleDeleteProject(projectId) {
    try {
      await apiFetch(`/projects/${projectId}`, { method: "DELETE" });
      if (selectedProjectId === projectId) {
        const remaining = projects.filter((p) => p.id !== projectId);
        setSelectedProjectId(remaining[0]?.id ?? null);
      }
      await loadWorkspace();
    } catch (err) {
      setAnalyzeError(err.message);
    }
  }

  if (authChecking) {
    return (
      <div className="auth-shell">
        <div className="loading-pill">
          <Loader2 size={20} className="spin" /> Loading ViralCut AI...
        </div>
      </div>
    );
  }

  if (!user) {
    return <AuthScreen onAuthSuccess={handleAuthSuccess} />;
  }

  const activeProject =
    projects.find((p) => p.id === selectedProjectId) || projects[0] || null;

  const isClipped = (projectId, start, end) =>
    clips.some(
      (c) =>
        c.project_id === projectId &&
        Math.abs(c.start - start) < 1 &&
        Math.abs(c.end - end) < 1
    );

  const initials = (user.name || "VC")
    .split(" ")
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="logo-row">
          <Scissors size={22} className="accent-icon" />
          <h2>
            ViralCut <span>AI</span>
          </h2>
        </div>

        <nav>
          <button
            className={activeTab === "dashboard" ? "active" : ""}
            onClick={() => setActiveTab("dashboard")}
          >
            <Home size={18} /> Dashboard
          </button>
          <button
            className={activeTab === "new" ? "active" : ""}
            onClick={() => setActiveTab("new")}
          >
            <Plus size={18} /> New Project
          </button>
          <button
            className={activeTab === "clips" ? "active" : ""}
            onClick={() => setActiveTab("clips")}
          >
            <FolderOpen size={18} /> My Clips
            <span className="nav-badge">{clips.length}</span>
          </button>
          <button
            className={activeTab === "templates" ? "active" : ""}
            onClick={() => setActiveTab("templates")}
          >
            <Film size={18} /> Templates
          </button>
          {user.is_admin && (
            <button
              className={`admin-nav-btn ${activeTab === "admin" ? "active" : ""}`}
              onClick={() => {
                setActiveTab("admin");
                loadAdminUsers();
              }}
            >
              <Users size={18} /> Registered Users
              <span className="nav-badge">Admin</span>
            </button>
          )}
          <button
            className={activeTab === "settings" ? "active" : ""}
            onClick={() => {
              setActiveTab("settings");
              loadHealth();
            }}
          >
            <Settings size={18} /> Settings
          </button>
        </nav>

        <div className="side-bottom">
          <div className="user-mini">
            <div className="avatar">{initials}</div>
            <div className="user-meta">
              <strong>{user.name}</strong>
              <span>{user.email}</span>
            </div>
          </div>
          <button className="logout-btn" onClick={handleLogout}>
            <LogOut size={15} /> Log Out
          </button>
        </div>
      </aside>

      <main>
        <header>
          <div className="header-left">
            <span className="workspace-label">AI VIDEO WORKSPACE</span>
            {health?.ffmpeg_ready && (
              <span className="status-pill ok">
                <CheckCircle2 size={13} /> {health.max_height}p · {health.font}
              </span>
            )}
          </div>
          <div className="header-right">
            {burnCaptions && (
              <div className="theme-picker" title="Premium caption highlight color">
                {[
                  { id: "gold", label: "Gold", dot: "#FFD726" },
                  { id: "violet", label: "Violet", dot: "#B885FF" },
                  { id: "mint", label: "Mint", dot: "#4AEF86" },
                ].map((t) => (
                  <button
                    key={t.id}
                    type="button"
                    className={captionTheme === t.id ? "active" : ""}
                    onClick={() => {
                      setCaptionTheme(t.id);
                      setSelectedTemplate(`Premium ${t.label} CC`);
                    }}
                  >
                    <span className="color-dot" style={{ background: t.dot }} />
                    {t.label}
                  </button>
                ))}
              </div>
            )}
            <button
              className={`caption-toggle ${burnCaptions ? "on" : ""}`}
              onClick={() => setBurnCaptions((v) => !v)}
              title="Toggle burned-in captions on exported clips"
            >
              <Subtitles size={15} />
              Captions: {burnCaptions ? "ON" : "OFF"}
            </button>
            <button className="profile-chip" onClick={() => setActiveTab("settings")}>
              <span className="mini-dot">{initials}</span>
              {user.name}
            </button>
          </div>
        </header>

        {/* ===================== DASHBOARD & NEW PROJECT ===================== */}
        {(activeTab === "dashboard" || activeTab === "new") && (
          <>
            <section className="hero">
              <div className="tag">
                <Sparkles size={15} /> AI POWERED VIDEO CLIPPING · {selectedTemplate.toUpperCase()}
              </div>
              <h1>
                Turn long videos into
                <br />
                <span>viral moments.</span>
              </h1>
              <p>
                Upload your video — we find the highest-scoring moments, burn styled captions, and
                write a viral title, description &amp; hashtags for every clip.
              </p>

              {/* Upload-only flow: no YouTube link required. */}
              <label
                style={{
                  marginTop: 22,
                  display: "flex",
                  flexDirection: "column",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 6,
                  width: "100%",
                  padding: "28px 22px",
                  borderRadius: 18,
                  border: "1.5px dashed rgba(139, 92, 246, 0.55)",
                  background:
                    "linear-gradient(180deg, rgba(139, 92, 246, 0.16), rgba(139, 92, 246, 0.04))",
                  cursor: analyzing ? "not-allowed" : "pointer",
                  opacity: analyzing ? 0.7 : 1,
                  textAlign: "center",
                  boxSizing: "border-box",
                }}
              >
                {analyzing ? (
                  <Loader2 size={26} className="spin" style={{ color: "#c4b5fd" }} />
                ) : (
                  <Upload size={26} style={{ color: "#c4b5fd" }} />
                )}
                <strong style={{ fontSize: 17 }}>
                  {analyzing ? "Processing your video…" : "Upload Video (MP4)"}
                </strong>
                <span style={{ fontSize: 12.5, color: "#9b9bb4" }}>
                  MP4 · MOV · MKV · WEBM — up to {health?.max_upload_mb || 1024} MB · big files
                  upload in 8 MB parts
                </span>
                {analyzing && (uploadPct > 0 || uploadNote) && (
                  <div style={{ width: "100%", maxWidth: 460, marginTop: 10 }}>
                    <div
                      style={{
                        height: 8,
                        borderRadius: 999,
                        background: "rgba(255,255,255,0.09)",
                        overflow: "hidden",
                      }}
                    >
                      <div
                        style={{
                          width: `${uploadPct}%`,
                          height: "100%",
                          background: "linear-gradient(90deg,#8b5cf6,#c4b5fd)",
                          transition: "width 0.25s ease",
                        }}
                      />
                    </div>
                    <span style={{ fontSize: 12, color: "#c4b5fd", display: "block", marginTop: 6 }}>
                      {uploadNote || "Uploading…"} {uploadPct > 0 ? `${uploadPct}%` : ""}
                    </span>
                  </div>
                )}
                <input
                  type="file"
                  accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv"
                  onChange={handleUploadVideo}
                  disabled={analyzing}
                  hidden
                />
              </label>

              <div className="clip-options-bar">
                <div className="option-group">
                  <span className="opt-label">
                    <Timer size={14} /> Clip Duration:
                  </span>
                  <div className="pill-group">
                    {[30, 45, 60].map((sec) => (
                      <button
                        key={sec}
                        type="button"
                        className={clipLength === sec ? "active" : ""}
                        onClick={() => setClipLength(sec)}
                        disabled={analyzing}
                      >
                        {sec}s
                      </button>
                    ))}
                  </div>
                </div>

                <div className="option-group">
                  <span className="opt-label">
                    <Layers size={14} /> Number of Clips:
                  </span>
                  <div className="pill-group">
                    {[5, 10, 20].map((cnt) => (
                      <button
                        key={cnt}
                        type="button"
                        className={maxClips === cnt ? "active" : ""}
                        onClick={() => setMaxClips(cnt)}
                        disabled={analyzing}
                      >
                        {cnt} Clips
                      </button>
                    ))}
                  </div>
                </div>
              </div>

              <div className="hero-subrow">
                <small>
                  Auto-generates Viral Title, Description &amp; Hashtags · 1080p smooth export · Max upload{" "}
                  {health?.max_upload_mb || 1024} MB · Want burned captions? Choose the{" "}
                  <strong>.srt</strong> file first, then upload your video
                </small>
                <div className="hero-quick-btns">
                  <label
                    className={`ghost-btn upload-label ${analyzing ? "disabled" : ""}`}
                    style={
                      subsFile
                        ? {
                            borderColor: "rgba(74, 239, 134, 0.55)",
                            color: "#9df7bd",
                            background: "rgba(74, 239, 134, 0.08)",
                          }
                        : undefined
                    }
                    title="Optional: add the video's .srt / .vtt subtitle file to burn Hindi/English captions"
                  >
                    <Subtitles size={14} />
                    {subsFile ? `CC ready: ${subsFile.name.slice(0, 18)}` : "Add Subtitles (.srt) — optional"}
                    <input
                      type="file"
                      accept=".srt,.vtt,text/plain"
                      onChange={(e) => {
                        setSubsFile(e.target.files?.[0] || null);
                        setAnalyzeError("");
                      }}
                      disabled={analyzing}
                      hidden
                    />
                  </label>

                  <button
                    type="button"
                    className="ghost-btn"
                    onClick={handleCreateDemo}
                    disabled={analyzing}
                  >
                    <Play size={14} /> Try Instant Demo Video
                  </button>
                </div>
              </div>

              {analyzeError && (
                <div className="alert error mt-12">
                  <AlertCircle size={16} />
                  <span>{analyzeError}</span>
                </div>
              )}
              {analyzeSuccess && (
                <div className="alert success mt-12">
                  <CheckCircle2 size={16} />
                  <span>{analyzeSuccess}</span>
                </div>
              )}
            </section>

            {activeTab === "dashboard" && (
              <>
                <section className="stats">
                  <div className="stat-card">
                    <Film size={20} />
                    <span>Analyzed Projects</span>
                    <h2>{stats.projects_count}</h2>
                  </div>
                  <div className="stat-card" onClick={() => setActiveTab("clips")} style={{ cursor: "pointer" }}>
                    <Scissors size={20} />
                    <span>My Clipped Videos</span>
                    <h2>{stats.clips_count}</h2>
                  </div>
                  <div className="stat-card">
                    <TrendingUp size={20} />
                    <span>Top Clip Score</span>
                    <h2>{stats.top_score ?? "--"}</h2>
                  </div>
                  <div className="stat-card">
                    <Clock size={20} />
                    <span>Time Saved</span>
                    <h2>{stats.time_saved_min} min</h2>
                  </div>
                </section>

                <section className="content">
                  {/* LEFT PANEL: RECENT PROJECTS */}
                  <div className="panel">
                    <div className="panel-head">
                      <h3>
                        <Film size={18} /> Recent Projects
                      </h3>
                      <span className="muted-pill">{projects.length} total</span>
                    </div>

                    {projects.length === 0 ? (
                      <div className="empty">
                        <Film size={40} />
                        <h3>No projects yet</h3>
                        <p>
                          Click <strong>Upload Video (MP4)</strong> to analyze your first video, or
                          try the <strong>Instant Demo</strong>.
                        </p>
                      </div>
                    ) : (
                      <div className="project-list">
                        {projects.map((proj) => {
                          const isSelected = activeProject?.id === proj.id;
                          const clippedCount = clips.filter(
                            (c) => c.project_id === proj.id
                          ).length;
                          return (
                            <div
                              key={proj.id}
                              className={`project-card ${isSelected ? "selected" : ""}`}
                              onClick={() => setSelectedProjectId(proj.id)}
                            >
                              <div className="project-thumb">
                                {proj.thumbnail ? (
                                  <img src={proj.thumbnail} alt={proj.title} />
                                ) : (
                                  <div className="thumb-fallback">
                                    <Film size={22} />
                                  </div>
                                )}
                                <span className="dur-badge">{formatTime(proj.duration)}</span>
                              </div>
                              <div className="project-info">
                                <h4>{proj.title}</h4>
                                <div className="project-meta">
                                  <span>{proj.suggestions.length} AI suggestions</span>
                                  <span>·</span>
                                  <span className="accent-text">
                                    {clippedCount} clipped by you
                                  </span>
                                  <span>·</span>
                                  <span>Lang: {proj.language}</span>
                                </div>
                              </div>
                              <button
                                className="icon-btn danger"
                                title="Remove project"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleDeleteProject(proj.id);
                                }}
                              >
                                <Trash2 size={15} />
                              </button>
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>

                  {/* RIGHT PANEL: AI CLIP INTELLIGENCE (SUGGESTIONS ONLY UNTIL YOU CLICK CUT) */}
                  <div className="panel">
                    <div className="panel-head">
                      <h3>
                        <Zap size={18} /> AI Clip Intelligence
                      </h3>
                      {activeProject && (
                        <span className="muted-pill">AI Suggestions</span>
                      )}
                    </div>

                    {!activeProject ? (
                      <div className="empty">
                        <Sparkles size={38} />
                        <h3>Ready to analyze</h3>
                        <p>
                          AI moment suggestions appear here after you analyze a video. Nothing is
                          clipped until you click Cut &amp; Download.
                        </p>
                      </div>
                    ) : (
                      <div className="intel-wrap">
                        <div className="intel-summary">
                          <ScoreRing score={activeProject.top_score} />
                          <div>
                            <span className="mini-tag">SELECTED PROJECT</span>
                            <h4>{activeProject.title}</h4>
                            <p>
                              Showing <strong>AI-suggested moments</strong> below. Only moments
                              you click <strong>Cut &amp; Download</strong> on are saved to{" "}
                              <strong>My Clips</strong>.
                            </p>
                          </div>
                        </div>

                        <div className="suggestions-list">
                          {activeProject.suggestions.map((sug, idx) => {
                            const key = `${activeProject.id}_${sug.start}_${sug.end}`;
                            const isCutting = cuttingKey === key;
                            const alreadyClipped = isClipped(
                              activeProject.id,
                              sug.start,
                              sug.end
                            );
                            const ytLink =
                              activeProject.video_id !== "demo_viralcut" &&
                              !String(activeProject.url || "").startsWith("upload://")
                                ? `https://www.youtube.com/watch?v=${activeProject.video_id}&t=${Math.floor(
                                    sug.start
                                  )}s`
                                : null;

                            const tagsText = (sug.hashtags || []).join(" ");
                            const fullCopy = `${sug.title}\n\n${sug.description || ""}\n\n${tagsText}`.trim();

                            return (
                              <div key={key} className="suggestion-card">
                                <div className="sug-top">
                                  <span className="rank-badge">#{idx + 1}</span>
                                  <span className="time-pill">
                                    {formatTime(sug.start)} – {formatTime(sug.end)} ({sug.duration}s)
                                  </span>
                                  <span className="score-pill">Score {sug.score}</span>
                                  {alreadyClipped && (
                                    <span className="clipped-pill">
                                      <CheckCircle2 size={12} /> Clipped
                                    </span>
                                  )}
                                </div>

                                <div className="meta-block">
                                  <div className="meta-row">
                                    <span className="meta-label">TITLE</span>
                                    <button
                                      type="button"
                                      className="copy-mini"
                                      onClick={() => handleCopyText(sug.title, `${key}_t`)}
                                    >
                                      {copiedKey === `${key}_t` ? <Check size={12} /> : <Copy size={12} />}
                                      {copiedKey === `${key}_t` ? "Copied" : "Copy"}
                                    </button>
                                  </div>
                                  <h5>{sug.title}</h5>

                                  {sug.description && (
                                    <>
                                      <div className="meta-row mt-8">
                                        <span className="meta-label">DESCRIPTION</span>
                                        <button
                                          type="button"
                                          className="copy-mini"
                                          onClick={() => handleCopyText(sug.description, `${key}_d`)}
                                        >
                                          {copiedKey === `${key}_d` ? <Check size={12} /> : <Copy size={12} />}
                                          {copiedKey === `${key}_d` ? "Copied" : "Copy"}
                                        </button>
                                      </div>
                                      <p className="clip-desc">{sug.description}</p>
                                    </>
                                  )}

                                  {sug.hashtags && sug.hashtags.length > 0 && (
                                    <>
                                      <div className="meta-row mt-8">
                                        <span className="meta-label">
                                          <Hash size={11} /> HASHTAGS
                                        </span>
                                        <button
                                          type="button"
                                          className="copy-mini"
                                          onClick={() => handleCopyText(tagsText, `${key}_h`)}
                                        >
                                          {copiedKey === `${key}_h` ? <Check size={12} /> : <Copy size={12} />}
                                          {copiedKey === `${key}_h` ? "Copied" : "Copy Tags"}
                                        </button>
                                      </div>
                                      <div className="hashtag-list">
                                        {sug.hashtags.map((tag) => (
                                          <span key={tag} className="hashtag-chip">
                                            {tag}
                                          </span>
                                        ))}
                                      </div>
                                    </>
                                  )}
                                </div>

                                <p className="sug-reason">{sug.reason}</p>
                                <div className="sug-actions">
                                  <button
                                    className="cut-btn"
                                    disabled={Boolean(cuttingKey)}
                                    onClick={() => handleCutClip(activeProject, sug)}
                                  >
                                    {isCutting ? (
                                      <>
                                        <Loader2 size={14} className="spin" /> Cutting...
                                      </>
                                    ) : (
                                      <>
                                        <Download size={14} /> Cut &amp; Download{" "}
                                        {burnCaptions ? "(CC)" : ""}
                                      </>
                                    )}
                                  </button>
                                  <button
                                    type="button"
                                    className="ghost-btn small"
                                    onClick={() => handleCopyText(fullCopy, `${key}_all`)}
                                  >
                                    {copiedKey === `${key}_all` ? <Check size={13} /> : <Copy size={13} />}
                                    {copiedKey === `${key}_all` ? "Copied All!" : "Copy All Meta"}
                                  </button>
                                  {ytLink && (
                                    <a
                                      href={ytLink}
                                      target="_blank"
                                      rel="noreferrer"
                                      className="ghost-btn small"
                                    >
                                      <ExternalLink size={13} /> Preview on YT
                                    </a>
                                  )}
                                </div>
                              </div>
                            );
                          })}
                        </div>

                        {clipStatus.text && (
                          <div className={`alert ${clipStatus.type} mt-12`}>
                            {clipStatus.type === "info" ? (
                              <Loader2 size={16} className="spin" />
                            ) : clipStatus.type === "success" ? (
                              <CheckCircle2 size={16} />
                            ) : (
                              <AlertCircle size={16} />
                            )}
                            <span>{clipStatus.text}</span>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                </section>
              </>
            )}
          </>
        )}

        {/* ===================== MY CLIPS (ONLY CLIPS YOU ACTUALLY CUT) ===================== */}
        {activeTab === "clips" && (
          <section className="page-section">
            <div className="section-header">
              <div>
                <h1>My Clipped Videos</h1>
                <p>
                  Only real clips that you actually cut and saved appear here — never unclipped
                  suggestions.
                </p>
              </div>
              <button className="primary-btn" onClick={() => setActiveTab("dashboard")}>
                <Plus size={16} /> Cut More Clips
              </button>
            </div>

            {clips.length === 0 ? (
              <div className="panel empty large">
                <Scissors size={44} />
                <h3>You haven&apos;t clipped any videos yet</h3>
                <p>
                  Analyze a video on the Dashboard, then click <strong>Cut &amp; Download</strong>{" "}
                  on any moment you want to save here.
                </p>
                <button
                  className="primary-btn mt-12"
                  onClick={() => setActiveTab("dashboard")}
                >
                  Go to Dashboard
                </button>
              </div>
            ) : (
              <div className="clips-grid">
                {clips.map((clip) => (
                  <div key={clip.id} className="clip-card">
                    <div className="clip-video-box" onClick={() => setPreviewClip(clip)}>
                      <video src={clip.video_url} muted preload="metadata" />
                      <div className="play-overlay">
                        <Play size={22} />
                      </div>
                      <div className="clip-badges">
                        <span>{clip.quality}</span>
                        {clip.has_captions && <span className="cc">CC</span>}
                        <span>{clip.duration}s</span>
                      </div>
                    </div>
                    <div className="clip-body">
                      <div className="sug-top">
                        <span className="time-pill">
                          {formatTime(clip.start)} – {formatTime(clip.end)}
                        </span>
                        <span className="score-pill">Score {clip.score}</span>
                      </div>
                      <h4>{clip.title}</h4>
                      {clip.description && (
                        <p className="clip-desc clamp">{clip.description}</p>
                      )}
                      {clip.hashtags && clip.hashtags.length > 0 && (
                        <div className="hashtag-list">
                          {clip.hashtags.slice(0, 5).map((tag) => (
                            <span key={tag} className="hashtag-chip">
                              {tag}
                            </span>
                          ))}
                        </div>
                      )}
                      <div className="clip-actions">
                        <button
                          className="ghost-btn small"
                          onClick={() => setPreviewClip(clip)}
                        >
                          <Play size={14} /> Watch
                        </button>
                        <button
                          type="button"
                          className="ghost-btn small"
                          onClick={() =>
                            handleCopyText(
                              `${clip.title}\n\n${clip.description || ""}\n\n${(clip.hashtags || []).join(" ")}`.trim(),
                              `clip_${clip.id}`
                            )
                          }
                        >
                          {copiedKey === `clip_${clip.id}` ? <Check size={13} /> : <Copy size={13} />}
                          {copiedKey === `clip_${clip.id}` ? "Copied" : "Copy Info"}
                        </button>
                        <button
                          type="button"
                          className="cut-btn small"
                          onClick={() => downloadClip(clip)}
                          disabled={downloadingId !== null}
                        >
                          {downloadingId === clip.id ? (
                            <>
                              <Loader2 size={14} className="spin" /> {downloadPct}%
                            </>
                          ) : (
                            <>
                              <Download size={14} /> Download
                            </>
                          )}
                        </button>
                        <button
                          className="icon-btn danger"
                          title="Delete clip"
                          onClick={() => handleDeleteClip(clip.id)}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        )}

        {/* ===================== TEMPLATES ===================== */}
        {activeTab === "templates" && (
          <section className="page-section">
            <div className="section-header">
              <div>
                <h1>Clip Templates</h1>
                <p>Choose your default caption and export style for newly cut clips.</p>
              </div>
            </div>
            <div className="templates-grid">
              {[
                {
                  name: "Premium Gold CC",
                  desc: "Compact sleek captions in pearl white + Luxe Gold keyword highlights, smooth 30fps 1080p.",
                  captions: true,
                  theme: "gold",
                },
                {
                  name: "Premium Violet CC",
                  desc: "Compact sleek captions with Electric Violet viral keyword accents & crisp dark outline.",
                  captions: true,
                  theme: "violet",
                },
                {
                  name: "Premium Mint CC",
                  desc: "Fresh Emerald Mint keyword pop with clean modern typography (English & Hindi).",
                  captions: true,
                  theme: "mint",
                },
                {
                  name: "Raw Clean 1080p (No CC)",
                  desc: "Fastest export — smooth 30fps 1080p clip without burned-in captions.",
                  captions: false,
                  theme: "gold",
                },
              ].map((tpl) => {
                const active = selectedTemplate === tpl.name;
                return (
                  <div
                    key={tpl.name}
                    className={`panel template-card ${active ? "selected" : ""}`}
                    onClick={() => {
                      setSelectedTemplate(tpl.name);
                      setBurnCaptions(tpl.captions);
                      setCaptionTheme(tpl.theme);
                    }}
                  >
                    <div className="panel-head">
                      <h3>{tpl.name}</h3>
                      {active && <span className="clipped-pill">Active</span>}
                    </div>
                    <p>{tpl.desc}</p>
                    <span className="muted-pill">
                      Captions: {tpl.captions ? "Enabled" : "Disabled"}
                    </span>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* ===================== REGISTERED USERS (ADMIN PANEL) ===================== */}
        {activeTab === "admin" && user.is_admin && (
          <section className="page-section">
            <div className="section-header">
              <div>
                <h1>Registered Users (Admin)</h1>
                <p>
                  Every account created on ViralCut AI along with their join date and activity.
                </p>
              </div>
              <button
                className="ghost-btn"
                onClick={loadAdminUsers}
                disabled={adminLoading}
              >
                <RefreshCw size={15} className={adminLoading ? "spin" : ""} /> Refresh Users
              </button>
            </div>

            {adminNotice && (
              <div className="alert error mb-16">
                <AlertCircle size={16} />
                <span>{adminNotice}</span>
              </div>
            )}

            {adminData && (
              <>
                <section className="stats admin-stats-3 mb-20">
                  <div className="stat-card">
                    <Users size={20} />
                    <span>Total Registered Users</span>
                    <h2>{adminData.summary.total_users}</h2>
                  </div>
                  <div className="stat-card">
                    <Film size={20} />
                    <span>All Projects Analyzed</span>
                    <h2>{adminData.summary.total_projects}</h2>
                  </div>
                  <div className="stat-card">
                    <Scissors size={20} />
                    <span>All Clips Created</span>
                    <h2>{adminData.summary.total_clips}</h2>
                  </div>
                </section>

                <div className="panel">
                  <div className="panel-head">
                    <h3>
                      <Users size={18} /> All Registered Accounts ({adminData.users.length})
                    </h3>
                    <div className="admin-search">
                      <Search size={14} />
                      <input
                        placeholder="Search by name or email..."
                        value={adminSearch}
                        onChange={(e) => setAdminSearch(e.target.value)}
                      />
                    </div>
                  </div>

                  <div className="table-wrap">
                    <table className="users-table">
                      <thead>
                        <tr>
                          <th>ID</th>
                          <th>Name</th>
                          <th>Email</th>
                          <th>Joined</th>
                          <th>Projects</th>
                          <th>Clips Created</th>
                        </tr>
                      </thead>
                      <tbody>
                        {adminData.users
                          .filter((u) => {
                            const q = adminSearch.trim().toLowerCase();
                            if (!q) return true;
                            return (
                              u.name.toLowerCase().includes(q) ||
                              u.email.toLowerCase().includes(q)
                            );
                          })
                          .map((u) => (
                            <tr key={u.id}>
                              <td>#{u.id}</td>
                              <td>
                                <strong>
                                  {u.name}
                                  {u.is_admin && <span className="admin-pill">ADMIN</span>}
                                </strong>
                              </td>
                              <td>{u.email}</td>
                              <td>
                                {new Date(u.created_at).toLocaleString(undefined, {
                                  year: "numeric",
                                  month: "short",
                                  day: "numeric",
                                  hour: "2-digit",
                                  minute: "2-digit",
                                })}
                              </td>
                              <td>{u.projects_count}</td>
                              <td>{u.clips_count}</td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              </>
            )}
          </section>
        )}

        {/* ===================== SETTINGS ===================== */}
        {activeTab === "settings" && (
          <section className="page-section">
            <div className="section-header">
              <div>
                <h1>Account &amp; Workspace Settings</h1>
                <p>Manage your ViralCut AI profile and inspect backend engine health.</p>
              </div>
            </div>
            <div className="settings-grid">
              <div className="panel">
                <h3>
                  <User size={18} /> Account Details
                </h3>
                <div className="kv-list">
                  <div>
                    <span>Name</span>
                    <strong>{user.name}</strong>
                  </div>
                  <div>
                    <span>Email</span>
                    <strong>{user.email}</strong>
                  </div>
                  <div>
                    <span>Saved Projects</span>
                    <strong>{stats.projects_count}</strong>
                  </div>
                  <div>
                    <span>Clipped Videos</span>
                    <strong>{stats.clips_count}</strong>
                  </div>
                </div>
                <button className="logout-btn mt-16" onClick={handleLogout}>
                  <LogOut size={15} /> Log Out of ViralCut AI
                </button>
              </div>

              <div className="panel">
                <h3>
                  <ShieldCheck size={18} /> YouTube Proxy Tester
                </h3>
                <p style={{ color: "#9aa0b5", fontSize: 13, marginTop: -4 }}>
                  Paste any residential proxy line and test it here. YouTube-URL clipping only works when this
                  passes. Formats accepted: <code>http://user:pass@host:port</code>,{" "}
                  <code>host:port:user:pass</code>, or <code>user:pass@host:port</code>.
                </p>
                <div className="field-input" style={{ marginTop: 10 }}>
                  <LinkIcon size={16} />
                  <input
                    value={proxyInput}
                    onChange={(e) => setProxyInput(e.target.value)}
                    placeholder="http://customer-xxxx:pass@pr.oxylabs.io:7777"
                    spellCheck={false}
                  />
                </div>
                <button
                  className="primary-btn mt-12"
                  disabled={proxyTesting}
                  onClick={async () => {
                    setProxyTesting(true);
                    setProxyResult(null);
                    try {
                      const res = await apiFetch("/proxy/check", {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({ proxy_url: proxyInput.trim() }),
                      });
                      setProxyResult(res);
                    } catch (err) {
                      setProxyResult({ ok: false, detail: err.message });
                    } finally {
                      setProxyTesting(false);
                    }
                  }}
                >
                  {proxyTesting ? (
                    <>
                      <Loader2 size={16} className="spin" /> Testing (10-30s)...
                    </>
                  ) : (
                    <>
                      <Zap size={16} /> Test this proxy
                    </>
                  )}
                </button>

                {proxyResult && (
                  <div className={`alert ${proxyResult.ok ? "success" : "error"} mt-12`}>
                    {proxyResult.ok ? <CheckCircle2 size={16} /> : <AlertCircle size={16} />}
                    <span>
                      {proxyResult.ok
                        ? `WORKS! Exit IP ${proxyResult.ipinfo?.exit_ip} (${proxyResult.ipinfo?.country}) — YouTube returned ${proxyResult.youtube?.formats} formats.`
                        : `FAILED — ipinfo: ${proxyResult.ipinfo?.ok ? "ok" : proxyResult.ipinfo?.detail || "-"} | YouTube: ${
                            proxyResult.youtube?.ok
                              ? "ok"
                              : String(proxyResult.youtube?.detail || proxyResult.detail || "-").slice(0, 140)
                          }`}
                    </span>
                  </div>
                )}

                <div className="kv-list mt-16">
                  <div>
                    <span>Saved PROXY_URL</span>
                    <strong>{health?.proxy_configured ? "Configured" : "Not set"}</strong>
                  </div>
                  <div>
                    <span>Upload limit</span>
                    <strong>{health?.max_upload_mb || 95} MB</strong>
                  </div>
                  <div>
                    <span>Free disk</span>
                    <strong>{health?.disk_free_mb != null ? `${Math.round(health.disk_free_mb)} MB` : "--"}</strong>
                  </div>
                  <div>
                    <span>Build</span>
                    <strong>{health?.build || "--"}</strong>
                  </div>
                </div>
              </div>

              <div className="panel">
                <h3>
                  <ShieldCheck size={18} /> Backend Engine Status
                </h3>
                <div className="kv-list">
                  <div>
                    <span>API Server</span>
                    <strong className={health?.status === "ok" ? "ok-text" : "err-text"}>
                      {health?.status === "ok" ? "Connected" : "Offline"}
                    </strong>
                  </div>
                  <div>
                    <span>FFmpeg Clipping</span>
                    <strong className={health?.ffmpeg_ready ? "ok-text" : "err-text"}>
                      {health?.ffmpeg_ready ? "Ready" : "Unavailable"}
                    </strong>
                  </div>
                  <div>
                    <span>Caption Font</span>
                    <strong>{health?.font || "--"}</strong>
                  </div>
                  <div>
                    <span>Max Export Quality</span>
                    <strong>{health?.max_height ? `${health.max_height}p (H.264)` : "--"}</strong>
                  </div>
                </div>
              </div>
            </div>
          </section>
        )}
      </main>

      {/* ===================== VIDEO PREVIEW MODAL ===================== */}
      {previewClip && (
        <div className="modal-backdrop" onClick={() => setPreviewClip(null)}>
          <div className="modal-card" onClick={(e) => e.stopPropagation()}>
            <div className="modal-head">
              <div>
                <h4>{previewClip.title}</h4>
                <span>
                  {formatTime(previewClip.start)} – {formatTime(previewClip.end)} ·{" "}
                  {previewClip.quality} {previewClip.has_captions ? "· Burned CC" : ""}
                </span>
              </div>
              <button className="icon-btn" onClick={() => setPreviewClip(null)}>
                <X size={18} />
              </button>
            </div>
            <video src={previewClip.video_url} controls autoPlay className="modal-video" />
            {(previewClip.description || (previewClip.hashtags && previewClip.hashtags.length > 0)) && (
              <div className="meta-block mt-12">
                {previewClip.description && (
                  <p className="clip-desc">{previewClip.description}</p>
                )}
                {previewClip.hashtags && previewClip.hashtags.length > 0 && (
                  <div className="hashtag-list mt-8">
                    {previewClip.hashtags.map((tag) => (
                      <span key={tag} className="hashtag-chip">
                        {tag}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}
            <div className="modal-foot">
              <button
                type="button"
                className="ghost-btn"
                onClick={() =>
                  handleCopyText(
                    `${previewClip.title}\n\n${previewClip.description || ""}\n\n${(previewClip.hashtags || []).join(" ")}`.trim(),
                    `modal_${previewClip.id}`
                  )
                }
              >
                {copiedKey === `modal_${previewClip.id}` ? <Check size={15} /> : <Copy size={15} />}
                {copiedKey === `modal_${previewClip.id}` ? "Copied Title, Desc & Tags!" : "Copy Title, Desc & Tags"}
              </button>
              <button
                type="button"
                onClick={() => downloadClip(previewClip)}
                className="primary-btn"
                disabled={downloadingId !== null}
              >
                {downloadingId === previewClip.id ? (
                  <>
                    <Loader2 size={16} className="spin" /> Downloading {downloadPct}%
                  </>
                ) : (
                  <>
                    <Download size={16} /> Download MP4
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
