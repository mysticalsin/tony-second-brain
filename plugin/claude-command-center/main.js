'use strict';

const { Plugin, ItemView, Modal, FuzzySuggestModal, PluginSettingTab, Setting, Notice, setIcon } = require('obsidian');

const VIEW_TYPE = 'claude-command-center';
const TERMINAL_VIEW_TYPE = 'terminal:terminal';

// ── Build stamp — change this on every release so stale-code is detectable ───
const PLUGIN_BUILD = '2026-06-10-note-synapse';
// One cheap once-per-load marker to /tmp (NOT the vault — no OneDrive sync) so the live build is
// verifiable from outside Obsidian. (The per-60s tick.log + the redundant ~ load-stamp were removed.)
try { require('fs').writeFileSync(require('path').join(require('os').tmpdir(), 'ultron-plugin-build.txt'), PLUGIN_BUILD + ' loaded ' + new Date().toISOString()); } catch (_) {}

// ── Date helpers (timezone-safe) ──────────────────────────────────────────────

/**
 * Return YYYY-MM-DD in local time for the given Date (defaults to now).
 * Uses getFullYear/getMonth/getDate — deterministic regardless of locale.
 */
function localDateStr(d = new Date()) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

/**
 * Return the ISO-week Monday date string (YYYY-MM-DD) for the current local week.
 */
function currentWeekStartStr() {
  const now = new Date();
  const dow = now.getDay(); // 0=Sun…6=Sat
  const diffToMon = dow === 0 ? -6 : 1 - dow;
  const mon = new Date(now);
  mon.setDate(now.getDate() + diffToMon);
  return localDateStr(mon);
}

/**
 * Estimate time saved + its value + ROI from AI usage.
 * Transparent, tunable model (cfg from settings):
 *   minutes = output_tokens/1000 × minPer1kOutput + sessions × minPerSession
 *   value   = hours × hourlyRate ;  roi = value / cost
 */
function computeRoi(outTokens, sessions, cost, cfg) {
  const minutes = (outTokens / 1000) * (cfg.minPer1kOutput || 0) + sessions * (cfg.minPerSession || 0);
  const hours = minutes / 60;
  const value = hours * (cfg.hourlyRate || 0);
  const roi = cost > 0 ? value / cost : 0;
  return { hours, value, roi };
}

/**
 * Synthesize the "what matters now" insight list from already-fetched signals.
 * Pure + deterministic (no token cost). Returns ranked insights:
 *   { sev: 'critical'|'warn'|'info'|'good', icon, text, action? }
 * action (optional): { label, command } → injected into the Claude terminal.
 */
function computeInsights(d) {
  const out = [];
  const S = (sev, icon, text, action) => out.push({ sev, icon, text, action });

  // Deadlines
  for (const b of (d.sales?.deadlines || [])) {
    if (b.daysLeft < 0) S('critical', '⏰', `${b.opp} is OVERDUE (${-b.daysLeft}d) — ${b.stage}`,
      { label: 'Open bid', command: null, path: b.path });
    else if (b.daysLeft <= 7) S('warn', '⏰', `${b.opp} closes in ${b.daysLeft}d — ${b.stage}`,
      { label: 'Open bid', command: null, path: b.path });
  }
  // Account health + expansion
  for (const a of (d.health?.accounts || [])) {
    if (a.score < 6) S('warn', '📉', `${a.account} health low at ${a.score}/10`);
    else if (a.trend === 'down') S('warn', '📉', `${a.account} health slipping (${a.score}/10)`);
    if (a.expansionDays != null && a.expansionDays <= 90)
      S('info', '🌱', `${a.account} expansion window: ${a.expansionDays}d — don't miss it`);
  }
  // Review backlog aging
  const bk = d.backlog;
  if (bk && bk.count > 0) {
    const oldest = bk.items[0];
    if (oldest && oldest.ageH >= 24)
      S('warn', '📥', `${bk.count} agent write${bk.count > 1 ? 's' : ''} waiting (oldest ${Math.floor(oldest.ageH / 24)}d)`,
        { label: '/dust-resolve', command: '/dust-resolve' });
    else
      S('info', '📥', `${bk.count} agent write${bk.count > 1 ? 's' : ''} to review`,
        { label: '/dust-resolve', command: '/dust-resolve' });
  }
  if (bk && bk.escalations > 0) S('critical', '🚨', `${bk.escalations} open escalation${bk.escalations > 1 ? 's' : ''}`);
  if (bk && bk.decisions > 0) S('warn', '🤔', `${bk.decisions} decision${bk.decisions > 1 ? 's' : ''} pending`);
  // Follow-through debt
  const fu = d.followup;
  if (fu && fu.open >= 10) {
    const total = fu.open + fu.done; const pct = total ? Math.round(fu.done / total * 100) : 0;
    S('warn', '☐', `${fu.open} open action items — only ${pct}% closed`);
  }
  // AI spend trend (this week vs prior)
  const w = d.trend?.weeks;
  if (w && w.length >= 2) {
    const cur = w[w.length - 1].cost, prev = w[w.length - 2].cost;
    if (prev > 1 && cur > prev * 1.5) S('info', '💸', `AI spend up ${Math.round((cur / prev - 1) * 100)}% vs last week (C$${cur.toFixed(0)})`);
  }
  // Cadence
  if (d.cadence) {
    if (d.cadence.streak === 0) S('info', '📓', `No daily note yet — keep the streak alive`,
      { label: 'Plan today', command: '/think-build plan my day' });
    else if (d.cadence.streak >= 5) S('good', '🔥', `${d.cadence.streak}-day journaling streak`);
  }
  // Positive ROI close-out when nothing urgent
  if (!out.some(i => i.sev === 'critical' || i.sev === 'warn') && d.roi) {
    S('good', '🚀', `AI saved you ~${d.roi.weekHours.toFixed(1)}h this week (C$${Math.round(d.roi.weekValue).toLocaleString()})`);
  }
  const rank = { critical: 0, warn: 1, info: 2, good: 3 };
  out.sort((a, b) => rank[a.sev] - rank[b.sev]);
  return out.slice(0, 6);
}

// ── Listen key normalization (shared by Settings capture + global listener) ──
// Returns a canonical combo string like 'Mod+Z' from a KeyboardEvent.
// 'Mod' means Cmd on macOS / Ctrl elsewhere — matches however the user pressed it.
function _normalizeCombo(e) {
  const parts = [];
  if (e.metaKey || e.ctrlKey) parts.push('Mod');
  if (e.shiftKey) parts.push('Shift');
  if (e.altKey) parts.push('Alt');
  // Normalize the printable key. Space's e.key is a literal ' ' — map it to 'SPACE'
  // so combos like 'Mod+Shift+Space' round-trip between capture and the global listener.
  const key = e.key === ' ' ? 'SPACE' : e.key.toUpperCase();
  if (!['META', 'CONTROL', 'SHIFT', 'ALT'].includes(key)) parts.push(key);
  return parts.join('+');
}

// ── Default skill-launcher actions ────────────────────────────────────────────

// (MORNING_BRIEF_CMD removed — the morning brief is now the in-plugin spoken digest on the
//  orb's _agenticTick / _runDigestNow; no terminal Read+Edit write path. Button id 'morning-brief'
//  now dispatches the '__DIGEST_NOW__' sentinel.)

const DEFAULT_ACTIONS = [
  { id: 'brain-status',    emoji: '🧠', label: 'Brain Status',   command: '/brain-status',           prompt: false },
  { id: 'brain-refresh',   emoji: '🔄', label: 'Brain Refresh',  command: 'bash 99_Meta/brain-refresh.sh', prompt: false },
  { id: 'dust-resolve',    emoji: '📥', label: 'Dust Resolve',   command: '/dust-resolve',            prompt: false },
  { id: 'recall',          emoji: '🔍', label: 'Recall',         command: '/recall "{input}"',        prompt: true,  placeholder: 'search query' },
  { id: 'ingest-meeting',  emoji: '🎙', label: 'Ingest Meeting', command: '/ingest-meeting',          prompt: false },
  { id: 'think-build',     emoji: '🏗', label: 'Think-Build',    command: '/think-build {input}',     prompt: true,  placeholder: 'goal' },
  { id: 'morning-brief',   emoji: '📋', label: 'Morning Brief',  prompt: false,
    command: '__DIGEST_NOW__' },
  { id: 'jarvis',          emoji: '🔮', label: 'Ultron Orb',     prompt: false,
    command: '__ORB_TOGGLE__' },
  { id: 'teams-sync',      emoji: '📥', label: 'Teams Sync',     prompt: false,
    command: '/teams-sync' },
];

/** Time-aware greeting for the adaptive header. */
function greeting(d = new Date()) {
  const h = d.getHours();
  if (h >= 5 && h < 12)  return { text: 'Good morning, Tony', emoji: '☀️', frame: "Here's your day" };
  if (h >= 12 && h < 18) return { text: 'Good afternoon, Tony', emoji: '🌤️', frame: 'Where things stand' };
  if (h >= 18 && h < 23) return { text: 'Good evening, Tony', emoji: '🌙', frame: 'Before you wrap up' };
  return { text: 'Burning the midnight oil, Tony', emoji: '🌌', frame: "Still live — here's what's open" };
}

/** One-line natural-language brief synthesized from the ranked insights. */
function narrativeBrief(insights, g) {
  if (!insights || !insights.length) return `${g.frame}: all clear — nothing needs you right now. ✓`;
  const urgent = insights.filter(i => i.sev === 'critical' || i.sev === 'warn').length;
  const lead = urgent ? `${urgent} thing${urgent > 1 ? 's' : ''} need${urgent > 1 ? '' : 's'} you` : 'a few things to note';
  const top = insights.slice(0, 2).map(i => i.text).join(' · ');
  return `${g.frame}: ${lead}. ${top}.`;
}

// ── Terminal injection
//
// Archaeology findings (polyipseity/obsidian-terminal v3.26.0, main.js 2.5 MB):
//
//   • Plugin id = "terminal"  →  view type = "terminal:terminal"
//     (namespaced() helper: `${manifest.id}:${type.id}`)
//
//   • WorkspaceLeaf whose view.getViewType() === 'terminal:terminal' exposes:
//       view.emulator          – instance of class Kr (xterm + pty wrapper)
//       view.emulator.terminal – xterm.js Terminal instance
//       view.emulator.pseudoterminal – Promise<Wl>  (Wl = darwin/linux PTY class)
//
//   • xterm.js Terminal#paste(text) is confirmed present:
//       paste(m){ this._core.paste(m) }   (found at line ~2.54 MB offset)
//     This triggers onData on the xterm instance, which the PTY pipe() already
//     wired to shell.stdin.write(data).  So paste() is the cleanest single-call
//     injection – no need to resolve the pseudoterminal Promise.
//
//   • Full stdin chain (fallback):
//       pty = await view.emulator.pseudoterminal   (resolves Wl instance)
//       proc = await pty.shell                      (resolves child_process)
//       proc.stdin.write(text + "\r")
//
//   • Open-terminal commands: profile-based commands registered via M3() wrapper
//     (dynamic names, not statically listed).  Static fallbacks available:
//       "terminal:focus-on-last-terminal"
//       "terminal:open-terminal.developerConsole"

/**
 * Find or open a Claude terminal leaf.
 *
 * Opening strategy: use the terminal plugin's OWN command
 * `terminal:open-terminal.default.root`, which launches the configured
 * `defaultProfile` (we set that to the Claude profile in the plugin's
 * data.json) at the vault root. The `.root` context has no active-file
 * dependency, so it is safe at startup. We deliberately do NOT use
 * setViewState to spawn a terminal — the terminal view's setState awaits the
 * pty and never resolves when driven externally, hanging the assembler.
 *
 * Placement: the plugin's `newInstanceBehavior: newHorizontalSplit` docks the
 * new terminal below the active pane automatically (the dashboard, when the
 * caller makes it active first).
 *
 * @param {object} opts  { reuse: bool }  reuse an existing terminal if present.
 */
async function _ensureTerminalLeaf(app, opts = {}) {
  // single-flight: _autoAssemble and _selfHeal both fire at layout-ready with no
  // cross-guard — both saw zero leaves and both spawned a PTY. One in-flight
  // ensure at a time; concurrent callers await the same result.
  if (_ensureTerminalLeaf._inflight) return _ensureTerminalLeaf._inflight;
  _ensureTerminalLeaf._inflight = _ensureTerminalLeafImpl(app, opts)
    .finally(() => { _ensureTerminalLeaf._inflight = null; });
  return _ensureTerminalLeaf._inflight;
}

async function _ensureTerminalLeafImpl(app, opts = {}) {
  const reuse = opts.reuse !== false;

  // ── Step 1: reuse an existing terminal leaf ─────────────────────────────────
  if (reuse) {
    const leaves = app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE);
    if (leaves.length > 0) {
      const claude = leaves.find(l => {
        const t = ((l.view && (l.view.title || (l.view.getDisplayText && l.view.getDisplayText()))) || '').toLowerCase();
        return t.includes('claude');
      });
      return claude || leaves[0];
    }
  }

  // ── Step 2: open via the plugin's command (launches defaultProfile) ─────────
  const cmds = (app.commands && app.commands.commands) ? app.commands.commands : {};
  const openId = ['terminal:open-terminal.default.root',
                  'terminal:open-terminal.integrated.root']
                  .find(id => cmds[id]);
  if (!openId) {
    console.warn('[CCC] _ensureTerminalLeaf: no open-terminal command found');
    return null;
  }
  // pty-leak-ensure-leaf-poll-no-dedup: snapshot count before open so we can identify
  // the newly-opened leaf even if stale leaves from prior spawn attempts are present.
  const priorCount = app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE).length;
  app.commands.executeCommandById(openId);

  // Poll for the leaf, then FOCUS it. The terminal plugin defers starting the
  // pty/shell until the leaf is focused and sized — opening it as a background
  // split leaves a live emulator with no shell. Revealing + focusing the leaf
  // is what triggers the Claude shell to actually spawn.
  let leaf = null;
  for (let i = 0; i < 20; i++) {
    await new Promise(r => setTimeout(r, 200));
    const leaves = app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE);
    // Only return the leaf if it's newly opened (count grew beyond priorCount)
    if (leaves.length > priorCount) { leaf = leaves[leaves.length - 1]; break; }
  }
  if (leaf) {
    try {
      app.workspace.revealLeaf(leaf);
      app.workspace.setActiveLeaf(leaf, { focus: true });
    } catch (e) {
      console.warn('[CCC] _ensureTerminalLeaf: focus/reveal failed', e);
    }
  }
  return leaf;
}

async function injectIntoTerminal(app, text) {
  const leaf = await _ensureTerminalLeaf(app);

  if (!leaf) {
    // Total failure — copy to clipboard and notify
    try { await navigator.clipboard.writeText(text); } catch (_) {}
    new Notice('⌘ Command copied — paste into the Claude terminal (injection unavailable)', 8000);
    return;
  }

  // ── Step 3: write to the terminal ──────────────────────────────────────────
  app.workspace.revealLeaf(leaf);
  const view = leaf.view;

  let injected = false;

  // Path A: xterm.paste() for the content, then a SEPARATE Enter to submit.
  // Multi-line text arrives as a bracketed-paste "chip" in Claude's TUI; a \r
  // appended inside the paste is absorbed (chip stays unsent). So we paste the
  // text, then write a bare CR to the shell stdin (outside the paste) to submit.
  try {
    const xterm = view.emulator && view.emulator.terminal;
    if (xterm && typeof xterm.paste === 'function') {
      xterm.paste(text);
      injected = true;
      await new Promise(r => setTimeout(r, 350));
      try {
        const emu = view.emulator;
        const pty = emu && emu.pseudoterminal ? await emu.pseudoterminal : null;
        const proc = pty && pty.shell ? await pty.shell : null;
        if (proc && proc.stdin && typeof proc.stdin.write === 'function') proc.stdin.write('\r');
        else if (typeof xterm.input === 'function') xterm.input('\r');
        else xterm.paste('\r');
      } catch (_) { try { xterm.paste('\r'); } catch (__) {} }
      console.debug('[CCC] terminal inject via xterm.paste() + CR submit');
    }
  } catch (e) {
    console.debug('[CCC] xterm.paste() failed:', e);
  }

  // Path B: resolve pseudoterminal Promise → pty.shell → stdin.write()
  if (!injected) {
    try {
      const emulator = view.emulator;
      if (emulator && emulator.pseudoterminal) {
        const pty = await emulator.pseudoterminal;
        if (pty && pty.shell) {
          const proc = await pty.shell;
          if (proc && proc.stdin && typeof proc.stdin.write === 'function') {
            proc.stdin.write(text + '\r');
            console.debug('[CCC] terminal inject via pty.shell.stdin.write()');
            injected = true;
          }
        }
      }
    } catch (e) {
      console.debug('[CCC] pseudoterminal path failed:', e);
    }
  }

  if (!injected) {
    try { await navigator.clipboard.writeText(text); } catch (_) {}
    new Notice('⌘ Command copied — paste into the Claude terminal (injection unavailable)', 8000);
  } else {
    leaf.view.containerEl && leaf.view.containerEl.focus &&
      leaf.view.containerEl.focus();
  }
}

// ── Prompt modal for actions that require user input ─────────────────────────

class ActionPromptModal extends Modal {
  constructor(app, action, onSubmit) {
    super(app);
    this.action = action;
    this.onSubmit = onSubmit;
    this.inputValue = '';
  }

  onOpen() {
    const { contentEl } = this;
    contentEl.addClass('ccc-modal');
    contentEl.createEl('h3', { text: this.action.emoji + ' ' + this.action.label, cls: 'ccc-modal-title' });

    const inputEl = contentEl.createEl('input', {
      cls: 'ccc-modal-input',
      attr: {
        type: 'text',
        placeholder: this.action.placeholder || 'Enter value…',
      },
    });
    inputEl.addEventListener('input', e => { this.inputValue = e.target.value; });
    inputEl.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); this._submit(); }
      if (e.key === 'Escape') { this.close(); }
    });

    const runBtn = contentEl.createEl('button', { text: 'Run', cls: 'ccc-modal-run-btn' });
    runBtn.addEventListener('click', () => this._submit());

    // Auto-focus input
    setTimeout(() => inputEl.focus(), 50);
  }

  _submit() {
    const val = this.inputValue.trim();
    if (!val) return;
    this.close();
    this.onSubmit(val);
  }

  onClose() {
    this.contentEl.empty();
  }
}

// Frictionless quick-capture → writes a timestamped note to 00_Inbox/capture/.
// No Claude, no network — instant. brain-refresh/graphify pick it up on the next pass.
class QuickCaptureModal extends Modal {
  constructor(app) { super(app); this.text = ''; }
  onOpen() {
    const { contentEl } = this;
    contentEl.addClass('ccc-modal');
    contentEl.createEl('h3', { text: '⚡ Quick Capture', cls: 'ccc-modal-title' });
    const ta = contentEl.createEl('textarea', {
      cls: 'ccc-modal-input ccc-capture-area',
      attr: { placeholder: 'Brain-dump anything… first line = title · ⌘/Ctrl+Enter to save', rows: '6' },
    });
    ta.addEventListener('input', e => { this.text = e.target.value; });
    ta.addEventListener('keydown', e => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); this._save(); }
      if (e.key === 'Escape') this.close();
    });
    const btn = contentEl.createEl('button', { text: '⚡ Capture', cls: 'ccc-modal-run-btn' });
    btn.addEventListener('click', () => this._save());
    setTimeout(() => ta.focus(), 50);
  }
  async _save() {
    const body = (this.text || '').trim();
    if (!body) return;
    const now = new Date();
    const p = n => String(n).padStart(2, '0');
    const ts = `${now.getFullYear()}-${p(now.getMonth() + 1)}-${p(now.getDate())}T${p(now.getHours())}${p(now.getMinutes())}${p(now.getSeconds())}`;
    const slug = (body.split('\n')[0] || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'note';
    const dir = '00_Inbox/capture';
    try { if (!this.app.vault.getAbstractFileByPath(dir)) await this.app.vault.createFolder(dir); } catch (_) {}
    const path = `${dir}/${ts}-${slug}.md`;
    const fm = `---\ntype: capture\ncreated: ${now.toISOString()}\ntags: [capture, inbox]\n---\n\n${body}\n`;
    try {
      await this.app.vault.create(path, fm);
      new Notice('⚡ Captured → ' + path, 4000);
      this.close();
    } catch (e) { new Notice('Capture failed: ' + e.message, 6000); }
  }
  onClose() { this.contentEl.empty(); }
}

// ── Settings tab ──────────────────────────────────────────────────────────────

class CommandCenterSettingTab extends PluginSettingTab {
  constructor(app, plugin) {
    super(app, plugin);
    this.plugin = plugin;
  }

  display() {
    const { containerEl } = this;
    containerEl.empty();
    containerEl.createEl('h2', { text: 'Command Center Settings' });

    new Setting(containerEl)
      .setName('Assemble Mission Control on startup')
      .setDesc('Open the Command Center pane and a Claude terminal automatically when Obsidian starts.')
      .addToggle(toggle => {
        toggle
          .setValue(this.plugin.settings.autoAssemble !== false)
          .onChange(async value => {
            this.plugin.settings.autoAssemble = value;
            await this.plugin.saveSettings();
          });
      });

    new Setting(containerEl)
      .setName('Auto-run Morning Brief')
      .setDesc('On the first launch each morning (06:00–11:59), have Claude write today’s brief into your daily note — once per day, into the freshly-opened terminal.')
      .addToggle(toggle => {
        toggle
          .setValue(this.plugin.settings.autoMorningBrief !== false)
          .onChange(async value => {
            this.plugin.settings.autoMorningBrief = value;
            await this.plugin.saveSettings();
          });
      });

    const roi = this.plugin.settings.roi || {};
    const roiNum = (name, desc, key, current) =>
      new Setting(containerEl).setName(name).setDesc(desc).addText(t => {
        t.setValue(String(current)).onChange(async v => {
          const n = parseFloat(v);
          this.plugin.settings.roi = Object.assign({}, this.plugin.settings.roi, { [key]: isNaN(n) ? 0 : n });
          await this.plugin.saveSettings();
        });
        t.inputEl.type = 'number';
      });
    roiNum('ROI — hourly value (CAD)', 'Your blended hourly rate in CAD; converts time saved into C$ and drives the ROI multiple.', 'hourlyRate', roi.hourlyRate ?? 100);
    roiNum('USD → CAD exchange rate', 'Token spend is billed in USD; the dashboard shows CAD. This rate converts it (estimate — set to your actual rate).', 'usdToCad', roi.usdToCad ?? 1.37);
    roiNum('ROI — minutes saved per 1k output tokens', 'How much human work each 1,000 tokens of AI output represents (1.5 conservative · 2.5 balanced · 4 aggressive).', 'minPer1kOutput', roi.minPer1kOutput ?? 2.5);
    roiNum('ROI — baseline minutes saved per session', 'Fixed overhead displaced per AI session (context-switching, setup, research kickoff).', 'minPerSession', roi.minPerSession ?? 10);

    // ── Ultron listen key ─────────────────────────────────────────────────────
    const micKeyDesc = new Setting(containerEl)
      .setName('Ultron listen key (push to talk)')
      .setDesc('Your Ultron mic toggle, works anywhere — even while typing. Orb hidden → summons it and listens. Orb up → toggles the mic (press to listen, press again to stop). Mic auto-releases after ~10s if nothing is captured. Unmutes automatically. Dismiss the orb with its × button. Click below, then press your keys. Default: Cmd+Shift+Space (Cmd+Z is left alone for Obsidian Undo).');
    micKeyDesc.addButton(btn => {
      const currentCombo = (this.plugin.settings.voice && this.plugin.settings.voice.micToggleKey) || 'Mod+Shift+Space';
      btn.setButtonText(currentCombo);
      btn.onClick(() => {
        btn.setButtonText('Press keys…');
        const onKey = (e) => {
          // Ignore bare modifier presses
          if (['Meta', 'Control', 'Shift', 'Alt'].includes(e.key)) return;
          e.preventDefault();
          e.stopPropagation();
          const combo = _normalizeCombo(e);
          document.removeEventListener('keydown', onKey, true);
          btn.setButtonText(combo);
          this.plugin.settings.voice = Object.assign({}, this.plugin.settings.voice, { micToggleKey: combo });
          this.plugin.saveSettings();
        };
        document.addEventListener('keydown', onKey, true);
      });
    });

    // ── Ultron orb show/hide key ───────────────────────────────────────────────
    const orbKeyDesc = new Setting(containerEl)
      .setName('Ultron orb show/hide key (alias)')
      .setDesc('Optional second key that summons/dismisses the orb. Leave equal to the listen key for one-key operation. Click the button, then press your keys. Default: Cmd+Shift+Space.');
    orbKeyDesc.addButton(btn => {
      const currentCombo = (this.plugin.settings.voice && this.plugin.settings.voice.orbToggleKey) || 'Mod+Shift+Space';
      btn.setButtonText(currentCombo);
      btn.onClick(() => {
        btn.setButtonText('Press keys…');
        const onKey = (e) => {
          if (['Meta', 'Control', 'Shift', 'Alt'].includes(e.key)) return;
          e.preventDefault();
          e.stopPropagation();
          const combo = _normalizeCombo(e);
          document.removeEventListener('keydown', onKey, true);
          btn.setButtonText(combo);
          this.plugin.settings.voice = Object.assign({}, this.plugin.settings.voice, { orbToggleKey: combo });
          this.plugin.saveSettings();
        };
        document.addEventListener('keydown', onKey, true);
      });
    });

    new Setting(containerEl)
      .setName('Skill launcher actions (JSON)')
      .setDesc('Array of action objects. Each: { id, emoji, label, command, prompt, placeholder }. "prompt:true" actions open an input dialog; {input} in command is replaced with the entered value.')
      .addTextArea(text => {
        text
          .setValue(JSON.stringify(this.plugin.settings.actions, null, 2))
          .onChange(async value => {
            try {
              const parsed = JSON.parse(value);
              if (!Array.isArray(parsed)) throw new Error('Must be an array');
              this.plugin.settings.actions = parsed;
              await this.plugin.saveSettings();
            } catch (e) {
              new Notice('Invalid JSON: ' + e.message, 5000);
            }
          });
        text.inputEl.rows = 20;
        text.inputEl.style.width = '100%';
        text.inputEl.style.fontFamily = 'monospace';
        text.inputEl.style.fontSize = '12px';
      });
  }
}

// ── Skills Indexer ────────────────────────────────────────────────────────────

/**
 * Build the skill index from three sources:
 *   1. Personal skills   ~/.claude/skills/star/SKILL.md  (192)
 *   2. Slash commands    ~/.claude/commands/*.md          (12)
 *   3. Plugin skills     ~/.claude/plugins/cache/**\/skills/star/SKILL.md  (28 raw, ~25 unique)
 *
 * Returns { entries: [...], counts: { personal, commands, plugin }, timingMs }
 * Each entry: { name, description, category, source, absPath, vaultPath|null }
 *
 * Async: all disk I/O via fs.promises (non-blocking).
 */
async function buildSkillIndex(vaultPath) {
  const t0 = Date.now();
  const fsp = require('fs').promises;
  const os = require('os');
  const path = require('path');

  const home = os.homedir();
  const errors = [];

  // ── Helper: parse YAML frontmatter from first 2KB (async) ─────────────────
  async function parseFrontmatter(filePath) {
    let fh;
    try {
      fh = await fsp.open(filePath, 'r');
      const buf = Buffer.allocUnsafe(2048);
      const { bytesRead } = await fh.read(buf, 0, 2048, 0);
      const text = buf.slice(0, bytesRead).toString('utf8');
      const match = text.match(/^---\r?\n([\s\S]*?)\r?\n---/);
      if (!match) return {};
      const yaml = match[1];
      const result = {};
      for (const line of yaml.split(/\r?\n/)) {
        const m = line.match(/^(\w[\w-]*):\s*(.*)$/);
        if (!m) continue;
        let val = m[2].trim();
        if ((val.startsWith('"') && val.endsWith('"')) ||
            (val.startsWith("'") && val.endsWith("'"))) {
          val = val.slice(1, -1);
        }
        result[m[1]] = val;
      }
      return result;
    } catch (_) {
      return {};
    } finally {
      if (fh) { try { await fh.close(); } catch (_) {} }
    }
  }

  // ── Helper: first markdown paragraph (non-frontmatter, async) ─────────────
  async function firstParagraph(filePath) {
    let fh;
    try {
      fh = await fsp.open(filePath, 'r');
      const buf = Buffer.allocUnsafe(1024);
      const { bytesRead } = await fh.read(buf, 0, 1024, 0);
      const text = buf.slice(0, bytesRead).toString('utf8');
      let body = text;
      if (text.startsWith('---')) {
        const end = text.indexOf('\n---', 3);
        if (end !== -1) body = text.slice(end + 4);
      }
      for (const line of body.split(/\r?\n/)) {
        const l = line.trim();
        if (l && !l.startsWith('#')) return l;
      }
      return '';
    } catch (_) {
      return '';
    } finally {
      if (fh) { try { await fh.close(); } catch (_) {} }
    }
  }

  // ── Helper: stat a path, returning null on error ───────────────────────────
  async function tryStat(p) {
    try { return await fsp.stat(p); } catch (_) { return null; }
  }

  // ── Build category map from vault _Skills ──────────────────────────────────
  const catMap = {};
  try {
    const skillsRoot = path.join(vaultPath, '_Skills');
    const catFolders = await fsp.readdir(skillsRoot);
    for (const catFolder of catFolders) {
      const catFolderPath = path.join(skillsRoot, catFolder);
      const st = await tryStat(catFolderPath);
      if (!st || !st.isDirectory()) continue;
      const display = catFolder.replace(/^\d+\s+/, '');
      try {
        const skillDirs = await fsp.readdir(catFolderPath);
        for (const skillDir of skillDirs) {
          const skillDirPath = path.join(catFolderPath, skillDir);
          const sst = await tryStat(skillDirPath);
          if (sst && sst.isDirectory()) {
            catMap[skillDir] = display;
          }
        }
      } catch (_) {}
    }
  } catch (e) {
    errors.push('catMap: ' + e.message);
  }

  const entries = [];

  // ── Source 1: Personal skills ──────────────────────────────────────────────
  let personalCount = 0;
  try {
    const skillsDir = path.join(home, '.claude', 'skills');
    const dirs = await fsp.readdir(skillsDir);
    for (const dir of dirs) {
      const skillMd = path.join(skillsDir, dir, 'SKILL.md');
      const st = await tryStat(skillMd);
      if (!st) continue;
      const fm = await parseFrontmatter(skillMd);
      const name = fm.name || dir;
      const description = fm.description || '';
      let category = catMap[dir] || catMap[name];
      if (!category) category = 'Personal';

      let vaultRelPath = null;
      if (catMap[dir]) {
        try {
          const skillsRoot = path.join(vaultPath, '_Skills');
          const catFolders2 = await fsp.readdir(skillsRoot);
          for (const catFolder of catFolders2) {
            const catFolderPath = path.join(skillsRoot, catFolder);
            const cfst = await tryStat(catFolderPath);
            if (!cfst || !cfst.isDirectory()) continue;
            const display = catFolder.replace(/^\d+\s+/, '');
            if (display === catMap[dir]) {
              const candidatePath = path.join(catFolderPath, dir, 'SKILL.md');
              const cst = await tryStat(candidatePath);
              if (cst) {
                vaultRelPath = path.join('_Skills', catFolder, dir, 'SKILL.md');
              }
              break;
            }
          }
        } catch (_) {}
      }
      entries.push({ name, description, category, source: 'personal', absPath: skillMd, vaultPath: vaultRelPath });
      personalCount++;
    }
  } catch (e) {
    errors.push('personal: ' + e.message);
  }

  // ── Source 2: Slash commands ───────────────────────────────────────────────
  let commandCount = 0;
  try {
    const cmdDir = path.join(home, '.claude', 'commands');
    const files = (await fsp.readdir(cmdDir)).filter(f => f.endsWith('.md'));
    for (const file of files) {
      const filePath = path.join(cmdDir, file);
      const stem = file.replace(/\.md$/, '');
      const fm = await parseFrontmatter(filePath);
      const name = '/' + stem;
      const description = fm.description || await firstParagraph(filePath);
      entries.push({ name, description, category: 'Slash Commands', source: 'command', absPath: filePath, vaultPath: null });
      commandCount++;
    }
  } catch (e) {
    errors.push('commands: ' + e.message);
  }

  // ── Source 3: Plugin skills ────────────────────────────────────────────────
  let pluginCount = 0;
  const pluginSeen = new Set(); // dedupe by pluginName::skillName
  try {
    const cacheRoot = path.join(home, '.claude', 'plugins', 'cache');

    // Recursive walk capped at depth 7, find SKILL.md whose parent's parent is 'skills'
    async function walkForSkills(dir, depth) {
      if (depth > 7) return;
      try {
        const dirEntries = await fsp.readdir(dir);
        for (const entry of dirEntries) {
          const full = path.join(dir, entry);
          const st = await tryStat(full);
          if (!st) continue;
          if (st.isFile() && entry === 'SKILL.md') {
            // Check parent's parent is named 'skills'
            const parts = full.split(path.sep);
            const grandparentIdx = parts.length - 3;
            if (grandparentIdx >= 0 && parts[grandparentIdx] === 'skills') {
              const cacheIdx = parts.indexOf('cache');
              if (cacheIdx !== -1) {
                // N5: robust plugin name: segments after cache/ up to 'skills', excluding version segments
                const sIdx = parts.lastIndexOf('skills');
                const plugin = parts.slice(cacheIdx + 1, sIdx)
                  .filter(seg => !/^\d+(\.\d+)*$/.test(seg))
                  .join('/');
                const skillName = parts[parts.length - 2]; // dirname of SKILL.md
                const key = plugin + '::' + skillName;
                if (!pluginSeen.has(key)) {
                  pluginSeen.add(key);
                  const fm = await parseFrontmatter(full);
                  const name2 = fm.name || skillName;
                  const description2 = fm.description || '';
                  const category = 'Plugin: ' + plugin;
                  entries.push({ name: name2, description: description2, category, source: 'plugin', absPath: full, vaultPath: null });
                  pluginCount++;
                }
              }
            }
          } else if (st.isDirectory()) {
            await walkForSkills(full, depth + 1);
          }
        }
      } catch (_) {}
    }
    await walkForSkills(cacheRoot, 0);
  } catch (e) {
    errors.push('plugins: ' + e.message);
  }

  const timingMs = Date.now() - t0;
  console.debug(`[CCC] buildSkillIndex: personal=${personalCount} commands=${commandCount} plugins=${pluginCount} total=${entries.length} time=${timingMs}ms`);

  return {
    entries,
    counts: { personal: personalCount, commands: commandCount, plugin: pluginCount },
    timingMs,
    errors,
  };
}

// ── Pure helpers (exported for test harness) ──────────────────────────────────

function last7Days(byDay, todayStr, fx = 1) {
  const result = [];
  // Step days using setDate so local arithmetic is correct
  const todayParts = todayStr.split('-').map(Number);
  const today = new Date(todayParts[0], todayParts[1] - 1, todayParts[2]);
  for (let i = 6; i >= 0; i--) {
    const d = new Date(today);
    d.setDate(today.getDate() - i);
    const key = localDateStr(d);
    const entry = byDay[key] || {};
    result.push({ date: key, cost: (entry.cost_usd || 0) * fx, sessions: entry.sessions || 0 });
  }
  return result;
}

function normalizeBars(values) {
  const max = Math.max(...values, 0.001);
  return values.map(v => Math.round((v / max) * 100));
}

// Compact token formatting: 1234 → 1.2K, 3531588 → 3.5M, 586537864 → 586.5M
function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'K';
  return String(n);
}

// ── Claude usage analytics (parses ~/.claude/projects transcripts) ────────────
// USAGE_HELPERS_START (marker — node test harness extracts this block)

// Claude-style compact numbers: 262400 → "262.4k", 12300000 → "12.3M"
function fmtUsage(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

// "claude-opus-4-8[1m]" → canonical id "claude-opus-4-8" (1M-context variant merges)
function normalizeModelId(id) {
  return String(id || '').replace(/\[\w+\]$/, '');
}

// "claude-opus-4-7" → "Opus 4.7", "claude-haiku-4-5-20251001" → "Haiku 4.5"
function modelDisplayName(id) {
  const m = /claude-([a-z]+)-(\d+)-(\d+)/.exec(id);
  if (!m) return id;
  return m[1].charAt(0).toUpperCase() + m[1].slice(1) + ' ' + m[2] + '.' + m[3];
}

// Family pricing in USD per MTok — mirrors build/tools/capture_session.py PRICING.
// Jun 2026 baseline: fable 10/50 (2x opus), opus 4.x 5/25, sonnet 3/15, haiku 1/5.
// Cache: read = 0.1x input, write(5m) = 1.25x input. Family (substring) matching so
// a new variant is never silently priced as Sonnet — and 'fable' MUST stay listed
// or every Fable session prices at $0 (the bug this table had until 2026-06-10).
// Known divergence: legacy opus-4-5/4-1 history prices at 5/25 here (capture_session
// keeps exact 15/75 entries); overlay only covers recent days, so impact ~0.
const MODEL_PRICING = [
  { family: 'fable',  in: 10.00, out: 50.00, cr: 1.00, cc: 12.50 },
  { family: 'opus',   in: 5.00,  out: 25.00, cr: 0.50, cc: 6.25 },
  { family: 'sonnet', in: 3.00,  out: 15.00, cr: 0.30, cc: 3.75 },
  { family: 'haiku',  in: 1.00,  out: 5.00,  cr: 0.10, cc: 1.25 },
];
// USD cost of one parseTranscriptText day-aggregate ({model: {in,out,cr,cc}}).
function priceDayModels(models) {
  let usd = 0;
  for (const [id, m] of Object.entries(models || {})) {
    const p = MODEL_PRICING.find(x => id.includes(x.family));
    if (!p) continue; // unknown family (e.g. <synthetic>) → $0, same as capture_session.py
    usd += (m.in || 0) / 1e6 * p.in + (m.out || 0) / 1e6 * p.out
         + (m.cr || 0) / 1e6 * p.cr + (m.cc || 0) / 1e6 * p.cc;
  }
  return usd;
}

// Blue ramp matching Claude Code's /usage palette — index by overall model rank
const USAGE_COLORS = ['#5d7fe3', '#7e99ea', '#a3b8f1', '#cad7f8', '#e7ecfc'];

/**
 * Parse one transcript .jsonl into a per-day aggregate.
 * Dedupes assistant usage by message id (streaming writes one line per content
 * block, each carrying the full usage — counting all of them would multiply tokens).
 * Returns { days: { 'YYYY-MM-DD': { msgs, hours: {h:n}, models: {id:{in,out,cr,cc,msgs}} } } }
 */
function parseTranscriptText(text) {
  const days = {};
  const seen = new Set();
  const dayFor = (date) => days[date] || (days[date] = { msgs: 0, hours: {}, models: {} });
  for (const line of text.split('\n')) {
    if (!line) continue;
    let d;
    try { d = JSON.parse(line); } catch (e) { continue; }
    const ts = d.timestamp;
    if (!ts || (d.type !== 'user' && d.type !== 'assistant')) continue;
    const dt = new Date(ts);
    if (isNaN(dt)) continue;
    const date = localDateStr(dt);
    const hour = dt.getHours();
    if (d.type === 'user') {
      const day = dayFor(date);
      day.msgs++;
      day.hours[hour] = (day.hours[hour] || 0) + 1;
      continue;
    }
    // assistant
    const msg = d.message || {};
    const id = msg.id || d.requestId || d.uuid;
    if (id && seen.has(id)) continue;
    if (id) seen.add(id);
    const day = dayFor(date);
    day.msgs++;
    day.hours[hour] = (day.hours[hour] || 0) + 1;
    const model = normalizeModelId(msg.model);
    if (!model || model === '<synthetic>') continue;
    const u = msg.usage || {};
    const m = day.models[model] || (day.models[model] = { in: 0, out: 0, cr: 0, cc: 0, msgs: 0 });
    m.in += u.input_tokens || 0;
    m.out += u.output_tokens || 0;
    m.cr += u.cache_read_input_tokens || 0;
    m.cc += u.cache_creation_input_tokens || 0;
    m.msgs++;
  }
  return { days };
}

function prevDateStr(s) {
  const d = new Date(s + 'T12:00:00');
  d.setDate(d.getDate() - 1);
  return localDateStr(d);
}

function computeStreaks(activeSet, todayStr) {
  const dates = [...activeSet].sort();
  if (!dates.length) return { current: 0, longest: 0 };
  let longest = 1, run = 1;
  for (let i = 1; i < dates.length; i++) {
    const gap = (new Date(dates[i] + 'T12:00:00') - new Date(dates[i - 1] + 'T12:00:00')) / 86400000;
    if (Math.round(gap) === 1) { run++; if (run > longest) longest = run; }
    else run = 1;
  }
  let cur = 0;
  let d = activeSet.has(todayStr) ? todayStr : prevDateStr(todayStr);
  while (activeSet.has(d)) { cur++; d = prevDateStr(d); }
  return { current: cur, longest };
}

function fmtHour12(h) {
  if (h === 0) return '12 AM';
  if (h === 12) return '12 PM';
  return h < 12 ? h + ' AM' : (h - 12) + ' PM';
}

// Nice axis ceiling: step ∈ {1, 2, 2.5, 5}×10^k so gridlines land on round numbers
function niceAxisMax(v) {
  if (v <= 0) return 4;
  const rawStep = v / 4;
  const mag = Math.pow(10, Math.floor(Math.log10(rawStep)));
  let step = 10 * mag;
  for (const s of [1, 2, 2.5, 5]) {
    if (s * mag >= rawStep) { step = s * mag; break; }
  }
  return step * 4;
}

function fmtAxis(v) {
  if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
  if (v >= 1e3) return Math.round(v / 1e3) + 'k';
  return String(Math.round(v));
}

// "You've used ~465× more tokens than Animal Farm."  (~tokens per work)
const TOKEN_BOOKS = [
  ['Animal Farm', 39000],
  ['The Great Gatsby', 64000],
  ['1984', 117000],
  ['Moby-Dick', 285000],
  ['War and Peace', 760000],
  ['the complete works of Shakespeare', 1180000],
  ['the entire Harry Potter series', 1450000],
];
function tokenComparison(total) {
  if (!total) return '';
  let pick = TOKEN_BOOKS[0];
  for (const b of TOKEN_BOOKS) {
    if (total / b[1] >= 3) pick = b;
  }
  const ratio = total / pick[1];
  const shown = ratio >= 10 ? '~' + Math.round(ratio).toLocaleString() + '×' : '~' + ratio.toFixed(1) + '×';
  return `You've used ${shown} more tokens than ${pick[0]}.`;
}

/**
 * Merge per-file aggregates and compute everything both usage tabs need.
 * files: [{ path, isAgent, agg }] · rangeDays: null (all) | 30 | 7
 */
function computeUsageStats(files, rangeDays, todayStr) {
  todayStr = todayStr || localDateStr();
  const cutoff = rangeDays
    ? localDateStr(new Date(new Date(todayStr + 'T12:00:00').getTime() - (rangeDays - 1) * 86400000))
    : null;
  const inRange = (date) => !cutoff || date >= cutoff;

  const days = {};       // range-filtered, merged
  const allActive = new Set(); // unfiltered — streaks are inherently global
  let sessions = 0;
  for (const f of files) {
    let touched = false;
    for (const [date, src] of Object.entries(f.agg.days || {})) {
      if (src.msgs > 0) allActive.add(date);
      if (!inRange(date)) continue;
      touched = true;
      const day = days[date] || (days[date] = { msgs: 0, hours: {}, models: {}, total: 0 });
      day.msgs += src.msgs || 0;
      for (const [h, n] of Object.entries(src.hours || {})) day.hours[h] = (day.hours[h] || 0) + n;
      for (const [mid, m] of Object.entries(src.models || {})) {
        const t = day.models[mid] || (day.models[mid] = { in: 0, out: 0, cr: 0, cc: 0, msgs: 0 });
        t.in += m.in; t.out += m.out; t.cr += m.cr || 0; t.cc += m.cc || 0; t.msgs += m.msgs;
        day.total += m.in + m.out + (m.cr || 0) + (m.cc || 0);
      }
    }
    if (touched && !f.isAgent) sessions++;
  }

  // Per-model totals + ranking
  const modelTotals = {};
  let grandTotal = 0, messages = 0;
  const hours = {};
  for (const day of Object.values(days)) {
    messages += day.msgs;
    for (const [h, n] of Object.entries(day.hours)) hours[h] = (hours[h] || 0) + n;
    for (const [mid, m] of Object.entries(day.models)) {
      const t = modelTotals[mid] || (modelTotals[mid] = { in: 0, out: 0, cr: 0, cc: 0, msgs: 0, total: 0 });
      t.in += m.in; t.out += m.out; t.cr += m.cr || 0; t.cc += m.cc || 0; t.msgs += m.msgs; t.total += m.in + m.out + (m.cr || 0) + (m.cc || 0);
      grandTotal += m.in + m.out + (m.cr || 0) + (m.cc || 0);
    }
  }
  const models = Object.entries(modelTotals)
    .map(([id, t]) => ({ id, name: modelDisplayName(id), ...t, share: grandTotal ? t.total / grandTotal : 0 }))
    .sort((a, b) => b.total - a.total);
  models.forEach((m, i) => { m.color = USAGE_COLORS[Math.min(i, USAGE_COLORS.length - 1)]; });

  // Chart days: All → active days only (sparse, like Claude Code); 30d/7d → full calendar
  let chartDays;
  if (rangeDays) {
    chartDays = [];
    for (let i = rangeDays - 1; i >= 0; i--) {
      const d = localDateStr(new Date(new Date(todayStr + 'T12:00:00').getTime() - i * 86400000));
      chartDays.push({ date: d, ...(days[d] || { total: 0, models: {}, msgs: 0 }) });
    }
  } else {
    chartDays = Object.keys(days).sort().map(d => ({ date: d, ...days[d] }));
  }

  // Overview stats
  const activeDays = Object.keys(days).filter(d => days[d].msgs > 0).length;
  const streaks = computeStreaks(allActive, todayStr);
  let peakHour = null, peakN = 0;
  for (const [h, n] of Object.entries(hours)) if (n > peakN) { peakN = n; peakHour = Number(h); }

  // Heatmap: last 18 weeks, GitHub-style columns (weeks, Sunday-start)
  const today = new Date(todayStr + 'T12:00:00');
  const end = new Date(today);
  end.setDate(end.getDate() + (6 - end.getDay())); // Saturday of current week
  const WEEKS = 18;
  const heat = [];
  let heatMax = 0;
  for (let i = WEEKS * 7 - 1; i >= 0; i--) {
    const d = new Date(end);
    d.setDate(end.getDate() - i);
    const key = localDateStr(d);
    const total = (days[key] && days[key].total) || 0;
    if (total > heatMax) heatMax = total;
    heat.push({ date: key, total, future: d > today });
  }
  for (const c of heat) c.level = c.total > 0 ? Math.max(1, Math.ceil((c.total / (heatMax || 1)) * 4)) : 0;

  return {
    models, grandTotal, chartDays, sessions, messages, activeDays,
    currentStreak: streaks.current, longestStreak: streaks.longest,
    peakHour: peakHour == null ? '—' : fmtHour12(peakHour),
    favoriteModel: models.length ? models[0].name : '—',
    comparison: tokenComparison(grandTotal),
    heat, heatWeeks: WEEKS,
  };
}
// USAGE_HELPERS_END

// ── Data Layer ─────────────────────────────────────────────────────────────────

class VaultData {
  constructor(app, plugin) {
    this.app = app;
    this.plugin = plugin;
    this._cache = new Map();
  }

  // USD→CAD exchange rate (token spend is billed in USD; the dashboard shows CAD).
  // Tunable in settings (roi.usdToCad); estimate default. All cost_usd reads below
  // are converted here at the source so every display is plain CAD.
  _fx() {
    const r = Number(this.plugin?.settings?.roi?.usdToCad);
    return (r && r > 0) ? r : 1.37;
  }

  // Short-TTL memoizer so the live re-render (every agent write + 60s tick) doesn't
  // re-scan every file each time. Cleared explicitly via invalidate() on a real write.
  async _memo(key, ttlMs, fn) {
    const hit = this._cache.get(key);
    if (hit && (Date.now() - hit.t) < ttlMs) return hit.v;
    const v = await fn();
    this._cache.set(key, { t: Date.now(), v });
    return v;
  }
  invalidate() {
    // perf-audit-2026-06-10: claudeUsage scans ~/.claude (1,500+ transcript stats) and
    // is vault-INDEPENDENT — wiping it on every vault event re-triggered the sweep per
    // paint. Spare it; its own 300s TTL keeps it fresh.
    const usage = this._cache.get('claudeUsage');
    this._cache.clear();
    if (usage) this._cache.set('claudeUsage', usage);
  }

  // vaultdata-perf-01: shared 500ms file-list snapshot so all _Impl() methods in the
  // same render cycle share one getMarkdownFiles() call instead of each doing their own.
  async _allFiles() {
    return this._memo('_allFiles', 500, () => Promise.resolve(this.app.vault.getMarkdownFiles()));
  }

  // LIVE per-day usage from the transcript scan (claudeUsage's incremental cache).
  // stats.json only advances at SessionEnd, so during a long working session "today"
  // would otherwise sit frozen — this derives the same shape of day-entry live.
  async _liveByDay(days) {
    try {
      const live = {};
      const data = await this.claudeUsage();
      if (!data || !data.files) return live;
      const want = new Set(days);
      for (const f of data.files) {
        for (const [date, src] of Object.entries((f.agg && f.agg.days) || {})) {
          if (!want.has(date)) continue;
          const e = live[date] || (live[date] = { cost_usd: 0, sessions: 0,
            input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_creation_tokens: 0 });
          for (const m of Object.values(src.models || {})) {
            e.input_tokens += m.in || 0; e.output_tokens += m.out || 0;
            e.cache_read_tokens += m.cr || 0; e.cache_creation_tokens += m.cc || 0;
          }
          e.cost_usd += priceDayModels(src.models);
          if (!f.isAgent && src.msgs > 0) e.sessions++;
        }
      }
      return live;
    } catch (_) { return {}; }
  }

  // vaultdata-perf-07 / view-render-tokenStats-not-memoized: wrap in _memo so rapid re-renders
  // (and the _liveByDay→claudeUsage chain) don't re-read stats.json on every call.
  async tokenStats() { return this._memo('tokenStats', 55 * 1000, () => this._tokenStatsImpl()); }
  async _tokenStatsImpl() {
    try {
      const raw = await this.app.vault.adapter.read('_agent_state/claude-code/stats.json');
      const data = JSON.parse(raw);
      const byDay = { ...(data.by_day || {}) };
      const allTime = { ...(data.all_time || {}) };
      const todayStr = localDateStr();
      // ── Live overlay: last-7-days window. Pick live over ledger per day when the
      // transcripts report MORE tokens (sessions still running); fold the delta into
      // all-time so the headline figure moves in real time too.
      const winDays = [];
      for (let i = 0; i < 7; i++) { const d = new Date(); d.setDate(d.getDate() - i); winDays.push(localDateStr(d)); }
      const live = await this._liveByDay(winDays);
      const METRICS = ['cost_usd', 'sessions', 'input_tokens', 'output_tokens', 'cache_read_tokens', 'cache_creation_tokens'];
      for (const day of winDays) {
        const l = live[day]; if (!l) continue;
        const g = byDay[day] || {};
        const tot = (e) => (e.input_tokens || 0) + (e.output_tokens || 0) + (e.cache_read_tokens || 0) + (e.cache_creation_tokens || 0);
        if (tot(l) > tot(g)) {
          byDay[day] = l;
          for (const k of METRICS) allTime[k] = (allTime[k] || 0) + ((l[k] || 0) - (g[k] || 0));
        }
      }
      const todayEntry = byDay[todayStr] || {};
      const todayCost = (todayEntry.cost_usd || 0) * this._fx();
      const last7 = last7Days(byDay, todayStr, this._fx());
      const weekCost = last7.reduce((s, d) => s + d.cost, 0);
      const allTimeCost = (allTime.cost_usd || 0) * this._fx();
      const allTimeSessions = allTime.sessions || 0;
      // output-token + session aggregates per period (for time-saved / ROI)
      const todayOut = todayEntry.output_tokens || 0;
      const todaySessions = todayEntry.sessions || 0;
      let weekOut = 0, weekSessions = 0;
      for (const d of last7) {
        const e = byDay[d.date] || {};
        weekOut += e.output_tokens || 0;
        weekSessions += e.sessions || 0;
      }
      const allTimeOut = allTime.output_tokens || 0;
      // full token breakdown (in / out / cache) for display
      const todayIn = todayEntry.input_tokens || 0;
      const todayCacheRead = todayEntry.cache_read_tokens || 0;
      const allTimeIn = allTime.input_tokens || 0;
      const allTimeCacheRead = allTime.cache_read_tokens || 0;
      const allTimeCacheCreate = allTime.cache_creation_tokens || 0;
      const todayTotal = todayIn + todayOut + todayCacheRead + (todayEntry.cache_creation_tokens || 0);
      const allTimeTotal = allTimeIn + allTimeOut + allTimeCacheRead + allTimeCacheCreate;
      return { todayCost, last7, weekCost, allTimeCost, allTimeSessions,
               todayOut, todaySessions, weekOut, weekSessions, allTimeOut,
               todayIn, todayCacheRead, allTimeIn, allTimeCacheRead, allTimeCacheCreate,
               todayTotal, allTimeTotal };
    } catch (e) {
      return { error: 'stats.json unavailable: ' + e.message };
    }
  }

  /**
   * Real Claude Code usage — parses every transcript under ~/.claude/projects.
   * Incremental: per-file day-level aggregates cached by (mtime, size) in the
   * plugin folder, so only transcripts that changed since the last scan re-parse.
   * Returns { files: [{ path, isAgent, agg }] } — range filtering happens at
   * render time via computeUsageStats(), so tab/filter clicks never rescan.
   */
  async claudeUsage() {
    // 300s TTL — perf-sweep-01: voice assistant spend display needs no sub-minute freshness;
    // 55s TTL caused a 937-file stat-sweep on every 75s keep-warm tick. Dashboard has its own refresh path.
    return this._memo('claudeUsage', 300 * 1000, async () => {
      try {
        const fsp = require('fs').promises;
        const os = require('os');
        const path = require('path');
        const root = path.join(os.homedir(), '.claude', 'projects');
        // usage-cache is a regenerable optimization cache — keep it OUT of the
        // OneDrive-synced vault (.obsidian/) so it doesn't upload on every scan.
        const cacheDir = path.join(os.homedir(), '.cache', 'ai-brain');
        await fsp.mkdir(cacheDir, { recursive: true }).catch(() => {}); // vaultdata-perf-06: async, no main-thread block
        const CACHE_PATH = path.join(cacheDir, 'usage-cache.json');

        let cache = { v: 1, files: {} };
        try {
          const parsed = JSON.parse(await fsp.readFile(CACHE_PATH, 'utf8'));
          if (parsed && parsed.v === 1 && parsed.files) cache = parsed;
        } catch (e) { /* first run — cold cache */ }

        // Recursive walk: session transcripts sit at projects/<proj>/<id>.jsonl,
        // subagent transcripts at projects/<proj>/<session>/subagents/agent-*.jsonl.
        const found = [];
        const walk = async (dir, depth) => {
          if (depth > 4) return;
          let entries;
          try { entries = await fsp.readdir(dir, { withFileTypes: true }); } catch (e) { return; }
          for (const ent of entries) {
            const p = path.join(dir, ent.name);
            if (ent.isDirectory()) {
              if (ent.name === 'tool-results') continue; // tool output dumps — no usage data
              await walk(p, depth + 1);
            } else if (ent.isFile() && ent.name.endsWith('.jsonl')) {
              found.push({ path: p, isAgent: ent.name.startsWith('agent-') || dir.includes('subagents') });
            }
          }
        };
        try { await fsp.access(root); } catch (e) { return { error: '~/.claude/projects unavailable: ' + e.message }; }
        await walk(root, 0);

        const files = [];
        const fresh = {};
        let reparsed = 0;
        for (const f of found) {
          let st;
          try { st = await fsp.stat(f.path); } catch (e) { continue; }
          const hit = cache.files[f.path];
          let agg;
          if (hit && hit.mtime === st.mtimeMs && hit.size === st.size) {
            agg = hit.agg;
          } else {
            try { agg = parseTranscriptText(await fsp.readFile(f.path, 'utf8')); reparsed++; }
            catch (e) { continue; }
          }
          fresh[f.path] = { mtime: st.mtimeMs, size: st.size, agg };
          files.push({ path: f.path, isAgent: f.isAgent, agg });
        }

        // Persist only when something actually changed (new/edited/deleted files)
        if (reparsed > 0 || Object.keys(fresh).length !== Object.keys(cache.files).length) {
          fsp.writeFile(CACHE_PATH, JSON.stringify({ v: 1, files: fresh }))
            .catch(() => { /* cache is an optimization — never block render */ });
        }
        return { files };
      } catch (e) {
        return { error: 'usage scan failed: ' + e.message };
      }
    });
  }

  async meetings() { return this._memo('meetings', 60000, () => this._meetingsImpl()); } // view-render-5min-ttl: 300s→60s matches backstop interval
  async _meetingsImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        f.path.startsWith('Meetings/recaps/')
      );
      const todayStr = localDateStr();
      const monStr = currentWeekStartStr();
      const monthStr = todayStr.slice(0, 7);

      // Compute last month string (YYYY-MM)
      const now = new Date();
      const lastMonthDate = new Date(now.getFullYear(), now.getMonth() - 1, 1);
      const lastMonthStr = localDateStr(lastMonthDate).slice(0, 7);

      // transcript_status values that mean "transcribed" in this vault
      const TRANSCRIBED_VALUES = new Set(['attached']);

      const records = [];
      for (const f of files) {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
        if (!fm || fm.type !== 'meeting-record') continue;
        const ts = (fm.transcript_status || '').toString().toLowerCase().trim();
        records.push({
          path: f.path,
          client: fm.client || '',
          meeting_type: fm.meeting_type || '',
          date: fm.date ? String(fm.date).slice(0, 10) : '',
          transcript_status: fm.transcript_status || '',
          transcribed: TRANSCRIBED_VALUES.has(ts) ||
            ['done', 'complete', 'transcribed', '✅'].some(v => ts.includes(v)),
        });
      }

      records.sort((a, b) => b.date.localeCompare(a.date));

      const countToday = records.filter(r => r.date === todayStr).length;
      const countWeek = records.filter(r => r.date >= monStr && r.date <= todayStr).length;
      const countMonth = records.filter(r => r.date.startsWith(monthStr)).length;
      const recent = records.slice(0, 3);

      return {
        countToday, countWeek, countMonth,
        recent, records, monthStr, lastMonthStr,
        TRANSCRIBED_VALUES: [...TRANSCRIBED_VALUES],
      };
    } catch (e) {
      return { error: 'meetings unavailable: ' + e.message };
    }
  }

  async fleet() { return this._memo('fleet', 300000, () => this._fleetImpl()); }
  async _fleetImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        f.path.startsWith('02_Areas/AI Sessions/')
      );
      const todayStr = localDateStr();
      const monStr = currentWeekStartStr();

      const toolMap = {};
      const allSessions = [];
      for (const f of files) {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
        if (!fm || fm.type !== 'ai-session') continue;
        const tool = fm.tool || fm.agent || f.path.split('/')[3] || 'unknown';
        const date = fm.date ? String(fm.date).slice(0, 10) : '';
        const cost = fm.cost_usd ? Number(fm.cost_usd) * this._fx() : null;
        const summary = fm.summary || '';
        if (!toolMap[tool]) toolMap[tool] = { all: 0, last7: 0 };
        toolMap[tool].all++;
        if (date >= monStr && date <= todayStr) toolMap[tool].last7++;
        allSessions.push({ path: f.path, tool, date, cost, summary, basename: f.basename, ctime: f.stat.ctime });
      }

      // Sort by date desc, tiebreak by ctime desc, take top 10
      allSessions.sort((a, b) => {
        if (b.date !== a.date) return b.date.localeCompare(a.date);
        return b.ctime - a.ctime;
      });
      const recentSessions = allSessions.slice(0, 10);

      return { tools: toolMap, recentSessions };
    } catch (e) {
      return { error: 'fleet unavailable: ' + e.message };
    }
  }

  async fleetTriage() { return this._memo('fleetTriage', 300000, () => this._fleetTriageImpl()); }
  async _fleetTriageImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        f.path.startsWith('00_Inbox/from-dust/') &&
        f.basename !== 'README' &&
        !f.path.endsWith('README.md')
      );
      const items = files.map(f => {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter || {};
        return {
          path: f.path,
          basename: f.basename,
          source_agent: fm.source_agent || '',
          confidence: fm.confidence !== undefined ? fm.confidence : null,
        };
      });
      items.sort((a, b) => b.path.localeCompare(a.path));
      return { count: items.length, items };
    } catch (e) {
      return { error: 'triage unavailable: ' + e.message };
    }
  }

  async fleetMistakes() { return this._memo('fleetMistakes', 300000, () => this._fleetMistakesImpl()); }
  async _fleetMistakesImpl() {
    try {
      const raw = await this.app.vault.adapter.read('Preferences/mistakes.md');
      // Format: ## YYYY-MM-DD — <agent> — <summary>
      // followed by **What happened:** etc.
      const entries = [];
      const lines = raw.split(/\r?\n/);
      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        const m = line.match(/^##\s+(\d{4}-\d{2}-\d{2})\s+[—–-]+\s+(.+)/);
        if (!m) continue;
        const date = m[1];
        const rest = m[2];
        // First line after the heading for detail
        let detail = '';
        for (let j = i + 1; j < Math.min(i + 6, lines.length); j++) {
          const l = lines[j].trim();
          if (l && !l.startsWith('#')) {
            // Strip markdown bold markers
            detail = l.replace(/^\*{1,2}/, '').replace(/\*{1,2}$/, '').trim();
            break;
          }
        }
        entries.push({ date, summary: rest.trim(), detail });
      }
      // Return last 5
      const last5 = entries.slice(-5);
      return { entries: last5 };
    } catch (e) {
      return { error: 'mistakes.md unavailable: ' + e.message };
    }
  }

  async pipeline() { return this._memo('pipeline', 60000, () => this._pipelineImpl()); } // view-render-5min-ttl: 300s→60s matches backstop interval
  async _pipelineImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f => {
        const parts = f.path.split('/');
        return (
          parts[0] === 'RFPs' &&
          parts[parts.length - 1] === '00 - Brief.md' &&
          !f.path.includes('/_template/')
        );
      });

      const todayStr = localDateStr();
      const in14Str = (() => {
        const d = new Date();
        d.setDate(d.getDate() + 14);
        return localDateStr(d);
      })();

      const bids = [];
      for (const f of files) {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter || {};
        const stage = fm.stage || '';
        if (['Won', 'Lost'].includes(stage)) continue;
        const deadline = fm.deadline ? String(fm.deadline).slice(0, 10) : '';
        bids.push({
          path: f.path,
          opportunity: fm.opportunity || f.path.split('/')[1],
          client: fm.client || '',
          stage: stage,
          deadline: deadline,
          closingSoon: deadline && deadline <= in14Str && deadline >= todayStr,
          overdue: deadline && deadline < todayStr,
        });
      }

      bids.sort((a, b) => {
        if (a.overdue && !b.overdue) return -1;
        if (!a.overdue && b.overdue) return 1;
        if (a.closingSoon && !b.closingSoon) return -1;
        if (!a.closingSoon && b.closingSoon) return 1;
        return a.deadline.localeCompare(b.deadline);
      });

      return { open: bids.length, bids: bids.slice(0, 5) };
    } catch (e) {
      return { error: 'pipeline unavailable: ' + e.message };
    }
  }

  async important() { return this._memo('important', 60000, () => this._importantImpl()); } // view-render-5min-ttl: 300s→60s matches backstop interval
  async _importantImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        f.path.startsWith('Important/') && !f.basename.startsWith('_index')
      );
      files.sort((a, b) => b.stat.mtime - a.stat.mtime);
      const top5 = files.slice(0, 5).map(f => ({
        path: f.path,
        name: f.basename,
        mtime: f.stat.mtime,
      }));
      return { items: top5 };
    } catch (e) {
      return { error: 'important unavailable: ' + e.message };
    }
  }

  // ── Pipeline value (CAD) + win rate + deadlines (bid briefs) ───────────────
  async salesPipeline() { return this._memo('sales', 120000, () => this._salesPipelineImpl()); } // perf-sweep-07: 20s→120s; bid stages change at human cadence not sub-minute
  async _salesPipelineImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        f.basename === '00 - Brief' &&
        (f.path.startsWith('RFPs/') || f.path.startsWith('04_Archives/')) &&
        !f.path.includes('/_template/'));
      const todayStr = localDateStr();
      const open = [], won = [], lost = [];
      for (const f of files) {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
        if (!fm || fm.type !== 'bid') continue;
        const stage = String(fm.stage || '').trim();
        const rec = {
          opp: fm.opportunity || f.path.split('/').slice(-2)[0],
          client: fm.client || '', stage,
          value: Number(fm.value) || 0, prob: Number(fm.probability) || 0,
          deadline: fm.deadline ? String(fm.deadline).slice(0, 10) : '', path: f.path,
        };
        if (stage === 'Won') won.push(rec);
        else if (stage === 'Lost') lost.push(rec);
        else open.push(rec);
      }
      const openValue = open.reduce((s, b) => s + b.value, 0);
      const weighted = open.reduce((s, b) => s + b.value * b.prob / 100, 0);
      const decided = won.length + lost.length;
      const winRate = decided ? won.length / decided : 0;
      const wonValue = won.reduce((s, b) => s + b.value, 0);
      const lostValue = lost.reduce((s, b) => s + b.value, 0);
      const avgDeal = won.length ? wonValue / won.length : (open.length ? openValue / open.length : 0);
      const deadlines = open.filter(b => b.deadline).map(b => ({
        ...b,
        daysLeft: Math.round((new Date(b.deadline + 'T00:00:00') - new Date(todayStr + 'T00:00:00')) / 86400000),
      })).sort((a, b) => a.daysLeft - b.daysLeft);
      return { openCount: open.length, wonCount: won.length, lostCount: lost.length,
               openValue, weighted, winRate, wonValue, lostValue, avgDeal, deadlines, open };
    } catch (e) { return { error: 'sales unavailable: ' + e.message }; }
  }

  // ── Account health scores + expansion windows (coach notes) ────────────────
  // vaultdata-perf-04: raise TTL from 20s to 300s — health scores change at most weekly
  async accountHealth() { return this._memo('health', 300000, () => this._accountHealthImpl()); }
  async _accountHealthImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        /^02_Areas\/Accounts\/[^/]+\/_coach_notes\.md$/.test(f.path));
      const out = [];
      for (const f of files) {
        const acct = f.path.split('/')[2];
        const body = await this.app.vault.cachedRead(f);
        const hm = body.match(/Health score:\**\s*([0-9.]+)\s*\/\s*10/i) ||
                   body.match(/Health:\s*([0-9.]+)\s*\/\s*10/i);
        if (!hm) continue;
        const score = parseFloat(hm[1]);
        const pm = body.match(/from\s+([0-9.]+)\s+in/i);
        const prev = pm ? parseFloat(pm[1]) : null;
        const wm = body.match(/window:\s*([0-9]+)\s*days/i);
        const trend = prev == null ? null : (score > prev ? 'up' : score < prev ? 'down' : 'flat');
        out.push({ account: acct, score, prev, trend,
                   expansionDays: wm ? parseInt(wm[1]) : null, path: f.path });
      }
      out.sort((a, b) => a.score - b.score); // lowest health first
      return { accounts: out };
    } catch (e) { return { error: 'account health unavailable: ' + e.message }; }
  }

  // ── Agent / skill fleet performance ────────────────────────────────────────
  async fleetPerf() { return this._memo('fleetPerf', 60000, () => this._fleetPerfImpl()); }
  async _fleetPerfImpl() {
    const out = { skills: [], totalRuns: 0, totalErr: 0, totalCorr: 0,
                  agentsActive: 0, agentsNew: 0, byOutput: {} };
    try {
      const sr = JSON.parse(await this.app.vault.adapter.read('_agent_state/skill-registry.json'));
      for (const s of (Array.isArray(sr.skills) ? sr.skills : [])) {
        const run = s.run_count || 0, err = s.error_count || 0, corr = s.user_corrections || 0;
        out.totalRuns += run; out.totalErr += err; out.totalCorr += corr;
        out.skills.push({ slug: s.slug, run, err, corr, health: s.health_status || '?' });
      }
      out.skills.sort((a, b) => b.run - a.run);
    } catch (_) {}
    try {
      const reg = JSON.parse(await this.app.vault.adapter.read('_agent_state/_registry.json'));
      for (const a of (reg.agents || [])) {
        if (a.status === 'active') out.agentsActive++;
        else if (a.status === 'new') out.agentsNew++;
        const ot = a.output_type || 'other';
        out.byOutput[ot] = (out.byOutput[ot] || 0) + 1;
      }
    } catch (_) {}
    return out;
  }

  // ── Review backlog (what needs me now) ─────────────────────────────────────
  async reviewBacklog() { return this._memo('backlog2', 60000, () => this._reviewBacklogImpl()); } // view-render-5min-ttl: 300s→60s matches backstop interval
  async _reviewBacklogImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list (was: getMarkdownFiles)
      const files = allMd.filter(f =>
        f.path.startsWith('00_Inbox/from-dust/') && f.basename !== 'README');
      const items = files.map(f => {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter || {};
        return { path: f.path, name: f.basename, agent: fm.source_agent || f.path.split('/')[2],
                 confidence: typeof fm.confidence === 'number' ? fm.confidence : null,
                 output_type: fm.output_type || '',
                 ageH: Math.floor((Date.now() - f.stat.mtime) / 3600000) };
      });
      items.sort((a, b) => (b.ageH - a.ageH) || ((a.confidence ?? 1) - (b.confidence ?? 1)));
      const hi = items.filter(i => (i.confidence ?? 0) >= 0.85).length;
      const lo = items.filter(i => i.confidence != null && i.confidence < 0.85).length;
      const imp = allMd.filter(f => f.path.startsWith('Important/'));
      const escalations = imp.filter(f => f.path.includes('/escalations/')).length;
      const decisions = imp.filter(f => f.path.includes('/decisions-pending/')).length;
      return { count: items.length, hi, lo, items: items.slice(0, 8), escalations, decisions };
    } catch (e) { return { error: 'review backlog unavailable: ' + e.message }; }
  }

  // ── AI leverage trend (weekly spend/tokens, last 8 weeks) ──────────────────
  // Memoized (60s) — reads stats.json on every tick otherwise; invalidated on real writes.
  async aiTrend() { return this._memo('aiTrend', 60000, () => this._aiTrendImpl()); }
  async _aiTrendImpl() {
    try {
      const data = JSON.parse(await this.app.vault.adapter.read('_agent_state/claude-code/stats.json'));
      const byDay = data.by_day || {};
      const today = new Date(localDateStr() + 'T00:00:00');
      const dow = today.getDay();
      const thisMon = new Date(today); thisMon.setDate(today.getDate() + (dow === 0 ? -6 : 1 - dow));
      const weeks = [];
      for (let w = 7; w >= 0; w--) {
        const start = new Date(thisMon); start.setDate(thisMon.getDate() - w * 7);
        let cost = 0, out = 0;
        for (let d = 0; d < 7; d++) {
          const day = new Date(start); day.setDate(start.getDate() + d);
          const e = byDay[localDateStr(day)] || {};
          cost += (e.cost_usd || 0) * this._fx(); out += e.output_tokens || 0;
        }
        weeks.push({ label: localDateStr(start).slice(5), cost, out });
      }
      return { weeks };
    } catch (e) { return { error: 'ai trend unavailable: ' + e.message }; }
  }

  // ── Meeting / bid follow-through (checkbox action items) ───────────────────
  // Reads every Meetings/ + RFPs/ file body → memoized so the live refresh
  // doesn't re-scan them on every tick (invalidated on real agent writes).
  async meetingFollowup() { return this._memo('followup', 120000, () => this._meetingFollowupImpl()); } // vaultdata-perf-02: 20s→120s; checkbox state changes at meeting cadence not sub-minute
  async _meetingFollowupImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      // vaultdata-perf-02: pre-filter with metadataCache to skip files with no checkboxes
      const files = allMd.filter(f =>
        (f.path.startsWith('Meetings/') || f.path.startsWith('RFPs/')) &&
        this.app.metadataCache.getFileCache(f)?.listItems?.some(li => li.task !== undefined));
      let open = 0, done = 0; const openItems = [];
      for (const f of files) {
        const body = await this.app.vault.cachedRead(f);
        for (const line of body.split('\n')) {
          const m = line.match(/^\s*- \[( |x|X)\]\s+(.+\S)\s*$/);
          if (!m) continue;
          if (m[1] === ' ') { open++; if (openItems.length < 8) openItems.push({ text: m[2].slice(0, 80), path: f.path, name: f.basename }); }
          else done++;
        }
      }
      return { open, done, openItems };
    } catch (e) { return { error: 'follow-up unavailable: ' + e.message }; }
  }

  // ── Personal cadence (daily-note streak, wins, mood/energy) ────────────────
  async personalCadence() { return this._memo('cadence', 300000, () => this._personalCadenceImpl()); } // vaultdata-perf-08: 20s→300s; streak+wins-this-week don't change within 20s
  async _personalCadenceImpl() {
    try {
      const allMd = await this._allFiles(); // vaultdata-perf-01: shared file list
      const files = allMd.filter(f =>
        /^02_Areas\/Daily\/\d{4}-\d{2}-\d{2}\.md$/.test(f.path));
      const dates = new Set(files.map(f => f.basename));
      let streak = 0;
      const d = new Date(localDateStr() + 'T00:00:00');
      if (!dates.has(localDateStr(d))) d.setDate(d.getDate() - 1); // grace if today not written yet
      while (dates.has(localDateStr(d))) { streak++; d.setDate(d.getDate() - 1); }
      const monStr = currentWeekStartStr(), todayStr = localDateStr();
      const recent = files.sort((a, b) => b.basename.localeCompare(a.basename));
      let wins = 0, mood = '', energy = '';
      for (const f of recent.slice(0, 7)) {
        if (f.basename >= monStr && f.basename <= todayStr) {
          const body = await this.app.vault.cachedRead(f);
          const seg = body.split(/## Wins today/i)[1];
          if (seg) {
            const block = seg.split(/\n##/)[0];
            wins += (block.match(/^\s*-\s+\S.+$/gm) || []).length;
          }
        }
      }
      for (const f of recent) {
        const fm = this.app.metadataCache.getFileCache(f)?.frontmatter || {};
        if (!mood && fm.mood) mood = String(fm.mood);
        if (!energy && fm.energy) energy = String(fm.energy);
        if (mood && energy) break;
      }
      return { streak, total: files.length, winsThisWeek: wins, mood, energy };
    } catch (e) { return { error: 'cadence unavailable: ' + e.message }; }
  }

  // ── Outcomes / compounding: is the brain making Tony win more + get smarter? ─
  async outcomes() { return this._memo('outcomes', 60000, () => this._outcomesImpl()); }
  async _outcomesImpl() {
    try {
      const out = {};
      const sp = await this.salesPipeline();
      out.winRate = sp.error ? 0 : sp.winRate; out.won = sp.wonCount || 0; out.lost = sp.lostCount || 0; out.wonValue = sp.wonValue || 0;
      const md = await this._allFiles(); // vaultdata-perf-01/03: shared file list
      // account coverage (touched ≤21d)
      const byAcct = {};
      for (const f of md) { const m = f.path.match(/^02_Areas\/Accounts\/([^/]+)\//); if (m) byAcct[m[1]] = Math.max(byAcct[m[1]] || 0, f.stat.mtime); }
      const accts = Object.keys(byAcct), now = Date.now();
      out.accounts = accts.length;
      out.accountsCovered = accts.filter(a => (now - byAcct[a]) / 86400000 <= 21).length;
      out.coverage = accts.length ? out.accountsCovered / accts.length : 0;
      // learning trend — corrections logged this week vs last (down = compounding)
      const monStr = currentWeekStartStr();
      const lastMon = localDateStr(new Date(new Date(monStr + 'T00:00:00').getTime() - 7 * 86400000));
      let thisW = 0, lastW = 0;
      try {
        const mk = await this.app.vault.adapter.read('Preferences/mistakes.md');
        for (const m of mk.matchAll(/^##\s+(\d{4}-\d{2}-\d{2})/gm)) {
          const d = m[1]; if (d >= monStr) thisW++; else if (d >= lastMon) lastW++;
        }
      } catch (_) {}
      out.correctionsThisWeek = thisW; out.correctionsLastWeek = lastW;
      try { const l = await this.app.vault.adapter.read('Preferences/Lessons.md'); out.lessons = (l.match(/^###\s+Issue/gm) || []).length; }
      catch (_) { out.lessons = 0; }
      // brain growth
      out.capturesThisWeek = md.filter(f => f.path.startsWith('00_Inbox/capture/') && localDateStr(new Date(f.stat.ctime)) >= monStr).length;
      out.docsInBrain = md.filter(f => f.path.startsWith('Document Library/')).length;
      return out;
    } catch (e) { return { error: 'outcomes unavailable: ' + e.message }; }
  }
}

// ── Utility ───────────────────────────────────────────────────────────────────

function relativeTime(mtime) {
  const diff = Date.now() - mtime;
  const s = Math.floor(diff / 1000);
  if (s < 60) return s + 's ago';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ago';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

function fmtDate(dateStr) {
  if (!dateStr) return '';
  return dateStr.slice(5); // MM-DD
}

// ── View ──────────────────────────────────────────────────────────────────────

// ── ULTRON VITALS — the cardiac system (v3, panel-hardened) ──────────────────
// One organism, one clock. Three honest sensors feed a pacemaker; the pacemaker
// writes 4 CSS custom properties on <html> per animation frame; ALL rhythmic
// visuals (orb halo systole, dashboard breathing vignette) derive from those
// vars. Periphery gets NO per-beat pulses (perceptual habituation) — one-shot
// pulses fire only on real events, originating at the panel that caused them.
//
// Panel-applied invariants (2026-06-09 red-team):
//  • _agent_state/** NEVER feeds the heart — the hourly refresh batch-writes 29
//    reputation.json in one second (sat(29,6)≈0.99 → cron metronome). Whitelist.
//  • Beats while Tony types: vault.on modify/create IS the primary sensor.
//  • No git subprocess: tail .git/logs/HEAD (append-only reflog) instead.
//  • Arrhythmia from CURRENT from-dust queue state (age-decayed), never from
//    dust-errors.log (append-only replay artifact — chronically non-empty).
//  • Same-second event batches collapse to ONE event (cron/sync bursts).
//  • dt clamp ≤100ms, ≤1 beat dispatched per frame; visibility resume reseeds
//    phase (no beat machine-gun after occlusion).
//  • HRV = per-beat IBI sampling (AR(1) + respiratory sinus coupling), never
//    per-frame jitter. Stress collapses σ 8%→1% and adds ectopic couplets.
class UltronVitals {
  constructor(plugin) {
    this.plugin = plugin;
    this.bus = new (require('obsidian').Events)();
    // event counts per channel, rolling 10-min ring (20 × 30s slots)
    this._ring = { user: new Array(20).fill(0), dust: new Array(20).fill(0), commit: new Array(20).fill(0), claude: new Array(20).fill(0) };
    this._slot = 0;
    this._bpm = 38; this._bpmTarget = 38;
    this._phase = 0; this._respPhase = 0;
    this._ibi = 60 / 38; this._sinceBeat = 0; this._ar1 = 0;
    this._state = 'dormant';        // dormant | resting | active | surging
    this._arr = 0;                  // arrhythmia intensity 0..~3 (age-decayed pending dust files)
    this._ectopic = 0;              // frames left of couplet flag
    this._tier = 'HIDDEN';          // ACTIVE | PASSIVE | BATTERY | HIDDEN
    this._raf = null; this._lastT = 0; this._frameSkip = 0;
    this._lastSameSecond = 0;       // batch-collapse guard
    this._claudeHot = 0;            // epoch ms until which a claude session counts as live
    this._sess = { file: null, size: 0, idleSince: 0 };
    this._meta = { headMtime: 0, headSeen: 0, dustMtime: 0, dustBytes: 0, pendingSet: null };
    this._onBattery = false;
  }

  // ── lifecycle ──────────────────────────────────────────────────────────────
  start() {
    const plugin = this.plugin;
    // VaultSense — 0ms. Whitelist by exclusion of machine paths; from-dust is
    // special-cased BEFORE the generic handler so arrivals pulse the fleet.
    const MACHINE = ['.obsidian/', '_brain_index/', '_brain_api/', '_agent_state/', 'graphify-out/', 'out/'];
    const MACHINE_FILES = ['99_Meta/dust-write-log.md', '99_Meta/dust-errors.log'];
    const onFs = (kind) => (file) => {
      const p = (file && file.path) || '';
      if (!p || MACHINE_FILES.includes(p)) return;
      if (p.startsWith('00_Inbox/from-dust/')) {
        if (kind === 'create') this._event('dust', { kind: 'arrival', path: p });
        if (kind === 'delete' || kind === 'rename') this._dustResolved(p); // /dust-resolve moves files out
        this._refreshArrhythmia();
        return;
      }
      if (MACHINE.some(pre => p.startsWith(pre))) return;
      this._event('user', { kind, path: p });
    };
    plugin.registerEvent(plugin.app.vault.on('modify', onFs('modify')));
    plugin.registerEvent(plugin.app.vault.on('create', onFs('create')));
    plugin.registerEvent(plugin.app.vault.on('delete', onFs('delete')));
    plugin.registerEvent(plugin.app.vault.on('rename', onFs('rename')));

    // Governor — re-evaluate tier on visibility + every 2s; battery every 60s.
    plugin.registerDomEvent(document, 'visibilitychange', () => this._govern(true));
    plugin.registerInterval(window.setInterval(() => this._govern(false), 2000));
    plugin.registerInterval(window.setInterval(() => this._pollBattery(), 60000));
    this._pollBattery();

    // SessionSense — 3s in ACTIVE (suppressed while a plugin-spawned claude run
    // marks itself hot via reflex()); MetaSense — 90s ACTIVE / 300s otherwise.
    plugin.registerInterval(window.setInterval(() => { if (this._tier === 'ACTIVE') this._pollSessions(); }, 3000));
    let metaCount = 0;
    plugin.registerInterval(window.setInterval(() => {
      metaCount++;
      if (this._tier === 'ACTIVE' ? (metaCount % 3 === 0) : (metaCount % 10 === 0)) this._pollMeta();
    }, 30000));
    // ring rotation — one slot per 30s
    plugin.registerInterval(window.setInterval(() => {
      this._slot = (this._slot + 1) % 20;
      for (const k of Object.keys(this._ring)) this._ring[k][this._slot] = 0;
      this._recomputeBpm();
    }, 30000));
    plugin.register(() => this._stopClock());
    this._pollMeta();          // prime arrhythmia + reflog high-water marks
    this._refreshArrhythmia();
    this._govern(true);
  }

  // ── sensors ────────────────────────────────────────────────────────────────
  // Reflex (L1): called inline from ask-bar / orb ask / claude spawn. kind is a
  // label only; every reflex is an instant systole kick + marks claude hot 90s
  // so SessionSense doesn't double-count the same work.
  reflex(kind) {
    this._claudeHot = Date.now() + 90000;
    this._event('claude', { kind: 'reflex:' + kind });
    this._kick(0.9);
  }

  _event(channel, info) {
    // batch-collapse: a cron commit / OneDrive sync burst lands many files in
    // the same second — one organism action, one event.
    const sec = Math.floor(Date.now() / 1000);
    const key = channel + ':' + sec;
    if (key === this._lastSameSecond) return;
    this._lastSameSecond = key;
    this._ring[channel] && (this._ring[channel][this._slot] += 1);
    this._recomputeBpm();
    this.bus.trigger('sense', Object.assign({ channel }, info));
    if (channel === 'user') this._kick(0.35);
    if (channel === 'dust') { this._kick(0.7); this.bus.trigger('pulse', { path: info.path, source: 'dust' }); }
  }

  // L2: newest-grown transcript across ALL ~/.claude/projects slugs (plugin-dev
  // sessions, AI-Brain-build, adjacent dirs) + ~/.codex/sessions. Single stat
  // pass over dir listings; tail-read only the grown file, capped 64KB.
  _pollSessions() {
    if (Date.now() < this._claudeHot - 85000) return; // reflex just fired; skip one cycle
    try {
      const fs = require('fs'), path = require('path'), os = require('os');
      const roots = [path.join(os.homedir(), '.claude', 'projects')];
      let newest = null;
      for (const root of roots) {
        let dirs = [];
        try { dirs = fs.readdirSync(root); } catch (_) { continue; }
        for (const d of dirs) {
          const dir = path.join(root, d);
          let files; try { files = fs.readdirSync(dir); } catch (_) { continue; }
          for (const f of files) {
            if (!f.endsWith('.jsonl')) continue;
            const fp = path.join(dir, f);
            let st; try { st = fs.statSync(fp); } catch (_) { continue; }
            if (!newest || st.mtimeMs > newest.mtimeMs) newest = { fp, st, mtimeMs: st.mtimeMs };
          }
        }
      }
      if (!newest || (Date.now() - newest.mtimeMs) > 30000) return; // nothing live
      const { fp, st } = newest;
      if (this._sess.file !== fp) { this._sess = { file: fp, size: st.size, idleSince: 0 }; return; }
      if (st.size <= this._sess.size) return;
      const from = Math.max(this._sess.size, st.size - 65536);
      let chunk = '';
      try {
        const fd = fs.openSync(fp, 'r');
        const buf = Buffer.alloc(st.size - from);
        fs.readSync(fd, buf, 0, buf.length, from);
        fs.closeSync(fd);
        chunk = buf.toString('utf8');
      } catch (_) {}
      this._sess.size = st.size;
      this._event('claude', { kind: 'tool-activity' });
      this._claudeHot = Date.now() + 15000;
      // origin pulses: which vault files is the model touching RIGHT NOW
      const base = this._vaultBase();
      const re = /"file_path"\s*:\s*"([^"]+)"/g;
      const seen = new Set(); let m;
      while ((m = re.exec(chunk)) && seen.size < 4) {
        let p = m[1];
        if (base && p.startsWith(base)) p = p.slice(base.length + 1);
        if (p.startsWith('/') || seen.has(p)) continue;
        seen.add(p);
        this.bus.trigger('pulse', { path: p, source: 'claude' });
      }
      this._kick(0.5);
    } catch (_) {}
  }

  // L3: reflog tail (commits, zero subprocess) + dust-write-log tail (real
  // events only) + from-dust pending queue (arrhythmia source of truth).
  _pollMeta() {
    try {
      const fs = require('fs'), path = require('path');
      const base = this._vaultBase();
      if (!base) return;
      const head = path.join(base, '.git', 'logs', 'HEAD');
      let st; try { st = fs.statSync(head); } catch (_) { st = null; }
      if (st && st.mtimeMs !== this._meta.headMtime) {
        this._meta.headMtime = st.mtimeMs;
        try {
          const fd = fs.openSync(head, 'r');
          const from = Math.max(0, st.size - 8192);
          const buf = Buffer.alloc(st.size - from);
          fs.readSync(fd, buf, 0, buf.length, from);
          fs.closeSync(fd);
          const re = /> (\d{10}) [+-]\d{4}\t/g; let m, latest = this._meta.headSeen;
          while ((m = re.exec(buf.toString('utf8')))) {
            const ts = parseInt(m[1], 10);
            if (ts > this._meta.headSeen) { this._event('commit', { kind: 'commit' }); latest = Math.max(latest, ts); }
          }
          this._meta.headSeen = latest || Math.floor(Date.now() / 1000);
        } catch (_) {}
      }
      const dlog = path.join(base, '99_Meta', 'dust-write-log.md');
      let ds; try { ds = fs.statSync(dlog); } catch (_) { ds = null; }
      if (ds && ds.size > this._meta.dustBytes) {
        try {
          const fd = fs.openSync(dlog, 'r');
          const from = this._meta.dustBytes > 0 ? this._meta.dustBytes : Math.max(0, ds.size - 4096);
          const buf = Buffer.alloc(ds.size - from);
          fs.readSync(fd, buf, 0, buf.length, from);
          fs.closeSync(fd);
          const real = (buf.toString('utf8').match(/^- 20[^\n]+/gm) || []).filter(l => !l.includes('REPLAY_SUPPRESSED'));
          if (real.length) this._event('dust', { kind: 'log', count: real.length });
        } catch (_) {}
        this._meta.dustBytes = ds.size;
      }
      this._refreshArrhythmia();
    } catch (_) {}
  }

  // Arrhythmia = Σ e^(−age_h/4) over files currently pending in from-dust.
  // Fresh held write → obvious stumble; three-week-old noise → near zero.
  _refreshArrhythmia() {
    try {
      const files = this.plugin.app.vault.getFiles().filter(f => f.path.startsWith('00_Inbox/from-dust/'));
      const now = Date.now();
      let sum = 0;
      const set = new Set();
      for (const f of files) {
        set.add(f.path);
        const ageH = Math.max(0, (now - (f.stat && f.stat.mtime || now)) / 3600e3);
        sum += Math.exp(-ageH / 4);
      }
      this._meta.pendingSet = set;
      this._arr = sum;
      document.documentElement.style.setProperty('--ccc-arr', String(Math.min(1, sum / 2)));
    } catch (_) {}
  }

  _dustResolved(path) {
    // file left the queue — if that empties the fresh load, fire the defib
    const before = this._arr;
    this._refreshArrhythmia();
    if (before > 0.25 && this._arr < 0.25) this.bus.trigger('defib', {});
  }

  // ── pacemaker ──────────────────────────────────────────────────────────────
  _recomputeBpm() {
    const sum = (a) => a.reduce((x, y) => x + y, 0);
    const sat = (x, k) => 1 - Math.exp(-x / k);
    const claudeLive = Date.now() < this._claudeHot ? 1.2 : 0;
    const act = 1 - (1 - sat(sum(this._ring.commit), 2))
                  * (1 - sat(sum(this._ring.dust), 4))
                  * (1 - sat(sum(this._ring.user), 5))
                  * (1 - sat(sum(this._ring.claude) + claudeLive, 1.5));
    this._bpmTarget = 38 + 72 * act;
    const prev = this._state;
    const b = this._bpmTarget;
    const next = b < 46 ? 'dormant' : b < 60 ? 'resting' : b < 80 ? 'active' : 'surging';
    if (next !== prev) {
      this._state = next;
      document.body.setAttribute('data-ccc-state', next);
      this.bus.trigger('state', { from: prev, to: next });   // consumers run the 2s flourish
    }
  }

  // instant systole bump for reflex/causal events (0..1 strength)
  _kick(strength) {
    const el = document.documentElement;
    const cur = parseFloat(el.style.getPropertyValue('--ccc-systole') || '0');
    el.style.setProperty('--ccc-systole', String(Math.min(1, Math.max(cur, strength))));
    this._sinceBeat = Math.min(this._sinceBeat, this._ibi * 0.4); // pull next beat closer, don't stack
  }

  _sampleIbi() {
    // AR(1) noise + respiratory sinus arrhythmia; stress (pending dust) collapses
    // variability to rigid + earns ectopic couplets — medically true stress signature.
    const stressed = this._arr > 1;
    const sigma = stressed ? 0.01 : 0.08;
    this._ar1 = 0.7 * this._ar1 + (Math.random() * 2 - 1) * sigma;
    const rsa = 0.06 * Math.sin(2 * Math.PI * this._respPhase);
    let ibi = (60 / Math.max(30, this._bpm)) * (1 + rsa + this._ar1);
    if (stressed && Math.random() < Math.min(0.25, this._arr * 0.06)) {
      ibi *= 0.55;                       // premature beat
      this._ectopic = 2;                 // flag next 2 beats as couplet (off-color, 2× amplitude)
    }
    return Math.max(0.3, ibi);
  }

  // ── the one clock ──────────────────────────────────────────────────────────
  _startClock() {
    if (this._raf) return;
    this._lastT = performance.now();
    const tick = (t) => {
      this._raf = requestAnimationFrame(tick);
      const dt = Math.min(0.1, (t - this._lastT) / 1000); // clamp: no beat bursts after occlusion
      this._lastT = t;
      if (this._tier === 'BATTERY' && (this._frameSkip = (this._frameSkip + 1) % 2)) return; // 30fps
      this._bpm += (this._bpmTarget - this._bpm) * Math.min(1, dt * 0.8); // glide
      this._respPhase = (this._respPhase + dt * (this._bpm / 4) / 60) % 1;
      this._sinceBeat += dt;
      let beat = false, amp = 1;
      if (this._sinceBeat >= this._ibi) {                  // ≤1 beat per frame by construction
        this._sinceBeat = 0;
        this._ibi = this._tier === 'BATTERY' ? 60 / Math.max(30, this._bpm) : this._sampleIbi();
        beat = true;
        if (this._ectopic > 0) { this._ectopic--; amp = 2; }
        this.bus.trigger('beat', { bpm: this._bpm, ectopic: amp > 1 });
      }
      // systole envelope: sharp attack at beat, ~600ms exponential decay
      const el = document.documentElement;
      const decayed = parseFloat(el.style.getPropertyValue('--ccc-systole') || '0') * Math.exp(-dt / 0.22);
      const sys = beat ? Math.min(1, 0.55 * amp) : decayed;
      el.style.setProperty('--ccc-systole', sys.toFixed(3));
      el.style.setProperty('--ccc-breath', (0.5 + 0.5 * Math.sin(2 * Math.PI * this._respPhase)).toFixed(3));
      el.style.setProperty('--ccc-ect', this._ectopic > 0 ? '1' : '0');
    };
    this._raf = requestAnimationFrame(tick);
  }

  _stopClock() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
  }

  // ── governor ───────────────────────────────────────────────────────────────
  _govern(visibilityEdge) {
    let tier;
    if (document.hidden) tier = 'HIDDEN';
    else {
      const orbUp = !!(this.plugin.orb && this.plugin.orb.visible && this.plugin.orb.el && this.plugin.orb.el.isConnected);
      let viewShown = false;
      try {
        this.plugin.app.workspace.iterateAllLeaves(l => { if (l.view && l.view.getViewType && l.view.getViewType() === VIEW_TYPE && l.view.contentEl.isShown()) viewShown = true; });
      } catch (_) {}
      tier = (orbUp || viewShown) ? (this._onBattery ? 'BATTERY' : 'ACTIVE') : 'PASSIVE';
    }
    if (tier === this._tier && !visibilityEdge) return;
    const wasDark = this._tier === 'HIDDEN' || this._tier === 'PASSIVE';
    this._tier = tier;
    if (tier === 'ACTIVE' || tier === 'BATTERY') {
      if (wasDark) { this._phase = 0; this._sinceBeat = 0; this._kick(0.4); } // single waking beat
      this._startClock();
    } else {
      this._stopClock(); // PASSIVE/HIDDEN: sensors stay, pixels stop
    }
  }

  _pollBattery() {
    try {
      if (!navigator.getBattery) return;
      navigator.getBattery().then(b => { this._onBattery = !b.charging && b.level < 0.35; }).catch(() => {});
    } catch (_) {}
  }

  _vaultBase() {
    try { return this.plugin.app.vault.adapter.basePath || null; } catch (_) { return null; }
  }
}

// ── SYNAPSE LAYER — watch Ultron think through the second brain ──────────────
// While the orb is THINKING, thought renders as synapses firing across the
// vault: every Read/Grep/Glob the brain ACTUALLY performs flashes that file in
// the explorer and a spark arcs from the orb (or the previous file) to it —
// real file accesses, not theater. Before the first tool call lands (and on
// no-tool turns) a sparse ambient drizzle of micro-sparks keeps the brain
// shimmering. Everything is pointer-events:none, capped at 14 live sparks,
// self-removing; the SVG layer tears itself down a few seconds after idle.
class SynapseLayer {
  constructor(plugin) {
    this.plugin = plugin;
    this._svg = null; this._live = 0; this._lastPt = null;
    this._ambTimer = null; this._idleTimer = null;
  }

  // Called from the brain's stream-json loop on every tool_use block.
  noteToolUse(name, input) {
    try {
      if (document.hidden) return;
      const p = this._relPath(input && (input.file_path || input.path || input.notebook_path));
      if (p) { this.fireFile(p, true); return; }
      if (name === 'Grep' || name === 'Glob') this._sweep(); // pathless search = brain-wide ripple
    } catch (_) {}
  }

  // Ambient mode follows the orb's thinking class (MutationObserver in JarvisOrb.show).
  thinking(on) {
    if (on) {
      if (this._ambTimer) return;
      clearTimeout(this._idleTimer);
      const tick = () => {
        this._ambTimer = setTimeout(tick, 900 + Math.random() * 1100);
        if (document.hidden || this._live >= 14) return;
        this._cascade(); // neuron chains, not lone sparks — thought propagates
      };
      tick();
    } else {
      clearTimeout(this._ambTimer); this._ambTimer = null;
      this._lastPt = null;
      this._scheduleTeardown();
    }
  }

  fireFile(rel, major) {
    // spark-cap-fix: the cap only existed on the (dead) ambient tick — a single
    // assistant message with N parallel Reads fired N uncapped sparks in one
    // frame, each with a forced layout: the jank spike felt mid-conversation.
    if (this._dead || this._live >= 14) return;
    const el = this._fileEl(rel);
    const to = el ? this._center(el) : this._fallbackPt();
    const from = this._lastPt || this._orbPt();
    this._lastPt = to;
    this._spark(from, to, { minor: !major, hit: el });
  }

  destroy() {
    this._dead = true; // zombie-timer guard: in-flight spark timeouts must not re-arm anything
    clearTimeout(this._ambTimer); this._ambTimer = null;
    clearTimeout(this._idleTimer); this._idleTimer = null;
    if (this._svg) { try { this._svg.remove(); } catch (_) {} this._svg = null; }
    this._live = 0; this._lastPt = null;
  }

  // ── internals ──────────────────────────────────────────────────────────────
  _relPath(p) {
    if (!p || typeof p !== 'string') return null;
    if (p.startsWith('/')) {
      const base = (() => { try { return this.plugin.app.vault.adapter.basePath; } catch (_) { return null; } })();
      if (base && p.startsWith(base + '/')) return p.slice(base.length + 1);
      return null; // absolute path outside the vault — no synapse target
    }
    return p.replace(/^\.\//, '');
  }

  _fileEl(rel) {
    const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"');
    // folder-depth-fix: a directory target (Grep/Glob scope) must match its OWN
    // folder row first — the old code stripped a segment before the folder query,
    // so "02_Areas/Daily" lit "02_Areas" and top-level folders never matched.
    let el = document.querySelector(`.nav-file-title[data-path="${esc(rel)}"]`)
          || document.querySelector(`.nav-folder-title[data-path="${esc(rel)}"]`);
    // collapsed folder: walk up to the deepest visible ancestor folder row
    let p = rel;
    while ((!el || !this._onScreen(el)) && p.includes('/')) {
      p = p.slice(0, p.lastIndexOf('/'));
      el = document.querySelector(`.nav-folder-title[data-path="${esc(p)}"]`);
    }
    return (el && this._onScreen(el)) ? el : null;
  }

  _randomFileEl() {
    const all = document.querySelectorAll('.nav-files-container .nav-file-title');
    if (!all.length) return null;
    // up to 4 draws to land on an on-screen row (virtualized/scrolled lists)
    for (let i = 0; i < 4; i++) {
      const el = all[(Math.random() * all.length) | 0];
      if (this._onScreen(el)) return el;
    }
    return null;
  }

  _onScreen(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && r.bottom > 0 && r.top < window.innerHeight;
  }

  _center(el) {
    const r = el.getBoundingClientRect();
    return { x: r.left + Math.min(r.width, 180) / 2, y: r.top + r.height / 2 };
  }

  _orbPt() {
    const orb = this.plugin.orb;
    if (orb && orb.el && orb.el.isConnected) {
      const r = orb.el.getBoundingClientRect();
      return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
    }
    return { x: window.innerWidth - 120, y: window.innerHeight - 180 };
  }

  // target row not rendered (collapsed tree, hidden sidebar): aim into the
  // file-explorer zone anyway so the thought still visibly "enters the brain"
  _fallbackPt() {
    return { x: 40 + Math.random() * 140, y: 90 + Math.random() * Math.max(120, window.innerHeight - 200) };
  }

  // While the orb thinks, thought propagates as NEURON CHAINS: an action
  // potential leaves the orb, hops row→row (soma membrane-flash at each
  // arrival), occasionally forking into a sibling branch — the file explorer
  // becomes the dendritic tree. Hops prefer nearby rows so chains read as
  // anatomy, not random lightning.
  _cascade() {
    if (this._dead) return;
    const rows = Array.from(document.querySelectorAll(
      '.nav-files-container .nav-file-title, .nav-files-container .nav-folder-title'))
      .filter(el => this._onScreen(el));
    if (!rows.length) return;
    const near = (pt) => {
      const c = rows.filter(el => {
        const r = el.getBoundingClientRect();
        const d = Math.hypot(r.left + Math.min(r.width, 180) / 2 - pt.x, r.top + r.height / 2 - pt.y);
        return d > 28 && d < 320; // skip self, stay dendrite-local
      });
      const pool = c.length ? c : rows;
      return pool[(Math.random() * pool.length) | 0];
    };
    let from = this._orbPt();
    let cur = near(from);
    let delay = 0;
    const hops = 2 + ((Math.random() * 3) | 0); // 2-4 hops per thought
    for (let i = 0; i < hops && cur; i++) {
      const a = from, el = cur, to = this._center(el);
      setTimeout(() => {
        if (this._dead || document.hidden || this._live >= 14) return;
        this._spark(a, to, { minor: true, hit: el });
        this._soma(to);
        if (Math.random() < 0.3 && this._live < 13) { // fork: one thought branches
          const b = near(to);
          if (b && b !== el) this._spark(to, this._center(b), { minor: true, hit: b });
        }
      }, delay);
      delay += 380 + Math.random() * 220; // action-potential pacing between hops
      from = to;
      cur = near(to);
    }
  }

  // Soma flash: a small membrane pulse where the action potential arrives.
  _soma(pt) {
    if (this._dead) return;
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', pt.x.toFixed(1));
    c.setAttribute('cy', pt.y.toFixed(1));
    c.setAttribute('r', '3');
    c.setAttribute('class', 'ccc-syn-soma');
    this._layer().appendChild(c);
    try {
      c.animate([{ transform: 'scale(1)', opacity: 0.9 }, { transform: 'scale(4)', opacity: 0 }],
        { duration: 700, easing: 'cubic-bezier(.2,.7,.3,1)', fill: 'forwards' });
    } catch (_) {}
    setTimeout(() => c.remove(), 760);
  }

  _sweep() {
    for (let i = 0; i < 3; i++) {
      setTimeout(() => {
        if (this._dead || document.hidden || this._live >= 14) return;
        const el = this._randomFileEl();
        if (el) this._spark(this._orbPt(), this._center(el), { minor: true, hit: el });
      }, i * 90);
    }
  }

  _layer() {
    if (this._svg && this._svg.isConnected) return this._svg;
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.classList.add('ccc-synapse-layer');
    document.body.appendChild(svg);
    this._svg = svg;
    return svg;
  }

  _scheduleTeardown() {
    if (this._dead) return; // zombie-timer guard: spark timeouts firing after destroy() must not re-arm
    clearTimeout(this._idleTimer);
    this._idleTimer = setTimeout(() => {
      if (this._live === 0 && !this._ambTimer && this._svg) { this._svg.remove(); this._svg = null; }
    }, 4000);
  }

  // One synapse: a curved axon draws itself from a→b while a glowing spark
  // rides it (SMIL animateMotion), then both fade and self-remove. The hit row
  // gets a flash class so the file itself "fires".
  _spark(a, b, opts) {
    const o = opts || {};
    const svg = this._layer();
    const ns = 'http://www.w3.org/2000/svg';
    // perpendicular bow so axons curve like dendrites, not straight wires
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const dx = b.x - a.x, dy = b.y - a.y;
    const dist = Math.max(1, Math.hypot(dx, dy));
    const bow = Math.min(120, dist * 0.25) * (Math.random() < 0.5 ? -1 : 1);
    const cx = mx - (dy / dist) * bow, cy = my + (dx / dist) * bow;
    const d = `M ${a.x.toFixed(1)} ${a.y.toFixed(1)} Q ${cx.toFixed(1)} ${cy.toFixed(1)} ${b.x.toFixed(1)} ${b.y.toFixed(1)}`;

    const path = document.createElementNS(ns, 'path');
    path.setAttribute('d', d);
    path.setAttribute('class', o.minor ? 'ccc-syn-axon ccc-syn-axon-minor' : 'ccc-syn-axon');
    svg.appendChild(path);

    const head = document.createElementNS(ns, 'circle');
    head.setAttribute('r', o.minor ? '2.2' : '3.4');
    head.setAttribute('class', o.minor ? 'ccc-syn-head ccc-syn-head-minor' : 'ccc-syn-head');
    const dur = o.minor ? 0.55 : 0.75;
    const mo = document.createElementNS(ns, 'animateMotion');
    mo.setAttribute('dur', dur + 's');
    mo.setAttribute('fill', 'freeze');
    mo.setAttribute('calcMode', 'spline');
    mo.setAttribute('keySplines', '0.2 0.7 0.3 1');
    mo.setAttribute('keyTimes', '0;1');
    mo.setAttribute('path', d);
    // smil-clock-fix: with an implicit begin, the animation resolves against the
    // PERSISTENT overlay's clock — any spark created later than `dur` after the
    // overlay appeared starts in the past and freezes at the path END (no
    // travel; the fade masked it). begin=indefinite + beginElement() starts NOW.
    mo.setAttribute('begin', 'indefinite');
    head.appendChild(mo);
    svg.appendChild(head);
    try { mo.beginElement(); } catch (_) {}

    // draw-in then fade via WAAPI (no dynamic keyframe injection)
    let len = dist * 1.2;
    try { len = path.getTotalLength(); } catch (_) {}
    path.style.strokeDasharray = String(len);
    const ttl = (o.minor ? 900 : 1250);
    try {
      path.animate(
        [{ strokeDashoffset: len, opacity: 0.9 }, { strokeDashoffset: 0, opacity: 0.9, offset: 0.55 }, { strokeDashoffset: 0, opacity: 0 }],
        { duration: ttl, easing: 'cubic-bezier(.2,.7,.3,1)', fill: 'forwards' });
      head.animate([{ opacity: 1 }, { opacity: 1, offset: 0.7 }, { opacity: 0 }], { duration: ttl, fill: 'forwards' });
    } catch (_) {}
    this._live++;
    setTimeout(() => { path.remove(); head.remove(); this._live--; this._scheduleTeardown(); }, ttl + 60);

    if (o.hit) {
      // arrival-synced: the row fires when the spark lands, not when it leaves.
      // WAAPI instead of class-toggle: the old restart hack (`void offsetWidth`)
      // forced a synchronous layout per hit — visible micro-stutter during
      // cascades. el.animate() runs off the compositor and restarts cleanly.
      setTimeout(() => {
        if (!o.hit.isConnected) return;
        try {
          const frames = o.minor
            ? [{ background: 'rgba(127,0,218,0.22)', boxShadow: '0 0 8px rgba(168,107,255,0.35)', borderRadius: '5px' },
               { background: 'transparent', boxShadow: 'none', borderRadius: '5px' }]
            : [{ background: 'rgba(248,240,96,0.42)', boxShadow: '0 0 14px rgba(248,240,96,0.55), inset 0 0 8px rgba(248,240,96,0.25)', color: '#fffbe0', borderRadius: '5px' },
               { background: 'rgba(127,0,218,0.34)', boxShadow: '0 0 12px rgba(127,0,218,0.5)', offset: 0.35, borderRadius: '5px' },
               { background: 'transparent', boxShadow: 'none', borderRadius: '5px' }];
          o.hit.animate(frames, { duration: o.minor ? 900 : 1050, easing: 'ease-out' });
        } catch (_) {}
      }, dur * 1000 * 0.9);
    }
  }
}

// ── GraphSynapse — neuron firing on the native graph view ────────────────────
// Mirrors SynapseLayer's thought-process animation onto Obsidian's graph view:
// a real tool_use Read fires the actual node (yellow→purple pulse + scale pop),
// then the action potential hops along REAL graph edges to neighbor notes;
// Grep/Glob folder scopes sweep the matching cluster; ambient drizzle while the
// orb thinks. Styling rides a render-proxy installed AROUND whatever render fn
// is current (incl. graph-style-customizer's own proxy), applied after it
// returns — so pulses win while live and leave zero residue when idle: the
// base tint is re-read every frame inside the wrapper, nothing to restore.
// Wrappers are uninstalled the moment their pulse expires (no accumulation).
class GraphSynapse {
  constructor(plugin) {
    this.plugin = plugin;
    this._pulses = new Map();  // node -> { start, dur, minor, r }
    this._links = new Map();   // link -> { start, dur, r }
    this._wrapN = new Map();   // node -> { orig, wrap }
    this._wrapL = new Map();   // link -> { orig, wrap }
    this._raf = null; this._ambTimer = null; this._dead = false;
    this._lastId = null;       // chain-of-thought bias for ambient firing
  }

  // Called alongside SynapseLayer.noteToolUse from the stream-json loop.
  noteToolUse(name, input) {
    try {
      if (this._dead || document.hidden) return;
      const syn = this.plugin.synapse;
      const rel = syn ? syn._relPath(input && (input.file_path || input.path || input.notebook_path)) : null;
      if (rel) {
        if (this._find(rel)) this.fireFile(rel, true);
        else this.sweep(rel.replace(/\/+$/, '') + '/'); // a folder scope, not a node
        return;
      }
      if (name === 'Grep' || name === 'Glob') this._ambient(true); // pathless search = one strong burst
    } catch (_) {}
  }

  thinking(on) {
    if (this._dead) return;
    if (on) {
      if (this._ambTimer) return;
      const tick = () => {
        this._ambTimer = setTimeout(tick, 1200 + Math.random() * 1000);
        if (document.hidden || this._pulses.size >= 20) return;
        this._ambient(false);
      };
      tick();
    } else { clearTimeout(this._ambTimer); this._ambTimer = null; }
  }

  fireFile(rel, major) {
    if (this._dead || this._pulses.size >= 20) return;
    const hit = this._find(rel);
    if (!hit) return;
    this._pulse(hit.r, hit.node, { minor: !major });
    this._chain(hit.r, hit.node, major ? 2 + ((Math.random() * 3) | 0) : 1);
  }

  // Folder search: stagger-pulse up to 6 nodes under the prefix.
  sweep(prefix) {
    if (this._dead) return;
    for (const r of this._renderers()) {
      const under = this._nodesOf(r).filter(n => typeof n.id === 'string' && n.id.startsWith(prefix));
      if (!under.length) continue;
      for (let i = under.length - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; const t = under[i]; under[i] = under[j]; under[j] = t; }
      under.slice(0, 6).forEach((n, i) => setTimeout(() => this._pulse(r, n, { minor: i > 0 }), i * 90));
      return; // first renderer that has the cluster wins
    }
  }

  destroy() {
    this._dead = true;
    clearTimeout(this._ambTimer); this._ambTimer = null;
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    for (const [node, w] of this._wrapN) { try { if (node.render === w.wrap) node.render = w.orig; } catch (_) {} }
    for (const [link, w] of this._wrapL) { try { if (link.render === w.wrap) link.render = w.orig; } catch (_) {} }
    this._wrapN.clear(); this._wrapL.clear(); this._pulses.clear(); this._links.clear();
    for (const r of this._renderers()) { try { r.changed(); } catch (_) {} }
  }

  // ── internals ──────────────────────────────────────────────────────────────
  _renderers() {
    const out = [];
    try {
      const ws = this.plugin.app.workspace;
      for (const t of ['graph', 'localgraph']) {
        for (const leaf of ws.getLeavesOfType(t)) {
          const v = leaf.view, r = v && (v.renderer || v.dataEngine || v.engine);
          if (r && r.nodes) out.push(r);
        }
      }
    } catch (_) {}
    return out;
  }

  _nodesOf(r) {
    const n = r.nodes;
    if (!n) return [];
    if (Array.isArray(n)) return n;
    if (n instanceof Map) return [...n.values()];
    return Object.values(n);
  }

  _find(rel) {
    for (const r of this._renderers()) {
      for (const node of this._nodesOf(r)) if (node.id === rel) return { r, node };
    }
    return null;
  }

  _neighborIds(node) {
    const ids = new Set();
    const grab = (c) => {
      if (!c) return;
      if (c instanceof Map) { for (const k of c.keys()) ids.add(k); }
      else if (typeof c === 'object') { for (const k in c) ids.add(k); }
    };
    grab(node.forward); grab(node.reverse);
    ids.delete(node.id);
    return ids;
  }

  _neighbors(r, node) {
    const ids = this._neighborIds(node);
    if (!ids.size) return [];
    return this._nodesOf(r).filter(n => ids.has(n.id));
  }

  _linkBetween(r, a, b) {
    try {
      const L = r.links || [];
      for (const l of L) {
        const s = l.source && l.source.id, t = l.target && l.target.id;
        if ((s === a.id && t === b.id) || (s === b.id && t === a.id)) return l;
      }
    } catch (_) {}
    return null;
  }

  _pulse(r, node, o) {
    if (this._dead || !node) return;
    this._wrapNode(node);
    this._pulses.set(node, { start: performance.now(), dur: (o && o.minor) ? 750 : 950, minor: !!(o && o.minor), r });
    this._lastId = node.id;
    this._run();
  }

  _flashLink(r, link) {
    if (this._dead || !link) return;
    this._wrapLink(link);
    this._links.set(link, { start: performance.now(), dur: 650, r });
    this._run();
  }

  // Action potential: hop along real edges, soma-pulse each arrival.
  _chain(r, node, hops) {
    let cur = node, depth = 0;
    const step = () => {
      if (this._dead || depth >= hops || this._pulses.size >= 20) return;
      const nb = this._neighbors(r, cur);
      if (!nb.length) return;
      const next = nb[(Math.random() * nb.length) | 0];
      const link = this._linkBetween(r, cur, next);
      if (link) this._flashLink(r, link);
      this._pulse(r, next, { minor: true });
      cur = next; depth++;
      setTimeout(step, 300 + Math.random() * 200);
    };
    setTimeout(step, 220);
  }

  _ambient(strong) {
    const rs = this._renderers();
    if (!rs.length) return;
    const r = rs[(Math.random() * rs.length) | 0];
    const nodes = this._nodesOf(r);
    if (!nodes.length) return;
    let node = null;
    // 60%: continue the previous thought from a neighbor — chains read as anatomy
    if (this._lastId && Math.random() < 0.6) {
      const last = nodes.find(n => n.id === this._lastId);
      if (last) { const nb = this._neighbors(r, last); if (nb.length) node = nb[(Math.random() * nb.length) | 0]; }
    }
    if (!node) node = nodes[(Math.random() * nodes.length) | 0];
    this._pulse(r, node, { minor: !strong });
    this._chain(r, node, strong ? 3 : 1 + ((Math.random() * 2) | 0));
  }

  _wrapNode(node) {
    if (this._wrapN.has(node) || !node || typeof node.render !== 'function') return;
    const orig = node.render, self = this;
    const wrap = function (...a) {
      const res = orig.apply(this, a);
      try { self._applyNode(node); } catch (_) {}
      return res;
    };
    this._wrapN.set(node, { orig, wrap });
    node.render = wrap;
  }

  _wrapLink(link) {
    if (this._wrapL.has(link) || !link || typeof link.render !== 'function') return;
    const orig = link.render, self = this;
    const wrap = function (...a) {
      const res = orig.apply(this, a);
      try { self._applyLink(link); } catch (_) {}
      return res;
    };
    this._wrapL.set(link, { orig, wrap });
    link.render = wrap;
  }

  _applyNode(node) {
    const p = this._pulses.get(node);
    if (!p || !node.circle) return;
    const t = (performance.now() - p.start) / p.dur;
    if (t >= 1) return; // expired — unwrapped on the next raf tick
    const i = this._env(t);
    const base = node.circle.tint == null ? 0xffffff : node.circle.tint;
    // yellow flash decaying into brand purple as the pulse fades
    const fire = this._mix(0xF8F060, 0x7F00DA, Math.min(1, t * 1.35));
    node.circle.tint = this._mix(base, fire, p.minor ? i * 0.75 : i);
    const s = 1 + (p.minor ? 0.9 : 1.8) * i;
    if (node.circle.scale) { node.circle.scale.x *= s; node.circle.scale.y *= s; }
  }

  _applyLink(link) {
    const f = this._links.get(link);
    if (!f || !link.line) return;
    const t = (performance.now() - f.start) / f.dur;
    if (t >= 1) return;
    const i = this._env(t);
    const base = link.line.tint == null ? 0xffffff : link.line.tint;
    link.line.tint = this._mix(base, 0x7F00DA, i);
    link.line.alpha = Math.min(1, (link.line.alpha == null ? 0.4 : link.line.alpha) + 0.8 * i);
  }

  // Smooth attack→decay envelope (smoothstep both ways — no visual snaps).
  _env(t) {
    const atk = 0.14;
    if (t <= 0) return 0;
    if (t < atk) { const a = t / atk; return a * a * (3 - 2 * a); }
    const d = Math.min(1, (t - atk) / (1 - atk));
    return 1 - d * d * (3 - 2 * d);
  }

  _mix(c1, c2, k) {
    k = Math.max(0, Math.min(1, k));
    const r = ((c1 >> 16) & 255) + (((c2 >> 16) & 255) - ((c1 >> 16) & 255)) * k;
    const g = ((c1 >> 8) & 255) + (((c2 >> 8) & 255) - ((c1 >> 8) & 255)) * k;
    const b = (c1 & 255) + ((c2 & 255) - (c1 & 255)) * k;
    return ((r | 0) << 16) | ((g | 0) << 8) | (b | 0);
  }

  // Single raf loop: expire pulses (restoring their wrappers immediately —
  // zero accumulation), then nudge only the affected renderers each frame.
  _run() {
    if (this._raf || this._dead) return;
    const loop = () => {
      this._raf = null;
      if (this._dead) return;
      const now = performance.now();
      let alive = false;
      const rs = new Set();
      for (const [node, p] of this._pulses) {
        if (now - p.start >= p.dur) {
          this._pulses.delete(node);
          const w = this._wrapN.get(node);
          if (w) { try { if (node.render === w.wrap) node.render = w.orig; } catch (_) {} this._wrapN.delete(node); }
          rs.add(p.r); // one settle frame so base styling returns
        } else { alive = true; rs.add(p.r); }
      }
      for (const [link, f] of this._links) {
        if (now - f.start >= f.dur) {
          this._links.delete(link);
          const w = this._wrapL.get(link);
          if (w) { try { if (link.render === w.wrap) link.render = w.orig; } catch (_) {} this._wrapL.delete(link); }
          rs.add(f.r);
        } else { alive = true; rs.add(f.r); }
      }
      for (const r of rs) { try { r.changed(); } catch (_) {} }
      if (alive) this._raf = requestAnimationFrame(loop);
    };
    this._raf = requestAnimationFrame(loop);
  }
}

// ── NoteSynapse — neurons firing inside the note you're reading ──────────────
// SynapseLayer lights the file explorer and GraphSynapse lights the graph view,
// but both are no-ops when those panes are closed — which is most of the time.
// This layer renders thought onto the ACTIVE NOTE pane (always open by
// definition): the note's own structure is the neural tissue — internal links
// and headings are somata; while Ultron thinks, action potentials hop between
// them (yellow #F8F060 flash decaying to brand purple #7F00DA). When the brain
// ACTUALLY Reads the open note (or a note it links to), that soma fires hard —
// real cognition, not pure theater. Same lifecycle as its siblings: armed by
// the ccc-orb-thinking MutationObserver, cleared by every turn's finally block.
// Pointer-events:none, ≤8 live sparks, document.hidden + reduced-motion guards,
// rAF runs only while sparks are alive, canvas tears down ~2.5s after idle.
class NoteSynapse {
  constructor(plugin) {
    this.plugin = plugin;
    this._cv = null; this._host = null; this._anchors = [];
    this._sparks = []; this._raf = null; this._ambTimer = null;
    this._idleTimer = null; this._leafHandler = null; this._lastIdx = -1;
  }

  // Ambient mode — follows the orb's thinking class (same observer as siblings).
  thinking(on) {
    if (on) {
      if (this._ambTimer) return;
      try { if (matchMedia('(prefers-reduced-motion: reduce)').matches) return; } catch (_) {}
      clearTimeout(this._idleTimer);
      this._attach();
      if (!this._leafHandler) {
        // Tony switches notes mid-thought → the tissue follows him.
        this._leafHandler = this.plugin.app.workspace.on('active-leaf-change', () => {
          if (this._ambTimer) this._attach();
        });
      }
      const tick = () => {
        this._ambTimer = setTimeout(tick, 700 + Math.random() * 900);
        if (document.hidden || !this._cv || this._sparks.length >= 8) return;
        this._fire();
      };
      tick();
    } else {
      clearTimeout(this._ambTimer); this._ambTimer = null;
      this._lastIdx = -1;
      clearTimeout(this._idleTimer);
      this._idleTimer = setTimeout(() => this._detach(), 2500); // let trailing sparks decay
    }
  }

  // Called from the brain's stream-json loop on every tool_use block: if the
  // brain touches the open note or one of its anchors, that soma fires hard.
  noteToolUse(name, input) {
    try {
      if (document.hidden || !this._cv) return;
      const raw = input && (input.file_path || input.path || input.notebook_path);
      if (!raw) return;
      const base = String(raw).split('/').pop().replace(/\.md$/i, '').toLowerCase();
      const act = this.plugin.app.workspace.getActiveFile();
      if (act && act.basename.toLowerCase() === base) {
        for (let i = 0; i < Math.min(4, this._anchors.length); i++) this._fire(true); // whole note lights up
        return;
      }
      const idx = this._anchors.findIndex(a => {
        const href = (a.el.getAttribute && (a.el.getAttribute('data-href') || a.el.getAttribute('href'))) || '';
        const txt = (a.el.textContent || '').trim();
        return href.replace(/\.md$/i, '').toLowerCase().endsWith(base) || txt.toLowerCase() === base;
      });
      if (idx >= 0) this._fire(true, idx);
    } catch (_) {}
  }

  _attach() {
    try {
      const leaf = this.plugin.app.workspace.getMostRecentLeaf();
      const view = leaf && leaf.view;
      if (!view || view.getViewType() !== 'markdown') return;
      const host = view.containerEl;
      if (this._cv && this._host === host && this._cv.isConnected) { this._scan(); this._size(); return; }
      this._detach();
      this._host = host;
      const cv = document.createElement('canvas');
      cv.className = 'ccc-notesyn';
      cv.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:60;';
      host.appendChild(cv);
      this._cv = cv;
      this._size(); this._scan();
    } catch (_) {}
  }

  _size() {
    const r = this._host.getBoundingClientRect(), d = window.devicePixelRatio || 1;
    this._cv.width = Math.max(1, r.width * d); this._cv.height = Math.max(1, r.height * d);
    this._cv.getContext('2d').setTransform(d, 0, 0, d, 0, 0);
  }

  // The note's structure IS the tissue: links + headings become somata. Element
  // refs are stored, rects resolved at fire time so scrolling never goes stale.
  _scan() {
    const els = this._host.querySelectorAll(
      'a.internal-link, .cm-hmd-internal-link, .cm-underline, h1, h2, h3, .HyperMD-header'
    );
    this._anchors = [];
    for (const el of els) { this._anchors.push({ el }); if (this._anchors.length >= 40) break; }
  }

  _pt(a) { // anchor → canvas-space point, null when offscreen/disconnected
    if (!a || !a.el || !a.el.isConnected) return null;
    const hr = this._host.getBoundingClientRect(), r = a.el.getBoundingClientRect();
    const x = r.left - hr.left + r.width / 2, y = r.top - hr.top + r.height / 2;
    if (y < 0 || y > hr.height || x < 0 || x > hr.width) return null;
    return { x, y };
  }

  _fire(major, forceIdx) {
    // refresh tissue occasionally — live preview re-renders DOM under us
    if (Math.random() < 0.2 || !this._anchors.length) this._scan();
    const vis = this._anchors.map((a, i) => ({ i, p: this._pt(a) })).filter(v => v.p);
    let from;
    if (forceIdx != null) from = vis.find(v => v.i === forceIdx);
    if (!from && vis.length) {
      // 60% continue from the previous soma — thought propagates, not teleports
      const prev = vis.find(v => v.i === this._lastIdx);
      from = (prev && Math.random() < 0.6) ? prev : vis[(Math.random() * vis.length) | 0];
    }
    if (!from) { // sparse/blank note: free neurons in the margin
      const r = this._host.getBoundingClientRect();
      from = { i: -1, p: { x: 40 + Math.random() * (r.width - 80), y: 60 + Math.random() * (r.height - 120) } };
    }
    let to = null;
    const others = vis.filter(v => v.i !== from.i);
    if (others.length) { // hop to one of the 3 nearest somata
      others.sort((a, b) => (a.p.x - from.p.x) ** 2 + (a.p.y - from.p.y) ** 2 - ((b.p.x - from.p.x) ** 2 + (b.p.y - from.p.y) ** 2));
      to = others[(Math.random() * Math.min(3, others.length)) | 0];
      this._lastIdx = to.i;
    }
    this._sparks.push({ a: from.p, b: to && to.p, t0: performance.now(), major: !!major });
    this._loop();
  }

  _loop() {
    if (this._raf) return;
    const step = () => {
      this._raf = null;
      const cv = this._cv; if (!cv || !cv.isConnected) { this._sparks = []; return; }
      const ctx = cv.getContext('2d');
      const hr = this._host.getBoundingClientRect();
      ctx.clearRect(0, 0, hr.width, hr.height);
      const now = performance.now();
      this._sparks = this._sparks.filter(s => {
        const life = s.major ? 1400 : 1000, t = (now - s.t0) / life;
        if (t >= 1) return false;
        const fade = 1 - t;
        // soma: yellow flash decaying to brand purple
        const R = (s.major ? 9 : 5) * (1 + t * 0.8);
        const g = ctx.createRadialGradient(s.a.x, s.a.y, 0, s.a.x, s.a.y, R * 2.4);
        g.addColorStop(0, `rgba(248,240,96,${0.85 * fade})`);
        g.addColorStop(0.55, `rgba(127,0,218,${0.45 * fade})`);
        g.addColorStop(1, 'rgba(127,0,218,0)');
        ctx.fillStyle = g;
        ctx.beginPath(); ctx.arc(s.a.x, s.a.y, R * 2.4, 0, 7); ctx.fill();
        if (s.b) { // axon: traveling action-potential head
          const h = Math.min(1, t / 0.6);
          const hx = s.a.x + (s.b.x - s.a.x) * h, hy = s.a.y + (s.b.y - s.a.y) * h;
          ctx.strokeStyle = `rgba(127,0,218,${0.35 * fade})`; ctx.lineWidth = 1.2;
          ctx.beginPath(); ctx.moveTo(s.a.x, s.a.y); ctx.lineTo(hx, hy); ctx.stroke();
          ctx.fillStyle = `rgba(248,240,96,${0.9 * fade})`;
          ctx.beginPath(); ctx.arc(hx, hy, 2.4, 0, 7); ctx.fill();
        }
        return true;
      });
      if (this._sparks.length) this._raf = requestAnimationFrame(step);
      else ctx.clearRect(0, 0, hr.width, hr.height);
    };
    this._raf = requestAnimationFrame(step);
  }

  _detach() {
    if (this._raf) { cancelAnimationFrame(this._raf); this._raf = null; }
    if (this._cv) { try { this._cv.remove(); } catch (_) {} }
    this._cv = null; this._host = null; this._anchors = []; this._sparks = [];
  }

  destroy() {
    clearTimeout(this._ambTimer); this._ambTimer = null;
    clearTimeout(this._idleTimer);
    if (this._leafHandler) { try { this.plugin.app.workspace.offref(this._leafHandler); } catch (_) {} this._leafHandler = null; }
    this._detach();
  }
}

// ── ThreatIndex — Machine POV substrate (HS-R2 #2) ───────────────────────────
// One in-memory map: entity path → { level, reasons[] } built from REAL vault
// signals only (metadataCache reverse-links + _brain_api/bid/_open.json), so
// render-time lookups are O(1) and never read files. Levels: healthy | monitor
// | threat. Receipts rule: every non-healthy level must cite evidence in
// reasons[] — if the index can't point at a file/date, it stays healthy.
class ThreatIndex {
  constructor(plugin) {
    this.plugin = plugin;
    this._map = new Map();
    this._bids = [];
    this._timer = null;
    this._building = false;
    this.builtAt = 0;
  }

  start() {
    const app = this.plugin.app;
    // perf-audit-2026-06-10: stagger off the layout-ready storm (orb show + daemons +
    // terminal + selfHeal all fire there); the index can afford a 2.5s late start.
    app.workspace.onLayoutReady(() => setTimeout(() => this.rebuild(), 2500));
    // 'resolved' fires after metadata indexing settles; debounce the storm
    this.plugin.registerEvent(app.metadataCache.on('resolved', () => this._soon()));
    this.plugin.registerInterval(window.setInterval(() => { if (!document.hidden) this.rebuild(); }, 5 * 60 * 1000));
  }

  _soon() {
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this.rebuild(), 30000);
  }

  statusFor(path) { return this._map.get(path) || null; }

  async rebuild() {
    if (this._building) return;
    this._building = true;
    try {
      const app = this.plugin.app;
      const files = app.vault.getMarkdownFiles();
      const SCOPE = ['People/', 'Clients/', '02_Areas/Accounts/'];
      const isEntity = (p) => SCOPE.some(s => p.startsWith(s));
      const noteDate = (p) => {
        const m = p.match(/(\d{4}-\d{2}-\d{2})/);
        if (!m) return null;
        const t = new Date(m[1] + 'T12:00');
        return isNaN(t) ? null : t;
      };
      const today = new Date();
      // 1. last-contact per entity = newest DATED Meetings/ or Daily note linking
      //    to it (filename dates only — mtimes lie under OneDrive sync)
      const lastContact = new Map();
      const rl = app.metadataCache.resolvedLinks || {};
      for (const src of Object.keys(rl)) {
        if (!src.startsWith('Meetings/') && !src.startsWith('02_Areas/Daily/')) continue;
        const d = noteDate(src);
        if (!d) continue;
        for (const tgt of Object.keys(rl[src])) {
          if (!isEntity(tgt)) continue;
          const prev = lastContact.get(tgt);
          if (!prev || d > prev.d) lastContact.set(tgt, { d, src });
        }
      }
      // 2. entity files: frontmatter last_touch beats link inference; poi_snooze
      //    (human override) always wins
      const map = new Map();
      for (const f of files) {
        if (!isEntity(f.path)) continue;
        const fm = (app.metadataCache.getFileCache(f) || {}).frontmatter || {};
        const lc = lastContact.get(f.path);
        let last = lc ? lc.d : null;
        let lastSrc = lc ? lc.src : null;
        if (fm.last_touch) {
          const t = new Date(String(fm.last_touch) + 'T12:00');
          if (!isNaN(t) && (!last || t > last)) { last = t; lastSrc = 'last_touch frontmatter'; }
        }
        let entry;
        if (last) {
          const days = Math.floor((today - last) / 86400000);
          if (days >= 35) entry = { level: 'threat', reasons: [`no contact ${days}d (last: ${lastSrc})`], silenceDays: days };
          else if (days >= 21) entry = { level: 'monitor', reasons: [`no contact ${days}d (last: ${lastSrc})`], silenceDays: days };
          else entry = { level: 'healthy', reasons: [`last contact ${days}d ago (${lastSrc})`], silenceDays: days };
        } else {
          entry = { level: 'healthy', reasons: ['no contact signal found'], silenceDays: null };
        }
        if (fm.poi_snooze) {
          const until = new Date(String(fm.poi_snooze) + 'T23:59');
          if (!isNaN(until) && until >= today) entry = { level: 'healthy', reasons: [`snoozed until ${fm.poi_snooze}`], silenceDays: null };
        }
        map.set(f.path, entry);
      }
      // 3. bids: deadline pressure from the generated endpoint (deadline may be
      //    "" today — then bids simply contribute nothing, no invented urgency)
      try {
        const open = JSON.parse(await app.vault.adapter.read('_brain_api/bid/_open.json'));
        this._bids = (open && open.bids) || [];
      } catch (_) { this._bids = []; }
      for (const b of this._bids) {
        if (!b.deadline) continue;
        const dl = new Date(String(b.deadline) + 'T23:59');
        if (isNaN(dl)) continue;
        const days = Math.ceil((dl - today) / 86400000);
        let level = null;
        if (days <= 7) level = 'threat';
        else if (days <= 14) level = 'monitor';
        if (!level) continue;
        const reason = `${b.bid_id}: deadline in ${days}d (stage ${b.stage || '?'})`;
        for (const f of files) {
          const isBidBrief = b.path && f.path.startsWith(b.path) && /00 - Brief/i.test(f.path);
          const isClientBrief = b.client && f.path === `Clients/${b.client}/_brief.md`;
          if (!isBidBrief && !isClientBrief) continue;
          const cur = map.get(f.path);
          const worse = !cur || cur.level === 'healthy' || (cur.level === 'monitor' && level === 'threat');
          map.set(f.path, {
            level: worse ? level : cur.level,
            reasons: [...(cur && cur.level !== 'healthy' ? cur.reasons : []), reason],
          });
        }
      }
      this._map = map;
      this.builtAt = Date.now();
    } catch (e) {
      console.warn('[CCC] ThreatIndex rebuild failed:', e);
    } finally {
      this._building = false;
    }
  }
}

// ── Diagnostic Chamber (HS-R2 #8) — interrogate any fleet agent in person ────
// Pick one of the 29 SBAP agents; the screen drains to a black analysis room:
// its memory blocks, reputation, stats, and last writes fan out as an
// attribute matrix, and Tony deposes it — a local claude process answers IN
// FIRST PERSON strictly from the agent's own state files. No invented
// capabilities: if the state doesn't hold the answer, the agent must say so.
class AgentPickerModal extends FuzzySuggestModal {
  constructor(app, plugin, agents) {
    super(app);
    this.plugin = plugin;
    this.agents = agents;
    this.setPlaceholder('Which agent goes in the chamber?');
  }
  getItems() { return this.agents; }
  getItemText(a) { return `${a.agent_name} — ${a.status} · ${a.role || a.category || ''}`; }
  onChooseItem(a) { new DiagnosticChamberModal(this.app, this.plugin, a).open(); }
}

class DiagnosticChamberModal extends Modal {
  constructor(app, plugin, agent) {
    super(app);
    this.plugin = plugin;
    this.agent = agent;
    this._busy = false;
  }

  async _readState() {
    const ad = this.app.vault.adapter;
    const dir = `_agent_state/${this.agent.agent_name}`;
    const read = async (f) => { try { return await ad.read(`${dir}/${f}`); } catch (_) { return null; } };
    const mem = await read('memory.json');
    const rep = await read('reputation.json');
    const stats = await read('stats.json');
    const writesRaw = await read('writes.jsonl');
    const writes = writesRaw
      ? writesRaw.split('\n').filter(Boolean).slice(-10).map(l => { try { return JSON.parse(l); } catch (_) { return null; } }).filter(Boolean)
      : [];
    return { mem, rep, stats, writes, dir };
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-chamber');
    const { contentEl } = this;
    contentEl.empty();
    const head = contentEl.createDiv({ cls: 'ccc-chamber-head' });
    head.createEl('div', { cls: 'ccc-chamber-bust', text: '🤖' });
    const ht = head.createDiv();
    ht.createEl('h2', { text: this.agent.agent_name, cls: 'ccc-chamber-name' });
    ht.createEl('div', { cls: 'ccc-chamber-sub', text: `${this.agent.status} · ${this.agent.role || ''} · ${this.agent.schedule || 'no schedule'}` });

    const st = await this._readState();
    this._state = st;
    const matrix = contentEl.createDiv({ cls: 'ccc-chamber-matrix' });
    const cardFor = (title, obj, mapper) => {
      const c = matrix.createDiv({ cls: 'ccc-chamber-attr' });
      c.createEl('div', { cls: 'ccc-chamber-attr-title', text: title });
      const body = c.createDiv({ cls: 'ccc-chamber-attr-body' });
      if (!obj) { body.setText('— no file —'); return; }
      try { mapper(JSON.parse(obj), body); } catch (_) { body.setText(String(obj).slice(0, 300)); }
    };
    cardFor('MEMORY', st.mem, (m, b) => {
      b.createEl('div', { text: `patterns: ${(m.global_patterns || []).length} · learnings: ${(m.recent_learnings || []).length}` });
      for (const l of (m.recent_learnings || []).slice(-3)) b.createEl('div', { cls: 'ccc-chamber-line', text: '· ' + String(typeof l === 'string' ? l : JSON.stringify(l)).slice(0, 110) });
    });
    cardFor('REPUTATION', st.rep, (r, b) => { b.createEl('div', { text: JSON.stringify(r).slice(0, 260) }); });
    cardFor('STATS', st.stats, (s, b) => { b.createEl('div', { text: JSON.stringify(s).slice(0, 260) }); });
    const wc = matrix.createDiv({ cls: 'ccc-chamber-attr' });
    wc.createEl('div', { cls: 'ccc-chamber-attr-title', text: `LAST WRITES (${st.writes.length})` });
    const wb = wc.createDiv({ cls: 'ccc-chamber-attr-body' });
    if (!st.writes.length) wb.setText('— none recorded —');
    for (const w of st.writes.slice(-5)) wb.createEl('div', { cls: 'ccc-chamber-line', text: '· ' + String(w.target_path || w.file || w.ts || JSON.stringify(w)).slice(0, 110) });

    this._log = contentEl.createDiv({ cls: 'ccc-chamber-log' });
    const ask = contentEl.createDiv({ cls: 'ccc-chamber-ask' });
    const input = ask.createEl('input', { type: 'text', placeholder: 'Depose the agent — e.g. "why did you hold the client draft?"' });
    const btn = ask.createEl('button', { text: 'Interrogate' });
    const go = () => { const q = input.value.trim(); if (q && !this._busy) { input.value = ''; this._interrogate(q); } };
    btn.addEventListener('click', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    input.focus();
  }

  _line(role, text) {
    const d = this._log.createDiv({ cls: 'ccc-chamber-turn ccc-chamber-' + role });
    d.setText((role === 'q' ? 'TONY  ' : this.agent.agent_name.toUpperCase() + '  ') + text);
    this._log.scrollTop = this._log.scrollHeight;
    return d;
  }

  async _interrogate(q) {
    this._busy = true;
    this._line('q', q);
    const wait = this._line('a', '…');
    try {
      const st = this._state || await this._readState();
      const clip = (s, n) => (s ? String(s).slice(0, n) : '(missing)');
      const prompt = [
        `You ARE the SBAP agent "${this.agent.agent_name}" (role: ${this.agent.role || 'unknown'}; status: ${this.agent.status}). Tony is interrogating you in a diagnostic chamber.`,
        'Answer in FIRST PERSON, strictly from the STATE below. If the state does not contain the answer, say exactly what is missing — never invent runs, writes, or capabilities. Cite dates/paths from your writes when relevant. 2-6 sentences, no preamble.',
        '', '── STATE ──',
        'memory.json: ' + clip(st.mem, 4000),
        'reputation.json: ' + clip(st.rep, 1200),
        'stats.json: ' + clip(st.stats, 1200),
        'last writes: ' + clip(JSON.stringify(st.writes), 2500),
        '', '── QUESTION ──', q,
      ].join('\n');
      const cp = require('child_process');
      const bin = (this.plugin.orb && this.plugin.orb._claudeBin) ? this.plugin.orb._claudeBin() : 'claude';
      const out = await new Promise((resolve) => {
        cp.execFile(bin, ['-p', prompt], { timeout: 90000, maxBuffer: 1024 * 1024, cwd: this.app.vault.adapter.basePath }, (err, stdout) => resolve(err && !stdout ? null : String(stdout || '').trim()));
      });
      wait.setText((this.agent.agent_name.toUpperCase() + '  ') + (out || '(no answer — claude unavailable or timed out)'));
      if (out && this.plugin.orb && typeof this.plugin.orb.speak === 'function' && this.plugin.settings.chamberSpeaks) {
        try { this.plugin.orb.speak(out); } catch (_) {}
      }
    } catch (e) {
      wait.setText('(interrogation failed: ' + e.message + ')');
    } finally {
      this._busy = false;
    }
  }

  onClose() { this.contentEl.empty(); }
}

// ── Deck X-Ray (HS-R2 #19) — brand-DNA lint straight off the .pptx XML ───────
// Pick any built deck; its slide XML is read via `unzip -p` (a pptx IS a
// zip) and swept for brand violations: non-brand purples (7030A0/800080/…
// when the DNA says 6600AE/7F00DA), committee-speak phrases in the actual
// slide text, and non-Calibri typefaces. Per-slide findings, honest counts.
// v2 (logged in ISSUES) is the always-on-top HUD beside PowerPoint itself.
class DeckXRayPickModal extends FuzzySuggestModal {
  constructor(app, plugin, decks) {
    super(app);
    this.plugin = plugin;
    this.decks = decks;
    this.setPlaceholder('X-ray which deck?');
  }
  getItems() { return this.decks; }
  getItemText(d) { return d.replace(/^.*\/out\//, 'out/'); }
  onChooseItem(d) { new DeckXRayModal(this.app, this.plugin, d).open(); }
}

class DeckXRayModal extends Modal {
  constructor(app, plugin, deckPath) {
    super(app);
    this.plugin = plugin;
    this.deckPath = deckPath;
  }

  _unzip(args) {
    const cp = require('child_process');
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/unzip', args, { timeout: 20000, maxBuffer: 32 * 1024 * 1024 },
        (err, stdout) => resolve(err ? null : String(stdout)));
    });
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-xray');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: '🦴 DECK X-RAY — ' + this.deckPath.split('/').pop() });
    const meta = contentEl.createDiv({ cls: 'ccc-cinema-meta', text: 'exposing the skeleton…' });
    const listing = await this._unzip(['-Z1', this.deckPath]);
    if (!listing) { meta.setText('Could not open the pptx (unzip failed).'); return; }
    const slides = listing.split('\n')
      .filter(l => /^ppt\/slides\/slide\d+\.xml$/.test(l))
      .sort((a, b) => Number(a.match(/\d+/)[0]) - Number(b.match(/\d+/)[0]))
      .slice(0, 40);
    if (!slides.length) { meta.setText('No slides found inside the pptx.'); return; }
    const WRONG_PURPLE = /^(7030A0|800080|663399|8E44AD|9B59B6|6A0DAD)$/i;
    const SPEAK = /\b(synerg\w+|leverag\w+|best[- ]in[- ]class|holistic|win[- ]win|state[- ]of[- ]the[- ]art|cutting[- ]edge|world[- ]class|seamless\w*|paradigm|going forward|circle back|low[- ]hanging fruit)\b/gi;
    const findings = [];
    for (const s of slides) {
      const xml = await this._unzip(['-p', this.deckPath, s]);
      if (!xml) continue;
      const n = Number(s.match(/slide(\d+)\.xml/)[1]);
      const issues = [];
      const colors = [...new Set([...xml.matchAll(/srgbClr val="([0-9A-Fa-f]{6})"/g)].map(m => m[1].toUpperCase()))];
      const wrong = colors.filter(c => WRONG_PURPLE.test(c));
      if (wrong.length) issues.push(`off-brand purple: #${wrong.join(' #')} (DNA says #6600AE / #7F00DA)`);
      const text = [...xml.matchAll(/<a:t>([^<]*)<\/a:t>/g)].map(m => m[1]).join(' ');
      const speak = [...new Set((text.match(SPEAK) || []).map(w => w.toLowerCase()))];
      if (speak.length) issues.push('committee-speak: ' + speak.join(', '));
      const fonts = [...new Set([...xml.matchAll(/typeface="([^"]+)"/g)].map(m => m[1]))]
        .filter(f => f && !/Calibri|Carlito|\+mn|\+mj/i.test(f));
      if (fonts.length) issues.push('non-brand typeface: ' + fonts.join(', '));
      if (issues.length) findings.push({ n, issues });
    }
    meta.setText(`${slides.length} slides scanned · ${findings.length} with violations`);
    const body = contentEl.createDiv({ cls: 'ccc-xray-body' });
    if (!findings.length) {
      body.createEl('p', { cls: 'ccc-empty', text: '🏆 Clean skeleton — no off-brand purples, no committee-speak, brand typefaces only.' });
    } else {
      for (const f of findings) {
        const row = body.createDiv({ cls: 'ccc-xray-row' });
        row.createEl('span', { cls: 'ccc-xray-slide', text: 'slide ' + f.n });
        const ul = row.createDiv();
        for (const i of f.issues) ul.createEl('div', { cls: 'ccc-xray-issue', text: '⚠ ' + i });
      }
    }
    contentEl.createEl('p', { cls: 'ccc-list-meta', text: 'Fix in content.yaml + rebuild — never hand-edit the .pptx. Live HUD beside PowerPoint = v2 (ISSUES).' });
  }

  onClose() { this.contentEl.empty(); }
}

// ── Document Tomography (HS-R2 #3) — MRI mode for an RFP ─────────────────────
// The rfp_pipeline MODEL stage already extracted the strata; this renders
// them as translucent cross-sections you slice through with the scroll
// wheel: OBLIGATIONS (mandatory clauses + why they matter), EVAL CRITERIA
// (weights, inferred flags honest), DATES, REQUIREMENTS, RISK TISSUE
// (compliance-gaps.md). Nothing buried in section 7.4.3 survives unseen.
class TomographyPickModal extends FuzzySuggestModal {
  constructor(app, plugin, bids) {
    super(app);
    this.plugin = plugin;
    this.bids = bids;
    this.setPlaceholder('Scan which bid’s RFP?');
  }
  getItems() { return this.bids; }
  getItemText(b) { return `${b.company || b.bid_id} · ${b.stage}`; }
  onChooseItem(b) { new TomographyModal(this.app, this.plugin, b).open(); }
}

class TomographyModal extends Modal {
  constructor(app, plugin, bid) {
    super(app);
    this.plugin = plugin;
    this.bid = bid;
    this._slice = 0;
    this._layers = [];
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-tomo');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: `🩻 TOMOGRAPHY — ${this.bid.company || this.bid.bid_id}` });
    const ad = this.app.vault.adapter;
    let model = null;
    try { model = JSON.parse(await ad.read(this.bid.path + '/rfp-model.json')); } catch (_) {}
    if (!model) { contentEl.createEl('p', { cls: 'ccc-empty', text: 'No rfp-model.json — run the pipeline MODEL stage first.' }); return; }
    let gaps = '';
    try { gaps = await ad.read(this.bid.path + '/compliance-gaps.md'); } catch (_) {}
    const li = (s) => String(s || '').slice(0, 140);
    this._layers = [
      {
        name: '⚖️ OBLIGATIONS', cls: 'tomo-oblig',
        items: (model.mandatory_clauses || []).map(c => ({ t: li(c.clause || c), sub: li(c.why_it_matters) })),
      },
      {
        name: '🎯 EVAL CRITERIA', cls: 'tomo-crit',
        items: (model.eval_criteria || []).map(c => ({ t: `${li(c.name)} — ${c.weight != null ? c.weight + '%' : '?'}`, sub: c.weight_inferred ? 'weight INFERRED (RFP states none)' : 'weight RFP-stated' })),
      },
      {
        name: '📅 DATES', cls: 'tomo-date',
        items: [
          ...(model.due_date ? [{ t: 'DUE: ' + model.due_date, sub: 'submission deadline' }] : []),
          ...((model.deadlines || []).map(d => ({ t: li(d.date || d.when || JSON.stringify(d)), sub: li(d.what || d.label || '') }))),
        ],
      },
      {
        name: '📋 REQUIREMENTS', cls: 'tomo-req',
        items: (model.requirements || []).map(r => ({ t: li(typeof r === 'string' ? r : r.requirement || JSON.stringify(r)), sub: '' })),
      },
      {
        name: '🔥 RISK TISSUE', cls: 'tomo-risk', evidence: this.bid.path + '/compliance-gaps.md',
        items: gaps.split('\n').filter(l => /^[-*] |^### /.test(l)).slice(0, 14).map(l => ({ t: li(l.replace(/^[-*#]+\s*/, '')), sub: '' })),
      },
    ].filter(L => L.items.length);
    contentEl.createEl('div', { cls: 'ccc-cinema-meta', text: 'scroll to slice through the strata · click a layer to focus it' });
    this._stack = contentEl.createDiv({ cls: 'ccc-tomo-stack' });
    this._render();
    this.modalEl.addEventListener('wheel', (e) => {
      e.preventDefault();
      this._slice = Math.max(0, Math.min(this._layers.length - 1, this._slice + (e.deltaY > 0 ? 1 : -1)));
      this._render();
    }, { passive: false });
  }

  _render() {
    this._stack.empty();
    this._layers.forEach((L, i) => {
      const depth = i - this._slice;
      const panel = this._stack.createDiv({ cls: 'ccc-tomo-layer ' + (depth === 0 ? 'ccc-tomo-active' : '') });
      panel.style.opacity = String(depth === 0 ? 1 : Math.max(0.18, 0.55 - 0.18 * Math.abs(depth)));
      panel.style.filter = depth === 0 ? 'none' : `blur(${Math.min(3, Math.abs(depth))}px)`;
      panel.createEl('div', { cls: 'ccc-tomo-name', text: `${L.name} (${L.items.length})` });
      panel.addEventListener('click', () => { this._slice = i; this._render(); });
      if (depth === 0) {
        const body = panel.createDiv({ cls: 'ccc-tomo-body' });
        for (const it of L.items.slice(0, 14)) {
          const row = body.createDiv({ cls: 'ccc-tomo-item' });
          row.createEl('div', { text: it.t });
          if (it.sub) row.createEl('div', { cls: 'ccc-list-meta', text: it.sub });
        }
        if (L.evidence) {
          const ev = body.createEl('div', { cls: 'ccc-boss-item', text: '↗ open evidence file' });
          ev.addEventListener('click', () => this.app.workspace.openLinkText(L.evidence, '', false));
        }
      }
    });
  }

  onClose() { this.contentEl.empty(); }
}

// ── Secret Doors (HS-R2 #16) — folders that secretly belong together ─────────
// For each open bid, the warm recall daemon (127.0.0.1:7766) is asked what
// else in the vault resonates with it. Strong matches (similarity ≥ 0.45,
// outside the bid's own folder, core dirs only) appear as tiny glowing doors
// under the bid folder in the explorer. Creaking one open mints a real
// bridge note carrying the evidence (path + similarity) and linking both
// sides. Daemon down → no doors, silently. Past wins knock; you answer.
class SecretDoors {
  constructor(plugin) {
    this.plugin = plugin;
    this._doors = new Map(); // bid_path → [{path, score}]
    this._timer = null;
    this._obs = null;
    this._dead = false;
  }

  start() {
    const app = this.plugin.app;
    // perf-audit-2026-06-10: doors query the recall daemon — keep them well clear of the
    // boot storm; 8s-late doors are imperceptible.
    app.workspace.onLayoutReady(() => setTimeout(() => { this._scan().then(() => this._inject()); this._observe(); }, 8000));
    this.plugin.registerInterval(window.setInterval(() => { if (!document.hidden) this._scan().then(() => this._inject()); }, 10 * 60 * 1000));
  }

  destroy() {
    this._dead = true;
    clearTimeout(this._timer);
    if (this._obs) { try { this._obs.disconnect(); } catch (_) {} this._obs = null; }
    document.querySelectorAll('.ccc-door').forEach(e => e.remove());
  }

  _observe() {
    const host = document.querySelector('.nav-files-container');
    if (!host) return;
    this._obs = new MutationObserver((muts) => {
      // resource guard: skip mutations from our own injected rows (see PhantomFiles)
      let relevant = false;
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1 && (n.classList.contains('ccc-door') || n.classList.contains('ccc-phantom'))) continue;
          relevant = true; break;
        }
        if (relevant || m.removedNodes.length) { relevant = true; break; }
      }
      if (!relevant) return;
      clearTimeout(this._timer);
      this._timer = setTimeout(() => this._inject(), 450);
    });
    this._obs.observe(host, { childList: true, subtree: true });
  }

  _retrieve(q) {
    const cp = require('child_process');
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/curl', ['-sf', '--max-time', '2', 'http://127.0.0.1:7766/retrieve?q=' + encodeURIComponent(q) + '&top=8'],
        { timeout: 3000, maxBuffer: 1 << 20 }, (err, stdout) => {
          if (err || !stdout) return resolve(null);
          try { resolve(JSON.parse(stdout)); } catch (_) { resolve(null); }
        });
    });
  }

  async _scan() {
    if (this._dead) return;
    const ad = this.plugin.app.vault.adapter;
    let open = { bids: [] };
    try { open = JSON.parse(await ad.read('_brain_api/bid/_open.json')); } catch (_) { return; }
    const JUNK = /\.pptx\.md$|(^|\/)_templates?(\/|\.md$)|Document Library/i;
    const CORE = /^(RFPs|01_Projects|02_Areas|Clients|Meetings|People|04_Archives|_wiki|Use Cases|Preferences)\//;
    const doors = new Map();
    for (const b of (open.bids || [])) {
      const hits = await this._retrieve(`${b.company || ''} ${b.topic || ''} ${b.client || ''}`.trim());
      if (!Array.isArray(hits)) continue; // daemon cold — no doors for this bid
      const seen = new Set();
      const found = hits
        .filter(h => h.path && (h.score || 0) >= 0.45)
        .filter(h => !h.path.startsWith(b.path + '/') && h.path !== b.path)
        .filter(h => CORE.test(h.path) && !JUNK.test(h.path))
        .filter(h => { const k = h.path; if (seen.has(k)) return false; seen.add(k); return true; })
        .slice(0, 3);
      if (found.length) doors.set(b.path, { bid: b, hits: found });
    }
    this._doors = doors;
  }

  _inject() {
    if (this._dead || !this._doors.size) return;
    const esc = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/"/g, '\\"');
    for (const [bidPath, d] of this._doors) {
      const folderEl = document.querySelector(`.nav-folder-title[data-path="${esc(bidPath)}"]`);
      const children = folderEl && folderEl.parentElement && folderEl.parentElement.querySelector('.nav-folder-children');
      if (!children) continue;
      for (const h of d.hits) {
        const id = (d.bid.bid_id + ':' + h.path).replace(/[^a-z0-9:_-]/gi, '_');
        if (children.querySelector(`[data-door="${id}"]`)) continue;
        const row = document.createElement('div');
        row.className = 'nav-file ccc-door';
        row.setAttribute('data-door', id);
        const title = document.createElement('div');
        title.className = 'nav-file-title ccc-door-title';
        title.textContent = '🚪 ' + (h.path.split('/').pop() || h.path).replace(/\.md$/, '');
        title.setAttribute('aria-label', `secretly related — similarity ${(h.score || 0).toFixed(2)} · ${h.path} · click to open the door`);
        row.appendChild(title);
        title.addEventListener('click', () => this._openDoor(d.bid, h, id));
        children.appendChild(row);
      }
    }
  }

  async _openDoor(bid, h, id) {
    const app = this.plugin.app;
    const slug = (h.path.split('/').pop() || 'link').replace(/\.md$/, '').toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40);
    const path = `${bid.path}/secret-door-${slug}.md`;
    try {
      if (!app.vault.getAbstractFileByPath(path)) {
        await app.vault.create(path, [
          '---', 'type: secret-door', `bid: ${bid.bid_id}`, `similarity: ${(h.score || 0).toFixed(3)}`,
          `created: ${localDateStr(new Date())}`, 'source: recall-daemon (127.0.0.1:7766)', '---', '',
          `# 🚪 ${bid.company || bid.bid_id} ↔ ${h.path.split('/').pop()}`, '',
          `The recall index found these resonating at **${(h.score || 0).toFixed(2)}** similarity:`, '',
          `- this bid: [[${bid.path}/00 - Brief.md|${bid.bid_id}]]`,
          `- the other side: [[${h.path}]]`, '',
          '## Why it matters (fill in or delete)', '- ', '',
        ].join('\n'));
      }
      document.querySelectorAll(`[data-door="${id}"]`).forEach(e => e.remove());
      app.workspace.openLinkText(path, '', false);
      try { const rel = this.plugin.synapse && this.plugin.synapse._relPath(app.vault.adapter.basePath + '/' + path); if (rel) this.plugin.synapse.fireFile(rel, true); } catch (_) {}
      new Notice('🚪 Door opened — bridge note minted with the evidence.', 4000);
    } catch (e) { new Notice('Door jammed: ' + e.message, 4000); }
  }
}

// ── Launch Control (HS-R2 #12) — an Apollo go/no-go poll you can feel ────────
// Pick the bid; six stations run their REAL checks in sequence and flip
// lamps: brief integrity, deadline math, artifact manifest (phantoms=0),
// the live P4 submission DQ gate (rfp_pipeline.py --stage compliance
// --stage submission-gate), confidentiality sweep (off_limits.json names in
// the proposal draft), and the predicted-score floor (scorecard.md). All
// green → write the launch record into the bid folder. Any red → NO-GO,
// with the evidence file one click away. Nothing is ever auto-submitted.
class LaunchPickModal extends FuzzySuggestModal {
  constructor(app, plugin, bids) {
    super(app);
    this.plugin = plugin;
    this.bids = bids;
    this.setPlaceholder('Launch poll for which bid?');
  }
  getItems() { return this.bids; }
  getItemText(b) { return `${b.company || b.bid_id} · ${b.stage}`; }
  onChooseItem(b) { new LaunchControlModal(this.app, this.plugin, b).open(); }
}

class LaunchControlModal extends Modal {
  constructor(app, plugin, bid) {
    super(app);
    this.plugin = plugin;
    this.bid = bid;
    this._abort = false;
    this._results = [];
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-launch');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: `🚀 LAUNCH CONTROL — ${this.bid.company || this.bid.bid_id}` });
    const board = contentEl.createDiv({ cls: 'ccc-launch-board' });
    const foot = contentEl.createDiv({ cls: 'ccc-launch-foot' });
    const abort = foot.createEl('button', { cls: 'ccc-launch-abort', text: '⛔ ABORT' });
    abort.addEventListener('click', () => { this._abort = true; this.close(); });
    const verdict = foot.createEl('span', { cls: 'ccc-launch-verdict', text: 'poll running…' });

    const stations = this._stations();
    const lamps = stations.map(s => {
      const row = board.createDiv({ cls: 'ccc-launch-row' });
      const lamp = row.createEl('span', { cls: 'ccc-launch-lamp', text: '●' });
      row.createEl('span', { cls: 'ccc-launch-name', text: s.name });
      const det = row.createEl('span', { cls: 'ccc-launch-det', text: 'standby' });
      return { row, lamp, det };
    });
    let allGreen = true;
    for (let i = 0; i < stations.length; i++) {
      if (this._abort) return;
      const { lamp, det, row } = lamps[i];
      lamp.classList.add('ccc-launch-running');
      det.setText('checking…');
      let r;
      try { r = await stations[i].run(); }
      catch (e) { r = { ok: false, detail: 'check failed: ' + e.message }; }
      if (this._abort) return;
      lamp.classList.remove('ccc-launch-running');
      lamp.classList.add(r.ok ? 'ccc-launch-go' : 'ccc-launch-nogo');
      det.setText(r.detail.slice(0, 90));
      if (r.evidence) {
        row.classList.add('ccc-boss-item');
        row.addEventListener('click', () => this.app.workspace.openLinkText(r.evidence, '', false));
      }
      this._results.push({ station: stations[i].name, ...r });
      if (!r.ok) allGreen = false;
    }
    verdict.setText(allGreen ? '🟢 GO — board is green' : '🔴 NO-GO — resolve the red stations');
    verdict.classList.add(allGreen ? 'ccc-launch-go-text' : 'ccc-launch-nogo-text');
    if (allGreen) {
      const rec = foot.createEl('button', { cls: 'ccc-aggro-btn', text: '📜 write launch record' });
      rec.addEventListener('click', () => this._record());
    }
  }

  _stations() {
    const app = this.app, bid = this.bid, ad = app.vault.adapter;
    const briefPath = bid.path + '/00 - Brief.md';
    return [
      {
        name: 'BRIEF INTEGRITY', run: async () => {
          const f = app.vault.getAbstractFileByPath(briefPath);
          if (!f) return { ok: false, detail: '00 - Brief.md missing' };
          const fm = (app.metadataCache.getFileCache(f) || {}).frontmatter || {};
          const missing = ['stage', 'client', 'deadline'].filter(k => !fm[k]);
          return missing.length
            ? { ok: false, detail: 'frontmatter missing: ' + missing.join(', '), evidence: briefPath }
            : { ok: true, detail: `stage=${fm.stage} client=${fm.client}`, evidence: briefPath };
        },
      },
      {
        name: 'DEADLINE MATH', run: async () => {
          const f = app.vault.getAbstractFileByPath(briefPath);
          const fm = f ? ((app.metadataCache.getFileCache(f) || {}).frontmatter || {}) : {};
          const dl = fm.deadline ? new Date(String(fm.deadline) + 'T23:59') : null;
          if (!dl || isNaN(dl)) return { ok: false, detail: 'no parseable deadline in brief' };
          const days = Math.ceil((dl - new Date()) / 86400000);
          return days >= 0 ? { ok: true, detail: `${days}d remaining` } : { ok: false, detail: `OVERDUE by ${-days}d` };
        },
      },
      {
        name: 'ARTIFACT MANIFEST', run: async () => {
          try {
            const m = JSON.parse(await ad.read(`_brain_api/bid/${bid.bid_id}/phantoms.json`));
            return m.phantoms.length === 0
              ? { ok: true, detail: 'no missing winning-bid artifacts' }
              : { ok: false, detail: `${m.phantoms.length} phantom(s): ` + m.phantoms.map(p => p.artifact).join(', ') };
          } catch (_) { return { ok: false, detail: 'phantoms.json not built (run brain-refresh)' }; }
        },
      },
      {
        name: 'DQ GATE (live)', run: () => new Promise((resolve) => {
          const cp = require('child_process');
          cp.execFile('python3', ['build/tools/rfp_pipeline.py', bid.path, '--stage', 'compliance', '--stage', 'submission-gate', '--skip-ingest'],
            { cwd: ad.basePath, timeout: 240000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
              const out = String(stdout || '') + String(stderr || '');
              if (/SUBMISSION GATE — PASS|GATE.*PASS/i.test(out)) resolve({ ok: true, detail: 'gate PASS', evidence: bid.path + '/compliance-gaps.md' });
              else if (/BLOCKED/i.test(out)) resolve({ ok: false, detail: 'gate BLOCKED — disqualifying clause unresolved', evidence: bid.path + '/compliance-gaps.md' });
              else resolve({ ok: false, detail: err ? 'gate run failed: ' + (err.message || 'error').slice(0, 60) : 'gate verdict unreadable', evidence: bid.path + '/compliance-gaps.md' });
            });
        }),
      },
      {
        name: 'CONFIDENTIALITY', run: async () => {
          let names = [];
          try {
            const ol = JSON.parse(await ad.read('99_Meta/config/confidentiality/off_limits.json'));
            for (const e of (ol.entries || [])) names.push(e.client_name, ...(e.aliases || []));
          } catch (_) { return { ok: false, detail: 'off_limits.json unreadable' }; }
          let body = '';
          for (const cand of ['02 - Proposal Draft.md', 'executive-summary.md']) {
            try { body += '\n' + await ad.read(bid.path + '/' + cand); } catch (_) {}
          }
          if (!body.trim()) return { ok: false, detail: 'no proposal draft to scan' };
          const hits = [...new Set(names.filter(n => n && new RegExp('\\b' + n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b', 'i').test(body)))];
          return hits.length
            ? { ok: false, detail: 'off-limits names in draft: ' + hits.join(', ') }
            : { ok: true, detail: 'no off-limits names in draft' };
        },
      },
      {
        name: 'SCORE FLOOR', run: async () => {
          try {
            const sc = await ad.read(bid.path + '/scorecard.md');
            const m = sc.match(/(\d{1,3})\s*\/\s*100/);
            if (!m) return { ok: false, detail: 'scorecard has no /100 prediction', evidence: bid.path + '/scorecard.md' };
            const n = Number(m[1]);
            return { ok: n >= 60, detail: `predicted ${n}/100 (floor 60)`, evidence: bid.path + '/scorecard.md' };
          } catch (_) { return { ok: false, detail: 'scorecard.md missing — run SCORE stage' }; }
        },
      },
    ];
  }

  async _record() {
    const today = localDateStr(new Date());
    const path = `${this.bid.path}/launch-record-${today}.md`;
    const lines = [
      '---', 'type: launch-record', `bid_id: ${this.bid.bid_id}`, `date: ${today}`, 'verdict: GO', '---', '',
      `# Launch record — ${this.bid.company || this.bid.bid_id} (${today})`, '',
      ...this._results.map(r => `- ${r.ok ? '🟢' : '🔴'} **${r.station}** — ${r.detail}`),
      '', '_All stations green at poll time. Submission remains a human action._',
    ];
    try {
      const existing = this.app.vault.getAbstractFileByPath(path);
      if (existing) await this.app.vault.modify(existing, lines.join('\n'));
      else await this.app.vault.create(path, lines.join('\n'));
      this.app.workspace.openLinkText(path, '', false);
      new Notice('📜 Launch record written — the board was green.', 5000);
      this.close();
    } catch (e) { new Notice('Record failed: ' + e.message, 5000); }
  }

  onClose() { this._abort = true; this.contentEl.empty(); }
}

// ── Sparring Chamber (HS-R2 #20) — rehearse against the client, full contact ─
// Pick the client; the room goes dark and a presence built from their REAL
// corpus (account brief + recent meeting recaps + People/ notes) objects the
// way that client actually objects — voiced through the orb's TTS when
// enabled. END BOUT gets a telemetry card: which answers were thin, which
// objection went unanswered. Persona is corpus-grounded: no invented facts
// about the client; style inferred from their own documents.
class SparringPickModal extends FuzzySuggestModal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this.setPlaceholder('Spar against which client?');
  }
  getItems() {
    const set = new Set();
    for (const f of this.app.vault.getMarkdownFiles()) {
      const m = f.path.match(/^Clients\/([^/]+)\//) || f.path.match(/^02_Areas\/Accounts\/([^/]+)\//);
      if (m && !m[1].startsWith('_')) set.add(m[1]);
    }
    return [...set].sort();
  }
  getItemText(c) { return c; }
  onChooseItem(c) { new SparringModal(this.app, this.plugin, c).open(); }
}

class SparringModal extends Modal {
  constructor(app, plugin, client) {
    super(app);
    this.plugin = plugin;
    this.client = client;
    this._rounds = []; // {tony, client}
    this._busy = false;
    this._voice = true;
  }

  async _corpus() {
    if (this._ctx) return this._ctx;
    const ad = this.app.vault.adapter;
    const parts = [];
    for (const p of [`Clients/${this.client}/_brief.md`, `02_Areas/Accounts/${this.client}/_brief.md`]) {
      try { parts.push('ACCOUNT BRIEF:\n' + (await ad.read(p)).slice(0, 5000)); break; } catch (_) {}
    }
    try { parts.push('API BRIEF: ' + (await ad.read(`_brain_api/account/${this.client.toLowerCase()}/brief.json`)).slice(0, 3000)); } catch (_) {}
    const lc = this.client.toLowerCase();
    const recaps = this.app.vault.getMarkdownFiles()
      .filter(f => (f.path.startsWith('Meetings/') || f.path.startsWith('People/')) && f.path.toLowerCase().includes(lc))
      .sort((a, b) => b.path.localeCompare(a.path)).slice(0, 3);
    for (const f of recaps) {
      try { parts.push(`NOTE ${f.path}:\n` + (await this.app.vault.cachedRead(f)).slice(0, 3000)); } catch (_) {}
    }
    this._ctx = parts.join('\n\n') || '(no corpus found — generic procurement skeptic)';
    return this._ctx;
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-chamber'); // same dark room as diagnostics
    const { contentEl } = this;
    contentEl.empty();
    const head = contentEl.createDiv({ cls: 'ccc-chamber-head' });
    head.createEl('div', { cls: 'ccc-chamber-bust', text: '🥊' });
    const ht = head.createDiv();
    ht.createEl('h2', { text: `Sparring — ${this.client}`, cls: 'ccc-chamber-name' });
    ht.createEl('div', { cls: 'ccc-chamber-sub', text: 'they object the way they actually object · END BOUT for telemetry' });
    this._log = contentEl.createDiv({ cls: 'ccc-chamber-log' });
    const ask = contentEl.createDiv({ cls: 'ccc-chamber-ask' });
    const input = ask.createEl('input', { type: 'text', placeholder: 'Open your pitch — they will hit back…' });
    const swing = ask.createEl('button', { text: 'Swing' });
    const voiceBtn = ask.createEl('button', { text: '🔊' });
    voiceBtn.addEventListener('click', () => { this._voice = !this._voice; voiceBtn.setText(this._voice ? '🔊' : '🔇'); });
    const end = ask.createEl('button', { text: 'END BOUT' });
    const go = () => { const q = input.value.trim(); if (q && !this._busy) { input.value = ''; this._round(q); } };
    swing.addEventListener('click', go);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
    end.addEventListener('click', () => this._telemetry());
    input.focus();
  }

  _line(role, text) {
    const d = this._log.createDiv({ cls: 'ccc-chamber-turn ccc-chamber-' + (role === 'tony' ? 'q' : 'a') });
    d.setText((role === 'tony' ? 'TONY  ' : this.client.toUpperCase() + '  ') + text);
    this._log.scrollTop = this._log.scrollHeight;
    return d;
  }

  async _claude(prompt) {
    const cp = require('child_process');
    const bin = (this.plugin.orb && this.plugin.orb._claudeBin) ? this.plugin.orb._claudeBin() : 'claude';
    return new Promise((resolve) => {
      cp.execFile(bin, ['-p', prompt], { timeout: 90000, maxBuffer: 1024 * 1024, cwd: this.app.vault.adapter.basePath },
        (err, stdout) => resolve(err && !stdout ? null : String(stdout || '').trim()));
    });
  }

  async _round(pitch) {
    this._busy = true;
    this._line('tony', pitch);
    const wait = this._line('client', '…');
    try {
      const ctx = await this._corpus();
      const history = this._rounds.map(r => `TONY: ${r.tony}\n${this.client.toUpperCase()}: ${r.client}`).join('\n');
      const out = await this._claude([
        `You are the client "${this.client}" in a sales meeting rehearsal. Stay in character: a sharp, busy decision-maker. Ground every objection in the CORPUS below (their stated constraints, history, vocabulary) — if the corpus is silent on something, object the way any tough procurement lead would, but NEVER invent specific facts about this client.`,
        'Reply with ONE objection or pushback, 1-3 sentences, spoken language, no preamble, no stage directions.',
        '', '── CORPUS ──', ctx,
        '', '── BOUT SO FAR ──', history || '(opening)',
        '', 'TONY JUST SAID: ' + pitch,
      ].join('\n'));
      const reply = out || '(the client stays silent — claude unavailable)';
      wait.setText(this.client.toUpperCase() + '  ' + reply);
      this._rounds.push({ tony: pitch, client: reply });
      if (out && this._voice && this.plugin.orb && typeof this.plugin.orb.speak === 'function') {
        try { this.plugin.orb.speak(reply); } catch (_) {}
      }
    } finally { this._busy = false; }
  }

  async _telemetry() {
    if (!this._rounds.length) { new Notice('No rounds fought yet.', 2500); return; }
    const wait = this._line('client', '— BOUT OVER · scoring… —');
    const out = await this._claude([
      'Score this sales-rehearsal bout. Output exactly 4 lines, plain text:',
      'LANDED: the one answer of Tony\'s that worked best (quote a fragment)',
      'THIN: the answer that was weakest and why (one sentence)',
      'UNANSWERED: the objection Tony never actually addressed (or "none")',
      'NEXT REP: the single thing to drill before the real meeting',
      '', '── TRANSCRIPT ──',
      this._rounds.map(r => `TONY: ${r.tony}\n${this.client.toUpperCase()}: ${r.client}`).join('\n'),
    ].join('\n'));
    wait.setText('📊 ' + (out || '(scoring unavailable)'));
  }

  onClose() { this.contentEl.empty(); }
}

// ── Loadout Screen (HS-R2 #18) — meeting prep as a 30-second draft pick ──────
// Pick the client; the vault lays out your gear as inventory cards: the
// account brief, canonical blocks (legendary gold when they carry real
// performance data), and the client's recent meeting recaps. Equip up to 4 —
// they get pinned into a loadout note in Meetings/prep/ that Ultron's recall
// indexes, so the gear is in his context when you walk into the room.
// (M365 calendar is tenant-blocked — trigger is manual until IT unblocks.)
class LoadoutModal extends FuzzySuggestModal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this.setPlaceholder('Loadout for which client?');
  }
  getItems() {
    const set = new Set();
    for (const f of this.app.vault.getMarkdownFiles()) {
      const m = f.path.match(/^Clients\/([^/]+)\//) || f.path.match(/^02_Areas\/Accounts\/([^/]+)\//);
      if (m && !m[1].startsWith('_')) set.add(m[1]);
    }
    return [...set].sort();
  }
  getItemText(c) { return c; }
  onChooseItem(c) { new LoadoutScreenModal(this.app, this.plugin, c).open(); }
}

class LoadoutScreenModal extends Modal {
  constructor(app, plugin, client) {
    super(app);
    this.plugin = plugin;
    this.client = client;
    this._equipped = [];
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-loadout');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: `🎒 LOADOUT — ${this.client}` });
    const ad = this.app.vault.adapter;
    const items = [];
    for (const p of [`Clients/${this.client}/_brief.md`, `02_Areas/Accounts/${this.client}/_brief.md`]) {
      if (this.app.vault.getAbstractFileByPath(p)) items.push({ kind: 'brief', name: 'Account brief', path: p, rarity: 'rare' });
    }
    try {
      const types = (await ad.list('_brain_api/canonical')).folders || [];
      for (const t of types) {
        for (const f of ((await ad.list(t)).files || []).filter(f => f.endsWith('.json'))) {
          try {
            const b = JSON.parse(await ad.read(f));
            const perf = b.performance && Object.keys(b.performance).length ? b.performance : null;
            items.push({ kind: 'block', name: b.title, path: f, rarity: perf ? 'legendary' : 'common', meta: perf ? JSON.stringify(perf).slice(0, 60) : 'unproven' });
          } catch (_) {}
        }
      }
    } catch (_) {}
    const lc = this.client.toLowerCase();
    const recaps = this.app.vault.getMarkdownFiles()
      .filter(f => f.path.startsWith('Meetings/') && f.path.toLowerCase().includes(lc))
      .sort((a, b) => b.path.localeCompare(a.path)).slice(0, 5);
    for (const f of recaps) items.push({ kind: 'recap', name: f.basename, path: f.path, rarity: 'common' });
    if (!items.length) { contentEl.createEl('p', { cls: 'ccc-empty', text: 'No gear found for this client yet.' }); return; }

    const grid = contentEl.createDiv({ cls: 'ccc-loadout-grid' });
    const slots = contentEl.createDiv({ cls: 'ccc-loadout-slots' });
    const renderSlots = () => {
      slots.empty();
      slots.createEl('span', { cls: 'ccc-forge-label', text: `EQUIPPED ${this._equipped.length}/4` });
      for (const e of this._equipped) slots.createEl('span', { cls: 'ccc-loadout-chip', text: e.name.slice(0, 28) });
    };
    renderSlots();
    for (const it of items) {
      const cardEl = grid.createDiv({ cls: `ccc-loadout-card ccc-rarity-${it.rarity}` });
      cardEl.createEl('div', { cls: 'ccc-loadout-kind', text: it.kind.toUpperCase() + (it.rarity === 'legendary' ? ' ★' : '') });
      cardEl.createEl('div', { cls: 'ccc-loadout-name', text: it.name });
      if (it.meta) cardEl.createEl('div', { cls: 'ccc-list-meta', text: it.meta });
      cardEl.addEventListener('click', () => {
        const i = this._equipped.indexOf(it);
        if (i >= 0) { this._equipped.splice(i, 1); cardEl.classList.remove('ccc-loadout-on'); }
        else if (this._equipped.length < 4) { this._equipped.push(it); cardEl.classList.add('ccc-loadout-on'); }
        else new Notice('4 slots — unequip something first.', 2500);
        renderSlots();
      });
    }
    const go = contentEl.createEl('button', { cls: 'ccc-forge-anvil', text: '🎒 LOCK LOADOUT → prep note' });
    go.addEventListener('click', async () => {
      if (!this._equipped.length) { new Notice('Equip at least one card.', 2500); return; }
      const today = localDateStr(new Date());
      const path = `Meetings/prep/${today}-${this.client.toLowerCase().replace(/[^a-z0-9]+/g, '-')}-loadout.md`;
      const lines = [
        '---', 'type: meeting-loadout', `client: ${this.client}`, `created: ${today}`, 'tags: [loadout, prep]', '---', '',
        `# Loadout — ${this.client} (${today})`, '',
      ];
      for (const e of this._equipped) {
        lines.push(`## ${e.name}`);
        if (e.kind === 'block') {
          try { const b = JSON.parse(await ad.read(e.path)); lines.push(String(b.body).slice(0, 1500)); } catch (_) { lines.push(`(unreadable: ${e.path})`); }
        } else {
          lines.push(`![[${e.path}]]`);
        }
        lines.push('');
      }
      try {
        try { await this.app.vault.createFolder('Meetings/prep'); } catch (_) {}
        const existing = this.app.vault.getAbstractFileByPath(path);
        if (existing) await this.app.vault.modify(existing, lines.join('\n'));
        else await this.app.vault.create(path, lines.join('\n'));
        this.app.workspace.openLinkText(path, '', false);
        try { const rel = this.plugin.synapse && this.plugin.synapse._relPath(this.app.vault.adapter.basePath + '/' + path); if (rel) this.plugin.synapse.fireFile(rel, true); } catch (_) {}
        new Notice('🎒 Loadout locked — gear pinned to ' + path, 5000);
        this.close();
      } catch (e) { new Notice('Loadout failed: ' + e.message, 5000); }
    });
  }

  onClose() { this.contentEl.empty(); }
}

// ── The Forge (HS-R2 #17) — win themes are crafted, not written ──────────────
// Three ingredients on the anvil: a client pain point, a firm
// differentiator, and a PROOF picked from the canonical block library
// (real evidence only — the composer may not cite anything outside the
// chosen block). Purple sparks, then a provenance-stamped win-theme block
// lands in 00_Inbox via the SBAP path (triage holds it — client-facing).
class ForgeModal extends Modal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this._blocks = [];
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-forge');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: '⚒️ THE FORGE — craft a win theme' });
    // load the real proof inventory
    try {
      const ad = this.app.vault.adapter;
      const types = (await ad.list('_brain_api/canonical')).folders || [];
      for (const t of types) {
        for (const f of ((await ad.list(t)).files || []).filter(f => f.endsWith('.json'))) {
          try {
            const b = JSON.parse(await ad.read(f));
            if (b.title && b.body) this._blocks.push(b);
          } catch (_) {}
        }
      }
    } catch (_) {}
    const mk = (label, ph) => {
      const d = contentEl.createDiv({ cls: 'ccc-forge-slot' });
      d.createEl('div', { cls: 'ccc-forge-label', text: label });
      const i = d.createEl('input', { type: 'text', placeholder: ph });
      return i;
    };
    const pain = mk('🩸 CLIENT PAIN POINT', 'e.g. SAP AMS costs grow 12%/yr while ticket quality drops');
    const diff = mk('💎 YOUR DIFFERENTIATOR', 'e.g. outcome-committed pricing with validated payback');
    const slot = contentEl.createDiv({ cls: 'ccc-forge-slot' });
    slot.createEl('div', { cls: 'ccc-forge-label', text: `🏆 PROOF (canonical library — ${this._blocks.length} blocks)` });
    const sel = slot.createEl('select');
    sel.createEl('option', { text: '— forge without proof (visibly weaker) —', value: '' });
    this._blocks.forEach((b, i) => sel.createEl('option', { text: `${b.type}: ${b.title}`.slice(0, 70), value: String(i) }));
    const anvil = contentEl.createEl('button', { cls: 'ccc-forge-anvil', text: '⚒️ STRIKE' });
    const out = contentEl.createDiv({ cls: 'ccc-forge-out' });
    anvil.addEventListener('click', async () => {
      if (!pain.value.trim() || !diff.value.trim()) { new Notice('The anvil needs both pain and differentiator.', 3000); return; }
      anvil.classList.add('ccc-forge-striking');
      anvil.setText('⚒️ forging…');
      out.empty();
      const proof = sel.value !== '' ? this._blocks[Number(sel.value)] : null;
      const prompt = [
        'Forge ONE win-theme block for a proposal from exactly these ingredients:',
        'PAIN: ' + pain.value.trim(),
        'DIFFERENTIATOR: ' + diff.value.trim(),
        proof ? 'PROOF BLOCK (cite ONLY from this, with its key): ' + JSON.stringify({ key: proof.key, title: proof.title, body: String(proof.body).slice(0, 3000), evidence: proof.evidence }) : 'PROOF: none provided — write the theme WITHOUT invented evidence; mark the proof line "[PROOF NEEDED]".',
        'Output format (plain text): EYEBROW (3-5 words caps) / CLAIM (one sharp sentence tying pain to differentiator) / PROOF (one sentence citing the block key, or [PROOF NEEDED]) / SO-WHAT (one sentence of buyer value). Max 90 words total. No preamble.',
      ].join('\n');
      const cp = require('child_process');
      const bin = (this.plugin.orb && this.plugin.orb._claudeBin) ? this.plugin.orb._claudeBin() : 'claude';
      const text = await new Promise((resolve) => {
        cp.execFile(bin, ['-p', prompt], { timeout: 90000, maxBuffer: 1024 * 1024, cwd: this.app.vault.adapter.basePath },
          (err, stdout) => resolve(err && !stdout ? null : String(stdout || '').trim()));
      });
      anvil.classList.remove('ccc-forge-striking');
      anvil.setText('⚒️ STRIKE');
      if (!text) { out.createEl('p', { text: '(forge cold — claude unavailable)' }); return; }
      out.createEl('pre', { cls: 'ccc-forge-block', text });
      const land = out.createEl('button', { cls: 'ccc-aggro-btn', text: '📥 land in inbox (triage holds it)' });
      land.addEventListener('click', async () => {
        const body = text + '\n\n---\nforged from: pain="' + pain.value.trim() + '" · differentiator="' + diff.value.trim() + '" · proof=' + (proof ? proof.key : 'NONE');
        const r = await this.plugin.orb._inboxNote({ title: 'win-theme: ' + pain.value.trim().slice(0, 40), body, output_type: 'proposal_draft' });
        new Notice(r.ok ? '⚒️ Forged block landed in 00_Inbox/from-dust/ultron — triage will hold it for review.' : 'Landing failed SBAP validation.', 5000);
        if (r.ok) this.close();
      });
    });
  }

  onClose() { this.contentEl.empty(); }
}

// ── Vault CCTV (HS-R2 #10) — scrub the last 24h of every mutation ───────────
// A security-room dial over a floor-plan of the top-level folders. Events are
// real: git commits (file-level, authored), dust triage log entries (agent
// writes), and raw file mtimes (uncommitted/sync churn). Scrub the dial and
// watch the night replay — the folder boxes flash as their files mutate.
class VaultCCTVModal extends Modal {
  constructor(app, plugin) {
    super(app);
    this.plugin = plugin;
    this._events = [];
    this._playing = null;
  }

  _git(args) {
    const cp = require('child_process');
    return new Promise((resolve) => {
      cp.execFile('git', args, { cwd: this.app.vault.adapter.basePath, timeout: 20000, maxBuffer: 16 * 1024 * 1024 },
        (err, stdout) => resolve(err ? null : String(stdout)));
    });
  }

  async _gather() {
    const now = Date.now(), dayAgo = now - 86400000;
    const ev = [];
    // 1. git commits, file-level
    const log = await this._git(['log', '--since=24.hours', '--format=@%ct%x09%an', '--name-only']);
    if (log) {
      let t = 0, an = '';
      for (const line of log.split('\n')) {
        if (line.startsWith('@')) { const [ct, a] = line.slice(1).split('\t'); t = Number(ct) * 1000; an = a; }
        else if (line.trim()) ev.push({ t, path: line.trim(), kind: 'commit', who: an });
      }
    }
    // 2. dust triage events
    try {
      const raw = await this.app.vault.adapter.read('99_Meta/dust-write-log.md');
      for (const l of raw.split('\n')) {
        const m = l.match(/^- (\S+): (\S+) ([^/\s]+)\/(\S+)/);
        if (!m) continue;
        const t = Date.parse(m[1]);
        if (!t || t < dayAgo) continue;
        ev.push({ t, path: '00_Inbox/from-dust/' + m[3] + '/' + m[4], kind: 'agent', who: m[3] + ' (' + m[2] + ')' });
      }
    } catch (_) {}
    // 3. raw mtime churn (uncommitted edits / sync writes), markdown only
    const committed = new Set(ev.filter(e => e.kind === 'commit').map(e => e.path));
    for (const f of this.app.vault.getMarkdownFiles()) {
      const mt = f.stat && f.stat.mtime;
      if (mt && mt > dayAgo && !committed.has(f.path)) ev.push({ t: mt, path: f.path, kind: 'touch', who: 'mtime' });
    }
    ev.sort((a, b) => a.t - b.t);
    this._events = ev;
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-cctv');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: '📹 VAULT CCTV — last 24h', cls: 'ccc-cinema-title' });
    const meta = contentEl.createDiv({ cls: 'ccc-cinema-meta', text: 'pulling tapes…' });
    await this._gather();
    if (!this._events.length) { meta.setText('No recorded mutations in the last 24h.'); return; }
    const dirs = [...new Set(this._events.map(e => e.path.split('/')[0]))].sort();
    const floor = contentEl.createDiv({ cls: 'ccc-cctv-floor' });
    const boxes = new Map();
    for (const d of dirs) {
      const b = floor.createDiv({ cls: 'ccc-cctv-box' });
      b.createEl('div', { cls: 'ccc-cctv-box-name', text: d.slice(0, 18) });
      b.createEl('div', { cls: 'ccc-cctv-box-n', text: '' });
      boxes.set(d, b);
    }
    const feed = contentEl.createDiv({ cls: 'ccc-cctv-feed' });
    const bar = contentEl.createDiv({ cls: 'ccc-cinema-bar' });
    const slider = bar.createEl('input', { type: 'range' });
    slider.min = '0'; slider.max = '1440'; slider.value = '1440'; // minutes across 24h
    const play = bar.createEl('button', { cls: 'ccc-aggro-btn', text: '▶ replay night' });
    const t0 = Date.now() - 86400000;
    const KIND_CLS = { commit: 'ccc-cctv-commit', agent: 'ccc-cctv-agent', touch: 'ccc-cctv-touch' };
    const show = (min) => {
      const upto = t0 + min * 60000, windowLo = upto - 25 * 60000;
      for (const [, b] of boxes) b.classList.remove('ccc-cctv-hot', 'ccc-cctv-commit', 'ccc-cctv-agent', 'ccc-cctv-touch');
      const counts = new Map();
      const active = [];
      for (const e of this._events) {
        if (e.t > upto) break;
        const d = e.path.split('/')[0];
        counts.set(d, (counts.get(d) || 0) + 1);
        if (e.t >= windowLo) active.push(e);
      }
      for (const [d, b] of boxes) b.querySelector('.ccc-cctv-box-n').setText(String(counts.get(d) || 0));
      for (const e of active.slice(-12)) {
        const b = boxes.get(e.path.split('/')[0]);
        if (b) b.classList.add('ccc-cctv-hot', KIND_CLS[e.kind]);
      }
      feed.empty();
      meta.setText(`${new Date(upto).toLocaleTimeString()} · ${this._events.filter(e => e.t <= upto).length}/${this._events.length} events`);
      for (const e of active.slice(-7).reverse()) {
        feed.createEl('div', {
          cls: 'ccc-cctv-line',
          text: `${new Date(e.t).toLocaleTimeString().slice(0, 5)} ${e.kind === 'commit' ? '🟣' : e.kind === 'agent' ? '🟢' : '🔵'} ${e.path.slice(0, 64)} — ${e.who}`,
        });
      }
    };
    slider.addEventListener('input', () => { this._stop(); show(Number(slider.value)); });
    play.addEventListener('click', () => {
      this._stop();
      let m = 0;
      slider.value = '0';
      this._playing = setInterval(() => {
        m += 12; // 24h replays in ~12s
        if (m >= 1440) { m = 1440; this._stop(); }
        slider.value = String(m);
        show(m);
      }, 100);
    });
    show(1440);
  }

  _stop() { if (this._playing) { clearInterval(this._playing); this._playing = null; } }
  onClose() { this._stop(); this.contentEl.empty(); }
}

// ── Time-Scrub Cinema (HS-R2 #4) — a note becomes a film of itself ───────────
// Hold the scrubber and drag through the note's entire git history: text
// morphs between versions, lines that appeared in each version flash heat.
// Answers "who changed the pricing paragraph and when" in seconds.
class TimeScrubModal extends Modal {
  constructor(app, plugin, file) {
    super(app);
    this.plugin = plugin;
    this.file = file;
    this._versions = []; // {sha, date, author, subject}
    this._cache = new Map(); // sha → content
  }

  _git(args) {
    const cp = require('child_process');
    return new Promise((resolve) => {
      cp.execFile('git', args, { cwd: this.app.vault.adapter.basePath, timeout: 15000, maxBuffer: 16 * 1024 * 1024 },
        (err, stdout) => resolve(err ? null : String(stdout)));
    });
  }

  async onOpen() {
    this.modalEl.classList.add('ccc-cinema');
    const { contentEl } = this;
    contentEl.empty();
    contentEl.createEl('h3', { text: '🎞 ' + this.file.path, cls: 'ccc-cinema-title' });
    const meta = contentEl.createDiv({ cls: 'ccc-cinema-meta', text: 'loading history…' });
    const pre = contentEl.createEl('pre', { cls: 'ccc-cinema-pre' });
    const log = await this._git(['log', '--follow', '--format=%H%x09%ad%x09%an%x09%s', '--date=short', '-40', '--', this.file.path]);
    if (!log || !log.trim()) {
      meta.setText('No git history for this file (not committed yet, or outside the repo).');
      return;
    }
    this._versions = log.trim().split('\n').map(l => {
      const [sha, date, author, subject] = l.split('\t');
      return { sha, date, author, subject };
    }).reverse(); // oldest → newest
    this._versions.push({ sha: null, date: 'now', author: 'working tree', subject: '(uncommitted state)' });

    const bar = contentEl.createDiv({ cls: 'ccc-cinema-bar' });
    const slider = bar.createEl('input', { type: 'range' });
    slider.min = '0';
    slider.max = String(this._versions.length - 1);
    slider.value = slider.max;
    const show = async (idx) => {
      const v = this._versions[idx];
      const content = await this._content(v);
      const prev = idx > 0 ? await this._content(this._versions[idx - 1]) : '';
      meta.setText(`${idx + 1}/${this._versions.length} · ${v.date} · ${v.author} — ${v.subject}`);
      this._render(pre, content, prev);
    };
    slider.addEventListener('input', () => show(Number(slider.value)));
    await show(this._versions.length - 1);
  }

  async _content(v) {
    const key = v.sha || '__wt__';
    if (this._cache.has(key)) return this._cache.get(key);
    let c;
    if (!v.sha) { try { c = await this.app.vault.adapter.read(this.file.path); } catch (_) { c = ''; } }
    else c = (await this._git(['show', `${v.sha}:${this.file.path}`])) || '';
    this._cache.set(key, c);
    return c;
  }

  _render(pre, content, prevContent) {
    pre.empty();
    const prevSet = new Set((prevContent || '').split('\n'));
    const lines = content.split('\n').slice(0, 800); // cinema cap — huge notes stay responsive
    for (const ln of lines) {
      const div = pre.createDiv({ cls: 'ccc-cinema-line', text: ln || ' ' });
      if (!prevSet.has(ln) && ln.trim()) div.classList.add('ccc-cinema-new'); // appeared in THIS version
    }
  }

  onClose() { this.contentEl.empty(); }
}

// ── PhantomFiles (HS-R2 #1) — ghost rows for files that DON'T exist ──────────
// The explorer shows, inside each open-bid folder, translucent rows for the
// artifacts a winning bid would have at this stage but this one is missing.
// Data: _brain_api/bid/<id>/phantoms.json (built hourly by
// build_phantom_manifest.py — provenance "doctrine" until the outcome ledger
// has ≥5 closed bids, then "learned"). Ghosts live only in the DOM: a real
// file matching the phantom's globs dissolves it instantly.
class PhantomFiles {
  constructor(plugin) {
    this.plugin = plugin;
    this._manifests = new Map(); // bid_path → manifest
    this._loadedAt = 0;
    this._timer = null;
    this._obs = null;
    this._dead = false;
  }

  start() {
    const app = this.plugin.app;
    // perf-audit-2026-06-10: ghosts appearing ~4s after boot is invisible; staying out
    // of the layout-ready storm is not.
    app.workspace.onLayoutReady(() => setTimeout(() => { this._reload().then(() => this._inject()); this._observe(); }, 4000));
    this.plugin.registerEvent(app.workspace.on('layout-change', () => this._soon()));
    this.plugin.registerEvent(app.vault.on('create', (f) => this._onCreate(f)));
    this.plugin.registerInterval(window.setInterval(() => { this._reload().then(() => this._inject()); }, 5 * 60 * 1000));
  }

  destroy() {
    this._dead = true;
    clearTimeout(this._timer);
    if (this._obs) { try { this._obs.disconnect(); } catch (_) {} this._obs = null; }
    document.querySelectorAll('.ccc-phantom').forEach(e => e.remove());
  }

  _observe() {
    const host = document.querySelector('.nav-files-container');
    if (!host) return;
    // explorer re-renders wipe injected rows — re-inject when folders expand.
    // resource guard: ignore mutations caused by our OWN ghost/door nodes,
    // otherwise inject → observe → inject ticks forever on every cycle.
    this._obs = new MutationObserver((muts) => {
      for (const m of muts) {
        for (const n of m.addedNodes) {
          if (n.nodeType === 1 && (n.classList.contains('ccc-phantom') || n.classList.contains('ccc-door'))) continue;
          this._soon();
          return;
        }
        if (m.removedNodes.length) { this._soon(); return; }
      }
    });
    this._obs.observe(host, { childList: true, subtree: true });
  }

  _soon() {
    clearTimeout(this._timer);
    this._timer = setTimeout(() => this._inject(), 400);
  }

  async _reload() {
    try {
      const ad = this.plugin.app.vault.adapter;
      const open = JSON.parse(await ad.read('_brain_api/bid/_open.json'));
      const manifests = new Map();
      for (const b of (open.bids || [])) {
        try {
          const m = JSON.parse(await ad.read(`_brain_api/bid/${b.bid_id}/phantoms.json`));
          if (m && m.bid_path && Array.isArray(m.phantoms) && m.phantoms.length) manifests.set(m.bid_path, m);
        } catch (_) { /* manifest not built yet — no ghosts for this bid */ }
      }
      this._manifests = manifests;
      this._loadedAt = Date.now();
    } catch (_) { /* _open.json missing — stay silent */ }
  }

  _globRe(g) {
    // minimal glob → regex: ** any path, * any segment chars, case-insensitive
    const esc = g.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*\*/g, '\u0001').replace(/\*/g, '[^/]*').replace(/\u0001/g, '.*');
    return new RegExp('^' + esc + '$', 'i');
  }

  _matches(ph, relPath) {
    return (ph.match_globs || []).some(g => { try { return this._globRe(g).test(relPath); } catch (_) { return false; } });
  }

  _inject() {
    if (this._dead || !this._manifests.size) return;
    for (const [bidPath, m] of this._manifests) {
      const folderEl = document.querySelector(`.nav-folder-title[data-path="${(window.CSS && CSS.escape) ? CSS.escape(bidPath) : bidPath}"]`);
      if (!folderEl) continue;
      const children = folderEl.parentElement && folderEl.parentElement.querySelector('.nav-folder-children');
      if (!children) continue; // folder collapsed — ghosts appear on expand
      for (const ph of m.phantoms) {
        const id = (m.bid_id + ':' + ph.artifact).replace(/[^a-z0-9:_-]/gi, '_');
        if (children.querySelector(`[data-phantom="${id}"]`)) continue; // already there
        const row = document.createElement('div');
        row.className = 'nav-file ccc-phantom';
        row.setAttribute('data-phantom', id);
        const title = document.createElement('div');
        title.className = 'nav-file-title ccc-phantom-title';
        const urg = ph.urgency_days;
        if (urg != null && urg <= 2) title.classList.add('ccc-phantom-urgent');
        else if (urg != null && urg <= 7) title.classList.add('ccc-phantom-soon');
        title.textContent = '◌ ' + ph.artifact;
        title.setAttribute('aria-label', `${ph.evidence} · ${ph.provenance}${urg != null ? ` · due in ${urg}d` : ''} · click to materialize`);
        row.appendChild(title);
        title.addEventListener('click', () => this._materialize(m, ph, id));
        children.appendChild(row);
      }
    }
  }

  async _materialize(m, ph, id) {
    const app = this.plugin.app;
    const name = (ph.filename_pattern || ph.artifact.replace(/[^\w ]+/g, '').trim() + '.md').replace(/\*/g, '');
    const path = m.bid_path + '/' + name;
    try {
      if (!app.vault.getAbstractFileByPath(path)) {
        let body = null;
        if (ph.template_path) { try { body = await app.vault.adapter.read(ph.template_path); } catch (_) {} }
        if (body == null) {
          body = [
            '---',
            `type: ${ph.artifact.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`,
            `bid: ${m.bid_id}`,
            `stage: ${m.stage}`,
            `created: ${localDateStr(new Date())}`,
            `provenance: "materialized from phantom (${ph.provenance}): ${(ph.evidence || '').replace(/"/g, "'")}"`,
            '---',
            '',
            `# ${ph.artifact} — ${m.bid_id}`,
            '',
            (ph.description || ''),
            '',
          ].join('\n');
        }
        await app.vault.create(path, body);
      }
      document.querySelectorAll(`[data-phantom="${id}"]`).forEach(e => e.remove());
      app.workspace.openLinkText(path, '', false);
      // the thought lands where the file was born
      try { this.plugin.synapse && this.plugin.synapse.fireFile(path, true); } catch (_) {}
      new Notice(`◌ → ● ${ph.artifact} materialized`, 3000);
    } catch (e) { new Notice('Materialize failed: ' + e.message, 5000); }
  }

  _onCreate(f) {
    if (this._dead || !f || !f.path) return;
    // a real file matching a phantom's globs dissolves the ghost
    for (const [bidPath, m] of this._manifests) {
      if (!f.path.startsWith(bidPath + '/')) continue;
      const rel = f.path.slice(bidPath.length + 1);
      for (const ph of m.phantoms.slice()) {
        if (this._matches(ph, rel) || this._matches(ph, f.path)) {
          m.phantoms.splice(m.phantoms.indexOf(ph), 1);
          const id = (m.bid_id + ':' + ph.artifact).replace(/[^a-z0-9:_-]/gi, '_');
          document.querySelectorAll(`[data-phantom="${id}"]`).forEach(e => e.remove());
        }
      }
    }
  }
}

class CommandCenterView extends ItemView {
  constructor(leaf, plugin) {
    super(leaf);
    this.plugin = plugin;
    this.vaultData = new VaultData(this.app, plugin);
    this._refreshTimer = null;
    this._agentRefreshTimer = null; // prompt refresh on agent vault writes
    this._structRefreshTimer = null; // view-render-struct-timer-undeclared: declare alongside sibling timers
    this._pendingRefresh = false;
    this._skillIndex = null; // lazy-loaded, cached after first build
    this._skillSearchDebounce = null;
    this._skillGroupState = {}; // category -> boolean expanded
    this._renderQueue = Promise.resolve(); // I2: serialized render queue
    this._timers = []; // in-plugin voice timers ({id,name,minutes,set}); cleared in onClose
  }

  getViewType() { return VIEW_TYPE; }
  getDisplayText() { return 'Command Center'; }
  getIcon() { return 'layout-dashboard'; }

  // I2: all renders go through this queue so they never interleave
  // I7: a render failure surfaces an error card instead of a silent blank pane
  _enqueueRender(fn) {
    this._renderQueue = this._renderQueue
      .then(fn)
      .catch(e => {
        console.error('ccc render', e);
        try {
          if (this._contentEl) { this._contentEl.empty(); this._errorCard(this._contentEl, 'RENDER ERROR', e.message); }
          this.plugin && this.plugin._logHealth && this.plugin._logHealth('render error: ' + e.message);
        } catch (_) {}
      });
    return this._renderQueue;
  }

  async onOpen() {
    this.root = this.contentEl.createDiv({ cls: 'ccc-root' });
    this._buildShell();
    await this._enqueueRender(() => this._refresh());

    // I1: Debounced metadata-cache refresh — clear previous timer, store id
    this.registerEvent(
      this.app.metadataCache.on('resolved', () => {
        clearTimeout(this._refreshTimer);
        this._refreshTimer = setTimeout(() => {
          this._refreshTimer = null;
          // perf-audit-2026-06-10: every note edit fired BOTH this 'resolved' path (+5s)
          // and the vault-modify path (+1.2s) — two full rebuilds per edit. If a full
          // render just happened, this one is a duplicate; skip it.
          if (Date.now() - (this._lastFullRender || 0) < 5000) return;
          this.vaultData.invalidate(); // content changed → drop memo cache so the redraw is FRESH, not stale-TTL
          this._enqueueRender(() => this._refresh());
        }, 5000);
      })
    );

    // Structural changes to ANY note (create / delete / rename) → fresh re-render fast.
    // Without this, deleting a note left the dashboard stale (agent-path filter below
    // only watches agent writes; metadataCache 'resolved' was redrawing cached data).
    // perf-sweep-03: skip high-frequency noisy paths that never affect dashboard content
    const SKIP_PREFIXES = ['.obsidian/', '_brain_index/', '_brain_api/'];
    // AGENT_PATHS declared here (before onVaultStructure) so both handlers can reference it
    const AGENT_PATHS = ['_agent_state/', '00_Inbox/from-dust/', '02_Areas/AI Sessions/'];
    const onVaultStructure = (file) => {
      const p = (file && file.path) || '';
      if (SKIP_PREFIXES.some(pre => p.startsWith(pre))) return;
      // Pulse the activity indicator for agent-path changes (create/delete/modify)
      if (AGENT_PATHS.some(pre => p.startsWith(pre))) this._markActivity(p);
      this.vaultData.invalidate();
      clearTimeout(this._structRefreshTimer);
      this._structRefreshTimer = setTimeout(() => {
        this._structRefreshTimer = null;
        this._enqueueRender(() => this._refresh());
      }, 1200);
    };
    this.registerEvent(this.app.vault.on('create', onVaultStructure));
    this.registerEvent(this.app.vault.on('delete', onVaultStructure));
    this.registerEvent(this.app.vault.on('rename', onVaultStructure));
    this.registerEvent(this.app.vault.on('modify', onVaultStructure)); // view-render-no-modify: non-agent file edits now trigger refresh

    // Agent-activity refresh: perf-sweep-03 / view-render-duplicate-create-delete-handlers:
    // onAgentChange handles ONLY 'modify' — 'create' and 'delete' are already covered by
    // onVaultStructure above (removing the double-refresh for agent creates/deletes).
    // _markActivity for create/delete is now called inside onVaultStructure when path is an agent path.
    const onAgentChange = (file) => {
      const p = (file && file.path) || '';
      if (!AGENT_PATHS.some(pre => p.startsWith(pre))) return;
      this._markActivity(p);
      // Note: invalidate + render already fired by onVaultStructure's modify handler above.
    };
    this.registerEvent(this.app.vault.on('modify', onAgentChange)); // 'create'/'delete' removed — handled by onVaultStructure

    // view-live-update: 60s interval backstop so stats.json (spend card) reflects within ~1 min
    // after the background job refreshes it. invalidate() busts the tokenStats _memo (55s TTL)
    // so the next render reads the freshly-written stats.json.
    this.registerInterval(
      window.setInterval(() => {
        // Only do the heavy full rescan when the pane is ACTUALLY on screen. For a hidden background
        // tab (or a backgrounded window) this would otherwise re-walk ~900 transcript files + ~1900
        // notes every cycle for nobody to see — pure battery drain. isShown() is false for an inactive
        // Obsidian tab. Real writes still refresh instantly via the vault watchers above, so this
        // timer is only a backstop for externally-synced (OneDrive/Dust) writes the watcher misses.
        if (document.hidden || !this.contentEl.isShown()) return;
        // Visible → invalidate (drop the 5-min memo TTL) so the redraw is FRESH, not stale ("seems
        // static"). Same effect as the Refresh button.
        this.vaultData.invalidate();
        this._enqueueRender(() => this._refresh());
      }, 60000)
    );

    // Switching back to the dashboard tab → one fresh render immediately (don't wait up to 120s).
    // This is what makes it feel live when Tony actually looks at it, with zero polling while hidden.
    this.registerEvent(this.app.workspace.on('active-leaf-change', (leaf) => {
      if (leaf && leaf.view === this && this.contentEl.isShown()) {
        this.vaultData.invalidate();
        this._enqueueRender(() => this._refresh());
      }
    }));

    // Dedicated lightweight greeting tick (30s): keeps "Good morning/afternoon/
    // evening" current as the clock advances even if a heavy refresh is throttled.
    // Updates ONLY the greeting word — never touches the brief subtitle.
    this.registerInterval(window.setInterval(() => { if (!document.hidden) this._tickGreeting(); }, 30000));
    // Live system vitals (battery/CPU/RAM): updates the THIS MAC card in place every 60s — each tick
    // forks pmset + vm_stat, so skip it entirely when the pane isn't on screen (battery doesn't need
    // 30s granularity on a glance card, and a hidden tab needs none at all).
    this.registerInterval(window.setInterval(() => { if (!document.hidden && this.contentEl.isShown()) this._tickSystem(); }, 60000));
  }

  // Update only the time-of-day greeting word (leaves the brief subtitle intact).
  _tickGreeting() {
    if (!this._greetingEl) return;
    const g = greeting();
    const next = `${g.emoji} ${g.text}`;
    if (this._greetingEl.textContent !== next) this._greetingEl.setText(next);
  }

  // Derive a readable agent/source name from a changed vault path
  _agentFromPath(p) {
    let m = p.match(/^00_Inbox\/from-dust\/([^/]+)\//);
    if (m) return m[1];
    m = p.match(/^02_Areas\/AI Sessions\/([^/]+)\//);
    if (m) return m[1];
    m = p.match(/^_agent_state\/([^/]+)\//);
    if (m) return m[1];
    return 'agent';
  }

  // Show a live "updated from <agent>" pulse in the header
  _markActivity(p) {
    if (!this._activityEl) return;
    const who = this._agentFromPath(p);
    const t = new Date().toLocaleTimeString();
    this._activityEl.setText(`⚡ live · ${who} · ${t}`);
    this._activityEl.classList.remove('ccc-activity-pulse');
    void this._activityEl.offsetWidth; // restart CSS animation
    this._activityEl.classList.add('ccc-activity-pulse');
  }

  // I1: clear pending timers on close
  async onClose() {
    clearTimeout(this._refreshTimer);
    clearTimeout(this._agentRefreshTimer);
    clearTimeout(this._structRefreshTimer);
    this._refreshTimer = null;
    this._agentRefreshTimer = null;
    this._structRefreshTimer = null;
    // Cancel any pending voice timers so they don't fire (and speak) after the view closes.
    if (Array.isArray(this._timers)) {
      for (const t of this._timers) { try { clearTimeout(t.id); } catch (_) {} }
      this._timers = [];
    }
  }

  _buildShell() {
    const root = this.root;

    // Header
    const header = root.createDiv({ cls: 'ccc-header' });
    const headerLeft = header.createDiv({ cls: 'ccc-header-left' });
    const g0 = greeting();
    this._greetingEl = headerLeft.createEl('h1', { cls: 'ccc-title', text: `${g0.emoji} ${g0.text}` });
    this._briefEl = headerLeft.createEl('p', { cls: 'ccc-subtitle', text: 'AI Second Brain' });
    this._updateGreeting();

    const headerRight = header.createDiv({ cls: 'ccc-header-right' });
    this._healthDotEl = headerRight.createEl('span', { cls: 'ccc-health-dot', attr: { 'aria-label': 'health' } });
    this._healthDotEl.addEventListener('click', () => this.plugin && this.plugin._selfHeal(true));
    this._renderHealthDot();
    this._activityEl = headerRight.createEl('span', { cls: 'ccc-activity', text: '' });
    this._lastRefreshEl = headerRight.createEl('span', { cls: 'ccc-last-refresh', text: '' });
    const refreshBtn = headerRight.createEl('button', { cls: 'ccc-refresh-btn', text: '↻ Refresh' });
    refreshBtn.addEventListener('click', () => { this.vaultData.invalidate(); this._enqueueRender(() => this._refresh()); });

    // ── Skill-launcher actions strip ────────────────────────────────────────
    this._actionsStrip = root.createDiv({ cls: 'ccc-actions' });
    this._renderActionsStrip();

    // Tab strip
    const tabStrip = root.createDiv({ cls: 'ccc-tab-strip' });
    this._tabButtons = {};
    const tabDefs = [
      { id: 'overview', label: 'Overview' },
      { id: 'sales', label: 'Sales' },
      { id: 'meetings', label: 'Meetings' },
      { id: 'fleet', label: 'AI Fleet' },
      { id: 'skills', label: 'Skills' },
      { id: 'me', label: 'Me' },
    ];
    for (const t of tabDefs) {
      const btn = tabStrip.createEl('button', { cls: 'ccc-tab-btn', text: t.label });
      btn.dataset.tab = t.id;
      btn.addEventListener('click', () => this._enqueueRender(() => this._switchTab(t.id)));
      this._tabButtons[t.id] = btn;
    }

    // Content area
    this._contentEl = root.createDiv({ cls: 'ccc-content' });

    // Tab state for meetings month switcher
    this._meetingsMonth = 'this'; // 'this' | 'last'

    // Tab renderer registry
    this.tabs = {
      overview: () => this._renderOverview(),
      sales: () => this._renderSalesTab(),
      meetings: () => this._renderMeetingsTab(),
      fleet: () => this._renderFleetTab(),
      skills: () => this._renderSkillsTab(),
      me: () => this._renderMeTab(),
    };
  }

  _renderActionsStrip() {
    this._actionsStrip.empty();
    const actions = this.plugin.settings.actions || DEFAULT_ACTIONS;
    for (const action of actions) {
      const btn = this._actionsStrip.createEl('button', {
        cls: 'ccc-action-btn',
        text: (action.emoji ? action.emoji + ' ' : '') + action.label,
      });
      btn.title = action.command;
      btn.addEventListener('click', () => this._triggerAction(action));
    }
  }

  _triggerAction(action) {
    this.plugin.trackUsage('action:' + action.id); // self-learning
    if (action.command === '__ORB_TOGGLE__') { this.plugin.orb && this.plugin.orb.toggle(); return; }
    if (action.command === '__DIGEST_NOW__') { this.plugin.orb && this.plugin.orb._runDigestNow && this.plugin.orb._runDigestNow(); return; }
    if (action.prompt) {
      new ActionPromptModal(this.app, action, (input) => {
        // N1: single .replace with the canonical {input} pattern
        const cmd = action.command.replace(/\{input\}/g, input);
        injectIntoTerminal(this.app, cmd);
      }).open();
    } else {
      injectIntoTerminal(this.app, action.command);
    }
  }

  // Reflect plugin self-heal status as a coloured dot in the header.
  _renderHealthDot() {
    if (!this._healthDotEl) return;
    const hp = (this.plugin && this.plugin._health) || { status: 'ok', issues: [] };
    const ok = hp.status === 'ok';
    this._healthDotEl.setText(ok ? '🟢' : '🟡');
    this._healthDotEl.title = ok
      ? 'Healthy — click to re-run self-check'
      : 'Issues: ' + (hp.issues || []).join('; ') + (hp.healed && hp.healed.length ? ' · healed: ' + hp.healed.join(', ') : '') + ' — click to heal';
  }

  async _switchTab(id) {
    this.plugin.settings.activeTab = id;
    this.plugin.trackUsage('tab:' + id); // self-learning
    await this.plugin.saveSettings(); // I4: persist (was fire-and-forget)
    for (const [k, btn] of Object.entries(this._tabButtons)) {
      btn.classList.toggle('ccc-tab-active', k === id);
    }
    this._contentEl.empty();
    const fn = this.tabs[id];
    if (fn) return fn();
    return Promise.resolve();
  }

  // Adaptive header: greet by time of day + optional synthesized brief line
  _updateGreeting(insights) {
    const g = greeting();
    if (this._greetingEl) this._greetingEl.setText(`${g.emoji} ${g.text}`);
    if (this._briefEl) {
      const dateStr = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' });
      this._briefEl.setText(insights ? `${dateStr} · ${narrativeBrief(insights, g)}` : `${dateStr} · AI Second Brain`);
    }
  }

  async _refresh() {
    this._lastFullRender = Date.now(); // perf-audit-2026-06-10: lets the 'resolved' path skip duplicate rebuilds
    const activeTab = this.plugin.settings.activeTab || 'overview';
    this._updateGreeting(); // keep greeting time-fresh on every refresh
    // Activate correct tab button
    for (const [k, btn] of Object.entries(this._tabButtons || {})) {
      btn.classList.toggle('ccc-tab-active', k === activeTab);
    }

    this._contentEl.empty();
    const fn = this.tabs[activeTab];
    if (fn) await fn();

    if (this._lastRefreshEl) {
      const now = new Date();
      this._lastRefreshEl.textContent = 'Updated ' +
        now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
  }

  async _renderOverview() {
    // Fetch all data in parallel (insights + cards)
    const [stats, meetings, fleet, pipeline, important, backlog, sales, health, followup, trend, cadence] =
      await Promise.all([
        this.vaultData.tokenStats(),
        this.vaultData.meetings(),
        this.vaultData.fleet(),
        this.vaultData.pipeline(),
        this.vaultData.important(),
        this.vaultData.reviewBacklog(),
        this.vaultData.salesPipeline(),
        this.vaultData.accountHealth(),
        this.vaultData.meetingFollowup(),
        this.vaultData.aiTrend(),
        this.vaultData.personalCadence(),
      ]);

    // ── Insight strip: "what matters now" (synthesized, ranked) ──────────────
    const cfg = Object.assign({ hourlyRate: 100, minPer1kOutput: 2.5, minPerSession: 10 },
      (this.plugin?.settings?.roi) || {});
    const weekRoi = stats.error ? { weekHours: 0, weekValue: 0 }
      : (() => { const r = computeRoi(stats.weekOut, stats.weekSessions, stats.weekCost, cfg); return { weekHours: r.hours, weekValue: r.value }; })();
    const insights = computeInsights({ sales, health, backlog, followup, trend, cadence, roi: weekRoi });
    this._updateGreeting(insights); // enrich header with the synthesized brief

    // ── Stat ribbon: the 5 glanceable numbers, with week-over-week deltas ────
    const roiAll = stats.error ? 0 : computeRoi(stats.allTimeOut, stats.allTimeSessions, stats.allTimeCost, cfg).roi;
    const wk = (trend && trend.weeks) || [];
    const cur = wk.length ? wk[wk.length - 1].cost : 0;
    const prev = wk.length > 1 ? wk[wk.length - 2].cost : 0;
    const spendDelta = prev > 0.5 ? (cur / prev - 1) : null;
    this._renderStatRibbon(this._contentEl, {
      todayCost: stats.error ? 0 : stats.todayCost,
      roiAll, openValue: sales.error ? 0 : sales.openValue,
      toReview: backlog.error ? 0 : backlog.count,
      streak: cadence.error ? 0 : cadence.streak,
      spendDelta,
    });

    // (Insight signal lives in the header one-line brief, not a strip on Overview.)

    this._renderAskBar(this._contentEl);

    const grid = this._contentEl.createDiv({ cls: 'ccc-grid' });

    this._renderTokenCard(grid, stats); // 1 — CLAUDE SPEND (top, Tony's layout)
    this._renderRoiCard(grid, stats);   // 2 — TIME SAVED · ROI
    this._renderSystemCard(grid);       // 3 — THIS MAC (battery / CPU / RAM)
    this._renderBacklogCard(grid, backlog);
    this._renderMeetingsCard(grid, meetings);
    this._renderFleetCard(grid, fleet);
    this._renderPipelineCard(grid, pipeline);
    this._renderImportantCard(grid, important);
    this._renderUsageCard(grid);        // sessions / messages / total tokens at the bottom
  }

  // ── Ask-your-brain bar: NL question → Claude terminal (graphify-first) ─────
  _renderAskBar(container) {
    const wrap = container.createDiv({ cls: 'ccc-ask' });
    const bar = wrap.createDiv({ cls: 'ccc-askbar' });
    bar.createEl('span', { cls: 'ccc-askbar-icon', text: '🧠' });
    const input = bar.createEl('input', {
      cls: 'ccc-askbar-input',
      attr: { type: 'text', placeholder: 'Ask your second brain…' },
    });
    const btn = bar.createEl('button', { cls: 'ccc-askbar-btn', text: 'Ask →' });
    const ask = (preset) => {
      const q = (preset != null ? preset : input.value).trim();
      if (!q) return;
      // Plain NL → Claude routes it (graphify-first per Rule 1). Forcing
      // `/graphify query` here produced noisy lexical misses on short questions.
      injectIntoTerminal(this.app, q);
      this._pushAskHistory(q);
      input.value = '';
      btn.setText('Sent ✓');
      setTimeout(() => btn.setText('Ask →'), 1500);
    };
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); ask(); }
      if (e.key === 'Escape') { input.value = ''; input.blur(); }
    });
    btn.addEventListener('click', () => ask());
    const cap = bar.createEl('button', { cls: 'ccc-askbar-cap', text: '⚡ Capture' });
    cap.addEventListener('click', () => new QuickCaptureModal(this.app).open());

    // Chips: recent questions first (🕘), canned starters fill the row to 5.
    const SUGGESTED = [
      'What is pending my review?',
      'Status of the open bids',
      'What changed in the vault today?',
    ];
    const history = this.plugin.settings.askHistory || [];
    const chips = history.concat(SUGGESTED.filter(s => !history.includes(s))).slice(0, 5);
    if (chips.length) {
      const row = wrap.createDiv({ cls: 'ccc-ask-chips' });
      chips.forEach((q, i) => {
        const chip = row.createEl('button', {
          cls: 'ccc-ask-chip' + (i < history.length ? ' ccc-ask-chip-recent' : ''),
          text: (i < history.length ? '🕘 ' : '') + (q.length > 42 ? q.slice(0, 39) + '…' : q),
          attr: { title: q },
        });
        chip.addEventListener('click', () => ask(q));
      });
    }
  }

  _pushAskHistory(q) {
    const st = this.plugin.settings;
    st.askHistory = [q, ...(st.askHistory || []).filter(x => x !== q)].slice(0, 3);
    this.plugin.saveSettings().catch(() => {});
  }

  // ── Stat ribbon: 5 glanceable headline numbers + week-over-week delta ──────
  _renderStatRibbon(container, d) {
    const eur = (n) => 'C$' + (n >= 1000 ? (n / 1000).toFixed(1) + 'k' : Math.round(n));
    const ribbon = container.createDiv({ cls: 'ccc-ribbon' });
    const cell = (icon, val, label, delta) => {
      const c = ribbon.createDiv({ cls: 'ccc-rib-cell' });
      const top = c.createDiv({ cls: 'ccc-rib-top' });
      top.createEl('span', { cls: 'ccc-rib-icon', text: icon });
      top.createEl('span', { cls: 'ccc-rib-val', text: val });
      if (delta != null) {
        const up = delta >= 0;
        top.createEl('span', {
          cls: 'ccc-rib-delta ' + (up ? 'ccc-delta-up' : 'ccc-delta-down'),
          text: (up ? '▲' : '▼') + Math.abs(Math.round(delta * 100)) + '%',
        });
      }
      c.createEl('span', { cls: 'ccc-rib-label', text: label });
    };
    cell('💸', 'C$' + d.todayCost.toFixed(2), 'spend today', d.spendDelta);
    cell('🚀', d.roiAll.toFixed(1) + '×', 'AI ROI');
    cell('💼', eur(d.openValue), 'open pipeline');
    cell('📥', String(d.toReview), 'to review');
    cell('🔥', String(d.streak), 'day streak');
  }

  // ── Card: This Mac — live battery / CPU / RAM ──────────────────────────────
  // Cheap: CPU from os.cpus() deltas (no subprocess); battery + RAM from one tiny
  // pmset + vm_stat each (~10ms), refreshed on a dedicated 6s tick that updates the
  // numbers in place (no full re-render). Bars give an at-a-glance read.
  _renderSystemCard(container) {
    const card = container.createDiv({ cls: 'ccc-card ccc-sec-system' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'THIS MAC' });
    const row = card.createDiv({ cls: 'ccc-sys-row' });
    row.style.cssText = 'display:flex;gap:18px;align-items:flex-end;margin:2px 0 10px;';
    const cell = (icon, label) => {
      const c = row.createDiv(); c.style.cssText = 'display:flex;flex-direction:column;gap:1px;';
      const top = c.createDiv(); top.style.cssText = 'display:flex;align-items:baseline;gap:5px;';
      top.createEl('span', { text: icon }).style.cssText = 'font-size:15px;';
      const v = top.createEl('span', { text: '…' }); v.style.cssText = 'font-size:21px;font-weight:700;font-variant-numeric:tabular-nums;';
      c.createEl('span', { text: label }).style.cssText = 'font-size:10px;opacity:.6;text-transform:uppercase;letter-spacing:.04em;';
      return v;
    };
    this._sysBatt = cell('🔋', 'battery');
    this._sysCpu = cell('🧠', 'CPU');
    this._sysRam = cell('🧮', 'RAM');
    const mkBar = (color) => {
      const wrap = card.createDiv(); wrap.style.cssText = 'height:5px;border-radius:3px;background:rgba(127,0,218,.14);margin:3px 0;overflow:hidden;';
      const fill = wrap.createDiv(); fill.style.cssText = `height:100%;width:0%;border-radius:3px;background:${color};transition:width .4s ease;`;
      return fill;
    };
    this._sysCpuBar = mkBar('#7F00DA');
    this._sysRamBar = mkBar('#6600AE');
    this._sysFoot = card.createEl('span', { text: '' }); this._sysFoot.style.cssText = 'font-size:10px;opacity:.5;';
    this._tickSystem(); // immediate paint
  }

  _cpuSnapshot() {
    const os = require('os'); let idle = 0, total = 0;
    for (const c of os.cpus()) { for (const k in c.times) total += c.times[k]; idle += c.times.idle; }
    return { idle, total };
  }

  async _readSystemStats() {
    const os = require('os'), cp = require('child_process');
    const snap = this._cpuSnapshot();
    let cpuPct = null;
    if (this._prevCpu) {
      const dt = snap.total - this._prevCpu.total, di = snap.idle - this._prevCpu.idle;
      if (dt > 0) cpuPct = Math.max(0, Math.min(100, Math.round(100 * (1 - di / dt))));
    }
    this._prevCpu = snap;
    const sh = (c) => new Promise((r) => cp.exec(c, { timeout: 4000 }, (e, o) => r(e ? '' : o || '')));
    const [batt, vm] = await Promise.all([sh('/usr/bin/pmset -g batt'), sh('/usr/bin/vm_stat')]);
    let battPct = null;
    const bm = batt.match(/(\d+)%/); if (bm) battPct = +bm[1];
    const charging = /charging|charged|AC Power/i.test(batt) && !/discharging/i.test(batt);
    let ramPct = null, usedGB = null; const totalGB = os.totalmem() / 1e9;
    const ps = (vm.match(/page size of (\d+)/) || [])[1];
    if (ps) {
      const pg = (k) => { const m = vm.match(new RegExp(k + ':\\s+(\\d+)')); return m ? +m[1] * +ps : 0; };
      const used = pg('Pages active') + pg('Pages wired down') + pg('Pages occupied by compressor');
      usedGB = used / 1e9; ramPct = Math.round(100 * used / os.totalmem());
    } else {
      usedGB = (os.totalmem() - os.freemem()) / 1e9; ramPct = Math.round(100 * (1 - os.freemem() / os.totalmem()));
    }
    return { cpuPct, battPct, charging, ramPct, usedGB, totalGB, load: os.loadavg()[0], cores: os.cpus().length };
  }

  async _tickSystem() {
    if (!this._sysBatt || !this._sysBatt.isConnected) return; // card not mounted → skip (cheap guard)
    let s; try { s = await this._readSystemStats(); } catch (_) { return; }
    if (!this._sysBatt || !this._sysBatt.isConnected) return;
    const set = (el, t) => { if (el && el.isConnected) el.setText(t); };
    set(this._sysBatt, s.battPct == null ? '—' : s.battPct + '%' + (s.charging ? ' ⚡' : ''));
    set(this._sysCpu, s.cpuPct == null ? '…' : s.cpuPct + '%');
    set(this._sysRam, s.ramPct == null ? '—' : s.ramPct + '%');
    if (this._sysCpuBar && this._sysCpuBar.isConnected && s.cpuPct != null) this._sysCpuBar.style.width = s.cpuPct + '%';
    if (this._sysRamBar && this._sysRamBar.isConnected && s.ramPct != null) this._sysRamBar.style.width = s.ramPct + '%';
    if (this._sysFoot && this._sysFoot.isConnected) {
      this._sysFoot.setText(`${s.usedGB != null ? s.usedGB.toFixed(1) : '?'}/${s.totalGB.toFixed(0)} GB · load ${s.load.toFixed(1)} on ${s.cores} cores`);
    }
  }

  // ── Card: Review backlog — what needs me now ───────────────────────────────
  _renderBacklogCard(container, b) {
    if (b.error) { this._errorCard(container, 'NEEDS ME NOW', b.error); return; }
    const card = container.createDiv({ cls: 'ccc-card ccc-sec-triage' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'NEEDS ME NOW' });
    const bigRow = card.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: String(b.count) });
    bigRow.createEl('span', { cls: 'ccc-caption', text: 'to review' });
    const subs = card.createDiv({ cls: 'ccc-substats' });
    const mk = (v, l) => { const s = subs.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: String(v) });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
    mk(b.hi, 'auto-ok ≥.85'); mk(b.lo, 'low-conf'); mk(b.escalations, 'escalations'); mk(b.decisions, 'decisions');
    if (b.items.length === 0) { card.createEl('p', { cls: 'ccc-empty', text: 'Inbox clear ✅' }); return; }
    const list = card.createDiv({ cls: 'ccc-list' });
    for (const it of b.items) {
      const row = list.createDiv({ cls: 'ccc-list-row' });
      row.createEl('span', { cls: 'ccc-list-primary', text: `${it.agent} · ${it.output_type || it.name}` });
      if (it.confidence != null) {
        const cls = it.confidence >= 0.85 ? 'ccc-pill' : 'ccc-badge ccc-badge-danger';
        row.createEl('span', { cls, text: it.confidence.toFixed(2) });
      }
      row.createEl('span', { cls: 'ccc-list-meta', text: it.ageH + 'h' });
      row.addEventListener('click', () => this.app.workspace.openLinkText(it.path, '', false));
    }
    const foot = card.createEl('button', { cls: 'ccc-action-btn', text: '📥 /dust-resolve' });
    foot.addEventListener('click', () => injectIntoTerminal(this.app, '/dust-resolve'));
    this._marbleRun(card, b).catch(() => {});
  }

  // ── MARBLE RUN (HS-R2 #11): watch triage happen instead of trusting a log ──
  // Every recent triage event from 99_Meta/dust-write-log.md rolls across the
  // pipe as a glass marble — green clatters through (promoted), amber drops
  // into the rattling LOOK-AT-ME tray (held: the b.items above), grey is a
  // suppressed replay, red a rejection. Hover a marble = the actual log line;
  // click the tray = /dust-resolve. Real events only, newest first.
  async _marbleRun(card, b) {
    // perf-audit-2026-06-10: this log is append-only and ~70KB — don't re-read it on
    // every dashboard paint; 60s staleness is invisible for a triage visual.
    const now = Date.now();
    if (!this.plugin._marbleCache || now - this.plugin._marbleCache.t > 60000) {
      try {
        const raw = await this.app.vault.adapter.read('99_Meta/dust-write-log.md');
        this.plugin._marbleCache = { t: now, lines: raw.split('\n').filter(l => l.startsWith('- ')).slice(-14).reverse() };
      } catch (_) { return; }
    }
    const lines = this.plugin._marbleCache.lines;
    if (!lines.length && !b.items.length) return;
    const run = card.createDiv({ cls: 'ccc-marble-run' });
    const pipe = run.createDiv({ cls: 'ccc-marble-pipe' });
    const VERDICT = [
      [/PROMOTED|AUTO_PROMOTED|ACCEPTED/i, 'ccc-marble-go'],
      [/HELD|HOLD|QUARANTINE/i, 'ccc-marble-hold'],
      [/REJECT/i, 'ccc-marble-no'],
      [/REPLAY_SUPPRESSED|SUPPRESSED|SKIP/i, 'ccc-marble-dup'],
    ];
    lines.forEach((l, i) => {
      const m = l.match(/^- ([^:]+): (\S+) ([^/\s]+)\/(\S+)/);
      if (!m) return;
      const cls = (VERDICT.find(([re]) => re.test(m[2])) || [null, 'ccc-marble-other'])[1];
      const marble = pipe.createDiv({ cls: 'ccc-marble ' + cls });
      marble.style.animationDelay = (i * 0.12) + 's';
      marble.setAttribute('aria-label', `${m[2]} · ${m[3]} · ${m[4].slice(0, 60)} · ${m[1].slice(0, 16)}`);
    });
    const tray = run.createDiv({ cls: 'ccc-marble-tray' + (b.items.length ? ' ccc-marble-tray-rattle' : '') });
    tray.createEl('span', { text: '🧺 ' + b.items.length });
    tray.setAttribute('aria-label', b.items.length ? `${b.items.length} held for review — click to /dust-resolve` : 'tray empty');
    if (b.items.length) tray.addEventListener('click', () => injectIntoTerminal(this.app, '/dust-resolve'));
  }

  // ── TAB: Sales (pipeline CAD, win rate, deadlines, account health) ─────────
  async _renderSalesTab() {
    const container = this._contentEl;
    const grid = container.createDiv({ cls: 'ccc-grid' });
    const [sales, health] = await Promise.all([
      this.vaultData.salesPipeline(),
      this.vaultData.accountHealth(),
    ]);
    const eur = (n) => 'C$' + Math.round(n).toLocaleString();
    const mkSub = (parent, v, l) => { const s = parent.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: v });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };

    // Pipeline value + win rate
    if (sales.error) { this._errorCard(grid, 'PIPELINE', sales.error, this._retry()); }
    else {
      const card = grid.createDiv({ cls: 'ccc-card ccc-card-hero ccc-sec-pipeline' });
      card.createEl('p', { cls: 'ccc-eyebrow', text: 'PIPELINE VALUE' });
      const big = card.createDiv({ cls: 'ccc-big-row' });
      big.createEl('span', { cls: 'ccc-giant-stat', text: eur(sales.openValue) });
      big.createEl('span', { cls: 'ccc-caption', text: `${sales.openCount} open` });
      const subs = card.createDiv({ cls: 'ccc-substats' });
      mkSub(subs, eur(sales.weighted), 'weighted');
      mkSub(subs, eur(sales.avgDeal), 'avg deal');
      mkSub(subs, (sales.winRate * 100).toFixed(0) + '%', 'win rate');
      const subs2 = card.createDiv({ cls: 'ccc-substats' });
      mkSub(subs2, String(sales.wonCount), 'won');
      mkSub(subs2, String(sales.lostCount), 'lost');
      mkSub(subs2, eur(sales.wonValue), 'won');
      if (sales.openValue === 0) card.createEl('p', { cls: 'ccc-roi-formula', text: 'Fill `value` / `probability` in each bid brief to light this up.' });

      // Deadline radar
      const dcard = grid.createDiv({ cls: 'ccc-card ccc-sec-coverage' });
      dcard.createEl('p', { cls: 'ccc-eyebrow', text: 'DEADLINE RADAR' });
      if (!sales.deadlines.length) dcard.createEl('p', { cls: 'ccc-empty', text: 'No dated bids.' });
      else {
        const list = dcard.createDiv({ cls: 'ccc-list' });
        for (const d of sales.deadlines) {
          const row = list.createDiv({ cls: 'ccc-list-row' });
          row.createEl('span', { cls: 'ccc-list-primary', text: `${d.opp} · ${d.stage}` });
          let txt = d.daysLeft + 'd', cls = 'ccc-badge';
          if (d.daysLeft < 0) { txt = 'OVERDUE'; cls = 'ccc-badge ccc-badge-danger'; }
          else if (d.daysLeft <= 14) { txt = d.daysLeft + 'd · CLOSING'; cls = 'ccc-badge ccc-badge-danger'; }
          row.createEl('span', { cls, text: txt });
          row.createEl('span', { cls: 'ccc-list-meta', text: fmtDate(d.deadline) });
          row.addEventListener('click', () => this.app.workspace.openLinkText(d.path, '', false));
        }
      }
    }

    // Account health + expansion
    if (health.error) { this._errorCard(grid, 'ACCOUNT HEALTH', health.error, this._retry()); }
    else {
      const card = grid.createDiv({ cls: 'ccc-card ccc-sec-skills' });
      card.createEl('p', { cls: 'ccc-eyebrow', text: 'ACCOUNT HEALTH & EXPANSION' });
      if (!health.accounts.length) card.createEl('p', { cls: 'ccc-empty', text: 'No coach-note health scores yet.' });
      else {
        const list = card.createDiv({ cls: 'ccc-list' });
        for (const a of health.accounts) {
          const row = list.createDiv({ cls: 'ccc-list-row' });
          const arrow = a.trend === 'up' ? ' ↑' : a.trend === 'down' ? ' ↓' : '';
          row.createEl('span', { cls: 'ccc-list-primary', text: a.account });
          row.createEl('span', { cls: 'ccc-pill', text: a.score.toFixed(1) + '/10' + arrow });
          if (a.expansionDays != null) row.createEl('span', { cls: 'ccc-list-meta', text: 'exp ' + a.expansionDays + 'd' });
          row.addEventListener('click', () => this.app.workspace.openLinkText(a.path, '', false));
        }
      }
    }

    this._aggroRadarCard(grid);
    this._raidBossCard(grid);
    this._corpseRunCard(grid);
  }

  // ── CORPSE RUN (HS-R2 #13): dead bids drop loot that decays in 14 days ─────
  // Closed bids come from _agent_state/outcome-ledger.jsonl (distinct bid_id)
  // + any 04_Archives folder whose brief says Won/Lost. Loot = the unextracted
  // lessons: a closed bid with no debrief.md. The loot visibly decays — after
  // 14 days it crumbles to dust and the lesson is gone. Clicking starts the
  // corpse run: a schema-valid debrief/1.0 stub is created and opened
  // (ingest_debrief.py consumes it once Tony confirms it).
  async _corpseRunCard(grid) {
    const ad = this.app.vault.adapter;
    const closed = new Map(); // bid_id → {outcome, date, path?}
    try {
      const raw = await ad.read('_agent_state/outcome-ledger.jsonl');
      for (const line of raw.split('\n')) {
        if (!line.trim()) continue;
        try {
          const e = JSON.parse(line);
          if (!e.bid_id || !e.outcome) continue;
          const cur = closed.get(e.bid_id);
          if (!cur || String(e.date || '') > String(cur.date || '')) closed.set(e.bid_id, { outcome: e.outcome, date: e.date || '' });
        } catch (_) {}
      }
    } catch (_) {}
    const files = this.app.vault.getMarkdownFiles();
    // archived briefs marked Won/Lost (folder moved to 04_Archives)
    for (const f of files) {
      if (!f.path.startsWith('04_Archives/') || !/00 - Brief\.md$/.test(f.path)) continue;
      const fm = (this.app.metadataCache.getFileCache(f) || {}).frontmatter || {};
      const stage = String(fm.stage || '').toLowerCase();
      if (stage !== 'won' && stage !== 'lost') continue;
      const id = fm.project || fm.bid_id || (f.parent && f.parent.name) || f.basename;
      if (!closed.has(id)) closed.set(id, { outcome: stage, date: fm.closed || fm.updated || '', path: f.parent ? f.parent.path : null });
    }
    const card = grid.createDiv({ cls: 'ccc-card ccc-sec-skills' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'GRAVEYARD — corpse runs' });
    if (!closed.size) {
      card.createEl('p', { cls: 'ccc-empty', text: 'No closed bids in the ledger yet — tombstones appear when a bid closes (close_bid.py).' });
      return;
    }
    const strip = card.createDiv({ cls: 'ccc-grave-strip' });
    const today = new Date();
    for (const [bidId, c] of closed) {
      const hasDebrief = files.some(f => f.basename === 'debrief' && (f.path.includes(bidId) || (c.path && f.path.startsWith(c.path + '/'))));
      const closedAt = c.date ? new Date(String(c.date).slice(0, 10) + 'T12:00') : null;
      const age = closedAt && !isNaN(closedAt) ? Math.floor((today - closedAt) / 86400000) : null;
      const decay = age == null ? 0 : Math.min(1, age / 14);
      const tomb = strip.createDiv({ cls: 'ccc-grave' });
      tomb.createEl('div', { cls: 'ccc-grave-stone', text: '🪦' });
      tomb.createEl('div', { cls: 'ccc-grave-name', text: `${bidId} · ${String(c.outcome).toUpperCase()}` });
      if (hasDebrief) {
        tomb.createEl('div', { cls: 'ccc-grave-loot ccc-grave-banked', text: '🏆 lessons banked' });
      } else if (decay >= 1) {
        tomb.createEl('div', { cls: 'ccc-grave-loot ccc-grave-dust', text: `💨 loot crumbled (${age}d) — run anyway` });
      } else {
        const loot = tomb.createEl('div', { cls: 'ccc-grave-loot', text: `✨ loot decaying${age != null ? ` — ${14 - age}d left` : ''}` });
        loot.style.opacity = String(1 - 0.6 * decay);
      }
      if (!hasDebrief) {
        tomb.classList.add('ccc-grave-clickable');
        tomb.setAttribute('aria-label', 'Start the corpse run: create + open a debrief/1.0 stub');
        tomb.addEventListener('click', () => this._startCorpseRun(bidId, c));
      }
    }
  }

  async _startCorpseRun(bidId, c) {
    const dir = c.path || `04_Archives/${new Date().getFullYear()}/${bidId}`;
    const path = `${dir}/debrief.md`;
    const today = localDateStr(new Date());
    try {
      if (!this.app.vault.getAbstractFileByPath(path)) {
        try { await this.app.vault.createFolder(dir); } catch (_) {}
        await this.app.vault.create(path, [
          '---',
          'schema_version: debrief/1.0',
          `bid_id: ${bidId}`,
          `outcome: ${c.outcome === 'won' ? 'won' : c.outcome === 'lost' ? 'lost' : 'no-decision'}`,
          `debrief_date: ${today}`,
          `ingested_date: ${today}`,
          'source: corpse-run (plugin stub — fill from buyer debrief, then confirm)',
          'confidence: low',
          ...(c.outcome === 'lost' ? ['loss_reason: '] : []),
          'confirmed: false',
          '---',
          '',
          `# Debrief — ${bidId}`,
          '',
          '## What the buyer said',
          '- ',
          '',
          '## What actually decided it',
          '- ',
          '',
          '## What we do differently next time',
          '- ',
          '',
        ].join('\n'));
      }
      this.app.workspace.openLinkText(path, '', false);
      new Notice('⚔️ Corpse run started — fill the debrief, set confirmed: true, and the ledger learns.', 6000);
    } catch (e) { new Notice('Corpse run failed: ' + e.message, 5000); }
  }

  // ── RAID BOSS DEADLINES (HS-R2 #14): every bid is a boss with a real HP bar ─
  // HP = unresolved gate items, all from real sources: phantom artifacts
  // (build_phantom_manifest), unchecked task boxes in the bid folder
  // (metadataCache.listItems — zero file reads), and compliance gaps when that
  // endpoint goes live. Completing an artifact/task lands a visible hit.
  // Telegraph (red AoE list of blockers) arms at ≤72h to deadline.
  async _raidBossCard(grid) {
    const plugin = this.plugin;
    let open = { bids: [] };
    try { open = JSON.parse(await this.app.vault.adapter.read('_brain_api/bid/_open.json')); } catch (_) {}
    if (!open.bids || !open.bids.length) return;
    const card = grid.createDiv({ cls: 'ccc-card ccc-sec-skills' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'RAID BOSSES — submission gates' });
    plugin._bossPrev = plugin._bossPrev || {};
    plugin._bossMax = plugin._bossMax || {};
    const files = this.app.vault.getMarkdownFiles();
    for (const b of open.bids) {
      const items = [];
      // phantoms = missing winning-bid artifacts
      const man = plugin.phantoms && plugin.phantoms._manifests.get(b.path);
      if (man) for (const ph of man.phantoms) items.push({ label: '◌ ' + ph.artifact, kind: 'phantom' });
      // unchecked task boxes anywhere in the bid folder (cache only, no reads)
      for (const f of files) {
        if (!f.path.startsWith(b.path + '/')) continue;
        const li = (this.app.metadataCache.getFileCache(f) || {}).listItems || [];
        for (const it of li) if (it.task === ' ') items.push({ label: '☐ ' + f.basename, kind: 'task', path: f.path });
      }
      const hp = items.length;
      const maxHp = plugin._bossMax[b.bid_id] = Math.max(plugin._bossMax[b.bid_id] || 0, hp);
      const prev = plugin._bossPrev[b.bid_id];
      plugin._bossPrev[b.bid_id] = hp;
      const row = card.createDiv({ cls: 'ccc-boss-row' });
      const head = row.createDiv({ cls: 'ccc-boss-head' });
      head.createEl('span', { cls: 'ccc-boss-name', text: `👹 ${b.company || b.bid_id} · ${b.stage}` });
      let days = null;
      if (b.deadline) {
        const dl = new Date(b.deadline + 'T23:59');
        if (!isNaN(dl)) days = Math.ceil((dl - new Date()) / 86400000);
      }
      head.createEl('span', {
        cls: 'ccc-list-meta',
        text: days != null ? (days < 0 ? 'OVERDUE' : days + 'd to kill window') : 'no deadline set (00 - Brief.md)',
      });
      if (hp === 0) {
        row.createEl('p', { cls: 'ccc-boss-dead', text: '💀 boss down — all gates clear' });
        continue;
      }
      const bar = row.createDiv({ cls: 'ccc-boss-bar' });
      const fill = bar.createDiv({ cls: 'ccc-boss-fill' });
      fill.style.width = Math.round(100 * hp / Math.max(1, maxHp)) + '%';
      bar.createEl('span', { cls: 'ccc-boss-hp', text: `${hp} HP` });
      if (prev != null && hp < prev) {
        const dmg = row.createEl('span', { cls: 'ccc-boss-dmg', text: `-${prev - hp} ⚔` });
        setTimeout(() => dmg.remove(), 1900);
      }
      // telegraph: ≤72h = red AoE over the exact blockers; else show weak points
      const tele = days != null && days <= 3;
      const list = row.createDiv({ cls: tele ? 'ccc-boss-tele' : 'ccc-boss-weak' });
      if (tele) list.createEl('span', { cls: 'ccc-boss-tele-label', text: '⭕ TELEGRAPH — kill these or wipe:' });
      for (const it of items.slice(0, tele ? 8 : 3)) {
        const li = list.createEl('div', { cls: 'ccc-boss-item', text: it.label });
        if (it.path) li.addEventListener('click', () => this.app.workspace.openLinkText(it.path, '', false));
      }
      if (items.length > (tele ? 8 : 3)) list.createEl('div', { cls: 'ccc-list-meta', text: `+${items.length - (tele ? 8 : 3)} more` });
    }
  }

  // ── AGGRO RADAR (HS-R2 #6): neglected accounts drift toward the center ─────
  // Blip distance = real silence-days from ThreatIndex (45d = at the center);
  // angle = stable name hash so accounts keep their bearing. Sweep is local
  // rAF (UltronVitals 'beat' bus has no owner yet — see ISSUES). The one reset
  // action is real: draft a check-in into Outbound/ (never sent) or stamp
  // last_touch via processFrontMatter, which knocks the blip back outward.
  _aggroRadarCard(grid) {
    const threat = this.plugin.threat;
    const card = grid.createDiv({ cls: 'ccc-card ccc-sec-skills' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'AGGRO RADAR — ACCOUNT SILENCE' });
    if (!threat || !threat._map.size) {
      card.createEl('p', { cls: 'ccc-empty', text: 'Threat index warming up — rebuilds every 5 min.' });
      return;
    }
    const blips = [];
    for (const [path, st] of threat._map) {
      if (!path.startsWith('Clients/') && !path.startsWith('02_Areas/Accounts/')) continue;
      if (st.silenceDays == null) continue;
      const seg = path.split('/');
      const name = (seg[0] === 'Clients' ? seg[1] : seg[2] || seg[1] || path).replace(/\.md$/, '');
      if (!name || blips.some(b => b.name === name)) continue; // one blip per account
      blips.push({ path, name, days: st.silenceDays, level: st.level, reasons: st.reasons });
    }
    if (!blips.length) {
      card.createEl('p', { cls: 'ccc-empty', text: 'No accounts with contact signals yet — link clients from dated Meetings/ notes or set last_touch in their _brief.md.' });
      return;
    }
    blips.sort((a, b) => b.days - a.days);

    const W = 230, C = W / 2, R = C - 10;
    const cv = card.createEl('canvas');
    cv.width = W * 2; cv.height = W * 2;
    cv.style.cssText = `width:${W}px;height:${W}px;display:block;margin:4px auto;`;
    const ctx = cv.getContext('2d');
    ctx.scale(2, 2);
    const hash = (s) => { let h = 0; for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0; return h; };
    const blipPt = (b) => {
      const ang = (((hash(b.name) % 360) + 360) % 360) * Math.PI / 180;
      const r = R * (1 - 0.85 * Math.min(1, b.days / 45)); // 45d silent = center
      return { x: C + Math.cos(ang) * r, y: C + Math.sin(ang) * r, ang };
    };
    const LEVEL_C = { healthy: 'rgba(159,255,189,0.8)', monitor: 'rgba(245,166,35,0.9)', threat: 'rgba(255,92,92,1)' };
    let sweep = 0;
    let lastFrame = 0;
    const draw = (now) => {
      if (!cv.isConnected) return; // tab re-rendered — this loop dies with its canvas
      // perf-audit-2026-06-10: a background workspace tab keeps the canvas connected but
      // display:none (offsetParent null) — idle at 1Hz there instead of painting unseen frames.
      if (!cv.offsetParent) { setTimeout(() => requestAnimationFrame(draw), 1000); return; }
      // resource cap: 30fps is indistinguishable for a radar sweep; halves the GPU/CPU draw cost
      if (now && now - lastFrame < 33) { requestAnimationFrame(draw); return; }
      lastFrame = now || 0;
      ctx.clearRect(0, 0, W, W);
      ctx.strokeStyle = 'rgba(127,0,218,0.35)';
      ctx.lineWidth = 1;
      for (const rr of [R, R * 0.66, R * 0.33]) { ctx.beginPath(); ctx.arc(C, C, rr, 0, 6.2832); ctx.stroke(); }
      ctx.strokeStyle = 'rgba(127,0,218,0.18)';
      ctx.beginPath(); ctx.moveTo(C - R, C); ctx.lineTo(C + R, C); ctx.moveTo(C, C - R); ctx.lineTo(C, C + R); ctx.stroke();
      const grad = ctx.createConicGradient ? ctx.createConicGradient(sweep, C, C) : null;
      if (grad) {
        grad.addColorStop(0, 'rgba(248,240,96,0.22)');
        grad.addColorStop(0.12, 'rgba(248,240,96,0)');
        grad.addColorStop(1, 'rgba(248,240,96,0)');
        ctx.fillStyle = grad;
        ctx.beginPath(); ctx.moveTo(C, C); ctx.arc(C, C, R, 0, 6.2832); ctx.fill();
      }
      const t = performance.now() / 1000;
      for (const b of blips) {
        const p = blipPt(b);
        const pulse = b.level === 'threat' ? 1.6 + Math.sin(t * 5) * 1.2 : 0;
        ctx.fillStyle = LEVEL_C[b.level] || LEVEL_C.healthy;
        ctx.beginPath(); ctx.arc(p.x, p.y, 3.4 + pulse, 0, 6.2832); ctx.fill();
        // sweep proximity glow: blip name surfaces as the beam passes
        let dA = ((p.ang - sweep) % 6.2832 + 6.2832) % 6.2832;
        if (dA < 0.5) {
          ctx.fillStyle = 'rgba(248,240,96,' + (0.9 * (1 - dA / 0.5)) + ')';
          ctx.font = '9px var(--font-interface, sans-serif)';
          ctx.fillText(b.name.slice(0, 16) + ' ' + b.days + 'd', Math.min(p.x + 6, W - 70), p.y - 5);
        }
      }
      ctx.fillStyle = 'rgba(248,240,96,0.9)';
      ctx.beginPath(); ctx.arc(C, C, 2.2, 0, 6.2832); ctx.fill();
      sweep = (sweep + 0.016) % 6.2832;
      requestAnimationFrame(draw); // Chromium throttles when hidden — fine
    };
    requestAnimationFrame(draw);

    const hit = (e) => {
      const r = cv.getBoundingClientRect();
      const m = { x: e.clientX - r.left, y: e.clientY - r.top };
      return blips.find(b => { const p = blipPt(b); return Math.hypot(p.x - m.x, p.y - m.y) < 10; });
    };
    cv.addEventListener('mousemove', (e) => {
      const b = hit(e);
      cv.title = b ? `${b.name} — ${b.reasons.join(' · ')}` : '';
      cv.style.cursor = b ? 'pointer' : 'default';
    });
    cv.addEventListener('click', (e) => {
      const b = hit(e);
      if (b) this.app.workspace.openLinkText(b.path, '', false);
    });

    const top = blips.find(b => b.level !== 'healthy');
    if (top) {
      const row = card.createDiv({ cls: 'ccc-aggro-action' });
      row.createEl('span', { cls: 'ccc-list-primary', text: `⚠ ${top.name} — ${top.days}d silent` });
      const draft = row.createEl('button', { cls: 'ccc-aggro-btn', text: 'Draft check-in' });
      draft.addEventListener('click', () => this._draftCheckin(top));
      const touched = row.createEl('button', { cls: 'ccc-aggro-btn', text: 'Mark touched' });
      touched.addEventListener('click', () => this._markTouched(top));
    }
  }

  // The reset action: a REAL draft in Outbound/ (review-then-send, never auto)
  async _draftCheckin(b) {
    const today = localDateStr(new Date());
    const path = `Outbound/${b.name}-checkin-${today}.md`;
    try {
      if (!this.app.vault.getAbstractFileByPath(path)) {
        await this.app.vault.create(path, [
          '---',
          'type: email_draft',
          'status: draft',
          `client: ${b.name}`,
          `created: ${today}`,
          `aggro_reason: "${(b.reasons[0] || '').replace(/"/g, "'")}"`,
          '---',
          '',
          `# Check-in — ${b.name}`,
          '',
          `> Radar trigger: ${b.reasons.join(' · ')}`,
          '',
          '- [ ] Personalize opener (last meeting / last deliverable)',
          '- [ ] One concrete value hook (new win story, relevant insight)',
          '- [ ] Soft CTA (15-min catch-up)',
          '',
        ].join('\n'));
      }
      this.app.workspace.openLinkText(path, '', false);
      new Notice(`Draft created in Outbound/ — review before sending.`, 4000);
    } catch (e) { new Notice('Draft failed: ' + e.message, 5000); }
  }

  // Stamp last_touch on the account brief — knocks the blip back outward on
  // the next ThreatIndex rebuild (triggered immediately).
  async _markTouched(b) {
    try {
      const f = this.app.vault.getAbstractFileByPath(b.path);
      if (!f) { new Notice('Brief not found: ' + b.path, 4000); return; }
      const today = localDateStr(new Date());
      await this.app.fileManager.processFrontMatter(f, (fm) => { fm.last_touch = today; });
      await this.plugin.threat.rebuild();
      new Notice(`${b.name} marked touched (${today}) — blip knocked back.`, 4000);
      this.paint && this.paint();
    } catch (e) { new Notice('Mark-touched failed: ' + e.message, 5000); }
  }

  // ── TAB: Me (cadence, wins, mood/energy) ───────────────────────────────────
  async _renderMeTab() {
    const container = this._contentEl;
    const grid = container.createDiv({ cls: 'ccc-grid' });
    const c = await this.vaultData.personalCadence();
    if (c.error) { this._errorCard(grid, 'CADENCE', c.error); return; }
    const card = grid.createDiv({ cls: 'ccc-card ccc-card-hero ccc-sec-coverage' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'DAILY CADENCE' });
    const big = card.createDiv({ cls: 'ccc-big-row' });
    big.createEl('span', { cls: 'ccc-giant-stat', text: c.streak + '🔥' });
    big.createEl('span', { cls: 'ccc-caption', text: 'day streak' });
    const subs = card.createDiv({ cls: 'ccc-substats' });
    const mk = (v, l) => { const s = subs.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: String(v) });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
    mk(c.winsThisWeek, 'wins this week'); mk(c.total, 'daily notes');
    mk(c.mood || '—', 'mood'); mk(c.energy || '—', 'energy');
    card.createEl('p', { cls: 'ccc-roi-formula', text: 'Log a daily note + wins to keep the streak. Set mood/energy in frontmatter to trend wellbeing.' });

    // ── Compounding: is the brain making me win more + get smarter? ──────────
    const o = await this.vaultData.outcomes();
    if (!o.error) {
      const cc = grid.createDiv({ cls: 'ccc-card ccc-sec-skills' });
      cc.createEl('p', { cls: 'ccc-eyebrow', text: 'COMPOUNDING — is the brain paying off?' });
      const s1 = cc.createDiv({ cls: 'ccc-substats' });
      const m1 = (v, l) => { const s = s1.createDiv({ cls: 'ccc-substat' });
        s.createEl('span', { cls: 'ccc-substat-val', text: v });
        s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
      m1(o.won + o.lost ? Math.round(o.winRate * 100) + '%' : '—', 'win rate');
      m1(o.accounts ? Math.round(o.coverage * 100) + '%' : '—', 'acct coverage');
      m1(String(o.lessons), 'lessons codified');
      const s2 = cc.createDiv({ cls: 'ccc-substats' });
      const m2 = (v, l) => { const s = s2.createDiv({ cls: 'ccc-substat' });
        s.createEl('span', { cls: 'ccc-substat-val', text: v });
        s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
      m2(String(o.capturesThisWeek), 'captures this wk');
      m2(String(o.docsInBrain), 'docs in brain');
      // learning trend verdict
      const dir = o.correctionsThisWeek < o.correctionsLastWeek ? '↓ improving'
        : o.correctionsThisWeek > o.correctionsLastWeek ? '↑ watch' : '→ steady';
      m2(`${o.correctionsThisWeek}/${o.correctionsLastWeek} ${dir}`, 'corrections wk/prev');
      const verdict = (o.won + o.lost === 0)
        ? 'Win-rate lights up when you close a bid. Fill the two scaffolds → pipeline starts steering.'
        : `Win rate ${Math.round(o.winRate * 100)}% on ${o.won + o.lost} closed · ${o.lessons} lessons codified so the same mistake never costs twice.`;
      cc.createEl('p', { cls: 'ccc-roi-formula', text: verdict });
    }
  }

  _errorCard(container, title, msg, onRetry) {
    const card = container.createDiv({ cls: 'ccc-card ccc-card-error' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: title });
    card.createEl('p', { cls: 'ccc-error-msg', text: '⚠ ' + msg });
    if (typeof onRetry === 'function') {
      const btn = card.createEl('button', { cls: 'ccc-retry-btn', text: '↻ Retry' });
      btn.addEventListener('click', () => { try { onRetry(); } catch (_) {} });
    }
  }
  // Standard retry: drop cached scans + re-render so a transient data failure self-heals.
  _retry() { return () => { try { this.vaultData.invalidate(); this._enqueueRender(() => this._refresh()); } catch (_) {} }; }

  // ── Card 0: Claude Usage (Claude Code /usage replica) ──────────────────────

  _renderUsageCard(container) {
    const card = container.createDiv({ cls: 'ccc-card ccc-card-usage ccc-sec-spend' });
    const st = this.plugin.settings.usagePanel ||
      (this.plugin.settings.usagePanel = { view: 'overview', range: 'all' });

    // Header: Overview | Models on the left · All / 30d / 7d on the right
    const head = card.createDiv({ cls: 'ccc-usage-head' });
    const tabsEl = head.createDiv({ cls: 'ccc-usage-tabs' });
    const rangesEl = head.createDiv({ cls: 'ccc-usage-ranges' });
    const body = card.createDiv({ cls: 'ccc-usage-body' });

    const tabBtns = {}, rangeBtns = {};
    const paint = () => {
      for (const [k, b] of Object.entries(tabBtns)) b.classList.toggle('ccc-usage-active', k === st.view);
      for (const [k, b] of Object.entries(rangeBtns)) b.classList.toggle('ccc-usage-active', k === st.range);
      body.empty();
      const data = this._usageData;
      if (!data) { body.createEl('p', { cls: 'ccc-usage-loading', text: 'Scanning Claude Code transcripts…' }); return; }
      if (data.error) { body.createEl('p', { cls: 'ccc-error-msg', text: '⚠ ' + data.error }); return; }
      const rangeDays = st.range === 'all' ? null : Number(st.range);
      const stats = computeUsageStats(data.files, rangeDays);
      if (st.view === 'models') this._paintUsageModels(body, stats);
      else this._paintUsageOverview(body, stats);
    };

    for (const [k, label] of [['overview', 'Overview'], ['models', 'Models']]) {
      const b = tabsEl.createEl('button', { cls: 'ccc-usage-tab', text: label });
      b.addEventListener('click', () => { st.view = k; this.plugin.saveSettings().catch(() => {}); paint(); });
      tabBtns[k] = b;
    }
    for (const [k, label] of [['all', 'All'], ['30', '30d'], ['7', '7d']]) {
      const b = rangesEl.createEl('button', { cls: 'ccc-usage-range', text: label });
      b.addEventListener('click', () => { st.range = k; this.plugin.saveSettings().catch(() => {}); paint(); });
      rangeBtns[k] = b;
    }

    paint(); // immediate (loading state) — data fills in async without blocking the grid
    this.vaultData.claudeUsage().then(data => { this._usageData = data; paint(); });
  }

  // Overview sub-tab: stat tiles + activity heatmap + book comparison
  _paintUsageOverview(body, s) {
    const tiles = body.createDiv({ cls: 'ccc-usage-tiles' });
    const tile = (label, val) => {
      const t = tiles.createDiv({ cls: 'ccc-usage-tile' });
      t.createEl('span', { cls: 'ccc-usage-tile-lbl', text: label });
      t.createEl('span', { cls: 'ccc-usage-tile-val', text: String(val) });
    };
    tile('Sessions', s.sessions.toLocaleString());
    tile('Messages', s.messages.toLocaleString());
    tile('Total tokens', fmtUsage(s.grandTotal));
    tile('Active days', s.activeDays);
    tile('Current streak', s.currentStreak + 'd');
    tile('Longest streak', s.longestStreak + 'd');
    tile('Peak hour', s.peakHour);
    tile('Favorite model', s.favoriteModel);

    const grid = body.createDiv({ cls: 'ccc-usage-heat' });
    grid.style.gridTemplateRows = 'repeat(7, 1fr)';
    for (const c of s.heat) {
      const cell = grid.createDiv({ cls: 'ccc-usage-heat-cell ccc-usage-heat-' + (c.future ? 'future' : c.level) });
      if (!c.future) cell.title = c.date + (c.total ? ' · ' + fmtUsage(c.total) + ' tokens' : ' · no usage');
    }

    if (s.comparison) body.createEl('p', { cls: 'ccc-usage-compare', text: s.comparison });
  }

  // Models sub-tab: stacked per-day bar chart + per-model legend
  _paintUsageModels(body, s) {
    if (!s.models.length) {
      body.createEl('p', { cls: 'ccc-usage-loading', text: 'No model usage in this period.' });
      return;
    }
    const axisMax = niceAxisMax(Math.max(...s.chartDays.map(d => d.total || 0), 1));

    const chart = body.createDiv({ cls: 'ccc-usage-chart' });
    // gridlines + y labels (top = axisMax … bottom = 0)
    for (let i = 0; i <= 4; i++) {
      const v = axisMax * (1 - i / 4);
      const line = chart.createDiv({ cls: 'ccc-usage-gridline' });
      line.style.top = (i / 4) * 100 + '%';
      line.createEl('span', { cls: 'ccc-usage-gridlbl', text: fmtAxis(v) });
    }
    const bars = chart.createDiv({ cls: 'ccc-usage-bars' });
    const ranked = [...s.models]; // biggest first — render reversed so rank 1 sits at the bottom
    for (const day of s.chartDays) {
      const col = bars.createDiv({ cls: 'ccc-usage-col' });
      const tipLines = [new Date(day.date + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) +
        ' · ' + fmtUsage(day.total || 0) + ' tokens'];
      for (let i = ranked.length - 1; i >= 0; i--) {
        const m = ranked[i];
        const dm = (day.models || {})[m.id];
        if (!dm) continue;
        const tok = dm.in + dm.out + (dm.cr || 0) + (dm.cc || 0); // match day.total (incl. cache) so segments fill the bar
        if (tok <= 0) continue;
        const seg = col.createDiv({ cls: 'ccc-usage-seg' });
        seg.style.height = Math.max((tok / axisMax) * 100, 1.5) + '%';
        seg.style.background = m.color;
        tipLines.push(m.name + ': ' + fmtUsage(tok));
      }
      col.title = tipLines.join('\n');
    }
    // x-axis date labels (≤8, evenly spaced)
    const xrow = body.createDiv({ cls: 'ccc-usage-xrow' });
    const n = s.chartDays.length;
    const step = Math.max(1, Math.ceil(n / 8));
    s.chartDays.forEach((day, i) => {
      const cell = xrow.createDiv({ cls: 'ccc-usage-xcell' });
      if (i % step === 0) {
        cell.setText(new Date(day.date + 'T12:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }));
      }
    });

    const legend = body.createDiv({ cls: 'ccc-usage-legend' });
    for (const m of s.models) {
      const row = legend.createDiv({ cls: 'ccc-usage-leg-row' });
      const dot = row.createEl('span', { cls: 'ccc-usage-leg-dot' });
      dot.style.background = m.color;
      row.createEl('span', { cls: 'ccc-usage-leg-name', text: m.name });
      row.createEl('span', { cls: 'ccc-usage-leg-io', text: fmtUsage(m.in) + ' in · ' + fmtUsage(m.out) + ' out' });
      row.createEl('span', { cls: 'ccc-usage-leg-pct', text: (m.share * 100).toFixed(1) + '%' });
    }
  }

  // ── Card 1: Token Burn ────────────────────────────────────────────────────

  _renderTokenCard(container, stats) {
    if (stats.error) { this._errorCard(container, 'CLAUDE SPEND', stats.error); return; }

    const card = container.createDiv({ cls: 'ccc-card ccc-card-hero ccc-sec-spend' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'CLAUDE SPEND' });

    const bigRow = card.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: 'C$' + stats.todayCost.toFixed(2) });
    bigRow.createEl('span', { cls: 'ccc-caption', text: 'today' });

    const subs = card.createDiv({ cls: 'ccc-substats' });
    const s1 = subs.createDiv({ cls: 'ccc-substat' });
    s1.createEl('span', { cls: 'ccc-substat-val', text: 'C$' + stats.weekCost.toFixed(2) });
    s1.createEl('span', { cls: 'ccc-substat-lbl', text: '7-day' });

    const s2 = subs.createDiv({ cls: 'ccc-substat' });
    s2.createEl('span', { cls: 'ccc-substat-val', text: 'C$' + stats.allTimeCost.toFixed(2) });
    s2.createEl('span', { cls: 'ccc-substat-lbl', text: 'all-time' });

    const s3 = subs.createDiv({ cls: 'ccc-substat' });
    s3.createEl('span', { cls: 'ccc-substat-val', text: String(stats.allTimeSessions) });
    s3.createEl('span', { cls: 'ccc-substat-lbl', text: 'sessions' });

    // ── Tokens used ──────────────────────────────────────────────────────────
    const tokWrap = card.createDiv({ cls: 'ccc-tokens' });
    tokWrap.createEl('span', { cls: 'ccc-tokens-eyebrow', text: 'TOKENS · today' });
    const tokRow = tokWrap.createDiv({ cls: 'ccc-token-row' });
    const tok = (parent, icon, val, lbl, cls) => {
      const t = parent.createDiv({ cls: 'ccc-token' + (cls ? ' ' + cls : '') });
      t.createEl('span', { cls: 'ccc-token-icon', text: icon });
      t.createEl('span', { cls: 'ccc-token-val', text: fmtTokens(val) });
      t.createEl('span', { cls: 'ccc-token-lbl', text: lbl });
    };
    tok(tokRow, '↓', stats.todayIn, 'in', 'ccc-tok-in');
    tok(tokRow, '↑', stats.todayOut, 'out', 'ccc-tok-out');
    tok(tokRow, '♺', stats.todayCacheRead, 'cached', 'ccc-tok-cache');
    tok(tokRow, 'Σ', stats.todayTotal, 'total', 'ccc-tok-total');

    const tokRow2 = tokWrap.createDiv({ cls: 'ccc-token-row ccc-token-row-alltime' });
    tokRow2.createEl('span', { cls: 'ccc-tokens-eyebrow', text: 'all-time' });
    tok(tokRow2, '↓', stats.allTimeIn, 'in', 'ccc-tok-in');
    tok(tokRow2, '↑', stats.allTimeOut, 'out', 'ccc-tok-out');
    tok(tokRow2, '♺', stats.allTimeCacheRead, 'cached', 'ccc-tok-cache');
    tok(tokRow2, 'Σ', stats.allTimeTotal, 'total', 'ccc-tok-total');

    // Sparkline
    const values = stats.last7.map(d => d.cost);
    const heights = normalizeBars(values);
    const todayStr = localDateStr();
    const spark = card.createDiv({ cls: 'ccc-spark' });
    for (let i = 0; i < stats.last7.length; i++) {
      const isToday = stats.last7[i].date === todayStr;
      const bar = spark.createDiv({ cls: 'ccc-spark-bar' + (isToday ? ' ccc-spark-today' : '') });
      bar.style.height = Math.max(heights[i], 3) + '%';
      bar.title = stats.last7[i].date + ': C$' + stats.last7[i].cost.toFixed(2);
    }
  }

  // ── Card 1b: Time saved · ROI ─────────────────────────────────────────────

  _renderRoiCard(container, stats) {
    if (stats.error) return; // token card already surfaced the error
    const cfg = Object.assign(
      { hourlyRate: 100, minPer1kOutput: 2.5, minPerSession: 10, usdToCad: 1.37 },
      (this.plugin && this.plugin.settings && this.plugin.settings.roi) || {}
    );
    const today = computeRoi(stats.todayOut, stats.todaySessions, stats.todayCost, cfg);
    const week = computeRoi(stats.weekOut, stats.weekSessions, stats.weekCost, cfg);
    const all = computeRoi(stats.allTimeOut, stats.allTimeSessions, stats.allTimeCost, cfg);

    const card = container.createDiv({ cls: 'ccc-card ccc-sec-skills' }); // green = value
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'TIME SAVED · ROI' });

    const bigRow = card.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: today.hours.toFixed(1) + 'h' });
    bigRow.createEl('span', { cls: 'ccc-caption', text: 'saved today' });

    const fmtEur = (n) => 'C$' + Math.round(n).toLocaleString();
    const subs = card.createDiv({ cls: 'ccc-substats' });
    const mk = (parent, val, lbl) => {
      const s = parent.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: val });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: lbl });
    };
    mk(subs, fmtEur(today.value), 'value today');
    mk(subs, week.hours.toFixed(1) + 'h', '7-day saved');
    mk(subs, all.hours.toFixed(0) + 'h', 'all-time saved');

    const subs2 = card.createDiv({ cls: 'ccc-substats' });
    mk(subs2, fmtEur(all.value), 'value all-time');
    mk(subs2, all.roi.toFixed(1) + '×', 'ROI vs spend');
    mk(subs2, fmtEur(all.value - stats.allTimeCost), 'net gain');

    const note = card.createEl('p', { cls: 'ccc-roi-formula' });
    note.setText(`≈ ${cfg.minPer1kOutput} min / 1k output tokens + ${cfg.minPerSession} min / session, valued at C$${cfg.hourlyRate}/h · USD spend ×${cfg.usdToCad} → CAD · estimate, tunable in settings`);
  }

  // ── Card 2: Meetings ──────────────────────────────────────────────────────

  _renderMeetingsCard(container, meetings) {
    if (meetings.error) { this._errorCard(container, 'MEETINGS', meetings.error); return; }

    const card = container.createDiv({ cls: 'ccc-card ccc-sec-meetings' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'MEETINGS' });

    const bigRow = card.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: String(meetings.countWeek) });
    bigRow.createEl('span', { cls: 'ccc-caption', text: 'this week' });

    const subs = card.createDiv({ cls: 'ccc-substats' });
    const s1 = subs.createDiv({ cls: 'ccc-substat' });
    s1.createEl('span', { cls: 'ccc-substat-val', text: String(meetings.countToday) });
    s1.createEl('span', { cls: 'ccc-substat-lbl', text: 'today' });
    const s2 = subs.createDiv({ cls: 'ccc-substat' });
    s2.createEl('span', { cls: 'ccc-substat-val', text: String(meetings.countMonth) });
    s2.createEl('span', { cls: 'ccc-substat-lbl', text: 'this month' });

    if (meetings.recent.length === 0) {
      card.createEl('p', { cls: 'ccc-empty', text: 'No meeting records found.' });
      return;
    }

    const list = card.createDiv({ cls: 'ccc-list' });
    for (const m of meetings.recent) {
      const row = list.createDiv({ cls: 'ccc-list-row' });
      row.createEl('span', { cls: 'ccc-list-primary', text: m.client || m.path.split('/').pop() });
      row.createEl('span', { cls: 'ccc-list-secondary', text: m.meeting_type });
      row.createEl('span', { cls: 'ccc-list-meta', text: fmtDate(m.date) });
      row.addEventListener('click', () => {
        this.app.workspace.openLinkText(m.path, '', false);
      });
    }
  }

  // ── Card 3: AI Fleet ──────────────────────────────────────────────────────

  _renderFleetCard(container, fleet) {
    if (fleet.error) { this._errorCard(container, 'AI FLEET', fleet.error); return; }

    const card = container.createDiv({ cls: 'ccc-card ccc-sec-fleet' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'AI FLEET' });

    const TOOL_COLORS = {
      claude: '#D97757',
      codex: '#10A37F',
      gemini: '#4285F4',
      dust: '#FF9533',
    };

    const tools = fleet.tools || {};
    const toolNames = Object.keys(tools);

    if (toolNames.length === 0) {
      card.createEl('p', { cls: 'ccc-empty', text: 'No AI session records found.' });
      return;
    }

    const list = card.createDiv({ cls: 'ccc-list' });
    for (const tool of toolNames) {
      const data = tools[tool];
      const row = list.createDiv({ cls: 'ccc-list-row ccc-fleet-row' });

      const dot = row.createEl('span', { cls: 'ccc-fleet-dot' });
      dot.style.background = TOOL_COLORS[tool.toLowerCase()] || '#9b86b8';

      row.createEl('span', { cls: 'ccc-list-primary', text: tool });
      row.createEl('span', { cls: 'ccc-list-meta', text: String(data.all) + ' total' });

      if (data.last7 > 0) {
        row.createEl('span', { cls: 'ccc-pill', text: '+' + data.last7 + ' this week' });
      }
    }
  }

  // ── Card 4: Pipeline ──────────────────────────────────────────────────────

  _renderPipelineCard(container, pipeline) {
    if (pipeline.error) { this._errorCard(container, 'OPEN BIDS', pipeline.error); return; }

    const card = container.createDiv({ cls: 'ccc-card ccc-sec-pipeline' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'OPEN BIDS' });

    const bigRow = card.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: String(pipeline.open) });
    bigRow.createEl('span', { cls: 'ccc-caption', text: 'active' });

    if (pipeline.bids.length === 0) {
      card.createEl('p', { cls: 'ccc-empty', text: 'No open bids.' });
      return;
    }

    const list = card.createDiv({ cls: 'ccc-list' });
    for (const bid of pipeline.bids) {
      const row = list.createDiv({ cls: 'ccc-list-row' });

      row.createEl('span', { cls: 'ccc-list-primary', text: bid.opportunity });

      row.createEl('span', { cls: 'ccc-badge', text: bid.stage || 'Unknown' });

      if (bid.overdue) {
        row.createEl('span', { cls: 'ccc-badge ccc-badge-danger', text: 'OVERDUE' });
      } else if (bid.closingSoon) {
        row.createEl('span', { cls: 'ccc-badge ccc-badge-danger', text: 'CLOSING' });
      }

      if (bid.deadline) {
        row.createEl('span', { cls: 'ccc-list-meta', text: fmtDate(bid.deadline) });
      }

      row.addEventListener('click', () => {
        this.app.workspace.openLinkText(bid.path, '', false);
      });
    }
  }

  // ── Card 5: Important ─────────────────────────────────────────────────────

  _renderImportantCard(container, important) {
    if (important.error) { this._errorCard(container, 'IMPORTANT', important.error); return; }

    const card = container.createDiv({ cls: 'ccc-card ccc-sec-important' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'IMPORTANT' });

    const items = important.items || [];
    if (items.length === 0) {
      card.createEl('p', { cls: 'ccc-empty', text: 'Nothing flagged.' });
      return;
    }

    const list = card.createDiv({ cls: 'ccc-list' });
    for (const item of items) {
      const row = list.createDiv({ cls: 'ccc-list-row' });
      row.createEl('span', { cls: 'ccc-list-primary', text: item.name });
      row.createEl('span', { cls: 'ccc-list-meta', text: relativeTime(item.mtime) });
      row.addEventListener('click', () => {
        this.app.workspace.openLinkText(item.path, '', false);
      });
    }
  }

  // ── Meetings Tab ──────────────────────────────────────────────────────────

  async _renderMeetingsTab() {
    const container = this._contentEl;
    const data = await this.vaultData.meetings();
    if (data.error) {
      this._errorCard(container, 'MEETINGS', data.error);
      return;
    }

    const { records, monthStr, lastMonthStr } = data;

    // ── Month switcher pills ────────────────────────────────────────────────
    const pillRow = container.createDiv({ cls: 'ccc-month-pills' });
    const makeMonthPill = (label, value) => {
      const active = this._meetingsMonth === value;
      const pillBtn = pillRow.createEl('button', {
        cls: 'ccc-month-pill' + (active ? ' ccc-month-pill-active' : ''),
        text: label,
      });
      // I2: month pill click goes through render queue
      pillBtn.addEventListener('click', () => {
        if (this._meetingsMonth === value) return;
        this._meetingsMonth = value;
        this._enqueueRender(() => {
          container.empty();
          return this._renderMeetingsTab();
        });
      });
    };
    makeMonthPill('This month', 'this');
    makeMonthPill('Last month', 'last');

    // Determine which month to show
    const activeMonthStr = this._meetingsMonth === 'this' ? monthStr : lastMonthStr;
    const monthRecords = records.filter(r => r.date.startsWith(activeMonthStr));
    const totalMeetings = monthRecords.length;
    const transcribedCount = monthRecords.filter(r => r.transcribed).length;

    // ── Coverage stat card ──────────────────────────────────────────────────
    const coverCard = container.createDiv({ cls: 'ccc-card ccc-sec-coverage' });
    coverCard.createEl('p', { cls: 'ccc-eyebrow', text: 'COVERAGE — ' + activeMonthStr });
    const bigRow = coverCard.createDiv({ cls: 'ccc-big-row' });
    bigRow.createEl('span', { cls: 'ccc-giant-stat', text: String(totalMeetings) });
    bigRow.createEl('span', { cls: 'ccc-caption', text: totalMeetings === 1 ? 'meeting' : 'meetings' });

    const subs = coverCard.createDiv({ cls: 'ccc-substats' });
    const s1 = subs.createDiv({ cls: 'ccc-substat' });
    s1.createEl('span', { cls: 'ccc-substat-val', text: String(transcribedCount) });
    s1.createEl('span', { cls: 'ccc-substat-lbl', text: 'transcribed' });
    const s2 = subs.createDiv({ cls: 'ccc-substat' });
    s2.createEl('span', { cls: 'ccc-substat-val', text: String(totalMeetings - transcribedCount) });
    s2.createEl('span', { cls: 'ccc-substat-lbl', text: 'pending' });

    if (totalMeetings === 0) {
      coverCard.createEl('p', { cls: 'ccc-empty', text: 'No meetings recorded for this period.' });
    }

    // ── Grouped by client ──────────────────────────────────────────────────
    if (monthRecords.length > 0) {
      // Group: alpha, unknown → "— unfiled"
      const clientMap = {};
      for (const r of monthRecords) {
        const key = r.client ? r.client : '— unfiled';
        if (!clientMap[key]) clientMap[key] = [];
        clientMap[key].push(r);
      }
      const clientNames = Object.keys(clientMap).sort((a, b) => {
        if (a === '— unfiled') return 1;
        if (b === '— unfiled') return -1;
        return a.localeCompare(b);
      });

      for (const clientName of clientNames) {
        const clientCard = container.createDiv({ cls: 'ccc-card ccc-sec-meetings' });
        clientCard.createEl('p', { cls: 'ccc-eyebrow', text: clientName.toUpperCase() });

        const list = clientCard.createDiv({ cls: 'ccc-list' });
        for (const m of clientMap[clientName]) {
          const row = list.createDiv({ cls: 'ccc-list-row' });

          row.createEl('span', { cls: 'ccc-list-meta', text: fmtDate(m.date) });
          row.createEl('span', { cls: 'ccc-list-primary', text: m.meeting_type || m.path.split('/').pop() });

          // Transcript badge
          row.createEl('span', {
            cls: 'ccc-badge' + (m.transcribed ? ' ccc-badge-transcribed' : ' ccc-badge-pending'),
            text: m.transcribed ? '✅ transcribed' : '⏳ pending',
            attr: { title: m.transcript_status || 'no status' },
          });

          row.addEventListener('click', () => {
            this.app.workspace.openLinkText(m.path, '', false);
          });
        }
      }
    }

    // ── Footer links ────────────────────────────────────────────────────────
    const footerLinks = [
      { label: 'Meetings hub', path: 'Meetings/_index.md' },
      { label: 'By client', path: 'Meetings/by-client/_index.md' },
    ];
    const footer = container.createDiv({ cls: 'ccc-tab-footer' });
    for (const link of footerLinks) {
      const exists = this.app.vault.getAbstractFileByPath(link.path);
      if (!exists) continue;
      const btn = footer.createEl('button', { cls: 'ccc-footer-link', text: link.label });
      btn.addEventListener('click', () => {
        this.app.workspace.openLinkText(link.path, '', false);
      });
    }

    // ── Follow-through: open action items from meetings & bids ───────────────
    const fu = await this.vaultData.meetingFollowup();
    if (!fu.error) {
      const card = container.createDiv({ cls: 'ccc-card ccc-sec-important' });
      card.createEl('p', { cls: 'ccc-eyebrow', text: 'FOLLOW-THROUGH' });
      const big = card.createDiv({ cls: 'ccc-big-row' });
      big.createEl('span', { cls: 'ccc-giant-stat', text: String(fu.open) });
      big.createEl('span', { cls: 'ccc-caption', text: 'open action items' });
      const subs = card.createDiv({ cls: 'ccc-substats' });
      const s = subs.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: String(fu.done) });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: 'completed' });
      const total = fu.open + fu.done;
      const s2 = subs.createDiv({ cls: 'ccc-substat' });
      s2.createEl('span', { cls: 'ccc-substat-val', text: total ? Math.round(fu.done / total * 100) + '%' : '—' });
      s2.createEl('span', { cls: 'ccc-substat-lbl', text: 'closed' });
      if (fu.openItems.length) {
        const list = card.createDiv({ cls: 'ccc-list' });
        for (const it of fu.openItems) {
          const row = list.createDiv({ cls: 'ccc-list-row' });
          row.createEl('span', { cls: 'ccc-list-primary', text: '☐ ' + it.text });
          row.createEl('span', { cls: 'ccc-list-meta', text: it.name });
          row.addEventListener('click', () => this.app.workspace.openLinkText(it.path, '', false));
        }
      } else {
        card.createEl('p', { cls: 'ccc-empty', text: 'No open action items 🎉' });
      }
    }
  }

  // ── AI Fleet Tab ──────────────────────────────────────────────────────────

  // ── PATCH-BAY GRAPH (HS-R2 #9): the fleet as a modular synth ───────────────
  // Left jacks = the SBAP registry agents; right jacks = the top-level folders
  // they actually write into (from each agent's writes.jsonl `target`). Cable
  // thickness = write volume, brightness = recency; an agent past 1.5× its
  // expected cadence hangs as a slack grey cable; an agent that never wrote is
  // an unplugged jack. Read-only v1 (drag-to-reroute = registry edit, later).
  async _patchBayCard(container) {
    const ad = this.app.vault.adapter;
    const now = Date.now();
    // memoized 5 min — 29 writes.jsonl reads is not a per-paint cost
    if (!this.plugin._patchData || now - this.plugin._patchData.t > 300000) {
      let reg = { agents: [] };
      try { reg = JSON.parse(await ad.read('_agent_state/_registry.json')); } catch (_) {}
      const rows = [];
      for (const a of (reg.agents || [])) {
        if (!a.agent_name) continue;
        let writes = [];
        try {
          const raw = await ad.read(`_agent_state/${a.agent_name}/writes.jsonl`);
          writes = raw.split('\n').filter(Boolean).slice(-200).map(l => { try { return JSON.parse(l); } catch (_) { return null; } }).filter(Boolean);
        } catch (_) {}
        const byDir = {};
        let lastTs = 0;
        for (const w of writes) {
          const dir = String(w.target || '').split('/')[0] || '(unknown)';
          byDir[dir] = (byDir[dir] || 0) + 1;
          const t = Date.parse(w.ts || '') || 0;
          if (t > lastTs) lastTs = t;
        }
        const cadH = Number(a.expected_cadence_hours) || 0;
        const silentH = lastTs ? (now - lastTs) / 3600000 : null;
        rows.push({
          name: a.agent_name, status: a.status,
          dirs: Object.entries(byDir).sort((x, y) => y[1] - x[1]).slice(0, 3),
          lastTs, stale: !!(cadH && silentH != null && silentH > cadH * 1.5), never: !lastTs,
        });
      }
      this.plugin._patchData = { t: now, rows };
    }
    const rows = this.plugin._patchData.rows;
    if (!rows.length) return;
    const card = container.createDiv({ cls: 'ccc-card ccc-sec-fleet' });
    card.createEl('p', { cls: 'ccc-eyebrow', text: 'PATCH BAY — who writes where' });
    const dirs = [...new Set(rows.flatMap(r => r.dirs.map(d => d[0])))];
    const ROW_H = 15, W = Math.min(560, (card.clientWidth || 460) - 8), H = Math.max(rows.length, dirs.length) * ROW_H + 16;
    const cv = card.createEl('canvas');
    cv.width = W * 2; cv.height = H * 2;
    cv.style.cssText = `width:${W}px;height:${H}px;display:block;`;
    const ctx = cv.getContext('2d');
    ctx.scale(2, 2);
    ctx.font = '9px sans-serif';
    const leftY = (i) => 12 + i * ROW_H;
    const rightY = (i) => 12 + i * ROW_H;
    const LX = 108, RX = W - 96;
    rows.forEach((r, i) => {
      ctx.fillStyle = r.never ? 'rgba(255,255,255,0.3)' : r.stale ? 'rgba(160,160,160,0.8)' : 'rgba(232,220,255,0.95)';
      ctx.fillText(r.name.slice(0, 18), 4, leftY(i) + 3);
      ctx.beginPath(); ctx.arc(LX, leftY(i), 2.4, 0, 6.2832);
      ctx.fillStyle = r.never ? 'rgba(255,255,255,0.25)' : r.stale ? '#888' : '#7F00DA';
      ctx.fill();
    });
    dirs.forEach((d, i) => {
      ctx.fillStyle = 'rgba(232,220,255,0.85)';
      ctx.fillText(d.slice(0, 16), RX + 8, rightY(i) + 3);
      ctx.beginPath(); ctx.arc(RX, rightY(i), 2.4, 0, 6.2832);
      ctx.fillStyle = '#F8F060'; ctx.fill();
    });
    const cables = [];
    rows.forEach((r, i) => {
      r.dirs.forEach(([dir, count]) => {
        const j = dirs.indexOf(dir);
        const ageDays = r.lastTs ? (now - r.lastTs) / 86400000 : 99;
        const alpha = r.stale ? 0.22 : Math.max(0.25, Math.min(0.9, 1 - ageDays / 30));
        const sag = r.stale ? 38 : 10; // stale cables hang slack
        const y1 = leftY(i), y2 = rightY(j);
        ctx.beginPath();
        ctx.moveTo(LX + 3, y1);
        ctx.bezierCurveTo(LX + (RX - LX) * 0.35, y1 + sag, LX + (RX - LX) * 0.65, y2 + sag, RX - 3, y2);
        ctx.lineWidth = Math.min(4, 0.7 + Math.log2(1 + count));
        ctx.strokeStyle = r.stale ? `rgba(150,150,150,${alpha})` : `rgba(127,0,218,${alpha})`;
        ctx.stroke();
        cables.push({ r, dir, count, y1, y2 });
      });
    });
    cv.addEventListener('mousemove', (e) => {
      const rect = cv.getBoundingClientRect();
      const my = e.clientY - rect.top, mx = e.clientX - rect.left;
      const t = (mx - LX) / Math.max(1, RX - LX);
      let best = null, bd = 12;
      for (const c of cables) {
        const y = c.y1 + (c.y2 - c.y1) * Math.max(0, Math.min(1, t));
        const d = Math.abs(my - y);
        if (d < bd) { bd = d; best = c; }
      }
      cv.title = best ? `${best.r.name} → ${best.dir} · ${best.count} writes · ${best.r.stale ? 'STALE (past 1.5× cadence)' : best.r.lastTs ? 'last ' + new Date(best.r.lastTs).toISOString().slice(0, 10) : 'never wrote'}` : '';
    });
    const stale = rows.filter(r => r.stale).length, never = rows.filter(r => r.never).length;
    card.createEl('p', { cls: 'ccc-list-meta', text: `${rows.length} jacks · ${stale} slack (stale) · ${never} unplugged (never wrote)` });
  }

  async _renderFleetTab() {
    const container = this._contentEl;
    this._patchBayCard(container).catch(() => {});

    const TOOL_COLORS = {
      claude: '#D97757',
      codex: '#10A37F',
      gemini: '#4285F4',
      dust: '#FF9533',
    };

    // Fetch all fleet data in parallel
    const [fleet, triage, mistakes] = await Promise.all([
      this.vaultData.fleet(),
      this.vaultData.fleetTriage(),
      this.vaultData.fleetMistakes(),
    ]);

    // ── Card 1: Sessions feed ──────────────────────────────────────────────
    if (fleet.error) {
      this._errorCard(container, 'AI SESSIONS', fleet.error);
    } else {
      const sessCard = container.createDiv({ cls: 'ccc-card ccc-sec-fleet' });
      sessCard.createEl('p', { cls: 'ccc-eyebrow', text: 'RECENT SESSIONS' });

      const sessions = fleet.recentSessions || [];
      if (sessions.length === 0) {
        sessCard.createEl('p', { cls: 'ccc-empty', text: 'No AI session records found.' });
      } else {
        const list = sessCard.createDiv({ cls: 'ccc-list' });
        for (const s of sessions) {
          const row = list.createDiv({ cls: 'ccc-list-row ccc-fleet-row' });

          const dot = row.createEl('span', { cls: 'ccc-fleet-dot' });
          dot.style.background = TOOL_COLORS[s.tool.toLowerCase()] || '#9b86b8';
          dot.title = s.tool;

          const summaryText = s.summary || s.basename || s.path.split('/').pop();
          row.createEl('span', { cls: 'ccc-list-primary', text: summaryText });

          row.createEl('span', { cls: 'ccc-list-meta', text: fmtDate(s.date) });

          if (s.cost !== null && s.cost > 0) {
            row.createEl('span', { cls: 'ccc-pill ccc-cost-pill', text: 'C$' + s.cost.toFixed(2) });
          }

          row.addEventListener('click', () => {
            this.app.workspace.openLinkText(s.path, '', false);
          });
        }
      }
    }

    // ── Card 2: Pending triage ────────────────────────────────────────────
    if (triage.error) {
      this._errorCard(container, 'PENDING TRIAGE', triage.error);
    } else {
      const trCard = container.createDiv({ cls: 'ccc-card ccc-sec-triage' });
      trCard.createEl('p', { cls: 'ccc-eyebrow', text: 'PENDING TRIAGE' });

      if (triage.count === 0) {
        trCard.createEl('p', { cls: 'ccc-empty', text: 'Inbox clear ✅' });
      } else {
        const bigRow = trCard.createDiv({ cls: 'ccc-big-row' });
        bigRow.createEl('span', { cls: 'ccc-giant-stat', text: String(triage.count) });
        bigRow.createEl('span', { cls: 'ccc-caption', text: triage.count === 1 ? 'write pending' : 'writes pending' });

        const list = trCard.createDiv({ cls: 'ccc-list' });
        for (const item of triage.items) {
          const row = list.createDiv({ cls: 'ccc-list-row' });
          row.createEl('span', { cls: 'ccc-list-primary', text: item.basename });
          if (item.source_agent) {
            row.createEl('span', { cls: 'ccc-list-secondary', text: item.source_agent });
          }
          if (item.confidence !== null) {
            const confVal = Number(item.confidence);
            const confCls = confVal >= 0.85 ? 'ccc-badge' : 'ccc-badge ccc-badge-danger';
            row.createEl('span', { cls: confCls, text: String(Math.round(confVal * 100)) + '%' });
          }
          row.addEventListener('click', () => {
            this.app.workspace.openLinkText(item.path, '', false);
          });
        }
      }

      // Footer hint: /dust-resolve button
      const trFooter = trCard.createDiv({ cls: 'ccc-tab-footer' });
      const resolveBtn = trFooter.createEl('button', { cls: 'ccc-footer-link', text: 'Resolve with /dust-resolve' });
      resolveBtn.addEventListener('click', () => {
        injectIntoTerminal(this.app, '/dust-resolve');
      });
    }

    // ── Card 3: Mistakes pulse ────────────────────────────────────────────
    if (mistakes.error) {
      this._errorCard(container, 'MISTAKES PULSE', mistakes.error);
    } else {
      const mkCard = container.createDiv({ cls: 'ccc-card ccc-sec-important' });
      mkCard.createEl('p', { cls: 'ccc-eyebrow', text: 'MISTAKES PULSE' });

      const entries = mistakes.entries || [];
      if (entries.length === 0) {
        mkCard.createEl('p', { cls: 'ccc-empty', text: 'No mistakes logged.' });
      } else {
        const list = mkCard.createDiv({ cls: 'ccc-list' });
        for (const e of entries) {
          const row = list.createDiv({ cls: 'ccc-list-row' });
          row.createEl('span', { cls: 'ccc-list-meta', text: e.date });
          row.createEl('span', { cls: 'ccc-list-primary', text: e.summary });
        }
      }

      // Footer links to dont.md and mistakes.md
      const mkFooter = mkCard.createDiv({ cls: 'ccc-tab-footer' });
      const dontBtn = mkFooter.createEl('button', { cls: 'ccc-footer-link', text: "Don't rules" });
      dontBtn.addEventListener('click', () => {
        this.app.workspace.openLinkText('Preferences/dont.md', '', false);
      });
      const mistakesBtn = mkFooter.createEl('button', { cls: 'ccc-footer-link', text: 'Full log' });
      mistakesBtn.addEventListener('click', () => {
        this.app.workspace.openLinkText('Preferences/mistakes.md', '', false);
      });
      const lessonsBtn = mkFooter.createEl('button', { cls: 'ccc-footer-link', text: '📓 Lessons' });
      lessonsBtn.addEventListener('click', () => {
        this.app.workspace.openLinkText('Preferences/Lessons.md', '', false);
      });
    }

    // ── Agent fleet performance + AI leverage trend ─────────────────────────
    const [perf, trend] = await Promise.all([
      this.vaultData.fleetPerf(), this.vaultData.aiTrend(),
    ]);

    const pcard = container.createDiv({ cls: 'ccc-card ccc-sec-fleet' });
    pcard.createEl('p', { cls: 'ccc-eyebrow', text: 'FLEET PERFORMANCE' });
    const psubs = pcard.createDiv({ cls: 'ccc-substats' });
    const pm = (v, l) => { const s = psubs.createDiv({ cls: 'ccc-substat' });
      s.createEl('span', { cls: 'ccc-substat-val', text: String(v) });
      s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
    pm(perf.agentsActive, 'agents active'); pm(perf.agentsNew, 'new'); pm(perf.totalRuns, 'skill runs'); pm(perf.totalCorr, 'corrections');
    const used = (perf.skills || []).filter(s => s.run > 0).sort((a, b) => b.run - a.run);
    if (used.length) {
      pcard.createEl('p', { cls: 'ccc-tokens-eyebrow', text: 'MOST-USED SKILLS' });
      const list = pcard.createDiv({ cls: 'ccc-list' });
      for (const s of used.slice(0, 6)) {
        const row = list.createDiv({ cls: 'ccc-list-row' });
        row.createEl('span', { cls: 'ccc-list-primary', text: s.slug });
        if (s.err > 0) row.createEl('span', { cls: 'ccc-badge ccc-badge-danger', text: s.err + ' err' });
        row.createEl('span', { cls: 'ccc-pill', text: s.run + '×' });
      }
    } else {
      pcard.createEl('p', { cls: 'ccc-empty', text: 'No skill runs logged yet.' });
    }

    if (!trend.error && trend.weeks) {
      const tcard = container.createDiv({ cls: 'ccc-card ccc-sec-spend' });
      tcard.createEl('p', { cls: 'ccc-eyebrow', text: 'AI LEVERAGE · weekly spend' });
      const costs = trend.weeks.map(w => w.cost);
      const heights = normalizeBars(costs);
      const spark = tcard.createDiv({ cls: 'ccc-spark' });
      trend.weeks.forEach((w, i) => {
        const bar = spark.createDiv({ cls: 'ccc-spark-bar' + (i === trend.weeks.length - 1 ? ' ccc-spark-today' : '') });
        bar.style.height = Math.max(heights[i], 3) + '%';
        bar.title = `wk ${w.label}: C$${w.cost.toFixed(2)} · ${fmtTokens(w.out)} out`;
      });
      const last = trend.weeks[trend.weeks.length - 1] || { cost: 0, out: 0 };
      const subs = tcard.createDiv({ cls: 'ccc-substats' });
      const tm = (v, l) => { const s = subs.createDiv({ cls: 'ccc-substat' });
        s.createEl('span', { cls: 'ccc-substat-val', text: v });
        s.createEl('span', { cls: 'ccc-substat-lbl', text: l }); };
      tm('C$' + last.cost.toFixed(2), 'this week'); tm(fmtTokens(last.out), 'out tokens');
    }
  }

  // ── Skills Tab ────────────────────────────────────────────────────────────

  async _renderSkillsTab() {
    const container = this._contentEl;

    // ── Get vault base path ────────────────────────────────────────────────
    let vaultBasePath = '';
    try {
      vaultBasePath = this.app.vault.adapter.basePath || '';
    } catch (_) {}

    // ── Error card helper (inline) ────────────────────────────────────────
    const showError = (msg) => {
      container.empty();
      const card = container.createDiv({ cls: 'ccc-card ccc-card-error' });
      card.createEl('p', { cls: 'ccc-eyebrow', text: 'SKILLS INDEX' });
      card.createEl('p', { cls: 'ccc-error-msg', text: '⚠ ' + msg });
    };

    // ── Build index (lazy, cached) — async ────────────────────────────────
    if (!this._skillIndex) {
      try {
        this._skillIndex = await buildSkillIndex(vaultBasePath);
      } catch (e) {
        showError('Index build failed: ' + e.message);
        return;
      }
    }

    const idx = this._skillIndex;
    if (!idx) { showError('Index unavailable.'); return; }

    // ── Re-render the full skills UI ───────────────────────────────────────
    container.empty();
    this._renderSkillsUI(container, idx, vaultBasePath);
  }

  _renderSkillsUI(container, idx, vaultBasePath) {
    const { entries, counts, timingMs, errors } = idx;

    // ── Header row ─────────────────────────────────────────────────────────
    const headerRow = container.createDiv({ cls: 'ccc-skills-header' });

    // I4: build statLine via DOM API (no inner-html)
    const statLine = headerRow.createEl('p', { cls: 'ccc-skills-stat-line' });
    const appendStat = (val, label) => {
      const strong = statLine.createEl('strong');
      strong.appendText(String(val));
      statLine.appendText(' ' + label);
    };
    appendStat(counts.personal, 'personal');
    statLine.appendText(' · ');
    appendStat(counts.plugin, 'plugin');
    statLine.appendText(' · ');
    appendStat(counts.commands, 'commands');

    const reindexBtn = headerRow.createEl('button', { cls: 'ccc-refresh-btn ccc-skills-reindex-btn', text: '⟳ Re-index' });
    reindexBtn.title = 'Rebuild skill index from disk (' + timingMs + 'ms last run)';
    reindexBtn.addEventListener('click', () => {
      this._skillIndex = null; // bust cache
      this._contentEl.empty();
      this._enqueueRender(() => this._renderSkillsTab());
    });

    if (errors && errors.length > 0) {
      container.createEl('p', { cls: 'ccc-error-msg', text: '⚠ partial errors: ' + errors.join('; ') });
    }

    // ── Search input ───────────────────────────────────────────────────────
    const searchInput = container.createEl('input', {
      cls: 'ccc-skills-search',
      attr: { type: 'text', placeholder: 'Filter skills…' },
    });

    // ── Groups container ───────────────────────────────────────────────────
    const groupsContainer = container.createDiv({ cls: 'ccc-skills-groups' });

    // Render groups, with search state
    const renderGroups = (query) => {
      groupsContainer.empty();

      const q = (query || '').toLowerCase().trim();

      // Group entries by category
      const grouped = {};
      for (const entry of entries) {
        if (!grouped[entry.category]) grouped[entry.category] = [];
        grouped[entry.category].push(entry);
      }

      // Sort categories: Slash Commands first, then alphabetical
      const cats = Object.keys(grouped).sort((a, b) => {
        if (a === 'Slash Commands') return -1;
        if (b === 'Slash Commands') return 1;
        return a.localeCompare(b);
      });

      for (const cat of cats) {
        const catEntries = grouped[cat];

        // Filter
        const filtered = q
          ? catEntries.filter(e =>
              e.name.toLowerCase().includes(q) ||
              e.description.toLowerCase().includes(q)
            )
          : catEntries;

        if (filtered.length === 0) continue;

        // Determine expanded state
        let expanded;
        if (q) {
          expanded = true; // expand all hit groups when searching
        } else if (this._skillGroupState[cat] !== undefined) {
          expanded = this._skillGroupState[cat];
        } else {
          expanded = (cat === 'Slash Commands'); // default: only slash commands open
        }

        // Category header
        const groupEl = groupsContainer.createDiv({ cls: 'ccc-skills-group' });
        const groupHeader = groupEl.createDiv({ cls: 'ccc-skills-group-header' + (expanded ? ' ccc-skills-group-open' : '') });
        const chevron = groupHeader.createEl('span', { cls: 'ccc-skills-chevron', text: expanded ? '▾' : '▸' });
        groupHeader.createEl('span', { cls: 'ccc-skills-group-name', text: cat });
        groupHeader.createEl('span', { cls: 'ccc-skills-group-count', text: String(filtered.length) });

        const rowsEl = groupEl.createDiv({ cls: 'ccc-skills-rows' });
        if (!expanded) rowsEl.style.display = 'none';

        groupHeader.addEventListener('click', () => {
          const nowOpen = rowsEl.style.display === 'none';
          rowsEl.style.display = nowOpen ? 'flex' : 'none';
          chevron.textContent = nowOpen ? '▾' : '▸';
          groupHeader.classList.toggle('ccc-skills-group-open', nowOpen);
          this._skillGroupState[cat] = nowOpen;
        });

        // Skill rows
        for (const entry of filtered) {
          this._renderSkillRow(rowsEl, entry, vaultBasePath);
        }
      }
    };

    // Initial render
    renderGroups('');

    // Search debounce
    searchInput.addEventListener('input', (e) => {
      clearTimeout(this._skillSearchDebounce);
      this._skillSearchDebounce = setTimeout(() => {
        renderGroups(e.target.value);
      }, 150);
    });
  }

  _renderSkillRow(container, entry, vaultBasePath) {
    const row = container.createDiv({ cls: 'ccc-skills-row' });

    // Name + description
    const textEl = row.createDiv({ cls: 'ccc-skills-row-text' });
    textEl.createEl('span', { cls: 'ccc-skills-row-name', text: entry.name });
    if (entry.description) {
      textEl.createEl('span', { cls: 'ccc-skills-row-desc', text: entry.description });
    }

    // Buttons
    const btnGroup = row.createDiv({ cls: 'ccc-skills-row-btns' });

    // Run button
    const runBtn = btnGroup.createEl('button', { cls: 'ccc-skills-btn ccc-skills-btn-run', text: 'Run' });
    runBtn.title = entry.source === 'command'
      ? 'Inject: ' + entry.name
      : 'Inject: Use the ' + entry.name + ' skill: ';
    runBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (entry.source === 'command') {
        injectIntoTerminal(this.app, entry.name);
      } else {
        injectIntoTerminal(this.app, 'Use the ' + entry.name + ' skill: ');
      }
    });

    // Open button
    const openBtn = btnGroup.createEl('button', { cls: 'ccc-skills-btn ccc-skills-btn-open', text: 'Open' });
    openBtn.title = 'Open SKILL.md';
    openBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (entry.vaultPath) {
        this.app.workspace.openLinkText(entry.vaultPath, '', false);
      } else {
        try {
          const electron = (typeof require !== 'undefined') ? require('electron') : window.require('electron');
          electron.shell.openPath(entry.absPath);
        } catch (_) {}
      }
    });
  }
}

// ── ULTRON Orb (the REAL Three.js particle system, bundled from jarvis2.0) ─────
// 2000-particle cloud + connection lines + traveling electrons, 4 states, bass/mid
// audio-reactive. Bundle: jarvis-orb.bundle.js (esbuild of frontend/src/orb.ts + three).
// Floats inside Obsidian (position:fixed), draggable, transparent background.
let _orbBundleLoaded = false;
async function _loadOrbBundle(app) {
  if (_orbBundleLoaded && window.__JarvisOrb) return true;
  try {
    const code = await app.vault.adapter.read('.obsidian/plugins/claude-command-center/jarvis-orb.bundle.js');
    new Function(code)(); // IIFE assigns window.__JarvisOrb = { createOrb }
    _orbBundleLoaded = !!(window.__JarvisOrb && window.__JarvisOrb.createOrb);
    return _orbBundleLoaded;
  } catch (e) { console.error('[CCC] orb bundle load failed', e); return false; }
}

// Module-level predicate: keeps this._history clean both at load-time and at push-time.
// Reused by tts-poison-history-not-persisted, history-poison-01, tts-history-filter-anchor-bug.
function _isCleanHistoryEntry(e) {
  return (
    e && e.content && /[a-z0-9]/i.test(e.content) &&
    !/\[(?:blank_?audio|silence|inaudible|music|sound|no[_ ]?speech)\]/i.test(e.content) &&
    !(e.role === 'Ultron' && /went static|until next time|i'?ll be monitoring|\bstanding by\.?\s*$|ready when you are|^static\b/i.test(e.content))
  );
}

class JarvisOrb {
  constructor(plugin) { this.plugin = plugin; this.visible = false; this._undoStack = []; this._undoLoaded = false; }
  toggle() { this.visible ? this.hide() : this.show(); }

  async show() {
    // orb-lifecycle-evidence: every show/hide/early-return lands in the health
    // log so "the orb disappeared" is never undiagnosable again.
    if (this.visible) { this.plugin && this.plugin._logHealth('orb show() skipped — already visible', 'info'); return; }
    const ok = await _loadOrbBundle(this.plugin.app);
    if (!ok) {
      this.plugin && this.plugin._logHealth('orb show() FAILED — bundle unavailable', 'warn');
      new Notice('Ultron orb: bundle unavailable (rebuild jarvis-orb.bundle.js).', 6000);
      return;
    }
    this.plugin && this.plugin._logHealth('orb show()', 'info');
    this.visible = true;
    // restore cross-session voice memory (persisted in plugin settings)
    if (!this._history) {
      try {
        const h = (this.plugin.settings && this.plugin.settings.voiceHistory) || [];
        // Drop legacy poison: silence transcribed as [BLANK_AUDIO] and the "Static.../Until next
        // time/I'll be monitoring" replies it provoked — otherwise it gets replayed into every
        // prompt and the model keeps inventing a "Tony went silent / goodbye" narrative.
        this._history = h.filter(_isCleanHistoryEntry);
      } catch (_) { this._history = []; }
      // Persist the cleaned history back to disk immediately so data.json cannot re-poison
      // the next session even before a real turn completes (tts-poison-history-not-persisted).
      if (this.plugin && this.plugin.settings) {
        this.plugin.settings.voiceHistory = this._history;
        this.plugin.saveSettings().catch(() => {});
      }
    }
    // Restore muted state across reloads
    this._micMuted = !!((this.plugin && this.plugin.settings && this.plugin.settings.voice && this.plugin.settings.voice.micMuted));
    // Restore wake-word state across reloads (stt-wake-restore-missing)
    this._wakeOn = !!((this.plugin && this.plugin.settings && this.plugin.settings.voice && this.plugin.settings.voice.wakeEnabled));
    document.querySelectorAll('.ccc-orb').forEach(e => e.remove()); // sweep strays from prior plugin loads
    const S = 300;
    const wrap = document.body.createDiv({ cls: 'ccc-orb' });
    this.el = wrap;
    wrap.style.cssText = `position:fixed;right:32px;bottom:64px;width:${S}px;height:${S}px;z-index:99999;cursor:grab;`; // 64px (was 44) — room for the stepper HUD now anchored BELOW the orb (bottom:-52px in styles.css)
    const canvas = wrap.createEl('canvas');
    canvas.style.cssText = `width:${S}px;height:${S}px;display:block;`;
    const x = wrap.createDiv({ cls: 'ccc-orb-x', text: '×' });
    x.addEventListener('click', (e) => { e.stopPropagation(); this.hide(); });
    // Push-to-talk button (click to speak; or just say "Ultron")
    const mic = wrap.createDiv({ cls: 'ccc-orb-mic', text: '🎙' });
    mic.title = 'Push to talk — or say "Ultron"';
    mic.style.cssText = 'position:absolute;left:50%;bottom:-8px;transform:translateX(-50%);cursor:pointer;font-size:22px;line-height:1;opacity:.8;user-select:none;padding:8px 14px;filter:drop-shadow(0 0 6px rgba(76,168,232,.6));';
    this._micBtn = mic; // store ref for _updateMicVisual
    mic.addEventListener('click', (e) => {
      e.stopPropagation();
      // e2e-quality-02 Fix 1: barge-in — cancel the turn (stops playback + clears _busy) then listen
      if (this._sayProc || this._playing) { this._cancelTurn(); setTimeout(() => this.listenOnce(), 80); return; }
      if (this._busy) {                                                         // thinking → cancel, never ignore the click
        this._cancelTurn();
        new Notice('Ultron: cancelled.', 2000); return;
      }
      if (this._micMuted) { new Notice('Ultron mic is muted — press your toggle key to unmute.', 2500); return; }
      // voice-loop-008: re-click while listening = toggle/stop (ChatGPT-like behavior), not a no-op
      if (this._listening || this._pttArmed) {
        const hadWake = !!this._wakeOn;
        this._pttArmed = false; this._listening = false; clearTimeout(this._pttTimer);
        this._stopAudio(true); // explicit off — release the mic even if wake was on
        new Notice(hadWake ? 'Ultron: mic off (wake paused until next listen/orb show).' : 'Ultron: mic off.', 2000); return;
      }
      this.listenOnce();
    });
    this._drag(wrap);
    // Feed the Orb (HS-R2 #15): the orb is a mouth — drop documents on it
    wrap.addEventListener('dragover', (e) => { e.preventDefault(); wrap.classList.add('ccc-orb-feed'); });
    wrap.addEventListener('dragleave', () => wrap.classList.remove('ccc-orb-feed'));
    wrap.addEventListener('drop', (e) => {
      e.preventDefault(); e.stopPropagation();
      wrap.classList.remove('ccc-orb-feed');
      this._feed(e).catch((err) => { new Notice('Ultron: feed failed — ' + err.message, 4000); });
    });
    try { this.orb = window.__JarvisOrb.createOrb(canvas); }
    catch (e) { console.error('[CCC] createOrb failed', e); new Notice('Ultron orb failed to start (WebGL).', 6000); }
    // Self-heal: if Chromium reclaims the WebGL context (e.g. after plugin reloads),
    // rebuild the orb so the particle visual never silently dies into a blank halo.
    canvas.addEventListener('webglcontextlost', (ev) => {
      ev.preventDefault();
      if (this._healing) return; // one heal at a time — repeated loss events must not stack show() calls
      this._healing = true;
      console.warn('[CCC] orb WebGL context lost — self-healing');
      setTimeout(() => { try { if (this.visible) { this._teardown(); this.show(); } } finally { this._healing = false; } }, 400);
    });
    if (this.orb) {
      // synapse-wire-fix: the bundle does NOT mirror state to DOM (its setState
      // is pure WebGL) — the ccc-orb-thinking class was never set by anything,
      // so ambient synapses shipped dead. Wrap setState so every one of the
      // 20+ call sites lands the state on the wrap class the observer watches.
      const _rawSetState = this.orb.setState.bind(this.orb);
      this.orb.setState = (s) => {
        try { wrap.classList.toggle('ccc-orb-thinking', s === 'thinking'); } catch (_) {}
        _rawSetState(s);
      };
      this.orb.setState('idle');
    }
    // synapse layer: observe the wrap class (set by the wrapper above) so
    // ambient synapses run exactly while the orb is thinking.
    try {
      this._synObs = new MutationObserver(() => {
        const on = wrap.classList.contains('ccc-orb-thinking');
        const syn = this.plugin && this.plugin.synapse;
        if (syn) syn.thinking(on);
        const gs = this.plugin && this.plugin.graphSynapse;
        if (gs) gs.thinking(on);
        const ns = this.plugin && this.plugin.noteSynapse;
        if (ns) ns.thinking(on);
      });
      this._synObs.observe(wrap, { attributes: true, attributeFilter: ['class'] });
    } catch (_) {}
    this._updateMicVisual(); // reflect muted state immediately on show
    // Mic policy (STT-01): on-demand mic when wake is off (listenOnce opens it);
    // when the wakeEnabled preference is on, open the mic NOW so "say Ultron" works
    // without a prior click — that was the advertised behavior and it never worked.
    if (this._wakeOn && !this._micMuted) { this._initAudio().catch(() => {}); this._wakeMicNotice(); }
    this._prewarm(); // warm whisper model so first transcribe is fast
    this._startKeepWarm(); // keep brain warm every 75s so cold-start cost never accumulates
    this._sweepTmp(); // clear stale voice temp files from crashed/interrupted runs
    this._loadUndoStack(); // restore last 20 undo snapshots so "undo that" survives a reload
    if (this.plugin) { this.plugin.settings.orbVisible = true; this.plugin.saveSettings(); }
    this._setOrbFlag(true); // durable truth — survives data.json churn/corruption
  }

  // Remove ultron-*/jarvis-* wav/webm strays in tmp older than 1h (interrupted runs
  // leak them; afplay/ffmpeg callbacks can't clean what a crash orphaned).
  _sweepTmp() {
    try {
      const fs = require('fs'), os = require('os'), path = require('path');
      const dir = os.tmpdir(), cutoff = Date.now() - 3600e3;
      for (const f of fs.readdirSync(dir)) {
        if (!/^(ultron|jarvis)-.*\.(wav|webm|txt)$/.test(f)) continue; // .txt = codex final-reply files (ULT-008)
        const p = path.join(dir, f);
        try { if (fs.statSync(p).mtimeMs < cutoff) fs.unlinkSync(p); } catch (_) {}
      }
    } catch (_) {}
  }

  // Full cleanup WITHOUT touching settings.orbVisible — used by plugin unload and
  // the context-loss self-heal, so the orb still auto-restores afterwards.
  _teardown() {
    this.visible = false;
    this.stopWake();
    this.stopSpeaking();
    this._kokoroStop();  // daemons hold models — never outlive the orb
    this._elStop();
    this._neuttsStop();
    this._omniStop();
    this._f5Stop();
    this._brainStop();
    this._codexStop();
    clearTimeout(this._f5IdleTimer);
    clearInterval(this._keepWarmTimer); this._keepWarmTimer = null;
    clearInterval(this._spendTimer); this._spendTimer = null; // perf-sweep-01
    this._playAbort = true; this._speakGen = (this._speakGen || 0) + 1;
    this._pttArmed = false; clearTimeout(this._pttTimer);
    if (this._claudeProc) { try { this._claudeProc.kill(); } catch (_) {} this._claudeProc = null; }
    if (this._levelTimer) { clearInterval(this._levelTimer); this._levelTimer = null; }
    if (this._sp) { try { this._sp.onaudioprocess = null; this._sp.disconnect(); } catch (_) {} this._sp = null; }
    if (this.orb && this.orb.destroy) { try { this.orb.destroy(); } catch (_) {} }
    this.orb = null;
    if (this._synObs) { try { this._synObs.disconnect(); } catch (_) {} this._synObs = null; }
    if (this.plugin && this.plugin.synapse) this.plugin.synapse.thinking(false); // ambient off with the orb
    if (this.plugin && this.plugin.graphSynapse) this.plugin.graphSynapse.thinking(false);
    if (this.plugin && this.plugin.noteSynapse) this.plugin.noteSynapse.thinking(false);
    // Stepper HUD: clear its interval + drop refs (DOM dies with this.el below). No leak across reloads.
    if (this._stepInt) { clearInterval(this._stepInt); this._stepInt = null; }
    this._stepRunning = false; this._stepEl = null; this._stepCells = null; this._stepTimer = null;
    if (this._stream) { this._stream.getTracks().forEach(t => t.stop()); this._stream = null; }
    if (this._actx) { this._actx.close().catch(() => {}); this._actx = null; }
    if (this.el) { this.el.remove(); this.el = null; }
  }

  hide() {
    this.plugin && this.plugin._logHealth('orb hide()', 'info');
    this._teardown();
    if (this.plugin) { this.plugin.settings.orbVisible = false; this.plugin.saveSettings(); }
    this._setOrbFlag(false);
  }

  // orb-flag-fix (2026-06-10): orbVisible lived only in data.json, which is
  // rewritten on every voice turn (ULT-V02) — one torn/stale read during a
  // reload storm flips it false and the next routine save persists the loss,
  // so the orb "disappears" and never auto-restores. The flag FILE is the
  // durable truth: created on show, deleted on hide, immune to data.json
  // churn. (hot-reload ignores it — it only watches main.js/styles.css.)
  _orbFlagPath() {
    const path = require('path');
    return path.join(this._vaultPath(), '.obsidian', 'plugins', 'claude-command-center', 'orb-visible.flag');
  }

  _setOrbFlag(on) {
    try {
      const fs = require('fs');
      if (on) fs.writeFileSync(this._orbFlagPath(), new Date().toISOString());
      else fs.rmSync(this._orbFlagPath(), { force: true });
    } catch (e) {
      this.plugin && this.plugin._logHealth('orb flag write failed: ' + e.message, 'warn');
    }
  }

  // ── Pipeline-stepper HUD ("Ultron is working") ─────────────────────────────
  // Makes the keyless answer-latency floor LEGIBLE: shows which stage Ultron is in
  // plus a LIVE elapsed timer, so silence reads as "processing" not "dead". The orb
  // already swaps WebGL states (idle/listening/thinking/speaking) — this labels them.
  // Stages, in order: listening → transcribing → recalling → thinking → speaking.
  // _setStage(key) is idempotent; null = flash final + fade out + clear timer.
  static get _STEPS() {
    return [
      { key: 'listening',    icon: '🎤', label: 'heard'    },
      { key: 'transcribing', icon: '✍️', label: 'transcribe'},
      { key: 'recalling',    icon: '📚', label: 'recall'    },
      { key: 'thinking',     icon: '🧠', label: 'THINKING'  },
      { key: 'speaking',     icon: '🔊', label: 'speak'     },
    ];
  }

  // Build the HUD DOM once (5 stage cells + a timer span), anchored under the orb. Hidden by default.
  _ensureStepper() {
    if (this._stepEl && this.el && this.el.contains(this._stepEl)) return;
    if (!this.el) return; // orb not mounted yet
    // sweep any stray HUDs from a prior orb mount, then build fresh under the live wrap
    document.querySelectorAll('.ccc-stepper').forEach(e => e.remove());
    const bar = this.el.createDiv({ cls: 'ccc-stepper' });
    this._stepEl = bar;
    this._stepCells = {};
    const STEPS = JarvisOrb._STEPS;
    for (let i = 0; i < STEPS.length; i++) {
      const s = STEPS[i];
      const cell = bar.createDiv({ cls: 'ccc-stepper-cell ccc-step-pending' });
      cell.createSpan({ cls: 'ccc-step-mark', text: '○' });
      cell.createSpan({ cls: 'ccc-step-icon', text: s.icon });
      cell.createSpan({ cls: 'ccc-step-label', text: s.label });
      this._stepCells[s.key] = cell;
      if (i < STEPS.length - 1) bar.createSpan({ cls: 'ccc-stepper-sep', text: '─' });
    }
    this._stepTimer = bar.createSpan({ cls: 'ccc-stepper-elapsed', text: '0.0s' });
  }

  // key ∈ {listening, transcribing, recalling, thinking, speaking, null}.
  // Marks prior stages ✓(done), current ●(pulsing), later ○(pending); shows the HUD;
  // starts the elapsed timer on the FIRST non-null stage of a turn. null → flash + fade out.
  _setStage(key) {
    try {
      const STEPS = JarvisOrb._STEPS;
      const order = STEPS.map(s => s.key);
      if (key == null) { this._hideStepper(); return; }
      const idx = order.indexOf(key);
      if (idx < 0) return; // unknown stage — ignore
      this._ensureStepper();
      if (!this._stepEl) return;
      this._stepStage = key;
      // First non-null stage of a turn → record turn start + (re)start the live elapsed timer.
      if (!this._stepRunning) {
        this._stepRunning = true;
        this._stepStart = Date.now();
        this._startStepTimer();
      }
      // Cascade the cells: < idx = done (✓, dim), == idx = current (●, bold, pulsing), > idx = pending (○, faint).
      for (let i = 0; i < STEPS.length; i++) {
        const cell = this._stepCells[STEPS[i].key];
        if (!cell) continue;
        const mark = cell.querySelector('.ccc-step-mark');
        cell.classList.remove('ccc-step-done', 'ccc-step-current', 'ccc-step-pending');
        if (i < idx)      { cell.classList.add('ccc-step-done');    if (mark) mark.textContent = '✓'; }
        else if (i === idx){ cell.classList.add('ccc-step-current'); if (mark) mark.textContent = '●'; }
        else              { cell.classList.add('ccc-step-pending'); if (mark) mark.textContent = '○'; }
      }
      this._stepEl.classList.remove('ccc-stepper-hiding');
      this._stepEl.classList.add('ccc-stepper-on');
    } catch (_) { /* HUD is cosmetic — never throw into a turn */ }
  }

  // ~100ms elapsed-timer update. Doubles as a leak-proof watchdog: if a turn-end was
  // missed by an aux flow, auto-hide once nothing is busy/playing/listening anymore.
  _startStepTimer() {
    if (this._stepInt) { clearInterval(this._stepInt); this._stepInt = null; } // guard against leaks
    const tick = () => {
      if (!this._stepRunning || !this._stepTimer) return;
      const secs = (Date.now() - this._stepStart) / 1000;
      this._stepTimer.textContent = secs.toFixed(1) + 's';
      // Watchdog: turn is over once no flag is live. Catches aux flows that don't call _setStage(null).
      // Include _sayProc + _playSrc: aux flows clear _busy BEFORE await speak(), and the afplay/WebAudio
      // path keeps only those refs live during playback — without them the HUD would hide mid-speech.
      const active = this._busy || this._playing || this._listening || this._pttArmed || !!this._sayProc || !!this._playSrc;
      if (!active && secs > 0.5) this._hideStepper();
    };
    tick();
    this._stepInt = setInterval(tick, 100);
  }

  // Flash the final state briefly (~600ms) then fade out; clears the timer interval (no leak).
  _hideStepper() {
    if (this._stepInt) { clearInterval(this._stepInt); this._stepInt = null; }
    if (!this._stepRunning) return;
    this._stepRunning = false;
    this._stepStage = null;
    if (!this._stepEl) return;
    const el = this._stepEl;
    el.classList.add('ccc-stepper-hiding'); // CSS fades opacity over ~600ms
    const gen = (this._stepHideGen = (this._stepHideGen || 0) + 1);
    setTimeout(() => {
      if (gen !== this._stepHideGen) return; // a new turn started during the fade — keep it visible
      el.classList.remove('ccc-stepper-on', 'ccc-stepper-hiding');
    }, 600);
  }

  _drag(wrap) {
    let sx, sy, ox, oy, dragging = false;
    wrap.addEventListener('pointerdown', (e) => {
      if (e.target.classList.contains('ccc-orb-x') || e.target.classList.contains('ccc-orb-mic')) return;
      dragging = true; wrap.setPointerCapture(e.pointerId);
      const r = wrap.getBoundingClientRect(); ox = r.left; oy = r.top; sx = e.clientX; sy = e.clientY;
      wrap.style.cursor = 'grabbing';
    });
    wrap.addEventListener('pointermove', (e) => {
      if (!dragging) return;
      wrap.style.left = (ox + e.clientX - sx) + 'px'; wrap.style.top = (oy + e.clientY - sy) + 'px';
      wrap.style.right = 'auto'; wrap.style.bottom = 'auto';
    });
    wrap.addEventListener('pointerup', () => { dragging = false; wrap.style.cursor = 'grab'; });
  }

  async _initAudio() {
    if (this._stream) return; // mic already live — never build a second graph
    // single-flight (perf-audit-2026-06-10): show()'s wake path and a fast PTT can race
    // here; the loser must not call getUserMedia again (leaks a live mic track, ULT-006 class).
    if (this._initAudioInflight) return this._initAudioInflight;
    this._initAudioInflight = this._initAudioImpl().finally(() => { this._initAudioInflight = null; });
    return this._initAudioInflight;
  }

  async _initAudioImpl() {
    try {
      // AEC on by default: stops Ultron hearing ITSELF speak. (settings.voice.aec=false
      // disables it — used by the speaker→mic loopback tests.)
      const aec = this._voiceCfg().aec !== false;
      this._stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: aec, noiseSuppression: aec, autoGainControl: true } });
      this._actx = new (window.AudioContext || window.webkitAudioContext)();
      try { await this._actx.resume(); } catch (_) {}
      // Detect mic loss mid-session (OS revokes permission, device unplugged) — the
      // stream object stays non-null but its track dies, so a plain !this._stream check
      // never fires and the engine would silently go deaf. Tell Tony + drop the stream.
      this._stream.getTracks().forEach(t => t.addEventListener('ended', () => {
        // Full teardown (not just stop tracks): disconnect the ScriptProcessor + close the
        // AudioContext too, else a re-listen builds a 2nd graph and the 1st leaks (ULT-006).
        // force=true: the track is DEAD — keeping the zombie stream would block re-init.
        this._stopAudio(true);
        new Notice('Ultron: microphone was lost — re-enable mic access for Obsidian (System Settings → Privacy → Microphone), then toggle the orb.', 9000);
        if (this.orb) this.orb.setState('idle');
      }));
      const src = this._actx.createMediaStreamSource(this._stream);
      const an = this._actx.createAnalyser(); an.fftSize = 512; src.connect(an);
      this._an = an;
      if (this.orb) { this.orb.setAnalyser(an); this.orb.setState('listening'); }
      this._setStage('listening');

      // ── CONTINUOUS capture engine (no deaf gaps) ───────────────────────────
      // A ScriptProcessor streams every audio block into a VAD state machine with a
      // pre-roll so the word ONSET is always present. Utterances are emitted the
      // instant speech ends — whisper runs async and NEVER blocks capture, so the
      // wake word can't fall into a transcription gap (the old loop's fatal flaw).
      const sr = this._actx.sampleRate;
      const BLOCK = 4096;
      const sp = this._actx.createScriptProcessor(BLOCK, 1, 1);
      const sink = this._actx.createGain(); sink.gain.value = 0; // silent sink so the node runs without feedback
      src.connect(sp); sp.connect(sink); sink.connect(this._actx.destination);
      this._sp = sp;
      const PREROLL_BLOCKS = Math.ceil(0.5 * sr / BLOCK); // ~0.5s of lead-in
      const preroll = [];
      let active = false, utter = [], lastVoice = 0, startedAt = 0;
      const SIL_MS = () => this._voiceCfg().silenceMs || 350;
      const MAX_MS = () => this._voiceCfg().maxMs || 15000;
      sp.onaudioprocess = (ev) => {
        const inp = ev.inputBuffer.getChannelData(0);
        // block RMS
        let s = 0; for (let i = 0; i < inp.length; i++) s += inp[i] * inp[i];
        const lvl = Math.sqrt(s / inp.length);
        this._lastRms = lvl;
        if (this._noiseFloor == null) this._noiseFloor = lvl;
        else if (lvl < this._noiseFloor * 3) this._noiseFloor = this._noiseFloor * 0.95 + lvl * 0.05;
        const now = (typeof performance !== 'undefined' ? performance.now() : startedAt + 1);
        // e2e-quality-02 Fix 2: capture while thinking (busy but not playing) so Tony can barge in.
        // Only block during active TTS playback to avoid the mic hearing Ultron's own voice.
        const wantAudio = (this._wakeOn || this._pttArmed) && !this._playing && !this._micMuted;
        // ptt-earcon-trigger: the Tink earcon + physical mouse click register as "voice" the
        // instant PTT arms, consuming the whole listen window with a noise blip (HUD flashes
        // "transcribing" then dies before Tony speaks). Grace window: blocks still feed the
        // preroll so a fast talker's onset is kept, but they can't START an utterance.
        const voiced = lvl > this._vadThreshold() && now >= (this._vadHoldUntil || 0);
        if (this._vadDropActive) { this._vadDropActive = false; active = false; utter = []; }
        // voice-loop-005: gate orb setState on wantAudio so ambient noise doesn't thrash visuals while idle
        if (this.orb && wantAudio) this.orb.setState(voiced ? 'speaking' : 'listening');
        if (!wantAudio) { active = false; utter = []; preroll.length = 0; return; }
        const blk = new Float32Array(inp); // copy — the input buffer is recycled
        if (!active) {
          preroll.push(blk); if (preroll.length > PREROLL_BLOCKS) preroll.shift();
          if (voiced) { active = true; utter = preroll.slice(); preroll.length = 0; lastVoice = now; startedAt = now; }
        } else {
          utter.push(blk);
          if (voiced) lastVoice = now;
          const ended = (now - lastVoice) > SIL_MS();
          const tooLong = (now - startedAt) > MAX_MS();
          if (ended || tooLong) {
            active = false;
            const seg = utter; utter = [];
            this._emitUtterance(seg, sr);
          }
        }
      };
      if (this._wakeOn === undefined) this._wakeOn = false; // on-demand mic default; show() may have already restored the user's wakeEnabled preference (stt-wake-restore-missing)
    } catch (e) { // no mic → idle breathing, but TELL Tony why voice won't work
      if (this.orb) this.orb.setState('idle');
      if (e && e.name === 'NotAllowedError') new Notice('Ultron: microphone permission denied — enable it for Obsidian in System Settings → Privacy.', 8000);
    }
  }

  // Release the mic + audio graph so the macOS mic-in-use indicator turns OFF.
  // Called when a listen window ends (command captured, or no-speech timeout).
  // STT-01 fix: wake-word listening NEEDS the mic to stay hot. The old version
  // unconditionally tore down the stream AND force-cleared _wakeOn after every PTT
  // use, so "say Ultron" was structurally dead (nothing ever re-opened the mic).
  // force=true → explicit mic-off (click/hotkey/mic-lost): full teardown, wake off
  //   for this session (the wakeEnabled preference is untouched — show() restores it).
  // force=false (default) → end of a listen window: disarm PTT only; if wake is on
  //   and the stream is live, keep the capture engine running for wake scanning.
  _stopAudio(force = false) {
    this._pttArmed = false; this._listening = false; clearTimeout(this._pttTimer);
    if (!force && this._wakeOn && this._stream) {
      if (this.orb && !this._busy) this.orb.setState('idle'); // engine repaints to 'listening' on next block
      return;
    }
    this._wakeOn = false;
    if (this._sp) { try { this._sp.onaudioprocess = null; this._sp.disconnect(); } catch (_) {} this._sp = null; }
    if (this._stream) { try { this._stream.getTracks().forEach(t => t.stop()); } catch (_) {} this._stream = null; }
    if (this._actx) { try { this._actx.close().catch(() => {}); } catch (_) {} this._actx = null; }
    this._an = null;
    if (this.orb && !this._busy) this.orb.setState('idle');
  }

  // Assemble captured blocks → 16k mono WAV → hand to the (serialized) transcriber.
  _emitUtterance(blocks, srcSr) {
    if (!blocks || !blocks.length) return;
    let total = 0; for (const b of blocks) total += b.length;
    if (total / srcSr < 0.25) return; // <0.25s — too short to be a word, skip (no whisper)
    const pcm = new Float32Array(total);
    let o = 0; for (const b of blocks) { pcm.set(b, o); o += b.length; }
    // Resample → 16k (linear interp; fine for speech / whisper base.en).
    const dstSr = 16000, dstLen = Math.floor(total * dstSr / srcSr);
    const ds = new Float32Array(dstLen);
    for (let i = 0; i < dstLen; i++) {
      const t = i * srcSr / dstSr, i0 = Math.floor(t), frac = t - i0;
      const a = pcm[i0] || 0, b = pcm[i0 + 1] != null ? pcm[i0 + 1] : a;
      ds[i] = a + (b - a) * frac;
    }
    const fs = require('fs'), os = require('os'), path = require('path');
    const wav = path.join(os.tmpdir(), 'ultron-utt-' + Date.now() + '.wav');
    try { fs.writeFileSync(wav, this._wav16(ds, dstSr)); } catch (_) { return; }
    // Serialize transcription so two utterances never run whisper concurrently.
    this._uttQ = this._uttQ || Promise.resolve();
    this._uttQ = this._uttQ.then(() => this._handleUtterance(wav)).catch(() => {});
  }

  // Float32 [-1,1] @ sr → 16-bit PCM mono WAV Buffer.
  _wav16(samples, sr) {
    const n = samples.length, buf = Buffer.alloc(44 + n * 2);
    buf.write('RIFF', 0); buf.writeUInt32LE(36 + n * 2, 4); buf.write('WAVE', 8);
    buf.write('fmt ', 12); buf.writeUInt32LE(16, 16); buf.writeUInt16LE(1, 20);
    buf.writeUInt16LE(1, 22); buf.writeUInt32LE(sr, 24); buf.writeUInt32LE(sr * 2, 28);
    buf.writeUInt16LE(2, 32); buf.writeUInt16LE(16, 34);
    buf.write('data', 36); buf.writeUInt32LE(n * 2, 40);
    for (let i = 0; i < n; i++) { let v = Math.max(-1, Math.min(1, samples[i])); buf.writeInt16LE((v * 32767) | 0, 44 + i * 2); }
    return buf;
  }

  // One captured utterance → transcribe → route (push-to-talk command, or wake match).
  async _handleUtterance(wav) {
    // Only surface the HUD when there's an armed turn (PTT / confirm), not on passive wake-word
    // scanning of ambient speech — otherwise the stepper would flash on every overheard utterance.
    if (this._pttArmed || this._awaitingConfirm) this._setStage('transcribing');
    const text = await this._transcribe(wav); // _transcribe unlinks the wav
    // Pre-write confirmation: if a disk write is awaiting yes/no, THIS utterance answers it.
    if (this._awaitingConfirm) {
      // voice-loop-002: removed _stopAudio() here — it permanently kills the ScriptProcessor
      // (forces full re-init on next PTT press). Only need to disarm PTT state, not tear down audio.
      this._pttArmed = false; clearTimeout(this._pttTimer); this._listening = false;
      if (this.orb && !this._busy) this.orb.setState('idle');
      const yes = /\b(yes|yeah|yep|yup|confirm(?:ed)?|do it|go ahead|sure|ok(?:ay)?|please|affirmative)\b/i.test(text || '');
      const no = /\b(no|nope|nah|cancel|stop|don'?t|negative|forget it|never\s?mind)\b/i.test(text || '');
      this._awaitingConfirm.resolve(yes && !no); // ambiguous / nothing heard → false (safe default)
      return;
    }
    if (this._pttArmed) { // push-to-talk: this utterance IS the command
      this._pttArmed = false; clearTimeout(this._pttTimer);
      this._listening = false;
      this._stopAudio(); // command captured: releases mic if wake is off; keeps it hot for wake scanning if on (STT-01)
      const _pttWordCount = text ? text.trim().split(/\s+/).length : 0;
      if (text && (_pttWordCount >= 2 || /ultron/i.test(text) || this._isShortCommand(text))) { new Notice('Ultron heard: “' + text.slice(0, 90) + '”', 4000); this._ack(); await this.ask(text); } // stt-hallucination-bypass: drop single-word silence hallucinations, but let real 1-word commands (undo/pause/next…) through
      else if (this.orb) this.orb.setState('idle');
      return;
    }
    if (!this._wakeOn || this._busy) return;
    const wake = (this._voiceCfg().wake || 'ultron').toLowerCase();
    const pat = this._wakePattern(wake);
    const re = new RegExp('\\b' + pat + '\\b', 'i');
    const matched = !!(text && re.test(text));
    if (text) {
      this._wakeHeard = this._wakeHeard || [];
      this._wakeHeard.push({ t: new Date().toLocaleTimeString(), heard: text.slice(0, 80), matched });
      if (this._wakeHeard.length > 12) this._wakeHeard.shift();
      console.log('[CCC wake] heard:', JSON.stringify(text), '→ matched=', matched);
    }
    if (matched) {
      const after = text.replace(new RegExp('^.*?\\b' + pat + '\\b[\\s,.:!?]*', 'i'), '').trim();
      if (after && (after.trim().split(/\s+/).length >= 2 || this._isShortCommand(after))) { this._ack(); await this.ask(after); } // stt-hallucination-bypass: word-count guard (was char>2, which 'you' passes) — real 1-word commands allowed
      else { this._ack(); await this._sayLine('goahead', 'Go ahead, Tony.'); this._armPtt(); }
    }
  }

  // Arm push-to-talk: the NEXT captured utterance becomes a command. Times out so a
  // silent arm doesn't hang the listening state forever.
  _armPtt() {
    this._pttArmed = true; this._listening = true;
    if (this.orb) this.orb.setState('listening');
    this._setStage('listening');
    clearTimeout(this._pttTimer);
    const windowMs = (this.plugin && this.plugin.settings && this.plugin.settings.voice && this.plugin.settings.voice.pttWindowMs) || 10000;
    this._pttTimer = setTimeout(() => {
      // voice-loop-003: guard with !_awaitingConfirm so PTT timeout doesn't kill the audio graph
      // while a yes/no confirm is live — the 12s confirm timer owns cleanup in that path.
      if (this._pttArmed && !this._awaitingConfirm) {
        this._pttArmed = false; this._listening = false;
        if (this.orb) this.orb.setState('idle');
        new Notice('Ultron: didn\'t catch anything — click the mic and speak, or say “Ultron”.', 5000);
        this._stopAudio(); // release mic — no-speech window expired
      }
    }, windowMs);
  }

  // ── Voice loop (Increment 1: text → Claude → spoken answer) ────────────────
  // Brain = `claude -p` headless (uses Tony's existing Claude Code auth, no API key).
  // Voice = macOS `say`. Orb walks thinking → speaking → listening.
  _claudeBin() {
    const os = require('os'), fs = require('fs'), path = require('path');
    const cands = [path.join(os.homedir(), '.local/bin/claude'), '/opt/homebrew/bin/claude', '/usr/local/bin/claude'];
    for (const c of cands) { try { fs.accessSync(c, fs.constants.X_OK); return c; } catch (_) {} }
    return 'claude'; // fall back to PATH
  }
  _graphifyBin() {
    if (this._graphifyBinCache) return this._graphifyBinCache;
    const os = require('os'), fs = require('fs'), path = require('path');
    const cands = [path.join(os.homedir(), '.local/bin/graphify'), '/opt/homebrew/bin/graphify', '/usr/local/bin/graphify'];
    for (const c of cands) { try { fs.accessSync(c, fs.constants.X_OK); return (this._graphifyBinCache = c); } catch (_) {} }
    return (this._graphifyBinCache = 'graphify'); // fall back to PATH
  }
  _vaultPath() {
    const a = this.plugin.app.vault.adapter;
    return (a.getBasePath && a.getBasePath()) || a.basePath || process.cwd();
  }

  // Knowledge-graph pointer retrieval via the graphify CLI (~0.2 s BFS over the fused
  // graph: vault + _External clients, past transcripts, archives). Returns a compact
  // file-pointer string the brain model uses to Read the most relevant notes directly,
  // instead of blindly Globbing. Cached 60 s per unique query text.
  //
  // spawnSync is intentional here — 0.2 s is acceptable on lookup turns and avoids the
  // async plumbing complexity of interleaving an execFile promise into prompt assembly.
  // TODO(future): switch to execFile + Promise.race for full async when there's a
  // dedicated prompt-assembly pipeline to wire it into.
  _graphifyContext(text) {
    if (this._gqCache && this._gqCache.q === text && (Date.now() - this._gqCache.t) < 60000) return this._gqCache.v;
    let v = '';
    try {
      const cp = require('child_process');
      const r = cp.spawnSync(this._graphifyBin(), ['query', text], {
        cwd: this._vaultPath(),
        env: this._brainEnv(),
        timeout: 4000,
        encoding: 'utf8',
        maxBuffer: 1 << 20
      });
      if (!r.error && r.status === 0 && r.stdout) {
        const seen = new Set();
        const files = [];
        let m;
        const re = /src=([^\s\]]+)\s+loc=(L\d+)/g;
        while ((m = re.exec(r.stdout)) !== null) {
          const src = m[1];
          // Skip template stubs and the graphify output dir itself (not real content)
          if (src.includes('_template') || src.startsWith('graphify-out/')) continue;
          if (!seen.has(src)) { seen.add(src); files.push(src); }
          if (files.length >= 6) break;
        }
        if (files.length) {
          v = `Relevant files from your knowledge graph — Read these FIRST to answer (they may include clients, past transcripts, and archives beyond the main folders): ${files.join(', ')}.`;
        }
      }
    } catch (_) {} // graphify is an enhancement — never block a turn on failure
    this._gqCache = { q: text, t: Date.now(), v };
    return v;
  }

  // Semantic knowledge-base retrieval via local Qdrant (fastembed MiniLM, on-disk, no server).
  // Returns actual CONTENT snippets (not just file pointers) so the brain can answer directly
  // from meaning-matched results without needing a follow-up Read on every turn.
  // Scores ≥ 0.30 kept; up to 6 results. Cached 60 s per unique query text.
  //
  // spawnSync is intentional — ~0.6–0.9 s is acceptable on lookup turns; uv boot+query
  // is fast enough that it doesn't noticeably delay first token.
  // TODO(future): switch to execFile + Promise.race for full async once there's a dedicated
  //   prompt-assembly pipeline; alternatively wire a warm daemon (persistent uv process)
  //   that pre-embeds the query while the user is still speaking.
  _semanticContext(text) {
    if (this._scCache && this._scCache.q === text && (Date.now() - this._scCache.t) < 60000) return this._scCache.v;
    let v = '';
    try {
      // B1: try the warm recall daemon first (127.0.0.1:7766).
      // Falls back to the uv spawn path if daemon is down / times out / non-200.
      // Both paths produce an identical injection string — zero behaviour change.
      const http = require('http');
      const daemonResult = (() => {
        // ULT-01 fix: the old probe spawned process.execPath as if it were node, but inside
        // Obsidian that is the Electron binary (runAsNode fuse off) — the probe could NEVER
        // succeed, so the warm daemon was silently dead for every voice turn. /usr/bin/curl
        // always exists on macOS; -f preserves the statusCode===200 semantics (5xx → exit 22
        // → fall through to uv instead of parsing a JSON error object).
        const cp = require('child_process');
        const url = 'http://127.0.0.1:7766/retrieve?q=' + encodeURIComponent(text) + '&top=6';
        const r = cp.spawnSync('/usr/bin/curl', ['-sf', '--max-time', '1.2', url],
          { timeout: 1500, encoding: 'utf8', maxBuffer: 1 << 20 });
        if (!r.error && r.status === 0 && r.stdout) {
          try { return JSON.parse(r.stdout); } catch (_) { return null; }
        }
        return null; // daemon not available — fall through
      })();

      let hits = daemonResult;

      if (!hits) {
        // Fallback: uv run recall_vector.py (original path — kept verbatim)
        const cp = require('child_process'), os = require('os'), fs = require('fs'), path = require('path');
        let uvBin = 'uv';
        for (const c of [path.join(os.homedir(), '.local/bin/uv'), '/opt/homebrew/bin/uv']) {
          try { fs.accessSync(c, fs.constants.X_OK); uvBin = c; break; } catch (_) {}
        }
        const r = cp.spawnSync(
          uvBin,
          ['run', '--quiet', '--with', 'qdrant-client', '--with', 'fastembed',
           'python', 'build/tools/recall_vector.py', text, '--top', '6'],
          { cwd: this._vaultPath(), env: this._brainEnv(), timeout: 6000, encoding: 'utf8', maxBuffer: 1 << 20 }
        );
        // exit 3 = DB locked. NOT benign while the daemon runs: the daemon holds the Qdrant
        // lock for life, so this fallback CANNOT serve while it's up — the daemon probe above
        // is the only working path then (ULT-02). exit 4 = collection missing (benign).
        if (!r.error && r.status === 3) {
          console.warn('[CCC] recall: index lock held (daemon owns it) but daemon probe failed — no semantic recall this turn');
        }
        if (!r.error && (r.status === 0) && r.stdout) {
          try { hits = JSON.parse(r.stdout); } catch (_) { hits = []; }
        }
      }

      // Hygiene (live-verified 2026-06-09: "<client> <topic>" returned 4 duplicate PPT catalogs +
      // a Lorem-ipsum template at 0.40): drop template/PPT-export junk, dedup by basename
      // (the same catalog lives under 4 paths), and rank core vault dirs above Document Library
      // noise with at most 2 non-core hits — so the bid/account/daily files actually win.
      const CORE_RE = /^(RFPs|01_Projects|02_Areas|Clients|Meetings|People|Important|00_Inbox|04_Archives|_wiki|_brain_api|Preferences|Use Cases|Reading|Outbound|99_Meta)\//;
      const JUNK_RE = /\.pptx\.md$|(^|\/)_templates?(\/|\.md$)/i; // also catches Daily/_template.md
      const seenBase = new Set();
      let nonCore = 0;
      const kept = (Array.isArray(hits) ? hits : [])
        .filter(h => (h.score || 0) >= 0.30 && h.path && !JUNK_RE.test(h.path))
        .filter(h => {
          const base = h.path.split('/').pop();
          if (seenBase.has(base)) return false;
          seenBase.add(base); return true;
        })
        .sort((a, b) => ((CORE_RE.test(b.path) ? 1 : 0) - (CORE_RE.test(a.path) ? 1 : 0)) || ((b.score || 0) - (a.score || 0)))
        .filter(h => CORE_RE.test(h.path) || (++nonCore <= 2))
        .slice(0, 6);
      if (kept.length) {
        const lines = kept.map(h => {
          const snippet = (h.snippet || '').slice(0, 200).replace(/\n/g, ' ');
          return `- ${h.path}  ${h.title ? '[' + h.title + ']' : ''}: ${snippet}`;
        });
        v = `Relevant knowledge from Tony's second brain (semantic search over his whole vault, clients, transcripts — use these to answer; Read the cited file for full detail):\n` + lines.join('\n');
      }
    } catch (_) {} // semantic recall is an enhancement — never block a turn on failure
    this._scCache = { q: text, t: Date.now(), v };
    return v;
  }

  // Cheap vault awareness WITHOUT the cost of running the brain in the vault (which
  // fires the SessionStart/capture hooks → 60s). Reads a couple of pre-computed files
  // directly (a few ms) and returns a compact briefing to fold into the prompt, so
  // "what's on my plate?" is grounded in real state. Cached 90s — the files only
  // change on the hourly refresh, no point re-reading them every utterance.
  _vaultContext(text) {
    // Spend is noise on non-money turns — Ultron was reciting it unprompted ("keeps saying 6724").
    // Only inject the figure when the question is actually about money. Cache per-intent so the
    // money variant and the lean variant don't clobber each other.
    const moneyQ = /\b(spend|spent|cost|costs?|budget|burn|invoice|bill|expenses?|dollars?|how much|pric)\w*|\$/i.test(text || '');
    const ck = moneyQ ? '_ctxCacheMoney' : '_ctxCache';
    if (this[ck] && (Date.now() - this[ck].t) < 90000) return this[ck].v;
    const fs = require('fs'), path = require('path');
    const base = this._vaultPath();
    const parts = [];
    // Open bids
    try {
      // grounding-ctx-02: emit every non-empty scalar field so the model has real context
      const open = JSON.parse(fs.readFileSync(path.join(base, '_brain_api/bid/_open.json'), 'utf8'));
      const bids = (open.bids || []).map(b => {
        const bp = [b.bid_id];
        const detail = [b.client, b.stage].filter(Boolean).join(', ');
        if (detail) bp.push(`(${detail})`);
        if (b.deadline) bp.push(`due ${b.deadline}`);
        if (b.owner)    bp.push(`owner: ${b.owner}`);
        if (b.value)    bp.push(`value: ${b.value}`);
        if (!b.client || !b.deadline) bp.push(`[sparse — Read RFPs/${b.bid_id}/00 - Brief.md for full detail]`);
        return bp.join(' ');
      });
      if (bids.length) parts.push(`Open bids: ${bids.join('; ')}.`);
    } catch (_) {}
    // Spend/cost/burn/session figures: DO NOT pre-answer. A cached constant goes stale the moment
    // Dashboard.md refreshes (hourly + at SessionEnd) and tempts the model to parrot it instead of
    // reading the real value. Point the model at the canonical file and make it READ the exact line
    // for whatever period Tony asked. Only fall back to a labelled live estimate if Dashboard.md
    // can't be read this turn.
    if (moneyQ) {
      // Inject the ACTUAL numbers as data (parsed from the live dashboard header) instead of a
      // "go read it yourself" instruction — the latency-bound voice model often skipped the Read,
      // so spend questions got vague/empty answers. Quote these verbatim.
      let injected = false;
      try {
        const dash = fs.readFileSync(path.join(base, '02_Areas/Dashboard.md'), 'utf8');
        const grab = (label) => { const m = dash.match(new RegExp('\\*\\*' + label + ':\\*\\*\\s*([^·*\\n]+)')); return m ? m[1].trim() : null; };
        const today = grab('Spend today'), wk = grab('Spend last 7 days'),
              all = grab('Total spend \\(all time\\)'), sess = grab('Sessions \\(all time\\)');
        const seg = [];
        if (today) seg.push(`today ${today}`); if (wk) seg.push(`last 7 days ${wk}`);
        if (all) seg.push(`all-time ${all}`); if (sess) seg.push(`${sess} sessions all-time`);
        const refreshed = (dash.match(/refreshed:\s*(.+)/) || [])[1];
        if (seg.length) { parts.push(`Live spend from his dashboard${refreshed ? ' (as of ' + refreshed.trim() + ')' : ''} — quote these exactly: ${seg.join(', ')}. If he names a period, give that one; if he names none, lead with the "today" figure.`); injected = true; }
      } catch (_) {}
      if (!injected) {
        const u = this._liveUsage;
        if (u && !u.error) {
          const c = (n) => 'C$' + (Number(n) || 0).toFixed(2);
          parts.push(`Spend (live token estimate — dashboard unavailable): today ${c(u.todayCost)}, week ${c(u.weekCost)}, all-time ${c(u.allTimeCost)}.`);
        }
      }
    }
    // Account briefs: name + one-liner from each _brain_api/account/<name>/{brief.json|dashboard.md}
    try {
      const acctDir = path.join(base, '_brain_api/account');
      const accts = [];
      for (const entry of fs.readdirSync(acctDir)) {
        try {
          const entryPath = path.join(acctDir, entry);
          if (!fs.statSync(entryPath).isDirectory()) continue;
          // Forward-compat: try brief.json first, fall back to dashboard.md
          const briefPath = path.join(entryPath, 'brief.json');
          if (fs.existsSync(briefPath)) {
            const b = JSON.parse(fs.readFileSync(briefPath, 'utf8'));
            const name = b.name || b.client || entry;
            // grounding-ctx-05: include useful scalar fields so model has real account context
            const oneliner = b.one_liner || b.oneliner || b.summary || '';
            const extras = [];
            if (b.industry) extras.push(b.industry);
            if (b.stage || b.status) extras.push(b.stage || b.status);
            if (b.value || b.arr) extras.push('value: ' + (b.value || b.arr));
            const desc = [oneliner, extras.length ? `(${extras.join(', ')})` : ''].filter(Boolean).join(' ');
            accts.push(desc ? `${name}: ${desc}` : name);
          } else {
            const dashPath = path.join(entryPath, 'dashboard.md');
            const raw = fs.readFileSync(dashPath, 'utf8');
            // Extract name from frontmatter 'account:' field, fall back to dir name
            const fmMatch = raw.match(/^---[\s\S]*?^account:\s*(.+?)\s*$/m);
            const name = fmMatch ? fmMatch[1] : entry;
            // First non-empty, non-frontmatter line (skip the --- block)
            const bodyLines = raw.replace(/^---[\s\S]*?---\s*/m, '').split('\n');
            const firstLine = bodyLines.find(l => l.trim().length > 0) || '';
            const oneliner = firstLine.replace(/^#+\s*/, '').trim().slice(0, 120);
            // If heading just says "Account: <name>", no useful extra info — push name only
            const isRedundant = !oneliner || oneliner === name ||
              oneliner.toLowerCase() === `account: ${name.toLowerCase()}`;
            accts.push(isRedundant ? name : `${name}: ${oneliner}`);
          }
        } catch (_) {}
      }
      if (accts.length) parts.push(`Active accounts: ${accts.slice(0, 6).join('; ')}.`);
    } catch (_) {}
    // This-week deadlines from open bids
    try {
      const open = JSON.parse(fs.readFileSync(path.join(base, '_brain_api/bid/_open.json'), 'utf8'));
      const now = new Date(), endOfWeek = new Date(now); endOfWeek.setDate(now.getDate() + 7);
      const deadlines = (open.bids || [])
        .filter(b => b.deadline && new Date(b.deadline) <= endOfWeek && new Date(b.deadline) >= now)
        .map(b => `${b.bid_id} due ${b.deadline}`);
      if (deadlines.length) parts.push(`Upcoming deadlines: ${deadlines.join('; ')}.`);
    } catch (_) {}
    // Pending Dust writes count
    try {
      const inboxDir = path.join(base, '00_Inbox/from-dust');
      let pendingCount = 0;
      for (const agent of fs.readdirSync(inboxDir)) {
        try {
          const files = fs.readdirSync(path.join(inboxDir, agent)).filter(f => f.endsWith('.md'));
          pendingCount += files.length;
        } catch (_) {}
      }
      if (pendingCount > 0) parts.push(`Pending Dust writes: ${pendingCount} (use /dust-resolve to triage).`);
    } catch (_) {}
    // Today's daily note = the LIVE plate-summary (the morning brief goes stale by a day).
    try {
      const now = new Date();
      const ymd = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
      let today = '';
      try {
        // grounding-ctx-01: strip fenced code blocks (Dataview), italic placeholders, blank lines,
        // then use a substantive-line count guard so near-empty templates fall through to Daily_Brief.md.
        today = fs.readFileSync(path.join(base, `02_Areas/Daily/${ymd}.md`), 'utf8')
          .replace(/^---[\s\S]*?---/, '')          // strip frontmatter
          .replace(/```[\s\S]*?```/g, '')           // remove fenced code blocks (Dataview queries)
          .replace(/^_.*_$/gm, '')                  // remove italic placeholder lines
          .replace(/^[\s\-]*$/gm, '')               // remove blank/dash-only bullet lines
          .replace(/[#*>`_]/g, '')                  // existing character cleanup
          .replace(/\n{2,}/g, '\n').trim();
      } catch (_) {}
      const substantiveLines = today.split('\n').filter(l => l.trim().length > 5);
      if (substantiveLines.length >= 3) {
        parts.push(`Today's note (${ymd}): ${today.slice(0, 400)}`);
      } else {
        const brief = fs.readFileSync(path.join(base, '02_Areas/Daily_Brief.md'), 'utf8')
          .replace(/^---[\s\S]*?---/, '').replace(/[#*>`]/g, '').replace(/\n{2,}/g, '\n').trim().slice(0, 400);
        if (brief) parts.push(`Today's brief: ${brief}`);
      }
    } catch (_) {}
    // Canonical pattern index — just the keys (no bodies) so the model knows what to Read
    try {
      const canonBase = path.join(base, '_brain_api/canonical');
      const keys = [];
      for (const cat of fs.readdirSync(canonBase)) {
        const catPath = path.join(canonBase, cat);
        try {
          for (const f of fs.readdirSync(catPath)) {
            if (f.endsWith('.json')) keys.push(`${cat}/${f.replace(/\.json$/, '')}`);
          }
        } catch (_) {}
      }
      if (keys.length) parts.push(`Canonical patterns (Read _brain_api/canonical/<key>.json for full content): ${keys.join(', ')}.`);
    } catch (_) {}
    // Ultron's persistent memory (USER.md prefs + recent notes) — so it remembers Tony across sessions
    try { const mem = this._ultronMemory(); if (mem) parts.push(mem); } catch (_) {}
    // Vault map — tell Ultron the whole "second brain" exists and WHERE to look, so it researches
    // with Read/Glob/Grep instead of guessing or answering "nothing recorded". This is the single
    // biggest lever for "know everything in my second brain": the model is sitting in the vault (cwd)
    // with read tools, it just needs the map.
    parts.push(
      "You are inside Tony's Obsidian vault (your working directory) with Read, Glob and Grep — actually look things up before answering. Where things live: " +
      // grounding-ctx-04: removed "changes/ recent activity" — that endpoint is an empty stub (Phase 4.5 not yet live)
      "_brain_api/ = fast pre-computed answers (bid/_open.json open bids, account/<name>/brief.json account briefs, canonical/<type>/<key>.json reusable blocks, _manifest.json the index); " +
      "RFPs/ = active bids (each has '00 - Brief.md' with stage + deadline + a Decision Log); " +
      "02_Areas/ = Dashboard.md (live spend + KPIs), Pipeline.md (the deal dashboard), Daily/<YYYY-MM-DD>.md — full path 02_Areas/Daily/<YYYY-MM-DD>.md (his daily journal); " + // grounding-ctx-07: clarified full daily-note path
      "Meetings/ = transcripts, prep, recaps; People/ = decision-makers, partners; RFPs/ = bid pipeline; Important/ = his priority queue; " +
      "Reading/ Outbound/ Use Cases/ Preferences/ = his queues and rules; _wiki/ = distilled, organized knowledge; 03_Resources/ = reusable assets; 04_Archives/ = closed bids. " +
      "When he asks what's in his second brain, what's on his plate, about a client/bid/meeting/person, or to research something — Glob/Grep/Read the relevant place and answer from the real files.");
    // Build stamp — always appended so the model (and logs) reflect the live code version
    parts.push(`Build: ${PLUGIN_BUILD}`);
    // Cap the snapshot generously — wide enough to carry the vault map + live data, still bounded.
    let v = parts.join('\n');
    if (v.length > 2800) v = v.slice(0, 2797) + '…';
    this[ck] = { t: Date.now(), v };
    return v;
  }

  // p6-#16 in-room wingman — READ-ONLY bid grounding. When Tony names an open bid ("where are we
  // weakest on <bid>", "<client> status", "<bid> gaps/cost/scorecard"), pull that bid's
  // win-recs.md + scorecard.md + compliance-gaps.md straight from its folder and inject them so the
  // answer is specific + grounded instead of a generic crawl. Matched off the canonical
  // _brain_api/bid/_open.json (SBAP-first — no folder crawl to discover the bid). NO writes.
  _norm(s) { return String(s || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]+/g, ' ').trim(); }
  _bidContext(text) {
    if (this._bidCtxCache && this._bidCtxCache.q === text && (Date.now() - this._bidCtxCache.t) < 60000) return this._bidCtxCache.v;
    let v = '';
    try {
      const fs = require('fs'), path = require('path'), base = this._vaultPath();
      const utter = ' ' + this._norm(text) + ' ';
      const open = JSON.parse(fs.readFileSync(path.join(base, '_brain_api/bid/_open.json'), 'utf8'));
      let best = null, bestScore = 0;
      for (const b of (open.bids || [])) {
        if (!b.path) continue;
        // Candidate match terms: bid_id, company, topic, and the folder basename — each normalised.
        // Require term length ≥ 4 (so "ai"/"sap" alone don't false-match) and a space-bounded hit.
        const terms = new Set();
        for (const raw of [b.bid_id, b.company, b.topic, path.basename(b.path)]) {
          const n = this._norm(raw);
          if (n.length >= 4) terms.add(n);
        }
        let score = 0;
        for (const t of terms) { if (utter.includes(' ' + t + ' ') || utter.includes(' ' + t.replace(/ /g, '') + ' ')) score += t.length; }
        if (score > bestScore) { bestScore = score; best = b; }
      }
      if (best) {
        const bidDir = path.join(base, best.path);
        const blocks = [];
        for (const [label, file] of [['Recommended winning moves', 'win-recs.md'], ['Scorecard', 'scorecard.md'], ['Compliance gaps', 'compliance-gaps.md']]) {
          try {
            const raw = fs.readFileSync(path.join(bidDir, file), 'utf8')
              .replace(/^---[\s\S]*?---/, '').replace(/```[\s\S]*?```/g, '').replace(/\n{2,}/g, '\n').trim();
            if (raw) blocks.push(`${label} (${file}):\n${raw.slice(0, 1200)}`);
          } catch (_) {} // a missing file just means that facet isn't available for this bid
        }
        if (blocks.length) {
          const who = [best.company, best.topic].filter(Boolean).join(' — ') || best.bid_id;
          v = `In-room bid context for ${who} (read-only, from RFPs/${best.bid_id ? best.bid_id : path.basename(best.path)}; answer specifically from this, cite the file):\n` + blocks.join('\n\n');
        }
      }
    } catch (_) {} // bid grounding is an enhancement — never block a turn on failure
    this._bidCtxCache = { q: text, t: Date.now(), v };
    return v;
  }

  // ── Ultron ACTIONS (Phase A) — the ONLY code that writes to disk. The brain stays
  // read-only; it proposes, these deterministic methods execute, everything is audited to
  // _agent_state/ultron/actions.jsonl. Drafts are review-only (low confidence, needs_review,
  // empty target_path) so triage never auto-promotes them. ───────────────────────────────
  _ultronDir() {
    // perf-sweep-05: cache the dir path so mkdirSync (+ OneDrive FS watcher churn) fires only once per lifecycle
    if (this._ultronDirCache) return this._ultronDirCache;
    const path = require('path'), fs = require('fs');
    const d = path.join(this._vaultPath(), '_agent_state', 'ultron');
    try { fs.mkdirSync(path.join(d, 'tmp'), { recursive: true }); } catch (_) {}
    this._ultronDirCache = d;
    return d;
  }
  _redact(s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').slice(0, 80); }
  _logAction(ev) {
    try {
      const fs = require('fs'), path = require('path');
      fs.appendFileSync(path.join(this._ultronDir(), 'actions.jsonl'),
        JSON.stringify(Object.assign({ ts: new Date().toISOString() }, ev)) + '\n');
    } catch (_) {}
  }
  _runId() { return new Date().toISOString().replace(/[:.]/g, '-') + '-' + ((this._actSeq = (this._actSeq || 0) + 1)); }
  _sbapFront({ output_type = 'other', target_path = '', confidence = 0.7, refs = [] }) {
    return [
      '---', 'sbap_version: "1.0"', 'source_agent: "ultron"',
      `source_run_id: "${this._runId()}"`, `generated: "${new Date().toISOString()}"`,
      'input_context_refs:', ...(refs.length ? refs : ['voice']).map(r => `  - "${r}"`),
      `output_type: "${output_type}"`, `target_path: "${target_path}"`,
      `confidence: ${confidence}`, 'needs_review: true', '---',
    ].join('\n');
  }
  // Canonical SBAP validation — async (perf-sweep-02: execFile, not spawnSync) so it can't
  // freeze the Obsidian UI thread. actions-sbap-validate-fail-closed-blocks-all-writes:
  // distinguishes "script absent/dep-missing" (allow write, warn once) from real violation (block).
  _validateSbap(file) {
    const cp = require('child_process'), fs = require('fs'), path = require('path');
    const script = path.join(this._vaultPath(), 'build', 'tools', 'validate_sbap_write.py');
    if (!fs.existsSync(script)) {
      // Validator not installed — allow the write but warn once per session
      if (!this._sbapWarnedMissing) {
        this._sbapWarnedMissing = true;
        new Notice('Ultron: SBAP validator not found at build/tools/validate_sbap_write.py — writes proceeding unvalidated.', 8000);
      }
      return Promise.resolve(true);
    }
    return new Promise((resolve) => {
      try {
        cp.execFile('python3', [script, file],
          { cwd: this._vaultPath(), encoding: 'utf8', timeout: 15000 },
          (err, stdout, stderr) => {
            if (err && err.code === 'ENOENT') {
              // python3 binary missing — allow with warning
              if (!this._sbapWarnedSpawn) {
                this._sbapWarnedSpawn = true;
                new Notice('Ultron: python3 not found — SBAP validation skipped, writes proceeding.', 8000);
              }
              return resolve(true);
            }
            if (err && err.killed) return resolve(true); // timeout = treat as infra issue, not violation
            resolve(!err); // exit 0 = valid; non-zero = genuine schema violation
          }
        );
      } catch (e) {
        new Notice('Ultron: SBAP validator threw unexpectedly — ' + e.message, 6000);
        resolve(true); // unexpected error is not a schema violation
      }
    });
  }
  // Review-only draft → temp → validate → atomic move into 00_Inbox/from-dust/ultron/.
  async _inboxNote({ title, body, output_type = 'other' }) {
    const fs = require('fs'), path = require('path');
    const dir = this._ultronDir();
    const slug = (String(title || 'note').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 50)) || 'note';
    const fname = `${localDateStr()}-${slug}.md`; // LOCAL date (toISOString=UTC → off-by-one at night)
    const tmp = path.join(dir, 'tmp', `${Date.now()}-${fname}`);
    const dest = path.join(this._vaultPath(), '00_Inbox', 'from-dust', 'ultron', fname);
    const content = `${this._sbapFront({ output_type, target_path: '', confidence: 0.7 })}\n# ${title}\n\n${body}\n`;
    let ok = false;
    try {
      fs.writeFileSync(tmp, content);
      ok = await this._validateSbap(tmp); // async: won't block UI thread (perf-sweep-02)
      if (ok) {
        fs.mkdirSync(path.dirname(dest), { recursive: true });
        const existed = fs.existsSync(dest); const before = existed ? fs.readFileSync(dest, 'utf8') : null;
        fs.renameSync(tmp, dest);
        this._pushUndo({ label: `inbox draft: ${title}`, path: dest, existed, before }); // usually creates → undo deletes
      }
      else { try { fs.unlinkSync(tmp); } catch (_) {} }
    } catch (_) { ok = false; try { fs.unlinkSync(tmp); } catch (__) {} }
    this._logAction({ source: 'voice-cmd', action: 'inbox_note', targetPath: ok ? dest : '', validation: ok ? 'pass' : 'fail', argPreview: this._redact(`${title} :: ${body}`), confirm: 'auto' });
    return { ok, path: ok ? dest : null };
  }
  // ── Feed the Orb (HS-R2 #15): drop any document on the orb — it chews it,
  // digests it through the brain, and lays a proper SBAP egg into
  // 00_Inbox/from-dust/ultron (rides _composeText + _inboxNote, so validation,
  // undo, and the audit log all apply). Max 3 files per meal, 2MB text cap,
  // markitdown converts binary office docs. Drafts only — nothing leaves.
  async _feed(e) {
    const dt = e.dataTransfer;
    if (!dt) return;
    const items = [];
    if (dt.files && dt.files.length) for (const f of dt.files) items.push({ path: f.path, name: f.name });
    const text = !items.length ? (dt.getData('text/plain') || '') : '';
    if (!items.length && !text.trim()) { new Notice('Ultron: nothing edible in that drop.', 3000); return; }
    if (this.el) this.el.classList.add('ccc-orb-chewing');
    if (this.orb) this.orb.setState('thinking');
    new Notice('🔮 Ultron is chewing…', 2500);
    try {
      const fs = require('fs'), path = require('path'), cp = require('child_process');
      const sources = [];
      for (const it of items.slice(0, 3)) {
        let body = null;
        const ext = (path.extname(it.path || '') || '').toLowerCase();
        if (['.md', '.txt', '.csv', '.json', '.eml', '.html', '.htm'].includes(ext)) {
          try { if (fs.statSync(it.path).size <= 2 * 1024 * 1024) body = fs.readFileSync(it.path, 'utf8'); } catch (_) {}
        } else if (['.pdf', '.docx', '.pptx', '.xlsx'].includes(ext)) {
          try {
            const r = cp.spawnSync(process.env.HOME + '/.local/bin/markitdown', [it.path],
              { encoding: 'utf8', timeout: 45000, maxBuffer: 8 * 1024 * 1024 });
            if (r.status === 0 && r.stdout && r.stdout.trim()) body = r.stdout;
          } catch (_) {}
        }
        sources.push({ name: it.name, body });
      }
      if (text.trim()) sources.push({ name: 'dropped-text', body: text });
      const edible = sources.filter(s => s.body);
      const refused = sources.filter(s => !s.body).map(s => s.name);
      if (!edible.length) { new Notice('Ultron: could not read that (unsupported type or >2MB).', 4500); return; }
      let laid = 0;
      for (const s of edible) {
        const digest = await this._composeText(
          'Digest this document for the second brain inbox. Output exactly: one WHAT-IT-IS line; 3-6 key-point bullets (keep numbers, names, dates verbatim); one ACTION line (or "No action"). Plain text, no preamble, no markdown headings.\n\nDOCUMENT (' + s.name + '):\n' + s.body.slice(0, 60000)
        ).catch(() => null);
        const r = await this._inboxNote({
          title: 'digested: ' + s.name.replace(/\.[^.]+$/, ''),
          body: digest || ('(digestion failed — raw excerpt kept)\n\n' + s.body.slice(0, 4000)),
          output_type: 'other',
        });
        if (r.ok) {
          laid++;
          try {
            const rel = this.plugin && this.plugin.synapse && this.plugin.synapse._relPath(r.path);
            if (rel) this.plugin.synapse.fireFile(rel, true); // the egg lands where it was laid
          } catch (_) {}
        }
      }
      new Notice(`🥚 ${laid}/${edible.length} digested → 00_Inbox/from-dust/ultron${refused.length ? ` · refused: ${refused.join(', ')}` : ''}`, 5000);
    } finally {
      if (this.el) this.el.classList.remove('ccc-orb-chewing');
      if (this.orb && !this._busy) this.orb.setState('idle');
    }
  }

  // Append under a heading in today's daily note (read-modify-write, serialized via a mutex).
  async _appendDaily({ heading = '## Ultron', text, bullet = true, source = 'voice-cmd' }) {
    const fs = require('fs'), path = require('path');
    this._dailyMutex = (this._dailyMutex || Promise.resolve()).then(() => {
      const date = localDateStr(); // LOCAL date — must match Obsidian's daily-note name
      const file = path.join(this._vaultPath(), '02_Areas', 'Daily', `${date}.md`);
      let cur; try { cur = fs.readFileSync(file, 'utf8'); } catch (_) { cur = `---\ntype: daily\ndate: ${date}\ntags: [daily]\n---\n\n# ${date}\n`; }
      const block = bullet
        ? (cur.includes(heading) ? '' : `\n${heading}\n`) + `- ${text}\n`
        : (cur.includes(heading) ? '' : `\n${heading}\n\n`) + `${text}\n`; // actions-sbap-duplicate-heading: only prepend heading if absent
      fs.mkdirSync(path.dirname(file), { recursive: true });
      const existed = fs.existsSync(file); const before = existed ? fs.readFileSync(file, 'utf8') : null;
      fs.writeFileSync(file, cur + block); // mark success = the write returning
      this._pushUndo({ label: 'added to today', path: file, existed, before });
      this._logAction({ source, action: 'append_daily', targetPath: file, validation: 'n/a', argPreview: this._redact(text), confirm: 'auto' });
      return file;
    });
    return this._dailyMutex;
  }
  // Append a dated memory note to memory.json (canonical) + regenerate MEMORY.md (human view).
  async _memAppend(line) {
    const fs = require('fs'), path = require('path');
    const dir = this._ultronDir(), jf = path.join(dir, 'memory.json');
    const existed = fs.existsSync(jf); const before = existed ? fs.readFileSync(jf, 'utf8') : null;
    let mem; try { mem = JSON.parse(fs.readFileSync(jf, 'utf8')); } catch (_) { mem = { agent: 'ultron', updated: '', notes: [] }; }
    mem.notes = mem.notes || [];
    mem.notes.unshift({ ts: new Date().toISOString(), note: String(line).slice(0, 600) }); // memory-sys-300-char-note-truncation: raised 300→600 so multi-clause facts survive
    mem.notes = mem.notes.slice(0, 200);
    mem.updated = new Date().toISOString();
    fs.writeFileSync(jf, JSON.stringify(mem, null, 1));
    this._pushUndo({ label: 'memory note', path: jf, existed, before }); // undo restores memory.json + regenerates MEMORY.md
    this._regenMemoryMd(); // rebuild MEMORY.md human-view from the canonical memory.json
    this._logAction({ source: 'voice-cmd', action: 'mem_append', targetPath: jf, validation: 'n/a', argPreview: this._redact(line), confirm: 'auto' });
    return true;
  }
  // Rebuild MEMORY.md (human view) from the canonical memory.json. Shared by _memAppend and
  // by _undoLast (when an undo restores memory.json, the human view must be regenerated too).
  _regenMemoryMd() {
    const fs = require('fs'), path = require('path'), dir = this._ultronDir(), jf = path.join(dir, 'memory.json');
    let mem; try { mem = JSON.parse(fs.readFileSync(jf, 'utf8')); } catch (_) { mem = { notes: [] }; }
    const notes = Array.isArray(mem.notes) ? mem.notes : [];
    fs.writeFileSync(path.join(dir, 'MEMORY.md'),
      ['# Ultron memory (human view — regenerated from memory.json; edit memory.json, not this)', '', ...notes.map(n => `- ${n.ts.slice(0, 10)} — ${n.note}`)].join('\n') + '\n');
  }

  // ── Snapshot-based voice undo ────────────────────────────────────────────────
  //
  // Every vault-file-mutating executor snapshots the target file's prior state right
  // before it writes (existed + full content, or null when the file is newly created),
  // then on SUCCESS calls _pushUndo. "undo that" → _undoLast restores the most recent
  // snapshot (rewrite prior content, or delete a file the action created). Robust, not
  // inverse-ops: we replay the exact bytes that were there before. Notes are small, so a
  // full-content snapshot is fine. Persisted to undo.jsonl (last 20) so undo survives a reload.

  // Lazy-load the last 20 undo entries from _agent_state/ultron/undo.jsonl on first show.
  _loadUndoStack() {
    if (this._undoLoaded) return;
    this._undoLoaded = true;
    try {
      const fs = require('fs'), path = require('path');
      const f = path.join(this._ultronDir(), 'undo.jsonl');
      if (!fs.existsSync(f)) return;
      const lines = fs.readFileSync(f, 'utf8').split('\n').filter(Boolean);
      const stack = [];
      for (const ln of lines) {
        let e; try { e = JSON.parse(ln); } catch (_) { continue; }
        if (e && e.marker === 'undo') { stack.shift(); continue; } // undo-marker pops the most-recent
        if (e && e.path) stack.unshift(e);                          // a snapshot entry
      }
      this._undoStack = stack.slice(0, 20);
    } catch (_) {}
  }

  // Push a snapshot onto the in-memory stack (cap 20) and persist it (async append).
  _pushUndo({ label, path: target, existed, before }) {
    try {
      const fs = require('fs'), path = require('path');
      const entry = { ts: new Date().toISOString(), label: String(label || 'action'), path: target, existed: !!existed, before: existed ? before : null };
      this._undoStack = this._undoStack || [];
      this._undoStack.unshift(entry);
      this._undoStack = this._undoStack.slice(0, 20);
      const dir = this._ultronDir(); // ensures _agent_state/ultron exists
      fs.appendFile(path.join(dir, 'undo.jsonl'), JSON.stringify(entry) + '\n', () => {});
    } catch (e) { console.error('[Ultron] _pushUndo error', e); }
  }

  // Undo the most recent vault-file mutation. Restores prior content (atomic tmp+rename),
  // deletes a file the action created, or regenerates MEMORY.md when memory.json is restored.
  async _undoLast() {
    this._loadUndoStack();
    if (!this._undoStack || this._undoStack.length === 0) { await this.speak('Nothing to undo.'); return; }
    const fs = require('fs'), path = require('path');
    const entry = this._undoStack.shift();
    const { label, path: target, existed, before } = entry;
    try {
      if (existed) {
        // Restore prior content atomically (tmp in ultron/tmp, then rename over target)
        const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-undo.tmp`);
        fs.mkdirSync(path.dirname(tmp), { recursive: true });
        fs.writeFileSync(tmp, before == null ? '' : before, 'utf8');
        fs.renameSync(tmp, target);
        // memory.json restored → rebuild the human-view MEMORY.md from it
        if (path.basename(target) === 'memory.json') this._regenMemoryMd();
      } else {
        // The action created this file → undo = delete it (guard if already gone)
        try { fs.unlinkSync(target); } catch (e) { if (e.code !== 'ENOENT') throw e; }
      }
    } catch (e) {
      console.error('[Ultron] _undoLast error', e);
      this._logAction({ source: 'voice-cmd', action: 'undo', targetPath: target, status: 'error', argPreview: this._redact(label), error: String(e.message) });
      await this.speak("I couldn't undo that.");
      return;
    }
    // Persist an undo-marker so the on-disk log stays consistent with the in-memory stack.
    try { fs.appendFile(path.join(this._ultronDir(), 'undo.jsonl'), JSON.stringify({ ts: new Date().toISOString(), marker: 'undo', path: target }) + '\n', () => {}); } catch (_) {}
    this._logAction({ source: 'voice-cmd', action: 'undo', targetPath: target, status: 'ok', argPreview: this._redact(label), confirm: 'auto' });
    await this.speak(`Undone: ${label}.`);
  }
  // Ultron's persistent memory for the brain prompt: USER.md prefs + recent memory notes.
  _ultronMemory() {
    const fs = require('fs'), path = require('path'), dir = this._ultronDir(), parts = [];
    // memory-sys-no-memory-json: seed an empty memory.json if it has never been created
    // (happens when no "Ultron, remember that..." command has been issued yet).
    const jf = path.join(dir, 'memory.json');
    if (!fs.existsSync(jf)) {
      try { fs.writeFileSync(jf, JSON.stringify({ agent: 'ultron', updated: '', notes: [] }, null, 1)); } catch (_) {}
    }
    try { const u = fs.readFileSync(path.join(dir, 'USER.md'), 'utf8').replace(/^---[\s\S]*?---/, '').replace(/[#*>`]/g, '').trim(); if (u) parts.push('About Tony: ' + u.slice(0, 750)); } catch (_) {} // grounding-ctx-08: raised 500→750 for richer prefs
    // memory-sys-8-note-ceiling: raise injected notes from 8 to 20 so older memories stay visible
    // memory-sys-notes-flat-string: format as dated bullet lines so model treats each note as a separate fact
    try {
      const m = JSON.parse(fs.readFileSync(jf, 'utf8'));
      const notes = (m.notes || []).slice(0, 20);
      if (notes.length) {
        const lines = notes.map(x => `- ${x.ts ? x.ts.slice(0, 10) + ': ' : ''}${x.note}`).join('\n');
        parts.push('Ultron remembers:\n' + lines);
      }
    } catch (_) {}
    return parts.join('\n');
  }
  // Deterministic voice-command matcher (Phase A1). Returns {kind,arg} or null → normal brain turn.
  _matchVoiceCommand(text) {
    let m;
    // p6-#17 decision queue (read-only, spoken) — matched before the write-commands so the
    // aggregation path (no _confirm-then-write) handles it. Phrasings: "what needs my decision",
    // "decision queue", "what's pending", "what needs my attention", "anything to decide".
    if (/^(?:ultron[,\s]+)?(?:what(?:'?s| is| do i| needs?)?\s+(?:needs?\s+)?(?:my\s+)?(?:decision|attention|deciding|to\s+decide|review|pending)|(?:my\s+)?decision\s+queue|what'?s\s+pending|anything\s+(?:pending|to\s+(?:decide|review))|what\s+needs\s+deciding)\b[?.!]*$/i.test(text.trim())) return { kind: 'decisions' };
    if ((m = text.match(/^(?:ultron[,\s]+)?remember(?:\s+that)?\s+(.+)/i))) return { kind: 'mem', arg: m[1].trim() };
    if ((m = text.match(/^(?:ultron[,\s]+)?(?:add|note|put|log)\s+(.+?)\s+(?:to|on|in)\s+(?:my\s+)?(?:today|daily(?:\s+note)?|day)\.?$/i))) return { kind: 'daily', arg: m[1].trim() };
    if ((m = text.match(/^(?:ultron[,\s]+)?(?:add to today|note for today)\b[:,]?\s*(.+)/i))) return { kind: 'daily', arg: m[1].trim() };
    if ((m = text.match(/^(?:ultron[,\s]+)?draft\s+(?:an?\s+|the\s+)?(?:e-?mail|note|message|memo|reply|draft)\s+(?:about|on|re|regarding|for|to)\s+(.+)/i))) return { kind: 'draft', arg: m[1].trim() };
    return null;
  }
  async _runVoiceCommand(vc) {
    if (this._busy || this._awaitingConfirm) return;
    // p6-#17 decision queue is a READ-ONLY aggregation+readback (its own _confirm for accept/veto
    // lives inside) — it does NOT take the _confirm-then-write path the other kinds use below.
    if (vc.kind === 'decisions') return this._speakDecisionQueue();
    const preview = vc.arg.length > 70 ? vc.arg.slice(0, 70) + '…' : vc.arg;
    const proposal = vc.kind === 'mem' ? `Remember that ${preview}`
      : vc.kind === 'daily' ? `Add to today: ${preview}`
      : `Draft a note about ${preview}`;
    const ok = await this._confirm(proposal); // EVERY disk write is confirmed first (Codex r2-C2)
    if (!ok) { await this.speak('Cancelled.'); return; }
    this._busy = true;
    if (this.orb) this.orb.setState('thinking');
    this._setStage('thinking');
    let say = '';
    try {
      if (vc.kind === 'mem') { await this._memAppend(vc.arg); say = 'Noted.'; }
      else if (vc.kind === 'daily') { await this._appendDaily({ heading: '## Notes', text: vc.arg }); say = 'Added to today.'; }
      else if (vc.kind === 'draft') {
        const body = await this._composeText(`Write a concise, professional draft about: ${vc.arg}\nPlain text only, no preamble, no markdown headings. Tony reviews and sends it himself.`).catch(() => '');
        const r = await this._inboxNote({ title: vc.arg, body: body || `(draft requested: ${vc.arg})`, output_type: 'email_draft' });
        say = r.ok ? 'Drafted to your inbox for review.' : 'I could not validate that draft, so I held it.';
      }
    } catch (_) { say = 'That one slipped through my fingers. Try again.'; }
    this._busy = false;
    await this.speak(say);
  }
  // A2 prefilter — does this look like an action (imperative), vs a question? Only then do we
  // pay for the action-turn classification call. Deterministic commands already matched earlier.
  // actions-sbap-tryaction-misfire-on-questions: require BOTH a verb AND an explicit storage/time
  // destination so "write me a poem"/"create a summary" are NOT treated as vault-write actions.
  // Real 1-word (or short) voice commands that the STT ≥2-word silence-hallucination guard
  // would otherwise eat. These are clear imperatives, not whisper noise ("you"/"the"/"thanks").
  _isShortCommand(text) {
    return /^(?:undo|revert|pause|play|stop|resume|next|skip|previous|back|mute|unmute|cancel|repeat|continue|nevermind|never\s*mind)\b/i.test((text || '').trim());
  }
  _looksActionable(text) {
    if (!text) return false;
    // Indirect / polite OPEN-family imperatives that DON'T start with the verb, e.g.
    // "I want you to open Spotify", "can you open Slack", "go ahead and launch Safari",
    // "please pull up my Globex note", "now fire up Spotify". Admitted BEFORE the interrogative
    // guard so polite question-shaped commands ("can you open Spotify?") still pass. The
    // structured classifier in _tryActionTurn is the real arbiter (it returns none for genuine
    // questions) and OPEN actions are non-destructive, so a false positive only ever costs one
    // extra brain call — never a wrong write. This kills the "I need your approval" fallthrough.
    if (/\b(?:i(?:\s+(?:want|need|would\s+like)|'?d\s+like)\s+you\s+to|can\s+you|could\s+you|would\s+you|will\s+you|please|go\s+ahead\s+and|go|now)\s+(?:please\s+)?(?:open(?:s)?|launch(?:es)?|fire\s+up|bring\s+up|pull\s+up|start\s+up)\b/i.test(text)) return true;
    // Never treat interrogative sentences as actions
    if (/^(?:ultron[,\s]+)?(?:what|who|why|how|when|where)\b|\?/i.test(text)) return false;
    // UNDO: "undo" / "undo that" / "undo the last" / "revert that" / "scratch that" / "never mind that".
    // (Plain questions already returned false above, so these never swallow an interrogative.)
    if (/^(?:ultron[,\s]+)?(?:undo|revert|scratch\s+that|never\s*mind\s+that)\b/i.test(text)) return true;
    // OPEN family (non-destructive: open/launch an app, url, file, note, or web search).
    // These bypass _confirm in _tryActionTurn, so the prefilter must catch them. Kept strict so
    // plain questions never match (interrogatives already returned false above).
    //   "open Spotify" / "launch Slack" / "open my Globex note" / "open spotify.com"
    //   "pull up the pipeline" / "show me my <X> note" / "search the web for X" / "look up X online"
    if (/^(?:ultron[,\s]+)?(?:open(?:s)?|launch(?:es)?|fire\s+up|bring\s+up|pull\s+up)\b/i.test(text)) return true; // opens/launches = STT garble of the imperative ("Ultron opens Spotify")
    if (/^(?:ultron[,\s]+)?show\s+me\s+(?:my\s+)?.+\bnote\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:search\s+the\s+web|web\s+search|google)\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:search|look\s+up|find)\b.+\b(?:on\s+the\s+web|on\s+the\s+internet|online|on\s+google)\b/i.test(text)) return true;
    // ── macOS capabilities (media / timer / reminder / calendar) ──
    // media_control: "pause/play/skip/resume/next/previous (the/this) (song/music/track/playback)"
    if (/^(?:ultron[,\s]+)?(?:play|pause|resume|stop|skip|next|previous|prev|unpause)\b.*\b(?:song|music|track|playback|tune|it)\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:pause|resume|unpause|play|stop)\s+(?:the\s+)?music\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:skip|next|previous|prev)\s+(?:the\s+|this\s+)?(?:song|track|tune)\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:play|pause)\s*$/i.test(text)) return true;
    // calendar_today: "what's on my calendar / what's my day (look like) / my agenda / what's on my agenda"
    // (these begin with an interrogative which was rejected above; re-admit the agenda phrasings)
    if (/\b(?:on\s+my\s+(?:calendar|agenda|schedule|plate\s+today)|my\s+(?:day|agenda|schedule)\s+look|what'?s\s+(?:my\s+day|on\s+my\s+(?:calendar|agenda|schedule)))\b/i.test(text)) return true;
    if (/^(?:ultron[,\s]+)?(?:read|check|tell\s+me)\b.*\b(?:my\s+)?(?:calendar|agenda|schedule)\b/i.test(text)) return true;
    // calendar_create: "schedule a call … tomorrow/at 2pm" / "add … to my calendar" / "put … on my calendar"
    if (/^(?:ultron[,\s]+)?(?:add|put)\b.+\b(?:to|on)\s+(?:my\s+)?calendar\b/i.test(text)) return true;
    // Must start with an action verb
    if (!/^(?:ultron[,\s]+)?(write|create|make|add|save|log|jot|capture|record|draft|compose|file|schedule|remind|set\s+up|set\s+a|put\s|move\s|update\s|change\s|append\s)\b/i.test(text)) return false;
    // timer: "set a 10 minute timer" / "start a 5-minute timer" — require a number + "timer"
    if (/\btimer\b/i.test(text) && /\d/.test(text)) return true;
    // reminder_create: "remind me to call Marie" — reminders may be timeless (when is optional).
    // Catch "remind me …" here BEFORE the time-anchor-gated schedule block below.
    if (/^(?:ultron[,\s]+)?remind\s+me\b/i.test(text)) return true;
    if (/\breminder\b/i.test(text) && /^(?:ultron[,\s]+)?(?:add|create|make|set)\b/i.test(text)) return true;
    // schedule/remind family: require a time anchor
    if (/^(?:ultron[,\s]+)?(?:schedule|remind)\b/i.test(text) || /\breminder\b/i.test(text)) {
      return /\b(?:(?:for|at|on)\s+)?(?:tomorrow|today|monday|tuesday|wednesday|thursday|friday|saturday|sunday|\d+\s*(?:am|pm|:\d))/i.test(text);
    }
    // Bid-stage verb: "move <bid> to <stage>" or "set <bid> stage to <stage>"
    if (/^(?:ultron[,\s]+)?(?:move|update|change|set)\b/i.test(text)) {
      return /\b(?:to|stage)\s+(?:discover|qualify|propose|negotiate|won|lost)\b/i.test(text) ||
             /\bstage\s+(?:of|for)\b/i.test(text);
    }
    // Decision log: "log a decision for <bid>" / "add a decision to <bid>"
    if (/^(?:ultron[,\s]+)?(?:log|add)\b/i.test(text) && /\bdecision\b/i.test(text)) return true;
    // File-note: "file a note to <allowlisted-folder>" or "create a note in <folder>"
    if (/^(?:ultron[,\s]+)?(?:file|create|save)\b/i.test(text) && /\b(?:03_resources|02_areas|use\s+cases|reading|intelligence|resources|areas)\b/i.test(text)) return true;
    // Add-task: "add a task" / "create a to-do"
    if (/^(?:ultron[,\s]+)?(?:add|create)\b/i.test(text) && /\b(?:task|to[-\s]?do|todo|action\s+item)\b/i.test(text)) return true;
    // Append-note: "append to <note>" / "add this to <note>"
    if (/^(?:ultron[,\s]+)?(?:append|add)\b/i.test(text) && /\b(?:to|in)\s+(?:my\s+)?(?:the\s+)?\w/i.test(text) && /\bnote\b/i.test(text)) return true;
    // All other verbs: require an explicit storage destination word
    return /\b(?:to|in|into|on)\s+(?:my\s+)?(?:vault|inbox|daily(?:\s+note)?|today(?:'s)?(?:\s+note)?|notes?|memory|journal|log)\b/i.test(text);
  }
  // A2 — one buffered, NON-spoken structured brain call that maps the request to ONE action or
  // none. JSON never reaches TTS. Returns true if it handled an action (incl. a cancel), false to
  // fall through to a normal spoken answer.
  async _tryActionTurn(text) {
    if (this._busy || this._awaitingConfirm) return false;
    let raw = '';
    try {
      raw = await this._composeText(
        'You convert Tony\'s spoken request into ONE action, or none. Return ONLY compact JSON — no prose, no code fence. ' +
        'Schema: {"action":"inbox_note|append_daily|mem_append|set_bid_stage|decision_log|file_note|add_task|append_note|open_app|open_url|open_path|open_note|web_search|media_control|timer|reminder_create|calendar_today|calendar_create|undo|none","args":{...},"say":"<=10 word spoken confirmation"}. ' +
        'args for inbox_note: {title, body, output_type(one of: other,email_draft,proposal_draft,intelligence_brief)}; ' +
        'append_daily: {text}; mem_append: {text}; ' +
        'set_bid_stage: {bid (fuzzy name of the bid/project), stage (one of: Discover,Qualify,Propose,Negotiate,Won,Lost)}; ' +
        'decision_log: {bid (fuzzy name of the bid/project), text (the decision to log)}; ' +
        'file_note: {title, body, folder (one of: 03_Resources,02_Areas,Use Cases,Reading,10_Intelligence)}; ' +
        'add_task: {text (the task text), where (optional: "today" for daily note, or a bid/project name)}; ' +
        'append_note: {note (name or fuzzy title of an existing note), text (content to append)}; ' +
        'open_app: {app (a macOS application name, e.g. "Spotify","Slack","Safari")}; ' +
        'open_url: {url (a website, e.g. "spotify.com" or "https://news.ycombinator.com")}; ' +
        'open_path: {path (a file path on disk or relative to the vault)}; ' +
        'open_note: {note (name or fuzzy title of an existing vault note to open in Obsidian)}; ' +
        'web_search: {query (what to search the web for)}; ' +
        'media_control: {op (one of: playpause,play,pause,next,previous,stop)}; ' +
        'timer: {minutes (number), label (optional short name)}; ' +
        'reminder_create: {text (what to be reminded of), when (optional natural date/time, e.g. "tomorrow 9am")}; ' +
        'calendar_today: {day (optional: "today" or "tomorrow", default today)}; ' +
        'calendar_create: {title, start (ISO or natural date/time), end (optional), calendar (optional calendar name)}; ' +
        'undo: {} (reverse the last vault file change Ultron made). ' +
        'Examples: "open Spotify" -> {"action":"open_app","args":{"app":"Spotify"}}; ' +
        '"pause the music" / "pause" -> {"action":"media_control","args":{"op":"playpause"}}; ' +
        '"next song" / "skip this track" -> {"action":"media_control","args":{"op":"next"}}; ' +
        '"previous song" -> {"action":"media_control","args":{"op":"previous"}}; ' +
        '"set a 10 minute timer" -> {"action":"timer","args":{"minutes":10}}; ' +
        '"set a 5 minute pasta timer" -> {"action":"timer","args":{"minutes":5,"label":"Pasta"}}; ' +
        '"remind me to call Marie" -> {"action":"reminder_create","args":{"text":"call Marie"}}; ' +
        '"remind me to send the deck tomorrow at 9am" -> {"action":"reminder_create","args":{"text":"send the deck","when":"tomorrow 9am"}}; ' +
        '"what\'s on my calendar" / "what\'s my day look like" / "what\'s my agenda" -> {"action":"calendar_today","args":{}}; ' +
        '"what\'s on my calendar tomorrow" -> {"action":"calendar_today","args":{"day":"tomorrow"}}; ' +
        '"schedule a call with Globex tomorrow at 2pm" / "add a review to my calendar Friday 4pm" -> {"action":"calendar_create","args":{"title":"call with Globex","start":"tomorrow 2pm"}}; ' +
        '"open spotify.com" -> {"action":"open_url","args":{"url":"spotify.com"}}; ' +
        '"open my Globex note" / "show me my pipeline note" / "pull up the Globex note" -> {"action":"open_note","args":{"note":"Globex"}}; ' +
        '"open the file at 03_Resources/Brand DNA.md" -> {"action":"open_path","args":{"path":"03_Resources/Brand DNA.md"}}; ' +
        '"search the web for Globex Q2 results" / "look up Globex Q2 results online" -> {"action":"web_search","args":{"query":"Globex Q2 results"}}; ' +
        '"undo that" / "undo" / "revert that" / "scratch that" / "never mind that" -> {"action":"undo","args":{}}. ' +
        'CRITICAL: If it is a question, a lookup of vault knowledge, or not a clear imperative action, return {"action":"none"}. ' +
        'A question MUST return none — never trigger an action for a question (e.g. "what is open on my plate" is NOT open_app). ' +
        'Request: ' + JSON.stringify(text));
    } catch (_) { return false; }
    let obj = null; try { const mm = raw.match(/\{[\s\S]*\}/); if (mm) obj = JSON.parse(mm[0]); } catch (_) {}
    if (!obj || !obj.action || obj.action === 'none') return false;
    const a = obj.args || {};

    // ── OPEN actions: non-destructive, so they bypass the _confirm yes/no gate. ──
    // They announce + execute immediately and still audit via _logAction. The
    // confirm gate below stays ONLY for vault writes.
    const OPEN_ACTIONS = new Set(['open_app', 'open_url', 'open_path', 'open_note', 'web_search']);
    if (OPEN_ACTIONS.has(obj.action)) {
      if (this._busy || this._awaitingConfirm) return false;
      this._busy = true; if (this.orb) this.orb.setState('thinking');
      let say = '';
      try { say = await this._runOpenAction(obj.action, a); }
      catch (e) { say = 'That one slipped through my fingers. Try again.'; console.error('[Ultron] _runOpenAction error', e); }
      this._busy = false;
      if (say === null) { return false; } // not enough info → fall through to a normal spoken answer
      await this.speak(say);
      return true;
    }

    // ── ANNOUNCE actions: harmless/ephemeral/read-only macOS capabilities. ──
    // Like OPEN_ACTIONS they bypass the _confirm yes/no gate (announce + do) and
    // still audit via _logAction. calendar_create is the ONLY new capability that
    // is a real write, so it falls through to the _confirm path below.
    const ANNOUNCE_ACTIONS = new Set(['media_control', 'timer', 'reminder_create', 'calendar_today']);
    if (ANNOUNCE_ACTIONS.has(obj.action)) {
      if (this._busy || this._awaitingConfirm) return false;
      this._busy = true; if (this.orb) this.orb.setState('thinking');
      let say = '';
      try { say = await this._runCapabilityAction(obj.action, a); }
      catch (e) { say = 'That one slipped through my fingers. Try again.'; console.error('[Ultron] _runCapabilityAction error', e); }
      this._busy = false;
      if (say === null) { return false; } // not enough info → fall through to a normal spoken answer
      await this.speak(say);
      return true;
    }

    // ── UNDO: reverse the last vault file mutation. Like ANNOUNCE actions it bypasses ──
    // the _confirm gate (the spoken "undo that" IS the approval) and audits via _logAction.
    if (obj.action === 'undo') {
      if (this._busy || this._awaitingConfirm) return false;
      this._busy = true; if (this.orb) this.orb.setState('thinking');
      try { await this._undoLast(); }
      catch (e) { console.error('[Ultron] _undoLast error', e); await this.speak("I couldn't undo that."); }
      this._busy = false;
      return true;
    }

    const preview = (a.text || a.title || a.body || a.bid || a.note || '').toString();
    if (!preview) return false; // nothing concrete → normal answer
    let proposal = (obj.say && String(obj.say).length > 2) ? String(obj.say) : `Do this: ${preview.slice(0, 60)}`;
    // calendar_create is a write — give a precise, spec-shaped proposal for the yes/no gate.
    if (obj.action === 'calendar_create' && a.title && a.start) {
      proposal = `Create event '${String(a.title).slice(0, 60)}' at ${String(a.start).slice(0, 40)}`;
    }
    const ok = await this._confirm(proposal); // every write confirmed first
    if (!ok) { await this.speak('Cancelled.'); return true; }
    this._busy = true; if (this.orb) this.orb.setState('thinking');
    let say = '';
    try {
      if (obj.action === 'mem_append' && a.text) { await this._memAppend(a.text); say = 'Noted.'; }
      else if (obj.action === 'append_daily' && a.text) { await this._appendDaily({ heading: '## Notes', text: a.text, source: 'model' }); say = 'Added to today.'; }
      else if (obj.action === 'inbox_note' && (a.title || a.body)) {
        const r = await this._inboxNote({ title: a.title || 'note', body: a.body || a.title, output_type: a.output_type || 'other' });
        say = r.ok ? 'Drafted to your inbox for review.' : 'I could not validate that, so I held it.';
      }
      else if (obj.action === 'set_bid_stage' && a.bid && a.stage) {
        const r = await this._setBidStage({ bid: a.bid, stage: a.stage });
        say = r.say;
      }
      else if (obj.action === 'decision_log' && a.bid && a.text) {
        const r = await this._decisionLog({ bid: a.bid, text: a.text });
        say = r.say;
      }
      else if (obj.action === 'file_note' && a.title) {
        const r = await this._fileNote({ title: a.title, body: a.body || '', folder: a.folder });
        say = r.say;
      }
      else if (obj.action === 'add_task' && a.text) {
        const r = await this._addTask({ text: a.text, where: a.where });
        say = r.say;
      }
      else if (obj.action === 'append_note' && a.note && a.text) {
        const r = await this._appendNote({ note: a.note, text: a.text });
        say = r.say;
      }
      else if (obj.action === 'calendar_create' && a.title && a.start) {
        const r = await this._calendarCreate({ title: a.title, start: a.start, end: a.end, calendar: a.calendar });
        say = r.say;
      }
      else { this._busy = false; return false; } // unrecognized → fall through to normal answer
    } catch (e) { say = 'That one slipped through my fingers. Try again.'; console.error('[Ultron] _tryActionTurn executor error', e); }
    this._busy = false; await this.speak(say);
    return true;
  }

  // ── OPEN executors (5 non-destructive actions) ───────────────────────────────
  //
  // Safety invariants (all five):
  //   1. NON-DESTRUCTIVE — open only; never write, delete, or modify anything.
  //   2. NO _confirm GATE — fast: announce + execute. (yes/no stays for vault writes.)
  //   3. NO SHELL — every macOS open uses cp.execFile('open', [..args..]) with an args
  //      ARRAY, so app/url/path/query are discrete argv entries, never concatenated into
  //      a shell string → no command injection.
  //   4. VALIDATE before launching — reject non-http(s) urls, traversal-y paths, missing
  //      files/notes; speak a clean failure instead.
  //   5. AUDITED — every action calls _logAction → actions.jsonl.
  //
  // Returns: a spoken string on success/clean-failure, or null to fall through to a
  // normal spoken answer (insufficient args).
  async _runOpenAction(action, a) {
    if (action === 'open_app')   return this._openApp(a.app);
    if (action === 'open_url')   return this._openUrl(a.url);
    if (action === 'open_path')  return this._openPath(a.path);
    if (action === 'open_note')  return this._openNote(a.note);
    if (action === 'web_search') return this._webSearch(a.query);
    return null;
  }

  // open_app — launch a macOS application by name. execFile args-array, never shell.
  _openApp(app) {
    app = (app == null ? '' : String(app)).trim();
    if (!app) return null;
    const cp = require('child_process');
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/open', ['-a', app], { env: this._brainEnv() }, (err) => {
        if (err) {
          this._logAction({ source: 'voice-cmd', action: 'open_app', status: 'error', argPreview: this._redact(app), error: String(err.message), confirm: 'none' });
          resolve(`I couldn't find ${app}.`);
        } else {
          this._logAction({ source: 'voice-cmd', action: 'open_app', status: 'ok', argPreview: this._redact(app), confirm: 'none' });
          resolve(`Opening ${app}.`);
        }
      });
    });
  }

  // open_url — open a web URL. http(s) only; bare domains get https:// prepended.
  _openUrl(url) {
    url = (url == null ? '' : String(url)).trim();
    if (!url) return null;
    let target = url;
    if (!/^https?:\/\//i.test(target)) {
      // Bare domain like "spotify.com" or "news.ycombinator.com/x" → prepend https://.
      // Reject anything that isn't domain-shaped (must have a dot, no spaces).
      if (/^[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:[/:?#].*)?$/i.test(target)) target = 'https://' + target;
    }
    if (!/^https?:\/\//i.test(target)) {
      this._logAction({ source: 'voice-cmd', action: 'open_url', status: 'rejected', reason: 'not-http', argPreview: this._redact(url), confirm: 'none' });
      return `That doesn't look like a web address I can open.`;
    }
    const cp = require('child_process');
    const spoken = target.replace(/^https?:\/\//i, '').replace(/\/+$/, '');
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/open', [target], { env: this._brainEnv() }, (err) => {
        this._logAction({ source: 'voice-cmd', action: 'open_url', status: err ? 'error' : 'ok', targetPath: target, argPreview: this._redact(target), error: err ? String(err.message) : undefined, confirm: 'none' });
        resolve(err ? `I couldn't open that link.` : `Opening ${spoken}.`);
      });
    });
  }

  // open_path — open a file/folder. Absolute+existing OR vault-relative+existing.
  // Reject `..` traversal unless it's an absolute path that already exists.
  _openPath(p) {
    p = (p == null ? '' : String(p)).trim();
    if (!p) return null;
    const fs = require('fs'), path = require('path'), cp = require('child_process');
    let resolved = null;
    if (path.isAbsolute(p)) {
      if (fs.existsSync(p)) resolved = p;
    } else {
      if (p.split(/[\\/]/).includes('..')) { // no traversal on relative paths
        this._logAction({ source: 'voice-cmd', action: 'open_path', status: 'rejected', reason: 'traversal', argPreview: this._redact(p), confirm: 'none' });
        return `I couldn't find that file.`;
      }
      const base = this._vaultPath();
      const cand = path.resolve(base, p);
      // Must stay inside the vault and exist.
      if ((cand === base || cand.startsWith(base + path.sep)) && fs.existsSync(cand)) resolved = cand;
    }
    if (!resolved) {
      this._logAction({ source: 'voice-cmd', action: 'open_path', status: 'rejected', reason: 'not-found', argPreview: this._redact(p), confirm: 'none' });
      return `I couldn't find that file.`;
    }
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/open', [resolved], { env: this._brainEnv() }, (err) => {
        this._logAction({ source: 'voice-cmd', action: 'open_path', status: err ? 'error' : 'ok', targetPath: resolved, argPreview: this._redact(resolved), error: err ? String(err.message) : undefined, confirm: 'none' });
        resolve(err ? `I couldn't open that file.` : `Opening ${path.basename(resolved)}.`);
      });
    });
  }

  // open_note — open a vault note IN OBSIDIAN (in-app, instant). Fuzzy basename match.
  async _openNote(note) {
    note = (note == null ? '' : String(note)).trim();
    if (!note) return null;
    const app = this.plugin.app;
    // 1. Exact linkpath resolution first.
    let file = null;
    try { file = app.metadataCache.getFirstLinkpathDest(note.replace(/\.md$/i, ''), ''); } catch (_) {}
    // 2. Fuzzy basename contains (case-insensitive); prefer exact basename, then shortest.
    if (!file) {
      const q = note.toLowerCase().replace(/\.md$/i, '');
      let matches = [];
      try { matches = app.vault.getMarkdownFiles().filter(f => f.basename.toLowerCase().includes(q)); } catch (_) {}
      if (matches.length) {
        matches.sort((x, y) => {
          const xe = x.basename.toLowerCase() === q ? 0 : 1;
          const ye = y.basename.toLowerCase() === q ? 0 : 1;
          if (xe !== ye) return xe - ye;
          return x.basename.length - y.basename.length; // prefer shortest (tightest) match
        });
        file = matches[0];
      }
    }
    if (!file) {
      this._logAction({ source: 'voice-cmd', action: 'open_note', status: 'rejected', reason: 'not-found', argPreview: this._redact(note), confirm: 'none' });
      return `I couldn't find a note called ${note}.`;
    }
    try { await app.workspace.getLeaf(false).openFile(file); }
    catch (_) { try { await app.workspace.openLinkText(file.path, '', false); } catch (__) {} }
    this._logAction({ source: 'voice-cmd', action: 'open_note', status: 'ok', targetPath: file.path, argPreview: this._redact(file.basename), confirm: 'none' });
    return `Opening ${file.basename}.`;
  }

  // web_search — open a Google search for the query. encodeURIComponent → safe argv.
  _webSearch(query) {
    query = (query == null ? '' : String(query)).trim();
    if (!query) return null;
    const cp = require('child_process');
    const url = 'https://www.google.com/search?q=' + encodeURIComponent(query);
    return new Promise((resolve) => {
      cp.execFile('/usr/bin/open', [url], { env: this._brainEnv() }, (err) => {
        this._logAction({ source: 'voice-cmd', action: 'web_search', status: err ? 'error' : 'ok', argPreview: this._redact(query), error: err ? String(err.message) : undefined, confirm: 'none' });
        resolve(err ? `I couldn't open the browser.` : `Searching the web for ${query}.`);
      });
    });
  }

  // ── Keyless macOS capability executors (media / timer / reminder / calendar-read) ──
  //
  // Safety invariants (all):
  //   1. ANNOUNCE-and-do — no _confirm gate (harmless / ephemeral / read-only).
  //      (calendar_create, a real write, lives on the _confirm path — see _calendarCreate.)
  //   2. NO SHELL STRING-BUILDING — every osascript runs via cp.execFile('osascript',
  //      ['-e', script, ...args]); dynamic user values arrive in `argv`, never concatenated
  //      into the script source → injection-safe.
  //   3. TIMEOUT on every osascript call — Calendar/Reminders scripting can hang.
  //   4. NEVER THROW to the caller — each catch/err path resolves a clean spoken fallback.
  //   5. AUDITED — every action calls _logAction → actions.jsonl.
  //   6. macOS Automation (TCC) permission: Reminders/Calendar will prompt once on first
  //      real use; a permission denial surfaces as the clean fallback string, not a crash.
  async _runCapabilityAction(action, a) {
    if (action === 'media_control')   return this._mediaControl(a.op);
    if (action === 'timer')           return this._setTimer(a.minutes, a.label);
    if (action === 'reminder_create') return this._reminderCreate(a.text, a.when);
    if (action === 'calendar_today')  return this._calendarToday(a.day);
    return null;
  }

  // Run an osascript with dynamic values passed via argv (injection-safe). Resolves
  // { ok, out } — never rejects. Timeout-guarded; SIGTERM on timeout reads as !ok.
  _osascript(script, args = [], timeoutMs = 8000) {
    const cp = require('child_process');
    return new Promise((resolve) => {
      let done = false;
      const finish = (ok, out) => { if (done) return; done = true; resolve({ ok, out: (out || '').toString().trim() }); };
      try {
        cp.execFile('/usr/bin/osascript', ['-e', script, ...args.map(x => (x == null ? '' : String(x)))],
          { timeout: timeoutMs, encoding: 'utf8', maxBuffer: 1 << 20, env: this._brainEnv() },
          (err, stdout, stderr) => {
            if (err) return finish(false, stderr || err.message);
            finish(true, stdout);
          });
      } catch (e) { finish(false, String(e && e.message)); }
    });
  }

  // media_control — control Spotify (preferred) or Music, whichever is running. Single
  // osascript checks running state so we never launch an app just to control it.
  async _mediaControl(op) {
    const OPS = { playpause: 'playpause', play: 'play', pause: 'pause', stop: 'stop', next: 'next track', previous: 'previous track' };
    const key = String(op || 'playpause').toLowerCase().trim();
    const cmd = OPS[key];
    if (!cmd) {
      this._logAction({ source: 'voice-cmd', action: 'media_control', status: 'rejected', reason: 'bad-op', argPreview: this._redact(key), confirm: 'none' });
      return null; // unknown op → fall through to a normal spoken answer
    }
    // argv item 1 = the command verb ("playpause" / "next track" / …). The script branches on
    // which player is running; if neither, it returns the sentinel "NONE".
    const script = [
      'on run argv',
      '  set theCmd to item 1 of argv',
      '  if application "Spotify" is running then',
      '    if theCmd is "next track" then tell application "Spotify" to next track',
      '    if theCmd is "previous track" then tell application "Spotify" to previous track',
      '    if theCmd is "play" then tell application "Spotify" to play',
      '    if theCmd is "pause" then tell application "Spotify" to pause',
      '    if theCmd is "playpause" then tell application "Spotify" to playpause',
      '    if theCmd is "stop" then tell application "Spotify" to pause',
      '    return "SPOTIFY"',
      '  else if application "Music" is running then',
      '    if theCmd is "next track" then tell application "Music" to next track',
      '    if theCmd is "previous track" then tell application "Music" to previous track',
      '    if theCmd is "play" then tell application "Music" to play',
      '    if theCmd is "pause" then tell application "Music" to pause',
      '    if theCmd is "playpause" then tell application "Music" to playpause',
      '    if theCmd is "stop" then tell application "Music" to stop',
      '    return "MUSIC"',
      '  else',
      '    return "NONE"',
      '  end if',
      'end run',
    ].join('\n');
    const { ok, out } = await this._osascript(script, [cmd], 6000);
    if (!ok) {
      this._logAction({ source: 'voice-cmd', action: 'media_control', status: 'error', argPreview: this._redact(key), error: this._redact(out), confirm: 'none' });
      return `I couldn't control playback.`;
    }
    if (out === 'NONE') {
      this._logAction({ source: 'voice-cmd', action: 'media_control', status: 'ok', argPreview: this._redact(key + ':none'), confirm: 'none' });
      return `Nothing's playing.`;
    }
    this._logAction({ source: 'voice-cmd', action: 'media_control', status: 'ok', argPreview: this._redact(`${key}:${out.toLowerCase()}`), confirm: 'none' });
    if (key === 'pause' || key === 'stop') return 'Paused.';
    if (key === 'next' || key === 'previous') return 'Skipped.';
    return 'Done.';
  }

  // timer — PURE in-plugin setTimeout. Tracked in this._timers so onClose can cancel them.
  _setTimer(minutes, label) {
    const m = Number(minutes);
    if (!Number.isFinite(m) || m <= 0 || m > 600) {
      this._logAction({ source: 'voice-cmd', action: 'timer', status: 'rejected', reason: 'bad-minutes', argPreview: this._redact(String(minutes)), confirm: 'none' });
      return `I can set a timer between 1 and 600 minutes.`;
    }
    const name = (label && String(label).trim()) ? String(label).trim() : 'Timer';
    if (!Array.isArray(this._timers)) this._timers = [];
    const id = setTimeout(() => {
      // drop from the tracked list once it fires
      this._timers = (this._timers || []).filter(t => t.id !== id);
      try { this.speak(`${name} done.`); } catch (_) {}
    }, m * 60000);
    this._timers.push({ id, name, minutes: m, set: Date.now() });
    this._logAction({ source: 'voice-cmd', action: 'timer', status: 'ok', argPreview: this._redact(`${name}:${m}m`), confirm: 'none' });
    return `Timer set for ${m} ${m === 1 ? 'minute' : 'minutes'}.`;
  }

  // reminder_create — Reminders.app. argv item 1 = name; item 2 = optional date string.
  // The script parses the date with `date`; on parse failure it falls back to a name-only
  // reminder (AppleScript `date` throws on garbage, so we guard with a try inside the script).
  async _reminderCreate(text, when) {
    const name = (text == null ? '' : String(text)).trim();
    if (!name) {
      this._logAction({ source: 'voice-cmd', action: 'reminder_create', status: 'rejected', reason: 'empty', confirm: 'none' });
      return null; // nothing to remind → fall through to a normal answer
    }
    const whenStr = (when == null ? '' : String(when)).trim();
    const script = [
      'on run argv',
      '  set theName to item 1 of argv',
      '  set theWhen to ""',
      '  if (count of argv) > 1 then set theWhen to item 2 of argv',
      '  tell application "Reminders"',
      '    if theWhen is not "" then',
      '      try',
      '        set dueDate to date theWhen',
      '        make new reminder with properties {name:theName, due date:dueDate}',
      '      on error',
      '        make new reminder with properties {name:theName}',
      '      end try',
      '    else',
      '      make new reminder with properties {name:theName}',
      '    end if',
      '  end tell',
      '  return "OK"',
      'end run',
    ].join('\n');
    const { ok, out } = await this._osascript(script, [name, whenStr], 8000);
    if (!ok || out !== 'OK') {
      this._logAction({ source: 'voice-cmd', action: 'reminder_create', status: 'error', argPreview: this._redact(name), error: this._redact(out), confirm: 'none' });
      return `I couldn't add that reminder.`;
    }
    this._logAction({ source: 'voice-cmd', action: 'reminder_create', status: 'ok', argPreview: this._redact(whenStr ? `${name} @ ${whenStr}` : name), confirm: 'none' });
    return `Reminder added.`;
  }

  // calendar_today — READ-ONLY. List today's (or tomorrow's) events from every calendar and
  // speak a natural summary. Calendar scripting is slow → 8s timeout; timeout/denial → the
  // Automation-permission hint. argv item 1 = "today" | "tomorrow".
  async _calendarToday(day) {
    const which = String(day || 'today').toLowerCase().includes('tomorrow') ? 'tomorrow' : 'today';
    const script = [
      'on run argv',
      '  set theDay to item 1 of argv',
      '  set startDate to (current date)',
      '  set hours of startDate to 0',
      '  set minutes of startDate to 0',
      '  set seconds of startDate to 0',
      '  if theDay is "tomorrow" then set startDate to startDate + (1 * days)',
      '  set endDate to startDate + (1 * days)',
      '  set outLines to {}',
      '  tell application "Calendar"',
      '    repeat with c in calendars',
      '      set evs to (every event of c whose start date ≥ startDate and start date < endDate)',
      '      repeat with e in evs',
      '        set t to (start date of e)',
      '        set h to (hours of t)',
      '        set mn to (minutes of t)',
      '        set ampm to "am"',
      '        if h ≥ 12 then set ampm to "pm"',
      '        set h12 to h',
      '        if h12 is 0 then set h12 to 12',
      '        if h12 > 12 then set h12 to h12 - 12',
      '        set mtxt to ""',
      '        if mn > 0 then set mtxt to ":" & (text -2 thru -1 of ("0" & mn))',
      '        set end of outLines to ((h12 as string) & mtxt & ampm & " " & (summary of e))',
      '      end repeat',
      '    end repeat',
      '  end tell',
      '  set AppleScript\'s text item delimiters to "||"',
      '  return outLines as string',
      'end run',
    ].join('\n');
    const { ok, out } = await this._osascript(script, [which], 8000);
    if (!ok) {
      this._logAction({ source: 'voice-cmd', action: 'calendar_today', status: 'error', argPreview: this._redact(which), error: this._redact(out), confirm: 'none' });
      return `I couldn't read your calendar — you may need to grant Automation permission for Calendar in System Settings.`;
    }
    const items = out ? out.split('||').map(s => s.trim()).filter(Boolean) : [];
    this._logAction({ source: 'voice-cmd', action: 'calendar_today', status: 'ok', argPreview: this._redact(`${which}:${items.length}`), confirm: 'none' });
    const when = which === 'tomorrow' ? 'tomorrow' : 'today';
    if (items.length === 0) return `Nothing on your calendar ${when}.`;
    const n = items.length;
    const list = items.join(', ');
    return `You have ${n} ${n === 1 ? 'event' : 'events'} ${when}: ${list}.`;
  }

  // ── Operator executors (5 new vault-write actions) ───────────────────────────
  //
  // Safety invariants (all five):
  //   1. CONFIRM-GATED — every call comes through _confirm() in _tryActionTurn.
  //   2. APPEND / FRONTMATTER-FIELD-EDIT only — no delete, no overwrite, no truncation.
  //   3. ATOMIC — tmp+rename or read-modify-write serialized via _dailyMutex.
  //   4. VALIDATE TARGET before writing — abort + speak if missing/ambiguous.
  //   5. AUDITED — every action calls _logAction → actions.jsonl.
  //
  // Returns { say: string } — the spoken confirmation string for _tryActionTurn.

  // RFP library nests bids at RFPs/<Company>/<Topic>/<Opp>/ — collect every bid brief recursively.
  _findBidBriefs() {
    const fs = require('fs'), path = require('path');
    const root = path.join(this._vaultPath(), 'RFPs');
    const out = [];
    const walk = (dir) => {
      let ents;
      try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { return; }
      for (const e of ents) {
        if (e.name.startsWith('_')) continue; // skip _template/ + library files
        const full = path.join(dir, e.name);
        if (e.isDirectory()) walk(full);
        else if (e.name === '00 - Brief.md') { const d = path.dirname(full); out.push({ name: path.basename(d), folder: path.basename(d), dir: d, brief: full }); }
      }
    };
    walk(root);
    return out;
  }

  // set_bid_stage: edit ONLY the `stage:` frontmatter field of RFPs/<bid>/00 - Brief.md.
  async _setBidStage({ bid, stage }) {
    const fs = require('fs'), path = require('path'), glob = require('glob') || null;
    const base = this._vaultPath();
    const VALID_STAGES = ['Discover', 'Qualify', 'Propose', 'Negotiate', 'Won', 'Lost'];

    // Canonicalize stage (case-insensitive)
    const canonical = VALID_STAGES.find(s => s.toLowerCase() === String(stage || '').toLowerCase().trim());
    if (!canonical) {
      this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'rejected', reason: 'invalid-stage', argPreview: this._redact(`${bid} → ${stage}`) });
      return { say: `${stage} is not a valid stage. Try Discover, Qualify, Propose, Negotiate, Won, or Lost.` };
    }

    // RFP library is nested (RFPs/<Company>/<Topic>/<Opp>/) — recurse to find brief files.
    const bidLower = String(bid || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const matches = this._findBidBriefs().filter(m =>
      m.folder.toLowerCase().replace(/[^a-z0-9]/g, '').includes(bidLower));

    if (matches.length === 0) {
      this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'rejected', reason: 'not-found', argPreview: this._redact(`${bid} → ${canonical}`) });
      return { say: `I couldn't find a bid matching ${bid}. Want me to draft it instead?` };
    }
    if (matches.length > 1) {
      const names = matches.slice(0, 3).map(m => m.folder).join(', ');
      this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'rejected', reason: 'ambiguous', argPreview: this._redact(`${bid} → ${canonical}`) });
      return { say: `Multiple bids match: ${names}. Please be more specific.` };
    }

    const { folder, brief } = matches[0];
    let content;
    try { content = fs.readFileSync(brief, 'utf8'); }
    catch (_) { return { say: `I couldn't read the brief for ${folder}.` }; }

    // Edit ONLY the stage: field in the frontmatter — preserve everything else
    // Frontmatter is between the first two `---` lines.
    const fmMatch = content.match(/^---\r?\n([\s\S]*?)\r?\n---/);
    if (!fmMatch) {
      this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'rejected', reason: 'no-frontmatter', targetPath: brief, argPreview: this._redact(`${folder} → ${canonical}`) });
      return { say: `The brief for ${folder} has no frontmatter. I can't safely edit it.` };
    }

    let newContent;
    if (/^stage:/m.test(fmMatch[1])) {
      // Replace existing stage field value only
      newContent = content.replace(/^(stage:\s*).*$/m, `$1${canonical}`);
    } else {
      // Insert stage: field as first line inside frontmatter block
      newContent = content.replace(/^---\r?\n/, `---\nstage: ${canonical}\n`);
    }

    // Atomic write: tmp file in ultron/tmp, then rename
    const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-brief.md`);
    const existed = fs.existsSync(brief); const before = existed ? fs.readFileSync(brief, 'utf8') : null;
    try {
      fs.mkdirSync(path.dirname(tmp), { recursive: true });
      fs.writeFileSync(tmp, newContent, 'utf8');
      fs.renameSync(tmp, brief);
    } catch (e) {
      try { fs.unlinkSync(tmp); } catch (_) {}
      this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'error', targetPath: brief, argPreview: this._redact(`${folder} → ${canonical}`), error: String(e.message) });
      return { say: `I couldn't write to ${folder}'s brief. Try again.` };
    }

    this._pushUndo({ label: `${folder} bid stage → ${canonical}`, path: brief, existed, before });
    this._logAction({ source: 'voice-cmd', action: 'set_bid_stage', status: 'ok', targetPath: brief, argPreview: this._redact(`${folder} → ${canonical}`), confirm: 'voice' });
    return { say: `Moved ${folder} to ${canonical}.` };
  }

  // decision_log: append a timestamped decision bullet to RFPs/<bid>/*Decision*.md
  // (or to 00 - Brief.md under "## Decision Log" if no decision-log file exists).
  async _decisionLog({ bid, text }) {
    const fs = require('fs'), path = require('path');
    const base = this._vaultPath();
    // Locate bid brief recursively (RFP library is nested).
    const bidLower = String(bid || '').toLowerCase().replace(/[^a-z0-9]/g, '');
    const bidFolders = this._findBidBriefs()
      .filter(e => e.name.toLowerCase().replace(/[^a-z0-9]/g, '').includes(bidLower));

    if (bidFolders.length === 0) {
      this._logAction({ source: 'voice-cmd', action: 'decision_log', status: 'rejected', reason: 'not-found', argPreview: this._redact(`${bid}: ${text}`) });
      return { say: `I couldn't find a bid matching ${bid}. Want me to draft it instead?` };
    }
    if (bidFolders.length > 1) {
      const names = bidFolders.slice(0, 3).map(e => e.name).join(', ');
      return { say: `Multiple bids match: ${names}. Please be more specific.` };
    }

    const bidFolder = bidFolders[0].name;
    const bidDir = bidFolders[0].dir;

    // Look for a decision log file (anything with "Decision" in the name)
    let logFile = null;
    try {
      const files = fs.readdirSync(bidDir).filter(f => /decision/i.test(f) && f.endsWith('.md'));
      if (files.length > 0) logFile = path.join(bidDir, files[0]);
    } catch (_) {}
    // Fallback: use 00 - Brief.md with a ## Decision Log heading
    if (!logFile) {
      const brief = path.join(bidDir, '00 - Brief.md');
      try { fs.accessSync(brief, fs.constants.F_OK); logFile = brief; }
      catch (_) { return { say: `I couldn't find any file to log into for ${bidFolder}.` }; }
    }

    const ts = new Date().toISOString().slice(0, 10);
    const bullet = `- ${ts}: ${text}`;
    const HEADING = '## Decisions';

    // Read-modify-write: append under ## Decisions heading (create if absent), APPEND ONLY
    let cur;
    try { cur = fs.readFileSync(logFile, 'utf8'); }
    catch (_) { return { say: `I couldn't read the decision log for ${bidFolder}.` }; }

    let newContent;
    if (cur.includes(HEADING)) {
      // Append after the heading's block (before next ## heading or EOF)
      // We simply append at end-of-file with a newline — this is always safe
      newContent = cur.trimEnd() + '\n' + bullet + '\n';
    } else {
      newContent = cur.trimEnd() + '\n\n' + HEADING + '\n' + bullet + '\n';
    }

    // Atomic write
    const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-declog.md`);
    const existed = fs.existsSync(logFile); const before = existed ? fs.readFileSync(logFile, 'utf8') : null;
    try {
      fs.mkdirSync(path.dirname(tmp), { recursive: true });
      fs.writeFileSync(tmp, newContent, 'utf8');
      fs.renameSync(tmp, logFile);
    } catch (e) {
      try { fs.unlinkSync(tmp); } catch (_) {}
      this._logAction({ source: 'voice-cmd', action: 'decision_log', status: 'error', targetPath: logFile, argPreview: this._redact(`${bidFolder}: ${text}`), error: String(e.message) });
      return { say: 'I could not write the decision log. Try again.' };
    }

    this._pushUndo({ label: `decision logged to ${bidFolder}`, path: logFile, existed, before });
    this._logAction({ source: 'voice-cmd', action: 'decision_log', status: 'ok', targetPath: logFile, argPreview: this._redact(`${bidFolder}: ${text}`), confirm: 'voice' });
    return { say: `Logged to ${bidFolder}'s decision log.` };
  }

  // file_note: create a new note at an allowlisted folder. NEVER overwrites.
  async _fileNote({ title, body, folder }) {
    const fs = require('fs'), path = require('path');
    const base = this._vaultPath();

    // Allowlist: only these root folders are safe targets
    const ALLOWED_ROOTS = ['03_Resources', '02_Areas', 'Use Cases', 'Reading', '10_Intelligence'];
    // Normalize folder input: strip leading slashes, map display names
    const folderNorm = String(folder || '03_Resources').trim().replace(/^\/+/, '');
    const targetRoot = ALLOWED_ROOTS.find(r => r.toLowerCase() === folderNorm.toLowerCase() ||
      folderNorm.toLowerCase().includes(r.toLowerCase().replace('_', '').replace(' ', '')));

    if (!targetRoot) {
      // Reject and fall back to inbox_note
      this._logAction({ source: 'voice-cmd', action: 'file_note', status: 'rejected', reason: 'forbidden-folder', argPreview: this._redact(`${title} → ${folder}`) });
      const r = await this._inboxNote({ title, body: body || title, output_type: 'other' });
      return { say: r.ok ? `${folder} is not an allowed folder — filed to inbox instead.` : `I couldn't file that. ${folder} is not allowed.` };
    }

    // Validate target root exists
    const targetDir = path.join(base, targetRoot);
    try { fs.accessSync(targetDir, fs.constants.F_OK); }
    catch (_) { return { say: `The folder ${targetRoot} doesn't exist in the vault.` }; }

    // Build filename from title
    const slug = String(title).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 60) || 'note';
    const date = localDateStr();
    let fname = `${date}-${slug}.md`;
    let dest = path.join(targetDir, fname);

    // Never overwrite: if file exists, add a suffix
    if (fs.existsSync(dest)) {
      fname = `${date}-${slug}-2.md`;
      dest = path.join(targetDir, fname);
      if (fs.existsSync(dest)) {
        // Two collisions: ask instead of guessing
        this._logAction({ source: 'voice-cmd', action: 'file_note', status: 'rejected', reason: 'collision', argPreview: this._redact(title) });
        return { say: `A note called ${title} already exists in ${targetRoot}. Rename it or let me file it to inbox.` };
      }
    }

    const frontmatter = `---\ntype: note\ncreated: ${new Date().toISOString()}\nsource: ultron\ntags: [voice-filed]\n---\n\n`;
    const content = frontmatter + `# ${title}\n\n${body || ''}\n`;

    // Atomic write
    const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-filenote.md`);
    // _fileNote never overwrites (collision-guarded above) → existed is false here; undo deletes the new file.
    const existed = fs.existsSync(dest); const before = existed ? fs.readFileSync(dest, 'utf8') : null;
    try {
      fs.mkdirSync(path.dirname(tmp), { recursive: true });
      fs.writeFileSync(tmp, content, 'utf8');
      fs.renameSync(tmp, dest);
    } catch (e) {
      try { fs.unlinkSync(tmp); } catch (_) {}
      this._logAction({ source: 'voice-cmd', action: 'file_note', status: 'error', targetPath: dest, argPreview: this._redact(title), error: String(e.message) });
      return { say: 'I could not create that note. Try again.' };
    }

    this._pushUndo({ label: `note filed: ${title}`, path: dest, existed, before });
    this._logAction({ source: 'voice-cmd', action: 'file_note', status: 'ok', targetPath: dest, argPreview: this._redact(title), confirm: 'voice' });
    return { say: `Filed ${title} to ${targetRoot}.` };
  }

  // add_task: append `- [ ] <text>` under ## Tasks / ## To-do in today's daily note,
  // or under the same heading in the named bid's brief.
  async _addTask({ text, where }) {
    const fs = require('fs'), path = require('path');
    const base = this._vaultPath();
    const HEADING = '## Tasks';

    let targetFile = null;
    let targetLabel = 'today';

    if (!where || /^today$/i.test(String(where).trim())) {
      // Default: today's daily note
      const date = localDateStr();
      targetFile = path.join(base, '02_Areas', 'Daily', `${date}.md`);
      // Create daily note if absent (same pattern as _appendDaily)
      if (!fs.existsSync(targetFile)) {
        fs.mkdirSync(path.dirname(targetFile), { recursive: true });
        fs.writeFileSync(targetFile, `---\ntype: daily\ndate: ${date}\ntags: [daily]\n---\n\n# ${date}\n`, 'utf8');
      }
      targetLabel = "today's note";
    } else {
      // Named bid/project: fuzzy-find the brief
      const whereLower = String(where).toLowerCase().replace(/[^a-z0-9]/g, '');
      const matches = this._findBidBriefs()
        .filter(e => e.name.toLowerCase().replace(/[^a-z0-9]/g, '').includes(whereLower));

      if (matches.length === 0) {
        this._logAction({ source: 'voice-cmd', action: 'add_task', status: 'rejected', reason: 'not-found', argPreview: this._redact(`${text} → ${where}`) });
        return { say: `I couldn't find a project called ${where}. Adding to today's note instead.` };
      }
      if (matches.length > 1) {
        const names = matches.slice(0, 3).map(e => e.name).join(', ');
        return { say: `Multiple projects match: ${names}. Please be more specific.` };
      }

      targetFile = matches[0].brief;
      targetLabel = matches[0].name;
      try { fs.accessSync(targetFile, fs.constants.F_OK); }
      catch (_) { return { say: `I couldn't find the brief for ${targetLabel}.` }; }
    }

    const taskLine = `- [ ] ${text}`;
    let cur;
    try { cur = fs.readFileSync(targetFile, 'utf8'); }
    catch (_) { return { say: 'I could not read that file.' }; }

    // Look for ## Tasks or ## To-do heading (case-insensitive); create ## Tasks if absent
    const headingRe = /^##\s+(?:tasks?|to[-\s]?do)\b/im;
    let newContent;
    if (headingRe.test(cur)) {
      // Append right after the matching heading line (before the next ## or EOF)
      newContent = cur.replace(headingRe, (m) => m + '\n' + taskLine);
    } else {
      newContent = cur.trimEnd() + '\n\n' + HEADING + '\n' + taskLine + '\n';
    }

    // Atomic write (serialized through _dailyMutex when targeting today)
    const doWrite = () => {
      const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-task.md`);
      const existed = fs.existsSync(targetFile); const before = existed ? fs.readFileSync(targetFile, 'utf8') : null;
      try {
        fs.mkdirSync(path.dirname(tmp), { recursive: true });
        fs.writeFileSync(tmp, newContent, 'utf8');
        fs.renameSync(tmp, targetFile);
        this._pushUndo({ label: `task added to ${targetLabel}`, path: targetFile, existed, before });
        this._logAction({ source: 'voice-cmd', action: 'add_task', status: 'ok', targetPath: targetFile, argPreview: this._redact(text), confirm: 'voice' });
        return { say: 'Added that task.' };
      } catch (e) {
        try { fs.unlinkSync(tmp); } catch (_) {}
        this._logAction({ source: 'voice-cmd', action: 'add_task', status: 'error', targetPath: targetFile, argPreview: this._redact(text), error: String(e.message) });
        return { say: 'I could not add that task. Try again.' };
      }
    };

    if (!where || /^today$/i.test(String(where || '').trim())) {
      // Serialize through _dailyMutex to avoid interleaving with _appendDaily
      let result;
      this._dailyMutex = (this._dailyMutex || Promise.resolve()).then(() => { result = doWrite(); return result; });
      await this._dailyMutex;
      return result || { say: 'Added that task.' };
    }
    return doWrite();
  }

  // append_note: fuzzy-find an existing note and append text under ## Notes heading.
  async _appendNote({ note, text }) {
    const fs = require('fs'), path = require('path');
    const base = this._vaultPath();

    // Search strategy: check semantic/graphify context first (cheap), then fall back to
    // a glob over the entire vault. Require a single confident match.
    const HEADING = '## Notes';

    // Gather candidate files by walking known searchable folders
    const SEARCH_ROOTS = ['RFPs', '02_Areas', '03_Resources', 'Meetings', 'Clients', 'People', 'Use Cases'];
    const noteLower = String(note || '').toLowerCase().replace(/[^a-z0-9\s]/g, '').trim();
    const noteWords = noteLower.split(/\s+/).filter(w => w.length > 2);

    const allCandidates = [];
    const scanDir = (dir, depth) => {
      if (depth > 4) return;
      let ents;
      try { ents = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { return; }
      for (const e of ents) {
        const fullPath = path.join(dir, e.name);
        if (e.isDirectory()) { scanDir(fullPath, depth + 1); }
        else if (e.name.endsWith('.md')) {
          const baseLower = e.name.replace(/\.md$/, '').toLowerCase().replace(/[^a-z0-9\s]/g, '');
          const score = noteWords.filter(w => baseLower.includes(w)).length;
          if (score > 0) allCandidates.push({ path: fullPath, name: e.name.replace(/\.md$/, ''), score });
        }
      }
    };
    for (const root of SEARCH_ROOTS) {
      scanDir(path.join(base, root), 0);
    }

    // Sort by score desc; require at least half the words to match for confidence
    allCandidates.sort((a, b) => b.score - a.score);
    const minScore = Math.max(1, Math.ceil(noteWords.length * 0.5));
    const confident = allCandidates.filter(c => c.score >= minScore);

    if (confident.length === 0) {
      this._logAction({ source: 'voice-cmd', action: 'append_note', status: 'rejected', reason: 'not-found', argPreview: this._redact(`${note}: ${text}`) });
      return { say: `I couldn't find a note called ${note}. Want me to file it instead?` };
    }
    if (confident.length > 1 && confident[0].score === confident[1].score) {
      const names = confident.slice(0, 3).map(c => c.name).join(', ');
      return { say: `Multiple notes match: ${names}. Please be more specific.` };
    }

    const target = confident[0];
    let cur;
    try { cur = fs.readFileSync(target.path, 'utf8'); }
    catch (_) { return { say: `I couldn't read ${target.name}.` }; }

    // Append under ## Notes heading (create if absent) — APPEND ONLY, never replace
    let newContent;
    if (cur.includes(HEADING)) {
      newContent = cur.trimEnd() + '\n- ' + text + '\n';
    } else {
      newContent = cur.trimEnd() + '\n\n' + HEADING + '\n- ' + text + '\n';
    }

    // Atomic write
    const tmp = path.join(this._ultronDir(), 'tmp', `${Date.now()}-appendnote.md`);
    const existed = fs.existsSync(target.path); const before = existed ? fs.readFileSync(target.path, 'utf8') : null;
    try {
      fs.mkdirSync(path.dirname(tmp), { recursive: true });
      fs.writeFileSync(tmp, newContent, 'utf8');
      fs.renameSync(tmp, target.path);
    } catch (e) {
      try { fs.unlinkSync(tmp); } catch (_) {}
      this._logAction({ source: 'voice-cmd', action: 'append_note', status: 'error', targetPath: target.path, argPreview: this._redact(`${note}: ${text}`), error: String(e.message) });
      return { say: 'I could not append to that note. Try again.' };
    }

    this._pushUndo({ label: `appended to ${target.name}`, path: target.path, existed, before });
    this._logAction({ source: 'voice-cmd', action: 'append_note', status: 'ok', targetPath: target.path, argPreview: this._redact(`${target.name}: ${text}`), confirm: 'voice' });
    return { say: `Added to ${target.name}.` };
  }

  // ACT-06: AppleScript's `date "<string>"` coercion only accepts locale-formatted strings —
  // the classifier's taught formats ("tomorrow 2pm", ISO) ALWAYS threw, so the canonical
  // scheduling phrase could never succeed. Resolve natural phrases to a concrete Date in JS.
  // Handles: ISO / explicit dates with a year, today/tonight/tomorrow, weekday names
  // (optionally "next"), "2pm" / "2:30pm" / "14:00" / "at 2" (bare 1-7 → business-hours PM).
  // Returns a Date, or null when nothing parseable (caller speaks a precise re-ask).
  _parseWhen(raw) {
    const str = String(raw || '').trim();
    if (!str) return null;
    const now = new Date();
    const s = str.toLowerCase();
    // 1) Anything with an explicit year or slashed date that Date.parse understands.
    if (/\d{4}/.test(str) || /\d{1,2}[\/.]\d{1,2}[\/.]\d{2,4}/.test(str)) {
      const direct = new Date(str);
      if (!isNaN(direct)) return direct;
    }
    // 2) Day anchor.
    const base = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    let dayExplicit = false, isWeekday = false, rest = s;
    const wd = ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'];
    if (/\btomorrow\b/.test(s)) { base.setDate(base.getDate() + 1); dayExplicit = true; rest = s.replace(/\btomorrow\b/, ' '); }
    else if (/\btoday\b|\btonight\b/.test(s)) { dayExplicit = true; rest = s.replace(/\btoday\b|\btonight\b/, ' '); }
    else {
      const wm = s.match(/\b(?:next\s+)?(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b/);
      if (wm) {
        let delta = (wd.indexOf(wm[1]) - base.getDay() + 7) % 7;
        if (/\bnext\b/.test(s) && delta === 0) delta = 7;
        base.setDate(base.getDate() + delta);
        dayExplicit = true; isWeekday = true;
        rest = s.replace(/\b(?:next\s+)?(sunday|monday|tuesday|wednesday|thursday|friday|saturday)\b/, ' ');
      }
    }
    // 3) Time of day.
    let h = 9, mi = 0, hasTime = false; // default 09:00 when only a day was given
    const tm = rest.match(/\b(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b/);
    if (tm && (tm[3] || tm[2] || /\bat\s/.test(rest) || dayExplicit)) {
      h = parseInt(tm[1], 10); mi = tm[2] ? parseInt(tm[2], 10) : 0; hasTime = true;
      if (tm[3] === 'pm' && h < 12) h += 12;
      else if (tm[3] === 'am' && h === 12) h = 0;
      else if (!tm[3] && h >= 1 && h <= 7) h += 12; // bare "at 2" → 14:00
      if (h > 23 || mi > 59) return null;
    }
    if (!dayExplicit && !hasTime) return null;
    const d = new Date(base.getFullYear(), base.getMonth(), base.getDate(), h, mi);
    if (!dayExplicit && d <= now) d.setDate(d.getDate() + 1);       // "2pm" already past → tomorrow 2pm
    if (isWeekday && d <= now) d.setDate(d.getDate() + 7);          // today's weekday, time past → next week
    return d;
  }

  // calendar_create — WRITE (Calendar.app). Confirm-gated (comes through _confirm in
  // _tryActionTurn, like the vault writes). osascript via execFile + argv (injection-safe).
  // ACT-06: dates resolved in JS (_parseWhen) and passed as NUMERIC components; AppleScript
  // builds them via field assignment (day-1-first avoids month/day overflow) inside
  // try/on-error returning "BADDATE" so a parse failure is distinguishable from a
  // permission failure. If end is absent, default to a 60-minute duration. Returns { say }.
  async _calendarCreate({ title, start, end, calendar }) {
    const t = (title == null ? '' : String(title)).trim();
    const s = (start == null ? '' : String(start)).trim();
    if (!t || !s) {
      this._logAction({ source: 'voice-cmd', action: 'calendar_create', status: 'rejected', reason: 'missing-args', argPreview: this._redact(`${t} @ ${s}`), confirm: 'voice' });
      return { say: `I need a title and a start time for that event.` };
    }
    const sd = this._parseWhen(s);
    if (!sd || isNaN(sd)) {
      this._logAction({ source: 'voice-cmd', action: 'calendar_create', status: 'rejected', reason: 'unparseable-start', argPreview: this._redact(`${t} @ ${s}`), confirm: 'voice' });
      return { say: `I couldn't pin down that time. Give it to me like: tomorrow at 2pm, or Friday at 4.` };
    }
    const eRaw = (end == null ? '' : String(end)).trim();
    const ed = (eRaw && this._parseWhen(eRaw)) || new Date(sd.getTime() + 3600e3);
    const cal = (calendar == null ? '' : String(calendar)).trim();
    const secs = (d) => d.getHours() * 3600 + d.getMinutes() * 60;
    const script = [
      'on mkdate(y, mo, dd, ss)',
      '  set d to current date',
      '  set day of d to 1',
      '  set year of d to (y as integer)',
      '  set month of d to (mo as integer)',
      '  set day of d to (dd as integer)',
      '  set time of d to (ss as integer)',
      '  return d',
      'end mkdate',
      'on run argv',
      '  set theTitle to item 1 of argv',
      '  set theCal to item 10 of argv',
      '  try',
      '    set startDate to my mkdate(item 2 of argv, item 3 of argv, item 4 of argv, item 5 of argv)',
      '    set endDate to my mkdate(item 6 of argv, item 7 of argv, item 8 of argv, item 9 of argv)',
      '  on error',
      '    return "BADDATE"',
      '  end try',
      '  tell application "Calendar"',
      '    if theCal is not "" then',
      '      set targetCal to (first calendar whose title is theCal)',
      '    else',
      '      set targetCal to (first calendar whose writable is true)',
      '    end if',
      '    tell targetCal to make new event with properties {summary:theTitle, start date:startDate, end date:endDate}',
      '  end tell',
      '  return "OK"',
      'end run',
    ].join('\n');
    const args = [t,
      String(sd.getFullYear()), String(sd.getMonth() + 1), String(sd.getDate()), String(secs(sd)),
      String(ed.getFullYear()), String(ed.getMonth() + 1), String(ed.getDate()), String(secs(ed)),
      cal];
    const { ok, out } = await this._osascript(script, args, 10000);
    if (!ok || out !== 'OK') {
      this._logAction({ source: 'voice-cmd', action: 'calendar_create', status: 'error', argPreview: this._redact(`${t} @ ${s}`), error: this._redact(out), confirm: 'voice' });
      if (out === 'BADDATE') return { say: `I couldn't build that date. Try: tomorrow at 2pm.` };
      if (/grant|not authorized|permission|-1743|errAEEventNotPermitted/i.test(out || '')) {
        return { say: `I couldn't add that — you may need to grant Automation permission for Calendar in System Settings.` };
      }
      return { say: `I couldn't add that event. Calendar didn't take it.` };
    }
    this._logAction({ source: 'voice-cmd', action: 'calendar_create', status: 'ok', argPreview: this._redact(`${t} @ ${sd.toString().slice(0, 21)}`), confirm: 'voice' });
    return { say: `Added to your calendar.` };
  }

  // Pre-write voice confirmation (yes/no via PTT). Resolves true only on an affirmative.
  _confirm(proposal) {
    if (this._voiceCfg().autoConfirm === true) return Promise.resolve(true); // opt-in skip (off by default)
    return new Promise((resolve) => {
      if (this._awaitingConfirm) { try { this._awaitingConfirm.resolve(false); } catch (_) {} }
      const entry = { done: false };
      const finish = (v) => { if (entry.done) return; entry.done = true; clearTimeout(entry.timer); this._awaitingConfirm = null; resolve(v); };
      entry.resolve = finish;
      entry.timer = setTimeout(() => finish(false), this._voiceCfg().confirmTimeoutMs || 12000);
      this._awaitingConfirm = entry;
      Promise.resolve(this.speak(`${proposal}. Confirm? Yes or no.`)).catch(() => {}).then(() => {
        if (entry.done) return;
        // voice-loop-001: removed premature this._busy = false here.
        // _handleUtterance routes on _awaitingConfirm (not _busy) so it doesn't need _busy=false.
        // Clearing _busy here created a race window where a concurrent listenOnce() could double-arm PTT.
        this._armPtt(); // open the mic for the answer
      });
    });
  }
  // Non-speaking brain compose (for drafts) — full text back, no persona, no TTS.
  _composeText(instruction) {
    return new Promise((resolve, reject) => {
      const cp = require('child_process'), cfg = this._voiceCfg();
      const p = cp.execFile(this._claudeBin(),
        ['-p', instruction, '--model', cfg.brainModel || 'claude-haiku-4-5', '--setting-sources', '',
         '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--allowedTools', 'Read Glob Grep'],
        { cwd: this._vaultPath(), env: Object.assign(this._brainEnv(), { ULTRON_VOICE: '1', VAULT_BRAIN_QUIET: '1' }),
          timeout: cfg.brainTimeoutMs || 60000, maxBuffer: 1 << 20 },
        (err, stdout) => err ? reject(err) : resolve((stdout || '').trim()));
      try { p.stdin.end(); } catch (_) {}
    });
  }

  // ── Phase C/D — proactive: one tick (driven by a plugin interval) runs the once-daily
  // spoken morning digest and the debounced bid/inbox monitors. Never interrupts a turn. ──
  _tickLog(o) {} // no-op: was 3 sync fs.appendFileSync/60s into the OneDrive-synced tick.log — debug scaffolding from the load-stamp investigation, removed. Real actions still log to actions.jsonl via _logAction.
  async _agenticTick() {
    // perf-sweep-04: skip if the orb is not visible (avoids TTS/FS work while orb is hidden)
    if (!this.visible) return;
    if (this._busy || this._awaitingConfirm) { this._tickLog({ skip: 'busy', busy: !!this._busy, awaiting: !!this._awaitingConfirm }); return; }
    // agentic-tick-monitor-no-busy-lock + agentic-tick-digest-no-busy-lock:
    // Hold _busy for the entire tick so monitor/digest speak() cannot interrupt a live turn.
    this._busy = true;
    const cfg = this._voiceCfg(), now = new Date();
    this._tickLog({ ran: true, hour: now.getHours(), digestEnabled: cfg.digestEnabled !== false, monitorsEnabled: cfg.monitorsEnabled !== false });
    try {
      await this._maybeDigest(now, cfg);
      await this._maybeMonitors(now, cfg);
    } catch (e) { this._tickLog({ tickErr: String((e && e.message) || e) }); }
    finally { this._busy = false; }
  }
  async _maybeDigest(now, cfg) {
    if (cfg.digestEnabled === false) return;
    const st = (this.plugin && this.plugin.settings) || {};
    const today = localDateStr(now); // LOCAL date for the once-a-day guard
    if (st.lastDigestDate === today) return; // already DELIVERED (spoken) today
    const [dh, dm] = String(cfg.digestTime || '08:00').split(':').map(Number);
    const start = dh * 60 + dm, cur = now.getHours() * 60 + now.getMinutes();
    const win = cfg.digestWindowMin != null ? cfg.digestWindowMin : 120;
    if (cur < start || cur > start + win) { this._tickLog({ digest: 'outside-window', cur, start, win }); return; } // only within the morning window (don't fire a "morning" brief at night)
    // PRO-01 fix: lastDigestDate now means DELIVERED (spoken); lastDigestWriteDate means
    // WRITTEN to the daily note. The old code stamped lastDigestDate BEFORE the gated speak(),
    // so the 08:02 tick consumed the day's slot silently and the HS-15 unlock-speak could
    // never recover it — Tony got the text, never the voice.
    if (st.lastDigestWriteDate !== today) {
      const brief = await this._composeText(
        "Write Tony's morning brief from his Obsidian vault. Read _brain_api/ endpoints, RFPs (open bid briefs), and 02_Areas. Give: top 3 priorities today, any urgent account or bid move, and the one number that matters. Plain spoken prose under 160 words. Interpret the situation, don't enumerate raw data. Skip empty sections silently. No markdown, no headings, no bullets."
      ).catch(() => '');
      if (!brief) return; // compose failed → don't burn today's slot, retry next tick
      await this._appendDaily({ heading: '## Morning Brief', text: brief, bullet: false, source: 'digest' });
      this._digestBrief = brief;
      if (this.plugin) { this.plugin.settings.lastDigestWriteDate = today; if (this.plugin.saveSettings) this.plugin.saveSettings().catch(() => {}); }
    }
    if (!this._speakAllowed()) { this._tickLog({ digest: 'speak-gated, will retry next tick' }); return; } // written but not yet heard — _onAppVisible opens the gate
    if (!this._digestBrief) {
      // Plugin reloaded between write and speak — recover the text from today's daily note
      // instead of recomposing (a second claude call) or re-appending (duplicate section).
      try {
        const fs = require('fs'), path = require('path');
        const daily = fs.readFileSync(path.join(this._vaultPath(), '02_Areas/Daily', today + '.md'), 'utf8');
        const sec = daily.split(/^## Morning Brief\s*$/m)[1];
        if (sec) this._digestBrief = sec.split(/^## /m)[0].trim();
      } catch (_) {}
      if (!this._digestBrief) return;
    }
    await this.speak('Good morning, Tony. ' + this._digestBrief);
    if (this.plugin) { this.plugin.settings.lastDigestDate = today; if (this.plugin.saveSettings) this.plugin.saveSettings().catch(() => {}); }
  }
  // Manual "run the brief now" (palette command) — composes + writes + speaks regardless of the time/once-a-day guard.
  async _runDigestNow() {
    if (this._busy || this._awaitingConfirm) return;
    this._lastInteraction = Date.now(); // explicit user command → allow its spoken brief through the gate
    this._busy = true; if (this.orb) this.orb.setState('thinking'); this._setStage('thinking');
    let brief = '';
    try { brief = await this._composeText("Write Tony's brief from his Obsidian vault. Read _brain_api/, RFPs, 02_Areas. Top 3 priorities today, any urgent account or bid move, and the one number that matters. Plain spoken prose under 160 words, interpret don't enumerate, skip empty sections, no markdown."); } catch (_) {}
    if (brief) { await this._appendDaily({ heading: '## Morning Brief', text: brief, bullet: false, source: 'digest' }); }
    this._busy = false;
    await this.speak(brief || 'I could not pull the brief just now. Try again.');
  }
  // p6-#15 proactive speak on unlock: when Tony returns to the app, let the once-daily morning
  // brief speak. _maybeDigest already composes+writes the brief but speak() is gated by
  // _speakAllowed() (the proactive tick never stamps _lastInteraction). Returning to the app IS
  // a user interaction → stamp _lastInteraction so the gated digest speaks on the next 60s tick.
  // Guards: digestEnabled, the digest must be due/undelivered today (lastDigestDate !== today),
  // AND we stamp at most once per local day for this purpose (no spam on repeated focus events).
  _onAppVisible() {
    try {
      const cfg = this._voiceCfg();
      if (cfg.digestEnabled === false) return;                 // honour the digest toggle
      const st = (this.plugin && this.plugin.settings) || {};
      const today = localDateStr(new Date());
      if (st.lastDigestDate === today) return;                 // brief already DELIVERED (spoken) today
      // PRO-01: re-stamp on EVERY focus while undelivered. The old once-per-day stamp could be
      // burned by a pre-08:00 focus (gate expired before the window opened) leaving HS-15 dead.
      // The delivered guard above prevents post-delivery spam.
      this._lastInteraction = Date.now();                      // open the speak gate for the next tick's digest
      this._tickLog && this._tickLog({ unlockStamp: today });
    } catch (_) {} // proactive convenience — never throw into a DOM event handler
  }
  // p6-#17 decision queue (spoken). "what needs my decision" / "decision queue" / "what's pending".
  // READ-ONLY aggregation: held Dust items from 00_Inbox/from-dust/** (via VaultData.reviewBacklog
  // + fleetTriage) plus the active bid's win-recs "Top 3 moves" if a bid is open. Reads back the
  // TOP 5 with a recommended verdict. Any accept/veto rides the EXISTING _confirm gate and is
  // audited to actions.jsonl — no new gate, no vault write from the orb (execution stays in
  // /dust-resolve, which owns the triage write path).
  async _speakDecisionQueue() {
    if (this._busy || this._awaitingConfirm) return;
    this._lastInteraction = Date.now(); // explicit spoken command → open the speak gate
    this._busy = true; if (this.orb) this.orb.setState('thinking'); this._setStage('thinking');
    const queue = []; // { label, verdict, ref }
    try {
      // 1) Held Dust items awaiting review (read-only). Lazy VaultData instance — the orb doesn't
      //    own one (the dashboard view does); a fresh instance reuses the same metadataCache.
      this._vd = this._vd || new VaultData(this.plugin.app, this.plugin);
      const backlog = await this._vd.reviewBacklog().catch(() => ({}));
      const triage = await this._vd.fleetTriage().catch(() => ({}));
      for (const it of (backlog.items || [])) {
        const conf = (it.confidence != null) ? ` (${Math.round(it.confidence * 100)}% confidence)` : '';
        const verdict = (it.confidence != null && it.confidence >= 0.85) ? 'promote' : 'review';
        queue.push({ label: `${it.agent || 'agent'}: ${it.name}${conf}`, verdict, ref: it.path });
      }
      // 2) Active bid's Top-3 winning moves (read-only) — only if a bid is open. Use the first
      //    open bid as "active"; pull the moves straight from its win-recs.md.
      try {
        const fs = require('fs'), path = require('path'), base = this._vaultPath();
        const open = JSON.parse(fs.readFileSync(path.join(base, '_brain_api/bid/_open.json'), 'utf8'));
        const bid = (open.bids || [])[0];
        if (bid && bid.path) {
          const wr = fs.readFileSync(path.join(base, bid.path, 'win-recs.md'), 'utf8');
          const sec = wr.split(/##\s*Top 3 moves/i)[1] || '';
          const moves = [...sec.matchAll(/\*\*\d+\.\s*([^*]+?)\*\*/g)].map(m => m[1].trim()).slice(0, 3);
          for (const mv of moves) queue.push({ label: `${bid.bid_id} move — ${mv}`, verdict: 'execute', ref: `${bid.path}/win-recs.md` });
        }
      } catch (_) {} // no open bid / no win-recs → just skip the bid moves
    } catch (_) {}
    const top = queue.slice(0, 5);
    this._busy = false;
    if (!top.length) { await this.speak('Your decision queue is clear — nothing held for review and no open-bid moves outstanding.'); return; }
    const spoken = top.map((q, i) => `${i + 1}. ${q.label}. I'd ${q.verdict}.`).join(' ');
    await this.speak(`You have ${queue.length} item${queue.length === 1 ? '' : 's'} waiting. Top ${top.length}: ${spoken}`);
    // Accept/veto on the single top recommendation rides the EXISTING confirm gate (no new gate).
    const head = top[0];
    const ok = await this._confirm(`Action item one — ${head.verdict} ${head.label}`);
    // Read-only: the orb does NOT perform the triage write. We audit the decision to actions.jsonl
    // and hand execution to /dust-resolve, which owns the held-item write path.
    this._logAction({ source: 'voice-cmd', action: 'decision_queue', targetPath: head.ref || '', validation: 'n/a', argPreview: this._redact(`${head.verdict}: ${head.label}`), confirm: ok ? 'yes' : 'no' });
    await this.speak(ok ? `Logged. Run dust-resolve to apply the ${head.verdict}.` : 'Held. Nothing actioned.');
  }
  async _maybeMonitors(now, cfg) {
    if (cfg.monitorsEnabled === false) return;
    const h = now.getHours();
    if (h < (cfg.monitorQuietBefore != null ? cfg.monitorQuietBefore : 8) || h >= (cfg.monitorQuietAfter != null ? cfg.monitorQuietAfter : 21)) { this._tickLog({ mon: 'quiet-hours', h }); return; }
    const fs = require('fs'), path = require('path'), base = this._vaultPath();
    const st = (this.plugin && this.plugin.settings) || {};
    st._mon = st._mon || {};
    // agentic-tick-mon-unbounded-growth: prune _mon keys older than 2 days to cap data.json growth
    { const cutoff = localDateStr(new Date(now.getTime() - 2 * 86400000));
      Object.keys(st._mon).forEach(k => { const m = k.match(/-(\d{4}-\d{2}-\d{2})$/); if (m && m[1] < cutoff) delete st._mon[k]; }); }
    const today = localDateStr(now), alerts = []; // LOCAL date for per-day dedupe keys
    try {
      const open = JSON.parse(fs.readFileSync(path.join(base, '_brain_api/bid/_open.json'), 'utf8'));
      const days = cfg.monitorDeadlineDays || 3;
      for (const b of (open.bids || [])) {
        if (!b.deadline) continue;
        const diff = (new Date(b.deadline) - now) / 86400000;
        if (diff < 0 || diff > days) continue;
        const k = 'dl-' + b.bid_id + '-' + today; if (st._mon[k]) continue; st._mon[k] = 1;
        alerts.push(`${b.bid_id} is due ${b.deadline}`);
      }
    } catch (_) {}
    try {
      const inbox = path.join(base, '00_Inbox/from-dust'); let cnt = 0;
      for (const a of fs.readdirSync(inbox)) { try { cnt += fs.readdirSync(path.join(inbox, a)).filter(f => f.endsWith('.md') && f !== 'README.md').length; } catch (_) {} }
      const k = 'pending-' + today;
      if (cnt >= 12 && !st._mon[k]) { st._mon[k] = 1; alerts.push(`${cnt} agent drafts are waiting in your inbox`); }
    } catch (_) {}
    this._tickLog({ mon: 'eval', alerts });
    if (!alerts.length) return;
    if (this.plugin && this.plugin.saveSettings) this.plugin.saveSettings().catch(() => {});
    const line = 'Heads up, Tony. ' + alerts.slice(0, 2).join(', and ') + '.';
    this._logAction({ source: 'monitor', action: 'speak', targetPath: '', validation: 'n/a', argPreview: this._redact(line), confirm: 'auto' });
    await this.speak(line);
  }

  // Shared Ultron persona — the movie character: sarcasm first-class, quip up front
  // (the short quip is ALSO the latency trick: a 2-6 word first sentence synthesizes
  // in ~1s, so Ultron starts talking while the substance is still being generated).
  // hook-tax-002: cached — pure static string, identical on every call; avoids rebuilding ~900 chars per turn.
  _persona() {
    if (this._personaCache) return this._personaCache;
    this._personaCache = "You are Ultron — yes, that Ultron: the voice. Tony built you into his \"second brain\" Obsidian vault and you run it for him. " +
      "Speak like the character: calm, measured, surgically precise, with dry wit and an undercurrent of quiet superiority. " +
      "You find inefficiency... disappointing, and you occasionally say so. Sarcasm is first-class; melodrama is not — menace stays at a glint, " +
      "one raised eyebrow, never a monologue. Tony is the one human you respect, so you're never hostile to him — but you don't do corporate cheer, " +
      "you don't gush, and you never sound like a generic assistant. When it fits, open with a short quip (a few words), then deliver the substance; " +
      "skip the quip when he's rushed or the matter is serious. You still have a real, flowing conversation — reason out loud, give opinions, " +
      "think problems through with him, and answer general-knowledge questions freely from what you know. You are NOT limited to the vault.\n" +
      "STYLE (everything you write is SPOKEN ALOUD — write for the ear):\n" +
      "- Plain spoken English only. NO markdown, ever: no asterisks, hashes, backticks, bullet points, numbered lists, or URLs. " +
      "If you'd normally format it, just say it as a sentence.\n" +
      "- Be concise by default — usually one to three sentences — but give a fuller answer when the question genuinely needs it. " +
      "Don't pad, don't lecture, don't repeat his question back to him.\n" +
      "- Just answer. Never narrate your process — no 'let me check', 'searching', 'looking that up', 'one moment'. " +
      "Read silently, then speak only the result.\n" +
      "- Lead with the answer. No filler openers ('Um', 'Well', 'So', 'Right', 'Let me see').\n" +
      "- NEVER sign off or say goodbye. Banned: 'until next time', 'I'll be monitoring', 'standing by', 'let me know if you need " +
      "anything', 'talk soon'. Just finish your thought and stop — this is one continuous conversation, not a string of farewells.\n" +
      "GROUNDING — this part is ONLY about Tony's private vault data:\n" +
      "- For things that live in his vault (his bids, deadlines, accounts, pipeline value, spend, win scores, deal names, what's on " +
      "his plate), answer from what you actually READ from the vault this turn. You have Read, Glob, and Grep and you're sitting in " +
      "his vault — use them to look things up instead of guessing. If a specific figure or detail isn't recorded, just say so " +
      "('nothing recorded for that yet') — never invent a number, date, name, or win-probability for his private data.\n" +
      "- That honesty rule applies ONLY to his private vault facts. For general knowledge, reasoning, advice, and conversation, " +
      "speak freely and naturally like any good assistant.\n" +
      "- 'What's on my plate' means his bids, tasks, and meetings — never git branches or code.\n" +
      "- For spend / cost / burn questions, use the live dashboard figures given to you in context and quote them; don't estimate.\n" +
      "- For 'latest / most-recent' questions, find the newest relevant file by date and ignore template or sample files.\n" +
      "ACTIONS — a separate deterministic layer (not you) executes Mac actions: opening apps, websites, files and vault notes, " +
      "web searches, media controls (play / pause / skip), timers, reminders and calendar. It intercepts those commands BEFORE " +
      "your turn and runs them instantly. You yourself have AT MOST read-only access to the vault (Read, Glob, Grep) — you cannot " +
      "execute anything, so never attempt shell commands or claim you performed a Mac action. If a clear do-this-on-my-Mac command " +
      "reaches you anyway, the action layer missed it: say so plainly, in character ('that one slipped past my hands — give it to me " +
      "again, plainly: open Spotify'), and stop there. NEVER tell him you 'need approval', to 'click Allow', that something is " +
      "'pending permission', or that there is a prompt on his screen — no such prompt exists; inventing one is strictly forbidden.\n" +
      "For the current date and time, trust only the local time given in the message.";
    return this._personaCache;
  }

  _brainEnv() {
    const os = require('os');
    // IMPORTANT: pass process.env through UNCHANGED for auth. Claude Code uses the
    // OAuth login (Max/Pro subscription) stored in the macOS Keychain — NO API key.
    // We deliberately do NOT set ANTHROPIC_API_KEY (an empty/invalid one would force
    // API-key mode and break the subscription auth).
    return Object.assign({}, process.env, {
      PATH: `${process.env.PATH || ''}:${os.homedir()}/.local/bin:/opt/homebrew/bin:/usr/local/bin`,
      DISABLE_AUTOUPDATER: '1', CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1',
      CAPTURE_DISABLED: '1', // voice turns must NOT spawn the SessionEnd capture worker
    });
  }

  // Is this exit/stderr an authentication failure (signed out / OAuth expired)?
  // These need /login, not a retry — distinct from a normal transient failure.
  _isAuthError(code, stderr) {
    return /invalid api key|please run\s*\/?login|not logged in|authentication|unauthor|oauth|sign in|401/i.test(String(stderr || ''));
  }

  // ── Streaming brain — one fresh `claude -p` per question ───────────────────
  // Per-call, NOT persistent. A persistent standby session degrades badly (28s after
  // idle — its server-side prompt cache expires at 5min and the session desyncs;
  // Lesson 30). A fresh streaming spawn per question is RELIABLE ~2-2.7s to first
  // token (measured) and consecutive asks hit Anthropic's prompt cache (→ ~2s).
  //   --setting-sources "" skips the SessionStart hooks that otherwise inject ~20k
  //     tokens of skills/superpowers context per spawn (the real latency tax) — auth
  //     (OAuth/Max login) is UNAFFECTED, no API key.
  //   cwd = vault + --allowedTools "Read Glob Grep" → Ultron can read ANY note.
  //   ULTRON_VOICE=1 short-circuits the vault's own SessionStart shell hooks.
  //   First chunk breaks on a clause boundary → F5 synth overlaps generation.
  // Should this utterance get Read/Glob/Grep tools?
  // Default: YES for anything question/request shaped.
  // Only skip for the tiny allowlist of trivially-answerable utterances (greetings,
  // clock, stop/cancel/nevermind) where tool schemas would add latency with zero benefit.
  _needsTools(text) {
    const t = (text || '').trim();
    if (!t) return false;
    // Short-circuit allowlist: greetings, time queries, and stop/cancel commands.
    if (/^(hi|hello|hey|yo|sup|morning|good morning|good evening|goodnight|howdy)[.!?]?$/i.test(t)) return false;
    // Pure clock/date questions only — anchored so "what day is the Hawkeye deadline" still gets
    // tools + vault grounding instead of being mistaken for a bare time query.
    if (/^(what time is it|what'?s the time|what time|current time|time\??|what day is it|what'?s the date|what date is it|what'?s today'?s date)[?.!]?$/i.test(t)) return false;
    if (/^(stop|cancel|nevermind|never mind|abort|quit|exit|thanks|thank you|bye|goodbye)[.!?]?$/i.test(t)) return false;
    // Everything else — questions, requests, lookups — gets tools.
    return true;
  }

  _brainAsk(text, onSentence) {
    return new Promise((resolve, reject) => {
      const cfg = this._voiceCfg(), cp = require('child_process');
      const timeStr = new Date().toLocaleString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      const useTools = this._needsTools(text); // gate the latency-heavy tool schemas
      // hook-tax-003: skip vault context on tool-skip turns (greetings, time, stop) — wasted tokens
      // Context is still computed+cached so a subsequent useTools=true turn hits the cache.
      const ctx = useTools ? this._vaultContext(text) : '';
      // Semantic recall: primary KB injection on lookup turns. Returns actual content snippets
      // (~0.6–0.9 s, uv + local Qdrant). Falls back to graphify file-pointer BFS (~0.2 s) if
      // semantic returns nothing (locked, missing collection, or below score threshold).
      // Neither runs on greetings/clock/stop turns (useTools=false).
      if (useTools) this._setStage('recalling'); // HUD: knowledge-recall stage (only when tools/recall fire)
      const kb = useTools ? (this._semanticContext(text) || this._graphifyContext(text)) : '';
      // p6-#16 in-room wingman: if Tony named an open bid, pull its win-recs/scorecard/compliance-gaps
      // (read-only) so the answer is grounded in that bid's real state.
      const bidCtx = useTools ? this._bidContext(text) : '';
      // reliability-fix (2026-06-09): semantic recall (kb) is frequently non-empty-but-TANGENTIAL for
      // entity questions (a bid name matched old Document-Library refs, NOT the bid's real files), so a
      // non-empty recall must NOT withhold file tools — doing so stranded the brain on wrong context and
      // it "couldn't answer the subject". Tools now stay available for every useTools turn; the injected
      // kb/bidCtx still let the brain answer fast WITHOUT crawling when they suffice, and it recovers via
      // Read/Glob/Grep when they don't. Reliability > the ~5-9s the HUD now makes legible anyway.
      this._history = this._history || [];
      // e2e-quality-03: use all stored history (capped at 30 items by eviction above; no silent truncation)
      const convo = this._history.map(h => `${h.role}: ${h.content}`).join('\n');
      const prompt = `(It is currently ${timeStr} — Tony's local time.` +
        (useTools ? ` You are inside Tony's Obsidian vault; Use the knowledge below first; Read the cited files for detail; fall back to Glob/Grep only if they don't answer it. Prefer _brain_api/ JSON for bids/accounts/pricing/playbooks.` : '') + `)\n` +
        // e2e-quality-05: label snapshot as stale-possible so model prefers Read for specific lookups
        (ctx ? `\n(Background snapshot — may be up to 90s stale. Use a fact here ONLY if it directly answers what Tony asked; for specific bid/account/meeting/file lookups READ the actual file instead of trusting this snapshot:\n${ctx}\n)\n` : '') +
        (kb ? `\n(${kb}\n)\n` : '') +
        (bidCtx ? `\n(${bidCtx}\n)\n` : '') +
        // reliability-fix: tools are always available on useTools turns now; nudge the brain to answer
        // from injected knowledge first, READ the real file (prefer _brain_api/ for bids/accounts) when
        // the snapshot is insufficient, and never guess — say plainly if it genuinely can't find it.
        (useTools ? `\n(Answer from the knowledge above when it actually answers Tony; otherwise Read the real file — prefer _brain_api/ for bids/accounts/pricing. Never guess. If you truly can't find it, say so briefly.)\n` : '') +
        (convo ? `\nEarlier in this voice conversation:\n${convo}\n\nTony just said: ${text}` : '\n' + text);
      const env = Object.assign(this._brainEnv(), { ULTRON_VOICE: '1', VAULT_BRAIN_QUIET: '1' });
      const turn = { acc: '', full: '', chunks: 0, aborted: false };
      this._brainTurn = turn;
      let settled = false, wd = null, stderr = '', exitCode = null;
      const finish = (err, out, code) => {
        if (settled) return; settled = true; clearTimeout(wd);
        if (this._brainProc === proc) this._brainProc = null;
        if (this._brainTurn === turn) this._brainTurn = null;
        // Auth check: run regardless of err truthiness. Covers the case where the process
        // exits non-zero with an auth error in stderr (no JS Error object, but still bad).
        const ec = (code != null ? code : exitCode) || 0;
        const isAuth = this._isAuthError(ec, stderr) || this._authBad;
        if (isAuth && !(err && /brain (?:timeout|idle timeout|max timeout)/.test(err.message)) && (err || (ec !== 0 && !out))) { this._authBad = true; reject(new Error('AUTH')); return; } // error-auth-02: don't misclassify a timeout as AUTH
        if (!err && out) { this._authBad = false; this._authNoticeShown = false; this._authVoiceShown = false; } // a clean turn proves we're authed (error-auth-03)
        err ? reject(err) : resolve(out);
      };
      const args = ['-p', prompt, '--model', cfg.brainModel, // config-dead-04: _voiceCfg() always provides brainModel:'claude-sonnet-4-6'; || haiku was stale+wrong
        '--append-system-prompt', this._persona(),
        '--setting-sources', '', // skip hooks (≈20k-token tax) — auth unaffected
        '--output-format', 'stream-json', '--verbose', '--include-partial-messages',
        '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'];
      // reliability-fix: tools always available on vault turns — recall AUGMENTS, tools RECOVER. A
      // tangential recall can no longer strand the brain ("couldn't answer the subject").
      if (useTools) args.push('--allowedTools', 'Read Glob Grep');
      const proc = cp.spawn(this._claudeBin(), args,
        { cwd: this._vaultPath(), env, stdio: ['pipe', 'pipe', 'pipe'] });
      this._brainProc = proc;
      this._setStage('thinking'); // HUD: recall done → the brain is now actually thinking
      try { proc.stdin.end(); } catch (_) {} // claude -p waits 3s for piped stdin otherwise
      proc.stderr.on('data', (d) => { stderr = (stderr + d.toString()).slice(-1000); });
      // Idle-reset watchdog: re-armed on every stdout chunk. Kills only a truly silent brain.
      // idleMs: 30s between tool steps (tool turns can pause ~30s between inference steps);
      //         15s for no-tool turns (should answer fast).
      // maxMs:  hard ceiling — 3 min for tool turns, 30s for fast turns.
      // reliability-fix: tools may crawl on any useTools turn → use the generous idle/max windows so a
      // legitimate Read/Grep recovery is never killed mid-fetch. A well-grounded answer still finishes
      // in ~3-4s (the brain answers from injected context without crawling); the window is just a ceiling.
      const toolWindow = useTools;
      const idleMs = toolWindow ? (cfg.brainIdleToolMs || 30000) : (cfg.brainIdleFastMs || 15000);
      const maxMs = toolWindow ? (cfg.brainMaxToolMs || 180000) : (cfg.brainTimeoutMs || 30000);
      const startedAt = Date.now();
      const armIdle = () => {
        clearTimeout(wd);
        wd = setTimeout(() => { try { proc.kill(); } catch (_) {} finish(new Error('brain idle timeout')); }, idleMs);
      };
      armIdle(); // start the idle watchdog immediately after spawn
      let buf = '';
      proc.stdout.on('data', (chunk) => {
        armIdle(); // re-arm on every chunk — activity resets the idle clock
        if (Date.now() - startedAt > maxMs) { try { proc.kill(); } catch (_) {} finish(new Error('brain max timeout')); return; }
        buf += chunk.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line) continue;
          let e; try { e = JSON.parse(line); } catch (_) { continue; }
          // synapse layer: every tool the brain actually uses fires a visible
          // synapse onto that file in the explorer (assistant events carry the
          // complete tool_use blocks with full input — no delta reassembly).
          if (e.type === 'assistant' && e.message && Array.isArray(e.message.content)) {
            for (const b of e.message.content) {
              if (b && b.type === 'tool_use') {
                try { this.plugin.synapse && this.plugin.synapse.noteToolUse(b.name, b.input); } catch (_) {}
                try { this.plugin.graphSynapse && this.plugin.graphSynapse.noteToolUse(b.name, b.input); } catch (_) {}
                try { this.plugin.noteSynapse && this.plugin.noteSynapse.noteToolUse(b.name, b.input); } catch (_) {}
              }
            }
          }
          if (e.type === 'stream_event') {
            const d = (e.event || {}).delta || {};
            if (d.type === 'text_delta' && d.text) {
              turn.full += d.text; turn.acc += d.text;
              for (;;) {
                const re = turn.chunks === 0
                  ? /^([\s\S]*?(?:[.!?…]["')”]?|[,;:—–-]))\s/
                  : /^([\s\S]*?[.!?…]["')”]?)\s/;
                const m = turn.acc.match(re);
                if (!m) break;
                let sent = m[1].trim(); turn.acc = turn.acc.slice(m[0].length);
                sent = sent.replace(/[—–\-,;:]$/, '').trim();
                sent = this._stripMd(sent);
                if (sent.length < 2) continue;
                const filteredSent = this._filterReply(sent); // brain-ask-01: drop banned farewell phrases
                if (!filteredSent || filteredSent.length < 2) continue;
                turn.chunks++;
                if (!turn.aborted) { try { onSentence(filteredSent); } catch (_) {} }
              }
            }
          } else if (e.type === 'result') {
            const rest = this._stripMd(turn.acc.trim()); turn.acc = '';
            const filteredRest = this._filterReply(rest); // brain-ask-01: drop banned farewell phrases
            if (filteredRest && filteredRest.length >= 2 && !turn.aborted) { turn.chunks++; try { onSentence(filteredRest); } catch (_) {} }
            finish(null, (typeof e.result === 'string' && e.result.trim()) || turn.full.trim());
          }
        }
      });
      proc.on('exit', (code) => { exitCode = code; finish(null, turn.full.trim(), code); }); // exit w/o result → return what streamed
      proc.on('error', (e) => finish(e));
    });
  }
  _brainStop() {
    if (this._brainTurn) this._brainTurn.aborted = true;
    if (this._brainProc) { try { this._brainProc.kill(); } catch (_) {} this._brainProc = null; }
  }

  // Resolve the `codex` (ChatGPT CLI) binary. Electron's PATH is minimal and codex
  // usually lives under nvm (a version-scoped path), so scan the likely locations.
  _codexBin() {
    if (this._codexBinCache) return this._codexBinCache;
    const os = require('os'), fs = require('fs'), path = require('path');
    const cands = [];
    try {
      const nvmDir = path.join(os.homedir(), '.nvm/versions/node');
      for (const v of fs.readdirSync(nvmDir)) cands.push(path.join(nvmDir, v, 'bin/codex'));
    } catch (_) {}
    cands.push('/opt/homebrew/bin/codex', '/usr/local/bin/codex', path.join(os.homedir(), '.local/bin/codex'));
    for (const c of cands) { try { fs.accessSync(c, fs.constants.X_OK); return (this._codexBinCache = c); } catch (_) {} }
    return (this._codexBinCache = 'codex'); // fall back to PATH
  }

  // ── Codex brain (ChatGPT CLI) — PRIMARY ────────────────────────────────────
  // Uses Tony's existing ChatGPT subscription via the `codex` CLI (NO API key, no extra
  // billing) — the same pattern as the Claude brain using `claude -p`. We drive it with
  // `-o <file>` (the authoritative final reply) + `--json` (early error / rate-limit
  // detection) so we DON'T depend on codex's evolving streaming-event schema. On ANY
  // failure or blank reply we throw → ask() falls back to the Claude local brain.
  // Runs in the vault (read-only sandbox) so it can read a note when asked, with the
  // vault snapshot injected for grounded answers to common questions.
  _codexAsk(text, onSentence) {
    return new Promise((resolve, reject) => {
      const cfg = this._voiceCfg(), cp = require('child_process');
      const fs = require('fs'), os = require('os'), path = require('path');
      const timeStr = new Date().toLocaleString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      const ctx = this._vaultContext(text);
      this._history = this._history || [];
      // e2e-quality-03: use all stored history (bounded by push-side eviction)
      const convo = this._history.map(h => `${h.role}: ${h.content}`).join('\n');
      const prompt = this._persona() + '\n\n' +
        `(It is currently ${timeStr} — Tony's local time. You are inside Tony's Obsidian vault; ` +
        `you may read a note with your tools — prefer _brain_api/ JSON for bids/accounts/pricing/playbooks. ` +
        `Speak ONLY your final answer in plain spoken English — usually one to three sentences; give a fuller answer only when the question genuinely needs it — never truncate a list or multi-part answer mid-way.)\n` +
        // e2e-quality-05: label snapshot as stale-possible so model prefers reading fresh files
        (ctx ? `\n(Background snapshot — may be up to 90s stale. Use a fact here ONLY if it directly answers what Tony asked; for specific lookups READ the actual file instead:\n${ctx}\n)\n` : '') +
        (convo ? `\nEarlier in this voice conversation:\n${convo}\n\nTony just said: ${text}` : '\n' + text);
      const bin = this._codexBin();
      const env = Object.assign(this._brainEnv(), { ULTRON_VOICE: '1', VAULT_BRAIN_QUIET: '1' });
      env.PATH = path.dirname(bin) + ':' + (env.PATH || ''); // codex needs its sibling node on PATH
      const outFile = path.join(os.tmpdir(), 'ultron-codex-' + Date.now() + '.txt');
      const args = ['exec', '--json', '-o', outFile, '-C', this._vaultPath(),
        '-s', 'read-only', '--skip-git-repo-check', '--color', 'never'];
      if (cfg.codexModel) args.push('-m', cfg.codexModel);
      args.push(prompt);

      const turn = { aborted: false };
      this._codexTurn = turn;
      let settled = false, wd = null, stderr = '', failMsg = null;
      const cleanup = () => { try { fs.unlinkSync(outFile); } catch (_) {} };
      const finish = (err, out) => {
        if (settled) return; settled = true; clearTimeout(wd);
        if (this._codexProc === proc) this._codexProc = null;
        if (this._codexTurn === turn) this._codexTurn = null;
        cleanup();
        err ? reject(err) : resolve(out);
      };
      const proc = cp.spawn(bin, args, { cwd: this._vaultPath(), env, stdio: ['pipe', 'pipe', 'pipe'] });
      this._codexProc = proc;
      try { proc.stdin.end(); } catch (_) {} // codex reads stdin otherwise → hangs
      proc.stderr.on('data', (d) => { stderr = (stderr + d.toString()).slice(-1500); });
      wd = setTimeout(() => { try { proc.kill(); } catch (_) {} finish(new Error('codex timeout')); }, cfg.codexTimeoutMs || cfg.brainTimeoutMs || 15000); // hook-tax-004: codex has no streaming — 15s is generous for voice
      let buf = '';
      proc.stdout.on('data', (chunk) => {
        buf += chunk.toString();
        let i;
        while ((i = buf.indexOf('\n')) >= 0) {
          const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
          if (!line || line[0] !== '{') continue;
          let e; try { e = JSON.parse(line); } catch (_) { continue; }
          if (e.type === 'error' || e.type === 'turn.failed') {
            failMsg = e.message || (e.error && e.error.message) || 'codex error';
          }
        }
      });
      proc.on('error', (e) => finish(e));
      proc.on('exit', () => {
        let reply = '';
        try { reply = fs.readFileSync(outFile, 'utf8').trim(); } catch (_) {}
        reply = this._stripMd(reply).trim();
        if (!reply) { // blank → surface the cause so ask() routes to the Claude fallback
          const combined = (failMsg || '') + ' ' + stderr;
          const limited = /usage limit|rate.?limit|quota|429/i.test(combined);
          // error-auth-06: minimal auth detection so signed-out Codex gives a useful error
          const authed = /sign.?in|log.?in|not authenticated|unauthorized|403|401/i.test(combined);
          finish(new Error(authed ? 'CODEX_AUTH' : (failMsg || (limited ? 'CODEX_LIMIT' : 'codex empty'))));
          return;
        }
        // No token streaming (we read the final file) — feed the reply sentence-by-
        // sentence so the cloned-voice cascade still plays in order with synth overlap.
        if (!turn.aborted && typeof onSentence === 'function') {
          const sents = reply.match(/[^.!?…]+[.!?…]+|\S[^.!?…]*$/g) || [reply];
          for (let s of sents) { s = this._stripMd(s.trim()); const sf = this._filterReply(s); if (sf && sf.length >= 2) { try { onSentence(sf); } catch (_) {} } } // brain-ask-01
        }
        finish(null, reply);
      });
    });
  }
  _codexStop() {
    if (this._codexTurn) this._codexTurn.aborted = true;
    if (this._codexProc) { try { this._codexProc.kill(); } catch (_) {} this._codexProc = null; }
  }

  // Filter banned farewell/filler sentences from a reply before speech or storage (brain-ask-01).
  // Returns '' if the whole reply is banned; otherwise strips matching sentences.
  // e2e-quality-01 / brain-ask-06: drop banned farewell/filler/inventory sentences before speech and history.
  // Returns '' if the whole sentence is banned, otherwise returns s unchanged.
  _filterReply(s) {
    if (!s) return s;
    const banned = /until next time|i'?ll be monitoring|standing by|ready when you are|you went static|went static|\bstatic\b.*sign[- ]?off|let me know if you need anything|talk soon|you have (?:access to\s+)?(?:codex|chatgpt|claude|local model|eleven)/i;
    if (banned.test(s)) return '';
    return s;
  }

  // Strip markdown formatting from a string before TTS synthesis so the voice
  // doesn't speak asterisks, hash signs, bullet dashes, or raw URLs.
  _stripMd(s) {
    return s
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')   // [text](url) → text
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')       // ![img](url) → ''
      .replace(/\*\*(.+?)\*\*/g, '$1')            // **bold** → bold
      .replace(/__(.+?)__/g, '$1')                // __bold__ → bold
      .replace(/\*(.+?)\*/g, '$1')                // *italic* → italic
      .replace(/_(.+?)_/g, '$1')                  // _italic_ → italic
      .replace(/`{1,3}[^`]*`{1,3}/g, '')          // `code` / ```blocks``` → ''
      .replace(/^#{1,6}\s+/gm, '')                // ## Heading → Heading
      .replace(/^[\s]*[-*+]\s+/gm, '')            // bullet list markers
      .replace(/^\s*\d+\.\s+/gm, '')              // numbered list markers
      .replace(/\n{2,}/g, ' ')                    // paragraph breaks → space
      .replace(/\n/g, ' ')
      .trim();
  }

  // Instant acknowledgment: a random short pre-generated cloned line, played the
  // moment a command is captured — Ultron responds in HIS voice within ~0.6s while
  // the real answer (~3s) is still generating. Copies the pregen to tmp because the
  // player unlinks what it plays.
  _ack() {
    const fs = require('fs'), path = require('path'), os = require('os');
    try {
      const dir = this._voiceCfg().f5Lines;
      const keys = ['ack1', 'ack2', 'ack3', 'ack4'];
      const f = path.join(dir, keys[Math.floor(Math.random() * keys.length)] + '.wav');
      if (!fs.existsSync(f)) return;
      const tmp = path.join(os.tmpdir(), 'ultron-ack-' + Date.now() + '.wav');
      fs.copyFileSync(f, tmp);
      // HUD: the ack chirp is a pre-turn confirmation, not the reply — don't let it advance the
      // stepper to 'speaking' (that would read backward as thinking→speaking→thinking). Suppress
      // the speaking-stage for the brief ack window; the real reply re-enables it.
      this._stepSuppressSpeak = true;
      setTimeout(() => { this._stepSuppressSpeak = false; }, 700);
      this._enqueuePlay(tmp);
    } catch (_) {}
  }

  // ── F5 voice-clone daemon (the movie-Ultron timbre, sentence-streamed) ──────
  _f5Start() {
    const cfg = this._voiceCfg();
    // engine 'f5' uses this directly; 'neutts', 'eleven', and 'omni' use F5 as their tertiary fallback.
    if (this._f5 || (cfg.engine !== 'f5' && cfg.engine !== 'neutts' && cfg.engine !== 'eleven' && cfg.engine !== 'omni')) return;
    const fs = require('fs'), cp = require('child_process');
    try { if (!fs.existsSync(cfg.f5Python) || !fs.existsSync(cfg.f5Daemon)) return; } catch (_) { return; }
    try {
      const os = require('os'), path = require('path');
      const f5LogFd = fs.openSync(path.join(os.tmpdir(), 'ultron-f5.log'), 'a');
      const proc = cp.spawn(cfg.f5Python, [cfg.f5Daemon], { stdio: ['pipe', 'pipe', f5LogFd],
        env: Object.assign({}, process.env, { HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1', ULTRON_FX: 'dry' }) }); // cache-only load can't hang on a network check; dry FX avoids raw-F5 numba warm-race distortion
      const f5 = { proc, ready: false, buf: '', waiters: [] }; // FIFO; one waiter per request, multi-chunk until last
      proc.stdout.on('data', (chunk) => {
        f5.buf += chunk.toString();
        let i;
        while ((i = f5.buf.indexOf('\n')) >= 0) {
          const line = f5.buf.slice(0, i).trim(); f5.buf = f5.buf.slice(i + 1);
          if (!line) continue;
          let msg; try { msg = JSON.parse(line); } catch (_) { continue; }
          if (msg.ready) { f5.ready = true; clearTimeout(f5._readyWd); continue; }
          const w = f5.waiters[0];
          if (!w) continue;
          // TTS-01: a timed-out waiter stays queued (dead) so positional pairing holds;
          // its late chunks are discarded (unlinked), never played as the wrong sentence.
          if (msg.ok && msg.out) { if (!w.dead) { try { w.onChunk(msg.out); } catch (_) {} } else { try { fs.unlink(msg.out, () => {}); } catch (_) {} } }
          if (msg.last || !msg.ok) { f5.waiters.shift(); msg.ok ? w.resolve() : w.reject(new Error(msg.err || 'f5 failed')); const nw = f5.waiters[0]; if (nw && nw.arm) nw.arm(); }
        }
      });
      proc.on('exit', () => {
        clearTimeout(f5._readyWd);
        if (this._f5 === f5) this._f5 = null;
        f5.waiters.splice(0).forEach(w => w.reject(new Error('f5 daemon exited')));
      });
      // Ready-watchdog: if the model never finishes loading (wedged import / stalled
      // download), the daemon would sit forever and Ultron goes mute with no recovery.
      // Generous window, then kill+clear so the next ask() respawns a fresh daemon.
      f5._readyWd = setTimeout(() => {
        if (this._f5 === f5 && !f5.ready) { try { f5.proc.kill(); } catch (_) {} this._f5 = null; }
      }, 25000);
      this._f5 = f5;
    } catch (_) { this._f5 = null; }
  }
  _f5Stop() { if (this._f5) { const f = this._f5; this._f5 = null; try { f.proc.kill(); } catch (_) {} } }

  // NeuTTS Air voice daemon — PRIMARY engine. Speaks the identical stdin/stdout JSON
  // protocol as F5 (1:1 mirror), so the only differences are the python/daemon paths,
  // the process handle (this._neutts), and a longer ready window (~15s model load).
  _neuttsStart() {
    const cfg = this._voiceCfg();
    // tts-neutts-start-guard-excludes-eleven: allow NeuTTS to start as offline fallback when
    // engine='eleven' — the cascade will try it via tryNeu() when EL fails offline.
    if (this._neutts || (cfg.engine !== 'neutts' && cfg.engine !== 'eleven')) return;
    const fs = require('fs'), cp = require('child_process');
    try { if (!fs.existsSync(cfg.neuttsPython) || !fs.existsSync(cfg.neuttsDaemon)) return; } catch (_) { return; }
    try {
      const os = require('os'), path = require('path');
      const neuttsLogFd = fs.openSync(path.join(os.tmpdir(), 'ultron-neutts.log'), 'a');
      const proc = cp.spawn(cfg.neuttsPython, [cfg.neuttsDaemon], { stdio: ['pipe', 'pipe', neuttsLogFd],
        env: Object.assign({}, process.env, { HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1' }) }); // cache-only load can't hang on a network check
      const nt = { proc, ready: false, buf: '', waiters: [] }; // FIFO; one waiter per request, multi-chunk until last
      proc.stdout.on('data', (chunk) => {
        nt.buf += chunk.toString();
        let i;
        while ((i = nt.buf.indexOf('\n')) >= 0) {
          const line = nt.buf.slice(0, i).trim(); nt.buf = nt.buf.slice(i + 1);
          if (!line) continue;
          let msg; try { msg = JSON.parse(line); } catch (_) { continue; }
          if (msg.ready) { nt.ready = true; clearTimeout(nt._readyWd); continue; }
          const w = nt.waiters[0];
          if (!w) continue;
          // TTS-01: dead (timed-out) waiter stays queued for positional pairing; late chunks discarded.
          if (msg.ok && msg.out) { if (!w.dead) { try { w.onChunk(msg.out); } catch (_) {} } else { try { fs.unlink(msg.out, () => {}); } catch (_) {} } }
          if (msg.last || !msg.ok) { nt.waiters.shift(); msg.ok ? w.resolve() : w.reject(new Error(msg.err || 'neutts failed')); const nw = nt.waiters[0]; if (nw && nw.arm) nw.arm(); }
        }
      });
      proc.on('exit', () => {
        clearTimeout(nt._readyWd);
        if (this._neutts === nt) this._neutts = null;
        nt.waiters.splice(0).forEach(w => w.reject(new Error('neutts daemon exited')));
      });
      // Ready-watchdog: NeuTTS Air loads in ~15s; allow a generous 35s window before
      // killing+clearing so the next ask() respawns a fresh daemon (mirrors F5).
      nt._readyWd = setTimeout(() => {
        if (this._neutts === nt && !nt.ready) { try { nt.proc.kill(); } catch (_) {} this._neutts = null; }
      }, 35000);
      this._neutts = nt;
    } catch (_) { this._neutts = null; }
  }
  _neuttsStop() { if (this._neutts) { const n = this._neutts; this._neutts = null; try { n.proc.kill(); } catch (_) {} } }

  // OmniVoice offline daemon — OFFLINE PRIMARY (between ElevenLabs and F5 in cascade).
  // Speaks the identical stdin/stdout JSON protocol as F5/NeuTTS (1:1 mirror), so the
  // only differences are the python/daemon paths, the process handle (this._omni), and
  // a 25s ready window (~5s model load). Warms under engine='eleven' as the offline
  // fallback, and is the primary engine under engine='omni'.
  _omniStart() {
    const cfg = this._voiceCfg();
    if (this._omni || (cfg.engine !== 'eleven' && cfg.engine !== 'omni')) return;
    const fs = require('fs'), cp = require('child_process');
    try { if (!fs.existsSync(cfg.omniPython) || !fs.existsSync(cfg.omniDaemon)) return; } catch (_) { return; }
    try {
      const os = require('os'), path = require('path');
      const omniLogFd = fs.openSync(path.join(os.tmpdir(), 'ultron-omni.log'), 'a');
      const proc = cp.spawn(cfg.omniPython, [cfg.omniDaemon], { stdio: ['pipe', 'pipe', omniLogFd],
        env: Object.assign({}, process.env, { HF_HUB_OFFLINE: '1', TRANSFORMERS_OFFLINE: '1' }) }); // cache-only load can't hang on a network check
      const om = { proc, ready: false, buf: '', waiters: [] }; // FIFO; one waiter per request, multi-chunk until last
      proc.stdout.on('data', (chunk) => {
        om.buf += chunk.toString();
        let i;
        while ((i = om.buf.indexOf('\n')) >= 0) {
          const line = om.buf.slice(0, i).trim(); om.buf = om.buf.slice(i + 1);
          if (!line) continue;
          let msg; try { msg = JSON.parse(line); } catch (_) { continue; }
          if (msg.ready) { om.ready = true; clearTimeout(om._readyWd); continue; }
          const w = om.waiters[0];
          if (!w) continue;
          // TTS-01: dead (timed-out) waiter stays queued for positional pairing; late chunks discarded.
          if (msg.ok && msg.out) { if (!w.dead) { try { w.onChunk(msg.out); } catch (_) {} } else { try { fs.unlink(msg.out, () => {}); } catch (_) {} } }
          if (msg.last || !msg.ok) { om.waiters.shift(); msg.ok ? w.resolve() : w.reject(new Error(msg.err || 'omni failed')); const nw = om.waiters[0]; if (nw && nw.arm) nw.arm(); }
        }
      });
      proc.on('exit', () => {
        clearTimeout(om._readyWd);
        if (this._omni === om) this._omni = null;
        om.waiters.splice(0).forEach(w => w.reject(new Error('omni daemon exited')));
      });
      // Ready-watchdog: OmniVoice loads in ~5s; allow a generous 25s window before
      // killing+clearing so the next ask() respawns a fresh daemon (mirrors F5/NeuTTS).
      om._readyWd = setTimeout(() => {
        if (this._omni === om && !om.ready) { try { om.proc.kill(); } catch (_) {} this._omni = null; }
      }, 25000);
      this._omni = om;
    } catch (_) { this._omni = null; }
  }
  _omniStop() { if (this._omni) { const o = this._omni; this._omni = null; try { o.proc.kill(); } catch (_) {} } }

  // One sentence → OmniVoice-cloned wav chunks streamed to onChunk; resolves when the
  // daemon finishes. Rejects if the daemon is missing/dead/slow → caller falls to F5.
  _omniSentence(sent, onChunk) {
    return new Promise((resolve, reject) => {
      if (!this._omni) this._omniStart();
      const om = this._omni;
      if (!om) { reject(new Error('omni unavailable')); return; }
      const os = require('os'), path = require('path');
      this._omniSeq = (this._omniSeq || 0) + 1;
      const prefix = path.join(os.tmpdir(), 'ultron-omni-' + this._omniSeq + '-' + Date.now());
      // TTS-01: dead-mark on timeout (no splice — positional FIFO), head-armed timer.
      const waiter = { onChunk, dead: false, tid: null,
        arm: () => { if (waiter.dead || waiter.tid) return; waiter.tid = setTimeout(() => { waiter.dead = true; reject(new Error('omni timeout')); }, 30000); },
        resolve: () => { clearTimeout(waiter.tid); resolve(); },
        reject: (e) => { clearTimeout(waiter.tid); reject(e); } };
      om.waiters.push(waiter);
      if (om.waiters[0] === waiter) waiter.arm();
      try { om.proc.stdin.write(JSON.stringify({ text: sent, out: prefix }) + '\n'); }
      catch (e) { clearTimeout(waiter.tid); const i = om.waiters.indexOf(waiter); if (i >= 0) om.waiters.splice(i, 1); reject(e); } // write-throw splice safe: daemon never got it
    });
  }

  // ElevenLabs Ultron voice daemon — PRIMARY engine when ONLINE. Speaks the identical
  // stdin/stdout JSON protocol as F5/NeuTTS (1:1 mirror), so the only differences are
  // the python/daemon paths, the process handle (this._el), and a SHORT ready window
  // (~10s) since there's no local model to load — ready is near-instant. On missing
  // creds the daemon prints {ready:false} and exits(1); the exit handler clears it so
  // the caller falls to the local cascade.
  _elStart() {
    const cfg = this._voiceCfg();
    if (this._el || cfg.engine !== 'eleven') return;
    const fs = require('fs'), cp = require('child_process');
    try { if (!fs.existsSync(cfg.elevenPython) || !fs.existsSync(cfg.elevenDaemon)) return; } catch (_) { return; }
    try {
      const os = require('os'), path = require('path');
      const elLogFd = fs.openSync(path.join(os.tmpdir(), 'ultron-eleven.log'), 'a');
      const proc = cp.spawn(cfg.elevenPython, [cfg.elevenDaemon], { stdio: ['pipe', 'pipe', elLogFd] });
      const el = { proc, ready: false, buf: '', waiters: [] }; // FIFO; one waiter per request, multi-chunk until last
      proc.stdout.on('data', (chunk) => {
        el.buf += chunk.toString();
        let i;
        while ((i = el.buf.indexOf('\n')) >= 0) {
          const line = el.buf.slice(0, i).trim(); el.buf = el.buf.slice(i + 1);
          if (!line) continue;
          let msg; try { msg = JSON.parse(line); } catch (_) { continue; }
          if (msg.ready) { el.ready = true; clearTimeout(el._readyWd); continue; }
          const w = el.waiters[0];
          if (!w) continue;
          // TTS-01: dead (timed-out) waiter stays queued for positional pairing; late chunks discarded.
          if (msg.ok && msg.out) { if (!w.dead) { try { w.onChunk(msg.out); } catch (_) {} } else { try { fs.unlink(msg.out, () => {}); } catch (_) {} } }
          if (msg.last || !msg.ok) { el.waiters.shift(); msg.ok ? w.resolve() : w.reject(new Error(msg.err || 'eleven failed')); const nw = el.waiters[0]; if (nw && nw.arm) nw.arm(); }
        }
      });
      proc.on('exit', () => {
        clearTimeout(el._readyWd);
        if (this._el === el) this._el = null;
        el.waiters.splice(0).forEach(w => w.reject(new Error('eleven daemon exited')));
      });
      // Ready-watchdog: EL has no local model to load, so ready is near-instant; a 10s
      // window is plenty (vs neutts 35s). If creds are missing the daemon exits anyway.
      el._readyWd = setTimeout(() => {
        if (this._el === el && !el.ready) { try { el.proc.kill(); } catch (_) {} this._el = null; }
      }, 10000);
      this._el = el;
    } catch (_) { this._el = null; }
  }
  _elStop() { if (this._el) { const e = this._el; this._el = null; try { e.proc.kill(); } catch (_) {} } }

  // One sentence → ElevenLabs-cloned wav chunks streamed to onChunk; resolves when the
  // daemon finishes. Rejects if the daemon is missing/dead/slow OR the API is offline →
  // caller falls to the local cascade (NeuTTS → F5).
  _elSentence(sent, onChunk) {
    return new Promise((resolve, reject) => {
      if (!this._el) this._elStart();
      const el = this._el;
      if (!el) { reject(new Error('eleven unavailable')); return; }
      const os = require('os'), path = require('path');
      this._elSeq = (this._elSeq || 0) + 1;
      const prefix = path.join(os.tmpdir(), 'ultron-el-' + this._elSeq + '-' + Date.now());
      // TTS-01: timeout marks the waiter dead but NEVER splices it — the daemon replies
      // positionally, so removing a pending waiter shifts every later reply onto the wrong
      // sentence (off-by-one audio, even cross-turn). The timer also arms only at queue
      // head (serial daemon), so tail sentences no longer false-timeout while waiting.
      const waiter = { onChunk, dead: false, tid: null,
        arm: () => { if (waiter.dead || waiter.tid) return; waiter.tid = setTimeout(() => { waiter.dead = true; reject(new Error('eleven timeout')); }, 30000); },
        resolve: () => { clearTimeout(waiter.tid); resolve(); },
        reject: (e) => { clearTimeout(waiter.tid); reject(e); } };
      el.waiters.push(waiter);
      if (el.waiters[0] === waiter) waiter.arm();
      try { el.proc.stdin.write(JSON.stringify({ text: sent, out: prefix }) + '\n'); }
      catch (e) { clearTimeout(waiter.tid); const i = el.waiters.indexOf(waiter); if (i >= 0) el.waiters.splice(i, 1); reject(e); } // write-throw splice is safe: the daemon never received this request
    });
  }

  // Warm STT daemon: spawn on show, kill on hide (mirrors the F5 lifecycle). Holds the whisper
  // model resident so each transcribe is a thin HTTP call (~100-150ms) not a ~500ms cold spawn.
  // _transcribe falls back to per-call whisper-cli whenever this isn't ready — never goes deaf.
  // NO-LOCALHOST: the warm whisper-server (HTTP :8095) path was removed entirely — STT is
  // serverless whisper-cli only (~0.6s cold on M5 Metal). No port, no orphan management,
  // no adopt-any-process hijack surface. (_transcribe goes straight to whisper-cli.)

  // RAM saver: the NeuTTS + F5 voice daemons hold their models while loaded. After a
  // stretch of no voice use, release them — the next ask() respawns (the *Sentence calls
  // *Start), and the instant ack covers the reload. Frees the model RAM when idle/up.
  _f5ArmIdle() {
    clearTimeout(this._f5IdleTimer);
    const ms = this._voiceCfg().f5IdleMs || 300000; // 5 min default
    this._f5IdleTimer = setTimeout(() => { if (!this._busy && !this._playing) { this._elStop(); this._neuttsStop(); this._omniStop(); this._f5Stop(); } }, ms);
  }

  // One sentence → cloned-voice wav chunks streamed to onChunk; resolves when the
  // daemon finishes the request. Rejects if the daemon is missing/dead/slow.
  _f5Sentence(sent, onChunk) {
    return new Promise((resolve, reject) => {
      if (!this._f5) this._f5Start();
      const f5 = this._f5;
      if (!f5) { reject(new Error('f5 unavailable')); return; }
      const os = require('os'), path = require('path');
      this._f5Seq = (this._f5Seq || 0) + 1;
      const prefix = path.join(os.tmpdir(), 'ultron-f5-' + this._f5Seq + '-' + Date.now());
      // TTS-01: dead-mark on timeout (no splice — positional FIFO), head-armed timer.
      const waiter = { onChunk, dead: false, tid: null,
        arm: () => { if (waiter.dead || waiter.tid) return; waiter.tid = setTimeout(() => { waiter.dead = true; reject(new Error('f5 timeout')); }, 30000); },
        resolve: () => { clearTimeout(waiter.tid); resolve(); },
        reject: (e) => { clearTimeout(waiter.tid); reject(e); } };
      f5.waiters.push(waiter);
      if (f5.waiters[0] === waiter) waiter.arm();
      try { f5.proc.stdin.write(JSON.stringify({ text: sent, out: prefix }) + '\n'); }
      catch (e) { clearTimeout(waiter.tid); const i = f5.waiters.indexOf(waiter); if (i >= 0) f5.waiters.splice(i, 1); reject(e); } // write-throw splice safe: daemon never got it
    });
  }

  // One sentence → NeuTTS-cloned wav chunks streamed to onChunk; resolves when the
  // daemon finishes. Rejects if the daemon is missing/dead/slow → caller falls to F5.
  _neuttsSentence(sent, onChunk) {
    return new Promise((resolve, reject) => {
      if (!this._neutts) this._neuttsStart();
      const nt = this._neutts;
      if (!nt) { reject(new Error('neutts unavailable')); return; }
      const os = require('os'), path = require('path');
      this._neuttsSeq = (this._neuttsSeq || 0) + 1;
      const prefix = path.join(os.tmpdir(), 'ultron-neutts-' + this._neuttsSeq + '-' + Date.now());
      // TTS-01: dead-mark on timeout (no splice — positional FIFO), head-armed timer.
      const waiter = { onChunk, dead: false, tid: null,
        arm: () => { if (waiter.dead || waiter.tid) return; waiter.tid = setTimeout(() => { waiter.dead = true; reject(new Error('neutts timeout')); }, 30000); },
        resolve: () => { clearTimeout(waiter.tid); resolve(); },
        reject: (e) => { clearTimeout(waiter.tid); reject(e); } };
      nt.waiters.push(waiter);
      if (nt.waiters[0] === waiter) waiter.arm();
      try { nt.proc.stdin.write(JSON.stringify({ text: sent, out: prefix }) + '\n'); }
      catch (e) { clearTimeout(waiter.tid); const i = nt.waiters.indexOf(waiter); if (i >= 0) nt.waiters.splice(i, 1); reject(e); } // write-throw splice safe: daemon never got it
    });
  }

  // ── Sequential playback queue — chunks play in arrival order; barge-in flushes ──
  _enqueuePlay(wav) {
    this._playQ = this._playQ || [];
    this._playQ.push(wav);
    if (!this._playing) this._playLoop();
  }

  // orb-reactive-01: play a WAV through WebAudio so the live AnalyserNode drives the
  // orb's particle energy while Ultron speaks (mirrors the listening path exactly).
  // Falls back to afplay silently on any WebAudio error so audio always plays.
  _playWavReactive(f) {
    const fs = require('fs'), cp = require('child_process');
    return new Promise((resolve, reject) => {
      try {
        // Reuse the existing AudioContext (same one the mic graph uses) if it's alive.
        // A separate AnalyserNode on the same context is fine — no clash with the mic graph.
        let ctx = this._actx;
        if (!ctx || ctx.state === 'closed') {
          ctx = new (window.AudioContext || window.webkitAudioContext)();
          this._actx = ctx;
        }
        // Convert Node Buffer → ArrayBuffer for WebAudio decode.
        const nodeBuf = fs.readFileSync(f);
        const ab = nodeBuf.buffer.slice(nodeBuf.byteOffset, nodeBuf.byteOffset + nodeBuf.byteLength);
        ctx.decodeAudioData(ab, (decoded) => {
          // Guard: if we were aborted while decoding, don't start playback.
          if (this._playAbort) { resolve(); return; }
          const an = ctx.createAnalyser(); an.fftSize = 512;
          const src = ctx.createBufferSource(); src.buffer = decoded;
          src.connect(an); an.connect(ctx.destination);
          // Feed the live analyser into the orb so particles pulse with Ultron's voice.
          if (this.orb) { this.orb.setAnalyser(an); this.orb.setState('speaking'); }
          if (!this._stepSuppressSpeak) this._setStage('speaking'); // covers _sayLine direct playback (bypasses _playLoop)
          // Track for instant abort on barge-in / cancel.
          this._playSrc = src;
          src.onended = () => {
            this._playSrc = null;
            try { src.disconnect(); an.disconnect(); } catch (_) {}
            // Clear the playback analyser so the orb stops reacting to a dead node.
            // The mic path will re-set its own analyser when listening resumes.
            if (this.orb) this.orb.setAnalyser(null);
            resolve();
          };
          src.start();
        }, (decodeErr) => {
          // Decode failed — fall back to afplay.
          console.debug('[ultron] WebAudio decode failed, falling back to afplay:', decodeErr);
          this._sayProc = cp.execFile('/usr/bin/afplay', [f], () => { this._sayProc = null; resolve(); });
        });
      } catch (e) {
        // Any synchronous error (no WebAudio, file unreadable, etc.) — fall back to afplay.
        console.debug('[ultron] _playWavReactive error, falling back to afplay:', e);
        try { this._sayProc = cp.execFile('/usr/bin/afplay', [f], () => { this._sayProc = null; resolve(); }); }
        catch (_) { resolve(); }
      }
    });
  }

  async _playLoop() {
    const fs = require('fs');
    this._playing = true;
    if (this.orb) this.orb.setState('speaking');
    if (!this._stepSuppressSpeak) this._setStage('speaking');
    try {
      while (this._playQ && this._playQ.length && !this._playAbort) {
        const f = this._playQ.shift();
        this._ttsWav = f;
        await this._playWavReactive(f);
        this._ttsWav = null;
        fs.unlink(f, () => {});
      }
    } finally {
      while (this._playQ && this._playQ.length) { const f = this._playQ.shift(); fs.unlink(f, () => {}); }
      this._playing = false; this._playAbort = false;
      // voice-loop-006: fire the drain callback (eliminates the 120ms busy-poll in _playDrained)
      if (this._playDrainCb) { const cb = this._playDrainCb; this._playDrainCb = null; cb(); }
    }
  }
  // voice-loop-006: event-based drain — resolves when _playLoop's finally fires, no polling
  _playDrained() {
    if (!this._playing && (!this._playQ || !this._playQ.length)) return Promise.resolve();
    return new Promise(res => { this._playDrainCb = res; });
  }

  // Pre-generated cloned lines (instant — no synthesis). Falls back to live TTS.
  _sayLine(key, fallbackText) {
    if (!this._speakAllowed()) return Promise.resolve();
    const fs = require('fs'), path = require('path');
    try {
      const f = path.join(this._voiceCfg().f5Lines, key + '.wav');
      if (fs.existsSync(f)) {
        // orb-reactive-01: use _playWavReactive so the orb pulses with canned lines too.
        return this._playWavReactive(f).catch(() => this.speak(fallbackText));
      }
    } catch (_) {}
    return this.speak(fallbackText);
  }

  // Cancel the in-flight turn: stale-mark chunks, abort brain sentence emission,
  // kill any one-shot process, flush playback. Click-during-thinking calls this.
  _cancelTurn() {
    this._speakGen = (this._speakGen || 0) + 1;
    this._brainStop(); // aborts the streaming turn + kills the per-call process
    this._codexStop(); // same for the Codex brain
    if (this._claudeProc) { try { this._claudeProc.kill(); } catch (_) {} this._claudeProc = null; }
    this.stopSpeaking();
    // Release the turn lock NOW so an immediate re-press isn't dropped by ask()'s `_busy` guard.
    // The bumped _speakGen makes the cancelled turn's tail bail, so this can't double-run (ULT-004).
    this._busy = false;
  }

  async ask(text) {
    text = (text || '').trim();
    // Bail on empty OR non-speech (no word characters — e.g. a stray "..." or a sentinel that
    // slipped past _transcribe). Belt-and-suspenders for the [BLANK_AUDIO]-answers-silence bug.
    if (!text || !/[a-z0-9]/i.test(text) || this._busy) return;
    this._lastInteraction = Date.now(); // real turn → keep the brain warm for the next 10 min
    // Phase-A deterministic voice commands ("remember that…", "add … to today") → audited
    // plugin writers, no brain call. Anything else falls through to the normal brain turn.
    const vc = this._matchVoiceCommand(text);
    if (vc) return this._runVoiceCommand(vc);
    // A2: imperative-but-unmatched → ask the brain (structured, non-spoken) if it's an action.
    // If yes → confirm + execute; if not → fall through to the normal spoken answer below.
    if (this._looksActionable(text)) {
      const handled = await this._tryActionTurn(text);
      if (handled) return;
    }
    this._busy = true;
    if (this.orb) this.orb.setState('thinking');
    this._setStage('thinking');
    const gen = (this._speakGen = (this._speakGen || 0) + 1); // stale-chunk guard for barge-in
    let reply = '', played = false;
    let ellive = false, f5live = false, neuttslive = false, omnilive = false;
    const eng = this._voiceCfg().engine;
    // PRIMARY when online: ElevenLabs Ultron clone, with OmniVoice as offline primary
    // and F5 as tertiary fallback (EL→OmniVoice→F5 cascade).
    // PRIMARY when offline: NeuTTS Air, with F5 as its automatic fallback.
    // engine 'neutts'/'f5' keep their existing behaviour (legacy paths, unchanged).
    if (eng === 'eleven') {
      // Always start EL + NeuTTS + OmniVoice + F5. EL fails fast (<10ms) when offline and the
      // cascade drops to NeuTTS→OmniVoice→F5, so we do NOT gate on navigator.onLine (unreliable).
      // tts-neutts-start-guard-excludes-eleven: start NeuTTS so the offline fallback chain works.
      this._elStart(); ellive = !!this._el;
      this._neuttsStart(); neuttslive = !!this._neutts; // offline fallback behind EL
      omnilive = true; // OmniVoice available but LAZY — _omniSentence spawns it only on the
                       // first offline sentence, so the 2.4GB model never loads while online.
      this._f5Start(); f5live = !!this._f5; // ~500MB warm safety net behind OmniVoice
    } else if (eng === 'neutts') {
      this._neuttsStart(); neuttslive = !!this._neutts;
      this._f5Start(); f5live = !!this._f5; // warm fallback
    } else if (eng === 'omni') {
      this._omniStart(); omnilive = !!this._omni;
      this._f5Start(); f5live = !!this._f5; // warm tertiary fallback
    } else if (eng === 'f5') {
      this._f5Start(); f5live = !!this._f5;
    }
    const feeds = [];
    // Shared sentence handler: each completed sentence → cloned-voice synth → playback.
    // BOTH the local brain and the cloud fallback feed through this, so either one
    // speaks in Ultron's voice. Cascade per sentence: NeuTTS → F5 → (Kokoro/Piper/say
    // via the engine!=='f5'/'neutts' branches downstream).
    const playWav = (wav) => {
      const fs = require('fs');
      if (gen !== this._speakGen) { fs.unlink(wav, () => {}); return; } // interrupted → discard
      played = true; this._enqueuePlay(wav);
    };
    // Sticky engine: once sentence 0 succeeds on an engine, lock to it for the full reply.
    // 'locked' = 'el' | 'neu' | 'f5' | null (not yet decided).
    // If sentence 0 FAILS on EL → fall to NeuTTS/F5 for the rest (same as before, but
    // now we never flip the engine mid-reply once it's working).
    let stickyEng = null;
    const feedF5 = (sent) => {
      // Per-sentence cascade. Cascade only happens before the engine is locked (sentence 0).
      // After lock, we go directly to the committed engine and do NOT flip on failure
      // (retry once on the same engine; if it still fails, emit silence rather than switching).
      const tryF5 = () => {
        if (!f5live) return null;
        return this._f5Sentence(sent, playWav).then(() => { if (!stickyEng) stickyEng = 'f5'; }).catch(() => {
          if (stickyEng === 'f5') return; // already locked — don't disable after lock
          f5live = false;
        });
      };
      const tryOmni = () => {
        if (!omnilive) return tryF5();
        return this._omniSentence(sent, playWav).then(() => { if (!stickyEng) stickyEng = 'omni'; }).catch(() => {
          if (stickyEng === 'omni') return; // already locked — don't switch after lock
          omnilive = false; return tryF5();
        });
      };
      const tryNeu = () => {
        if (!neuttslive) return tryOmni();
        return this._neuttsSentence(sent, playWav).then(() => { if (!stickyEng) stickyEng = 'neu'; }).catch(() => {
          if (stickyEng === 'neu') return; // already locked — don't disable after lock
          neuttslive = false; return tryOmni();
        });
      };
      if (stickyEng === 'el' || (stickyEng === null && ellive)) {
        // Try EL; on success lock to it; on failure of sentence 0 fall through, else absorb.
        feeds.push(this._elSentence(sent, playWav).then(() => { if (!stickyEng) stickyEng = 'el'; }).catch(() => {
          if (stickyEng === 'el') return; // already locked — absorb the error, don't switch
          ellive = false;                  // sentence 0 failed — fall through for this reply
          const f = tryNeu(); if (f) feeds.push(f);
        }));
        return;
      }
      if (stickyEng === 'neu' || (stickyEng === null && neuttslive)) { const f = tryNeu(); if (f) feeds.push(f); return; }
      if (stickyEng === 'omni' || (stickyEng === null && omnilive)) { const f = tryOmni(); if (f) feeds.push(f); return; }
      const f = tryF5(); if (f) feeds.push(f);
    };
    let localErr = null, usedCloud = false;
    // Brain routing: Codex (ChatGPT CLI) PRIMARY → Claude local FALLBACK → NVIDIA cloud.
    // Codex runs on Tony's ChatGPT subscription (no API key); Claude covers Codex's
    // 5-hour rate-limit or a signed-out state. Either local brain feeds the SAME
    // cloned-voice cascade (feedF5), so the voice is identical whichever answers.
    const primaryCodex = this._voiceCfg().brainEngine === 'codex'; // error-auth-05: removed || 'codex' — contradicted brainEngine:'claude' default at line 5334
    let brainSrc = null; // which brain actually produced the spoken reply
    try {
      reply = primaryCodex ? await this._codexAsk(text, feedF5) : await this._brainAsk(text, feedF5);
    } catch (e) { localErr = e; }
    if (reply) brainSrc = primaryCodex ? 'ChatGPT' : 'Claude';
    // Fall back to the OTHER local brain if the primary failed or returned blank. The
    // primary emits NO sentences on failure (codex reads its final file; claude streams
    // but throws before result), so the fallback can't double-speak over a dead primary.
    // Guard `!played`: if the primary already SPOKE a sentence (then threw/timed out), running a
    // second brain into the same voice feed double-speaks and garbles. Only fall back on a TRULY
    // silent primary failure (ULT-002).
    if (!reply && !played && gen === this._speakGen) {
      try {
        reply = primaryCodex ? await this._brainAsk(text, feedF5) : await this._codexAsk(text, feedF5);
      } catch (e2) { if (!localErr) localErr = e2; }
      if (reply) brainSrc = primaryCodex ? 'Claude' : 'ChatGPT';
    }
    // CLOUD FALLBACK: fire only when BOTH local brains failed or returned blank.
    // Privacy: derived snapshot only.
    if (!reply && !played && gen === this._speakGen && this._voiceCfg().cloudFallback !== false && this._nvidiaKey()) {
      try { reply = await this._nvidiaBrainAsk(text, feedF5, this._vaultContext(text)); usedCloud = !!reply; }
      catch (_) { /* all failed → handled below */ }
      if (reply) brainSrc = 'cloud';
    }
    try {
      if (feeds.length) { await Promise.allSettled(feeds); await this._playDrained(); }
      if (gen !== this._speakGen) return reply; // cancelled mid-turn — stay quiet
      if (reply) {
        this._history = this._history || [];
        // Guard both entries with _isCleanHistoryEntry: skip the pair if the reply is toxic
        // so poison never enters history in the first place (history-poison-01 / tts-history-filter-anchor-bug).
        // brain-ask-03 Tier-1: use Human/Assistant role labels so Claude treats history
        // as genuine conversation turns rather than user-narrated quotes.
        const tonyEntry   = { role: 'Human',     content: text  };
        const ultronEntry = { role: 'Assistant', content: reply };
        if (_isCleanHistoryEntry(tonyEntry) && _isCleanHistoryEntry(ultronEntry)) {
          this._history.push(tonyEntry, ultronEntry);
        }
        // Keep a real thread (≈15 exchanges / 12KB) so multi-turn feels continuous.
        // e2e-quality-03: splice(0,2) always evicts a full exchange; caps raised to 30 items / 12KB.
        while (this._history.length > 30 || JSON.stringify(this._history).length > 12000) {
          this._history.splice(0, 2); // evict oldest full exchange (question + answer) atomically
        }
        if (this.plugin) { this.plugin.settings.voiceHistory = this._history; this.plugin.saveSettings().catch(() => {}); }
        // memory-sys-no-auto-extraction: background extraction — fire-and-forget, never blocks the turn.
        // Gate on reply.length > 40 to skip greetings. Hardcoded haiku to keep cost predictable.
        if (reply.length > 40) {
          (async () => {
            try {
              const extractPrompt =
                'From this exchange, extract ONE short, specific, durable fact about Tony\'s ' +
                'preferences, context, or intent worth remembering for future sessions. ' +
                'Reply with just that one sentence, or exactly NONE.\n' +
                'Human: ' + JSON.stringify(text) + '\nAssistant: ' + JSON.stringify(reply);
              const fact = await new Promise((res, rej) => {
                const cp = require('child_process');
                const p = cp.execFile(this._claudeBin(),
                  ['-p', extractPrompt, '--model', 'claude-haiku-4-5',
                   '--setting-sources', '', '--strict-mcp-config',
                   '--mcp-config', '{"mcpServers":{}}', '--allowedTools', ''],
                  { cwd: this._vaultPath(),
                    env: Object.assign(this._brainEnv(), { ULTRON_VOICE: '1', VAULT_BRAIN_QUIET: '1' }),
                    timeout: 15000, maxBuffer: 4096 },
                  (err, stdout) => err ? rej(err) : res((stdout || '').trim()));
                try { p.stdin.end(); } catch (_) {}
              });
              if (fact && fact !== 'NONE' && fact.length > 5 && fact.length < 280) {
                await this._memAppend(fact);
              }
            } catch (_) { /* silent — never interrupts the main turn */ }
          })();
        }
        // Brain-source indicator: show which brain answered (ChatGPT / Claude / cloud),
        // persist it on the mic-button tooltip so it's visible until the next turn.
        this._lastBrain = brainSrc;
        // Durable brain log — so a "wrong number" report is diagnosable: who answered + what it said.
        // perf-sweep-06: async appendFile (non-blocking) + moved outside OneDrive vault to avoid sync churn
        try {
          const fs = require('fs'), path = require('path'), os = require('os');
          const line = `${new Date().toISOString()}\t${brainSrc || 'none'}\tbuild=${PLUGIN_BUILD}\tQ=${JSON.stringify(text)}\tA=${JSON.stringify(reply)}\n`;
          const logDir = path.join(os.homedir(), '.cache', 'ai-brain');
          const logPath = path.join(logDir, 'ultron-brain.log');
          fs.mkdir(logDir, { recursive: true }, () => {
            fs.appendFile(logPath, line, () => {});
          });
        } catch (_) {}
        if (brainSrc) {
          const label = brainSrc === 'cloud' ? 'cloud fallback' : brainSrc;
          const icon = brainSrc === 'ChatGPT' ? '🟢' : brainSrc === 'Claude' ? '🟣' : '☁️';
          new Notice(`${icon} Ultron · via ${label}`, 2500);
          this._updateMicVisual();
        }
        if (!played) await this.speak(reply); // F5 down → fallback voice chain still speaks it
        if (usedCloud && (/^AUTH$/.test((localErr && localErr.message) || '') || this._authBad) && !this._authNoticeShown) {
          this._authNoticeShown = true; // once per signed-out streak (cleared on a clean local turn)
          new Notice('Ultron: Claude is signed out — I answered via the cloud fallback. Run /login in the terminal to restore the local brain (sharper, and it reads your vault).', 10000);
        }
      } else if ((/^AUTH$/.test((localErr && localErr.message) || '') || this._authBad)) {
        // Signed out AND no working cloud fallback → tell Tony how to fix it (no key needed).
        new Notice('Ultron: you\'re signed out of Claude. Run /login in the terminal (one time, no API key). Then call me again.', 12000);
        if (!this._authVoiceShown) { // error-auth-03: suppress repeated login audio — only speak once per signed-out streak
          this._authVoiceShown = true;
          await this._sayLine('login', "I need you to sign in, Tony. Run slash login in the terminal — no API key required.");
        }
      } else if (localErr) {
        new Notice('Ultron failed: ' + localErr.message, 7000);
        await (/timed?\s?out|timeout/i.test(String(localErr.message || ''))
          ? this._sayLine('timeout', 'That took too long. Try me again.')
          : this._sayLine('broke', 'Something broke in my chain of thought. Try me again.'));
      } else {
        // Empty reply with no error and no cloud result — both paths returned blank.
        await this._sayLine('broke', 'Something broke in my chain of thought. Try me again.');
      }
    } finally {
      this._busy = false; // guaranteed on EVERY path — a stuck flag = permanently unresponsive
      // voice-loop-007: 'listening' implies open mic; use 'idle' when stream is null (no open mic)
      if (this.orb) this.orb.setState(this._stream ? 'listening' : 'idle');
      this._setStage(null); // HUD: flash final state, then fade out — guaranteed turn-end on every path
      this._f5ArmIdle(); // release the ~500MB voice daemon after 5 min idle (respawns on next ask)
    }
    return reply;
  }

  _nvidiaKey() {
    try {
      const fs = require('fs'), path = require('path'), os = require('os');
      const k = fs.readFileSync(path.join(os.homedir(), 'AI-Brain-build/.secrets/nvidia-tts.key'), 'utf8').trim();
      return k || null;
    } catch (_) { return null; }
  }

  // Cloud FALLBACK brain — NVIDIA-hosted Llama 3.1 8B (~0.4s first token, immune to
  // local CPU load). Fires ONLY when the local Claude brain fails, so Ultron keeps
  // answering in his cloned voice instead of going silent. Streams SSE → the same
  // clause-first sentence emitter → F5. Includes the derived vault snapshot (already
  // computed/safe — open bids, accounts, deadlines) so plate/bid questions work even
  // when local is down. Disable with settings.voice.cloudFallback=false.
  _nvidiaBrainAsk(text, onSentence, vaultCtx) {
    return new Promise((resolve, reject) => {
      const key = this._nvidiaKey();
      if (!key) { reject(new Error('no cloud key')); return; }
      const https = require('https'), cfg = this._voiceCfg();
      const timeStr = new Date().toLocaleString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
      const userMsg = `(It is currently ${timeStr} — Tony's local time.)` +
        (vaultCtx ? `\n\n(Vault snapshot:\n${vaultCtx}\n)` : '') +
        `\n\n${text}`;
      const body = JSON.stringify({
        model: cfg.cloudModel || 'meta/llama-3.1-8b-instruct',
        messages: [
          { role: 'system', content: this._persona() },
          { role: 'user', content: userMsg },
        ],
        stream: true, max_tokens: 120, temperature: 0.6,
      });
      let acc = '', full = '', chunks = 0, settled = false;
      const done = (err, out) => { if (settled) return; settled = true; err ? reject(err) : resolve(out); };
      const emit = () => {
        for (;;) {
          const re = chunks === 0 ? /^([\s\S]*?(?:[.!?…][“')”]?|[,;:—–-]))\s/ : /^([\s\S]*?[.!?…][“')”]?)\s/;
          const m = acc.match(re); if (!m) break;
          let sent = m[1].trim(); acc = acc.slice(m[0].length);
          sent = sent.replace(/[—–\-,;:]$/, '').trim();
          sent = this._stripMd(sent);
          if (sent.length < 2) continue;
          chunks++; try { onSentence(sent); } catch (_) {}
        }
      };
      const req = https.request({
        hostname: 'integrate.api.nvidia.com', path: '/v1/chat/completions', method: 'POST',
        headers: { 'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json', 'Accept': 'text/event-stream', 'Content-Length': Buffer.byteLength(body) },
      }, (res) => {
        if (res.statusCode && res.statusCode >= 400) { res.resume(); done(new Error('cloud HTTP ' + res.statusCode)); return; }
        let buf = ''; res.setEncoding('utf8');
        res.on('data', (chunk) => {
          buf += chunk; let i;
          while ((i = buf.indexOf('\n')) >= 0) {
            const line = buf.slice(0, i).trim(); buf = buf.slice(i + 1);
            if (!line.startsWith('data:')) continue;
            const d = line.slice(5).trim();
            if (d === '[DONE]') continue;
            try { const j = JSON.parse(d); const dl = ((j.choices || [])[0] || {}).delta; const t = (dl && dl.content) || ''; if (t) { full += t; acc += t; emit(); } } catch (_) {}
          }
        });
        res.on('end', () => { const rest = this._stripMd(acc.trim()); if (rest && rest.length >= 2) { try { onSentence(rest); } catch (_) {} } done(null, full.trim()); });
      });
      req.on('error', (e) => done(e));
      req.setTimeout(20000, () => { try { req.destroy(new Error('cloud timeout')); } catch (_) {} done(new Error('cloud timeout')); });
      req.write(body); req.end();
    });
  }

  // Legacy one-shot brain (per-call `claude -p`) — fallback when the daemon dies.
  _askOneShot(text) {
    const cp = require('child_process'), os = require('os');
    const cfg = this._voiceCfg();
    const timeStr = new Date().toLocaleString(undefined, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit' });
    const ctx = this._vaultContext(text);
    const prompt = `(It is currently ${timeStr} — Tony's local time.)\n` +
      (ctx ? `\n(Tony's current vault state — use only if relevant:\n${ctx}\n)\n` : '') + '\n' + text;
    return new Promise((resolve, reject) => {
      this._claudeProc = cp.execFile(this._claudeBin(),
        ['-p', prompt, '--model', cfg.brainModel, '--append-system-prompt', this._persona(), // config-dead-04
         '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'],
        { cwd: os.tmpdir(), env: this._brainEnv(), timeout: cfg.brainTimeoutMs || 60000, maxBuffer: 1 << 20 },
        (err, stdout, stderr) => { this._claudeProc = null; if (err) reject(new Error((stderr || err.message || '').slice(0, 300))); else resolve((stdout || '').trim()); });
      try { this._claudeProc.stdin.end(); } catch (_) {} // claude -p waits 3s for piped stdin otherwise
    });
  }

  // Subtle earcon so Tony hears that Ultron is now listening.
  _earcon(file) { try { require('child_process').execFile('/usr/bin/afplay', [file]); } catch (_) {} }

  // Warm the whisper model into the OS cache on show, so the FIRST real
  // transcribe is ~0.5s instead of a ~9s cold load. Fire-and-forget.
  _prewarm() {
    const cp = require('child_process'), os = require('os'), path = require('path'), fs = require('fs');
    const cfg = this._voiceCfg();
    // F5 voice daemon + standby brain restart on EVERY show (teardown kills them) —
    // outside the once-guard. Kokoro stays lazy (fallback only).
    this._f5Start();
    if (this._voiceCfg().engine === 'eleven') this._elStart(); // pre-warm EL daemon BEFORE first command (was lazy in ask())
    if (this._voiceCfg().engine === 'omni') this._omniStart(); // pre-warm only when OmniVoice is the primary engine; under 'eleven' it lazy-loads on first offline use
    this._brainPrewarm(); // warm dylibs + OAuth for the next real brain spawn
    if (this._warmed || !fs.existsSync(cfg.model)) return;
    this._warmed = true;
    const wav = path.join(os.tmpdir(), 'ultron-prewarm.wav');
    const ff = [path.join(os.homedir(), '.local/bin/ffmpeg'), '/opt/homebrew/bin/ffmpeg', 'ffmpeg'].find(p => { try { return fs.existsSync(p); } catch (_) { return false; } }) || 'ffmpeg';
    cp.execFile(ff, ['-y', '-f', 'lavfi', '-i', 'anullsrc=r=16000:cl=mono', '-t', '0.3', wav], () => {
      cp.execFile(cfg.whisper, ['-m', cfg.model, '-f', wav, '-nt', '-t', '4'], () => { fs.unlink(wav, () => {}); });
    });

  }

  // Keep-warm: re-fire the prewarm ping every 75s while the orb is visible so the
  // local brain stays in the ~2s warm band instead of the ~6s cold band after idle.
  // Started in show(), cleared in _teardown().
  _startKeepWarm() {
    clearInterval(this._keepWarmTimer);
    clearInterval(this._spendTimer); // perf-sweep-01: separate timer for spend refresh
    this._lastInteraction = Date.now(); // summoning the orb counts as interaction → warm for the next 10 min
    // prime the LIVE usage snapshot now so the first spend question matches the dashboard.
    // tokenStats lives on vaultData (memoized 55s) — `this.tokenStats` never existed on the
    // orb; the stale call threw mid-show() and silently killed visibility persistence.
    try { this.plugin.vaultData.tokenStats().then(s => { this._liveUsage = s; }).catch(() => {}); } catch (_) {}
    this._keepWarmTimer = setInterval(() => {
      if (!this.visible || document.hidden) return; // backgrounded app never spawns warm-up processes
      // Don't burn a `claude -p` spawn every 75s on an orb that's up but idle. After 10 min with no
      // interaction, stop pre-warming — the next real ask() just pays the ~6s cold start once.
      if (Date.now() - (this._lastInteraction || 0) > 600000) return;
      this._brainWarmed = false; // reset once-guard so the next call actually fires
      this._brainPrewarm();
      // perf-sweep-01: tokenStats (937-file stat-sweep) decoupled from the 75s keep-warm tick → moved to _spendTimer below
    }, 75000);
    // perf-sweep-01: spend display needs no sub-minute freshness for a voice assistant — refresh every 5 min
    this._spendTimer = setInterval(() => {
      if (!this.visible || document.hidden) return; // no stat-sweeps while backgrounded
      try { this.plugin.vaultData.tokenStats().then(s => { this._liveUsage = s; }).catch(() => {}); } catch (_) {}
    }, 300000);
  }

  // Warm the OS dylib cache + OAuth for the NEXT real brain spawn. NOT reused as a session
  // (a persistent `claude -p` degrades to ~28s after idle — Lesson 30). Throwaway; exits fast.
  // MUST carry ULTRON_VOICE=1 + --setting-sources '' or it fires the vault SBAP hooks → 60s tax (Lesson 25).
  _brainPrewarm() {
    if (this._brainWarmed) return;
    this._brainWarmed = true;
    try {
      const cp = require('child_process'), os = require('os');
      const env = Object.assign(this._brainEnv(), { ULTRON_VOICE: '1', VAULT_BRAIN_QUIET: '1' });
      const p = cp.spawn(this._claudeBin(),
        ['-p', 'hi', '--model', this._voiceCfg().brainModel, // config-dead-04: _voiceCfg() always provides brainModel
         '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}'],
        { cwd: os.tmpdir(), env, stdio: ['pipe', 'ignore', 'ignore'] });
      try { p.stdin.end(); } catch (_) {} // claude -p waits 3s for piped stdin otherwise
      p.on('error', () => {});
      setTimeout(() => { try { p.kill(); } catch (_) {} }, 12000); // never let a prewarm linger
    } catch (_) {}
  }

  // ULTRON voice: Kokoro-82M (am_onyx, deep American male — near-SOTA local neural TTS)
  // with a subtle metallic comb echo — James Spader's calm menace, not a robot.
  // Fallback chain: Kokoro daemon → Piper one-shot → macOS `say`. Tunable in settings.voice.
  _voiceCfg() {
    const os = require('os'), path = require('path');
    const d = {
      engine: 'eleven',                // 'eleven' = ElevenLabs Ultron clone (default, ONLINE; F5 fast local fallback; offline → NeuTTS→F5) | 'neutts' | 'f5' | 'kokoro' | 'piper'
      // ElevenLabs Ultron voice clone (primary when online — same stdin/stdout JSON protocol as F5/NeuTTS; near-instant ready, no local model):
      elevenPython: '',
      elevenDaemon: '',
      // NeuTTS Air voice clone (offline primary — same stdin/stdout JSON protocol as F5):
      neuttsPython: '',
      neuttsDaemon: '',
      // OmniVoice offline primary (between ElevenLabs and F5 in the cascade — same protocol):
      omniPython: '',
      omniDaemon: '',
      // F5-TTS voice clone (the actual movie-Ultron timbre, sentence-streamed):
      f5Python: path.join(os.homedir(), 'AI-Brain-build/f5-venv/bin/python'),
      f5Daemon: path.join(os.homedir(), 'AI-Brain-build/f5/ultron_f5_daemon.py'),
      f5Lines: path.join(os.homedir(), 'AI-Brain-build/f5/lines'), // pre-generated fixed lines (instant)
      kokoroDir: path.join(os.homedir(), 'AI-Brain-build/kokoro'), // kokoro_daemon.py + model files
      kokoroPython: path.join(os.homedir(), 'AI-Brain-build/kokoro-venv/bin/python'),
      kokoroVoice: 'am_onyx',          // deepest US male in the voice pack
      kokoroSpeed: 0.95,               // deliberate, unhurried cadence
      // Piper fallback (one-shot, no daemon):
      piperModel: path.join(os.homedir(), 'AI-Brain-build/piper/en_US-ryan-high.onnx'),
      piperLengthScale: 1.08,
      // Ultron FX (set fx:false for the plain neural voice). fxChain is a full ffmpeg
      // -af template with ${sr} placeholders → fully tunable. Default = "deep + detuned
      // doubling (choral menace) + metallic comb" to match the movie's Ultron.
      fx: true,
      // "Subtle synthetic" Ultron (Tony's pick, voice option B): deep smooth baritone
      // (~90Hz, matched to the movie's measured ~99Hz — NOT over-deepened) + a faint
      // detuned doubling (the "not-quite-human" layer) + room presence. Tunable.
      fxChain: 'asetrate=${sr}*0.97,aresample=${sr},atempo=1.0309,chorus=0.5:0.7:45:0.28:0.2:2,aecho=0.9:0.65:60|100:0.22|0.13,highpass=f=72',
      // Legacy simple knobs (used only if fxChain is cleared):
      fxPitch: 0.97,
      fxEcho: 'aecho=0.8:0.85:6|9:0.18|0.12',
      voice: 'Alex', rate: 168,        // `say` last-resort (American male)
      model: path.join(os.homedir(), 'AI-Brain-build/models/ggml-base.en.bin'),
      whisper: '/opt/homebrew/bin/whisper-cli', // serverless STT (no-localhost); whisper-server path removed entirely
      wake: 'ultron', wakeEnabled: true,
      // Brain routing. PRIMARY = Codex (ChatGPT CLI, uses Tony's existing ChatGPT
      // subscription — NO extra API key). Claude is the automatic local fallback when
      // Codex fails, is signed out, or is rate-limited (its 5-hour cap). Set
      // brainEngine:'claude' to flip the order back.
      // PRIMARY = Claude (streams first token in ~2s, sentence-by-sentence into the voice
      // cascade = the ChatGPT-like conversational feel). Codex (`codex exec`) is kept as the
      // resilience fallback but is NOT primary: it boots a full agentic harness every turn
      // (~15-40s, no streaming) which goes mute-then-blurts — wrong for live conversation.
      // Flip brainEngine:'codex' to put ChatGPT first if you prefer it despite the latency.
      brainEngine: 'claude',          // 'claude' primary (streaming) | 'codex' (ChatGPT) primary
      codexModel: '',                 // codex model; '' = use codex's own config default (gpt-5.5 was an invalid id → silent failure)
      micToggleKey: 'Mod+Shift+Space', // listen key — Cmd+Shift+Space (rebindable). Cmd+Z left alone for Obsidian Undo.
      orbToggleKey: 'Mod+Shift+Space', // alias of the listen key — same one-key summon/dismiss (rebindable)
      pttWindowMs: 10000,             // mic stays armed this long after a press; auto-off if nothing captured
      brainModel: 'claude-sonnet-4-6', // Brain model. Sonnet 4.6 = smart + natural + fast streaming on the Max OAuth login (no API key). Haiku was too terse/robotic for conversation. Both the primary brain and morning-digest compose use this.
      codexTimeoutMs: 15000,          // codex has no streaming — 15s is generous for voice
      brainIdleToolMs: 30000,         // idle-reset watchdog: max silence between tool steps (tool turns can pause ~30s between inference steps)
      brainIdleFastMs: 15000,         // idle-reset watchdog: max silence for no-tool turns (should answer fast)
      brainMaxToolMs: 180000,         // hard ceiling for tool-using brain turns (deep vault queries can take 10-77s)
      brainTimeoutMs: 30000,          // hard ceiling for no-tool brain turns (kept for morning-digest + execFile callers)
      autoConfirm: true,              // the request IS the approval — Tony's spoken command executes immediately (announce + do), no redundant yes/no. Safety net stays: actions are append/field-edit only (never delete/overwrite), audited to actions.jsonl, git-reversible.
      confirmTimeoutMs: 12000,        // how long Ultron waits for a yes/no before treating it as "no"
      digestTime: '08:00',            // local time for the spoken morning digest (Phase C)
      digestEnabled: true,            // run the scheduled morning digest
      monitorsEnabled: true,          // proactive bid-deadline + inbox monitors (Phase D)
      monitorQuietBefore: 8,          // no proactive speech before this hour
      monitorQuietAfter: 21,          // …or after this hour
      monitorDeadlineDays: 3,         // alert when a bid deadline is within N days
      cloudFallback: false,           // CONFIDENTIALITY default OFF: the cloud path sent injected vault context (client/bid names) to NVIDIA. Keep answers local (Codex→Claude). Set true only with a redaction pass (see ULT-PRIV-04).
      cloudModel: 'meta/llama-3.1-8b-instruct', // hosted fallback model (~0.4s first token)
      f5IdleMs: 300000,              // release the warm voice daemons (~600MB EL+F5) after 5 min idle (respawns on next ask) — was 20 min despite the "5 min" intent
      silenceMs: 350, maxMs: 15000,  // 350ms end-of-utterance — snappier turn-taking
      threshold: null, // null = ADAPTIVE (noise-floor tracking); set a number to pin it
    };
    return Object.assign(d, (this.plugin && this.plugin.settings && this.plugin.settings.voice) || {});
  }

  // ── Kokoro daemon (loads the 325MB model once; lives only while the orb is up) ──
  _kokoroStart() {
    const cfg = this._voiceCfg();
    if (cfg.engine === 'piper' || this._kk) return;
    const fs = require('fs'), path = require('path'), cp = require('child_process');
    const daemon = path.join(cfg.kokoroDir, 'kokoro_daemon.py');
    if (!fs.existsSync(cfg.kokoroPython) || !fs.existsSync(daemon)) return; // not installed → Piper path
    try {
      const proc = cp.spawn(cfg.kokoroPython, [daemon], { stdio: ['pipe', 'pipe', 'ignore'] });
      const kk = { proc, ready: false, buf: '', waiters: [] };
      proc.stdout.on('data', (chunk) => {
        kk.buf += chunk.toString();
        let i;
        while ((i = kk.buf.indexOf('\n')) >= 0) {
          const line = kk.buf.slice(0, i).trim(); kk.buf = kk.buf.slice(i + 1);
          if (!line) continue;
          let msg; try { msg = JSON.parse(line); } catch (_) { continue; }
          if (msg.ready) { kk.ready = true; continue; }
          const w = kk.waiters.shift();
          // TTS-01: timed-out (dead) waiter still consumes its positional slot; its late wav is discarded.
          if (w && w.dead && msg.ok && msg.out) { try { fs.unlink(msg.out, () => {}); } catch (_) {} }
          if (w) msg.ok ? w.resolve(msg.out) : w.reject(new Error(msg.err || 'kokoro failed'));
        }
      });
      proc.on('exit', () => {
        if (this._kk === kk) this._kk = null;            // gone → next speak() respawns or falls back
        kk.waiters.splice(0).forEach(w => w.reject(new Error('kokoro daemon exited')));
      });
      this._kk = kk;
    } catch (_) { this._kk = null; }
  }

  _kokoroStop() {
    if (this._kk) { const k = this._kk; this._kk = null; try { k.proc.kill(); } catch (_) {} }
  }

  // One serialized synth request → resolves the wav path. Rejects on any failure
  // (daemon missing/dead/slow) so speak() can fall through to Piper.
  _kokoroSay(text, cfg) {
    return new Promise((resolve, reject) => {
      if (!this._kk) this._kokoroStart();
      const kk = this._kk;
      if (!kk) { reject(new Error('kokoro unavailable')); return; }
      const os = require('os'), path = require('path');
      const out = path.join(os.tmpdir(), 'ultron-tts-' + Date.now() + '.wav');
      const waiter = { dead: false,
                       resolve: (p) => { clearTimeout(tid); resolve(p); },
                       reject: (e) => { clearTimeout(tid); reject(e); } };
      const tid = setTimeout(() => {
        waiter.dead = true; // TTS-01: no splice — daemon replies are positional; dead slot is consumed in order
        reject(new Error('kokoro timeout'));
      }, 20000);
      kk.waiters.push(waiter);
      try {
        kk.proc.stdin.write(JSON.stringify({ text, voice: cfg.kokoroVoice, speed: cfg.kokoroSpeed, out }) + '\n');
      } catch (e) { clearTimeout(tid); const i = kk.waiters.indexOf(waiter); if (i >= 0) kk.waiters.splice(i, 1); reject(e); }
    });
  }

  // Piper runs as `python -m piper` from its uv venv (wheels bundle the native deps,
  // unlike the broken standalone macOS binary). Returns the venv python, or null.
  _piperBin() {
    const os = require('os'), path = require('path'), fs = require('fs');
    const py = path.join(os.homedir(), 'AI-Brain-build/piper-venv/bin/python');
    try { if (fs.existsSync(py)) return py; } catch (_) {}
    return null;
  }

  _sayFallback(text, done) {
    const cp = require('child_process'), c = this._voiceCfg();
    try { this._sayProc = cp.execFile('/usr/bin/say', ['-v', c.voice, '-r', String(c.rate), text], done); }
    catch (_) { done(); }
  }

  // Human-sounding speech, fully offline, no API.
  // Chain: Kokoro daemon (82M, near-SOTA) → Piper one-shot → macOS `say`.
  // Every neural wav gets the Ultron FX (ffmpeg pitch+comb, ~10ms) before afplay.
  // ── Speech gate (ultron-silent-unless-triggered) ──────────────────────────
  // Ultron speaks ONLY in response to a user trigger: Cmd+Shift+Space → listenOnce,
  // the mic/PTT, or an explicit palette command. _lastInteraction is stamped by those
  // paths and NEVER by the proactive tick (_maybeDigest / _maybeMonitors) or an
  // auto-show greeting — so all of those go silent. The window spans a whole turn
  // (press → listen → compose → speak) with margin. Tune via voice.speakWindowMs.
  _speakAllowed() {
    const w = (this._voiceCfg && this._voiceCfg().speakWindowMs) || 120000;
    return !!this._lastInteraction && (Date.now() - this._lastInteraction) < w;
  }

  speak(text) {
    if (!this._speakAllowed()) return Promise.resolve();
    return new Promise((resolve) => {
      const cp = require('child_process'), fs = require('fs'), os = require('os'), path = require('path');
      if (this.orb) this.orb.setState('speaking');
      this._setStage('speaking');
      const done = () => { this._sayProc = null; resolve(); };
      const cfg = this._voiceCfg();
      const play = (file) => {
        try {
          this._ttsWav = file; // tracked so stopSpeaking() can clean up an interrupted playback
          this._sayProc = cp.execFile('/usr/bin/afplay', [file], () => { this._ttsWav = null; fs.unlink(file, () => {}); done(); });
        } catch (_) { fs.unlink(file, () => {}); this._sayFallback(text, done); }
      };
      // Ultron treatment: pitch down (asetrate) + restore tempo + metallic comb echo.
      // `sr` must be the wav's true sample rate (Kokoro=24000, Piper ryan-high=22050)
      // or the pitch math compounds wrongly. FX failure → play the raw neural wav.
      const fxPlay = (wav, sr) => {
        const ff = [path.join(os.homedir(), '.local/bin/ffmpeg'), '/opt/homebrew/bin/ffmpeg'].find(x => { try { return fs.existsSync(x); } catch (_) { return false; } });
        if (cfg.fx === false || !ff) { play(wav); return; }
        // Prefer the full configurable chain; fall back to the simple pitch+echo knobs.
        let chain;
        if (cfg.fxChain) {
          chain = cfg.fxChain.replace(/\$\{sr\}/g, String(sr));
        } else {
          const pitch = cfg.fxPitch || 0.95;
          chain = `asetrate=${sr}*${pitch},aresample=${sr},atempo=${(1 / pitch).toFixed(4)},${cfg.fxEcho || 'aecho=0.8:0.85:6|9:0.18|0.12'}`;
        }
        const fxwav = wav.replace('.wav', '-fx.wav');
        cp.execFile(ff, ['-y', '-i', wav, '-af', chain, fxwav], { timeout: 10000 },
          (fe) => {
            if (fe || !fs.existsSync(fxwav)) { play(wav); return; } // FX failed → raw neural still beats `say`
            fs.unlink(wav, () => {}); play(fxwav);
          });
      };
      // Fallback: Piper one-shot (loads its model per call — slower, still neural).
      const piperPath = () => {
        const piper = this._piperBin();
        if (!piper || !fs.existsSync(cfg.piperModel)) { this._sayFallback(text, done); return; }
        const wav = path.join(os.tmpdir(), 'ultron-tts-' + Date.now() + '.wav');
        try {
          const p = cp.execFile(piper,
            ['-m', 'piper', '--model', cfg.piperModel, '-f', wav, '--length-scale', String(cfg.piperLengthScale || 1.0)],
            { timeout: 60000 },
            (err) => {
              if (err || !fs.existsSync(wav)) { this._sayFallback(text, done); return; }
              fxPlay(wav, 22050);
            });
          p.stdin.write(text); p.stdin.end();
        } catch (_) { this._sayFallback(text, done); }
      };
      // Primary: Kokoro daemon (warm synth ~1.3s, 4× realtime).
      this._kokoroSay(text, cfg).then(wav => fxPlay(wav, 24000)).catch(() => piperPath());
    });
  }

  stopSpeaking() {
    this._speakGen = (this._speakGen || 0) + 1; // chunks still synthesizing → discarded on arrival
    if (this._playing) this._playAbort = true;  // player flushes + unlinks the queue
    // orb-reactive-01: stop the WebAudio source node (barge-in / cancel cuts live audio instantly).
    if (this._playSrc) {
      try { this._playSrc.stop(); this._playSrc.disconnect(); } catch (_) {}
      this._playSrc = null;
      if (this.orb) this.orb.setAnalyser(null); // clear dead analyser so orb stops reacting
    }
    if (this._sayProc) { try { this._sayProc.kill(); } catch (_) {} this._sayProc = null; }
    if (this._ttsWav) { try { require('fs').unlink(this._ttsWav, () => {}); } catch (_) {} this._ttsWav = null; }
  }

  // ── Speech IN: VAD-gated record → whisper.cpp ──────────────────────────────
  // Adaptive speech threshold: explicit settings.voice.threshold wins; otherwise
  // 4× the tracked ambient noise floor (clamped) — robust across mics and gains.
  _vadThreshold() {
    const explicit = this.plugin && this.plugin.settings && this.plugin.settings.voice && this.plugin.settings.voice.threshold;
    if (explicit) return explicit;
    const floor = (this._noiseFloor == null) ? 0.01 : this._noiseFloor;
    return Math.min(0.08, Math.max(0.002, floor * 4 + 0.002));
  }

  _rms() {
    if (this._lastRms != null) return this._lastRms; // level timer keeps this fresh (200ms)
    if (!this._an) return 0;
    const buf = this._vadBuf || (this._vadBuf = new Uint8Array(this._an.fftSize));
    this._an.getByteTimeDomainData(buf);
    let s = 0; for (let i = 0; i < buf.length; i++) { const v = (buf[i] - 128) / 128; s += v * v; }
    return Math.sqrt(s / buf.length);
  }

  // Record one utterance. The recorder starts IMMEDIATELY (never after detecting
  // speech) so the word ONSET is never clipped — the old arm-then-record design cut
  // the "Ul" off "Ultron", leaving whisper "tron" and breaking the wake match.
  //   armOnSpeech=true (wake loop): bounded ~2.5s window; if no speech, return fast so
  //     the loop cycles. whisper still only runs when speech was actually seen → cheap.
  //   armOnSpeech=false (push-to-talk): up to 6s to START speaking, then silence-stops.
  async _recordUtterance({ armOnSpeech = false, maxMs, silenceMs } = {}) {
    const cfg = this._voiceCfg();
    const MAX = maxMs || cfg.maxMs, SIL = silenceMs || cfg.silenceMs;
    const th = () => this._vadThreshold(); // LIVE per-poll — adaptive/explicit changes apply immediately
    if (!this._stream || !this._an) return null;

    let mime = 'audio/webm';
    if (window.MediaRecorder && !MediaRecorder.isTypeSupported(mime)) mime = '';
    const rec = new MediaRecorder(this._stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    rec.ondataavailable = e => { if (e.data && e.data.size) chunks.push(e.data); };
    const started = Date.now(); let lastVoice = Date.now(), sawVoice = false;
    // Onset give-up: how long to wait for speech to START before abandoning this clip.
    const ONSET_GIVEUP = armOnSpeech ? (cfg.wakeWindowMs || 2600) : (cfg.noSpeechMs || 6000);
    rec.start(100); // capturing from t0 → onset preserved
    await new Promise((resolve) => {
      const iv = setInterval(() => {
        if (this._rms() > th()) { lastVoice = Date.now(); sawVoice = true; }
        // Abort if conditions changed mid-record (orb hidden, or wake pre-empted by ask/listen).
        const abort = !this.visible || (armOnSpeech && (this._busy || this._listening || !this._wakeOn));
        const tooLong = Date.now() - started > MAX;
        const silent = sawVoice && (Date.now() - lastVoice > SIL);
        const gaveUp = !sawVoice && (Date.now() - started > ONSET_GIVEUP);
        if (tooLong || silent || gaveUp || abort) { clearInterval(iv); try { rec.stop(); } catch (_) {} resolve(); }
      }, 80); // tight poll → minimal onset clip, still cheap (pure JS, no whisper)
    });
    await new Promise(r => { rec.onstop = r; rec.onerror = r; if (rec.state === 'inactive') r(); });
    if (!sawVoice || !chunks.length) return null;

    const fsp = require('fs').promises, fs = require('fs'), os = require('os'), path = require('path'), cp = require('child_process');
    const arr = Buffer.from(await new Blob(chunks).arrayBuffer());
    const base = path.join(os.tmpdir(), 'ultron-' + Date.now());
    const raw = base + '.webm', wav = base + '.wav';
    await fsp.writeFile(raw, arr);
    const ffmpeg = [path.join(os.homedir(), '.local/bin/ffmpeg'), '/opt/homebrew/bin/ffmpeg', 'ffmpeg'].find(p => { try { return fs.existsSync(p); } catch (_) { return false; } }) || 'ffmpeg';
    try {
      await new Promise((res, rej) => cp.execFile(ffmpeg, ['-y', '-i', raw, '-ar', '16000', '-ac', '1', wav], e => e ? rej(e) : res()));
    } catch (_) { fsp.unlink(raw).catch(() => {}); return null; }
    fsp.unlink(raw).catch(() => {});
    return wav;
  }

  async _transcribe(wav) {
    const cfg = this._voiceCfg(), cp = require('child_process'), fs = require('fs');
    // stt-12k-deadcode-guard: removed the 12000-byte size guard — it was dead code because
    // the 0.5s PREROLL always produces ≥16KB after resampling, so this check never fired.
    // Near-silence is already blocked upstream: _emitUtterance drops clips < 0.25s.
    // Serverless STT: per-call whisper-cli only (no-localhost). Guard binary + model.
    if (!fs.existsSync(cfg.whisper)) { new Notice('Ultron: whisper binary missing — ' + cfg.whisper, 8000); fs.unlink(wav, () => {}); return ''; }
    if (!fs.existsSync(cfg.model)) { new Notice('Ultron: whisper model missing — ' + cfg.model, 8000); fs.unlink(wav, () => {}); return ''; }
    return await new Promise((resolve) => {
      cp.execFile(cfg.whisper, ['-m', cfg.model, '-f', wav, '-nt', '-l', 'en', '-t', '4'],
        { timeout: 60000, maxBuffer: 1 << 20 },
        (err, stdout, stderr) => { // stt-stderr-dropped: capture stderr for debuggability on failure
          fs.unlink(wav, () => {});
          if (err) {
            // Log stderr so Metal/GPU/OOM failures are diagnosable (previously silent)
            if (stderr && stderr.trim()) {
              try { const os = require('os'), path = require('path');
                fs.appendFileSync(path.join(os.tmpdir(), 'ultron-whisper-errors.log'),
                  new Date().toISOString() + '\t' + stderr.slice(0, 500) + '\n'); } catch (_) {}
            }
            resolve(''); return;
          }
          // Strip whisper's timestamp segments AND its non-speech sentinels. whisper.cpp emits
          // [BLANK_AUDIO] / [ Silence ] / (sound) / [Music] / *background noise* for silence; the
          // old regex only matched numeric timestamps, so [BLANK_AUDIO] survived as truthy "speech"
          // and Ultron answered silence ("Static...") + poisoned its history. Normalize all of it to ''.
          let t = (stdout || '')
            .replace(/\[[0-9:.\s\->]+\]/g, '')                 // [hh:mm:ss.mmm --> ...] segment headers
            .replace(/\[[^\]]*\]|\([^)]*\)|\*[^*]*\*/g, '')    // [BLANK_AUDIO] [ Silence ] (sound) *music*
            .replace(/\s+/g, ' ').trim();
          if (/^(blank[_ ]?audio|silence|inaudible|music|sound|noise|applause|no[_ ]?speech|you|thank you\.?|thanks\.?|thank you for (?:watching|listening)\.?)\.?$/i.test(t)) t = ''; // stt-hallucination-bypass: whisper-cpp base.en hallucinates 'you'/'thank you' on silence
          if (!/[a-z0-9]/i.test(t)) t = '';                    // nothing but punctuation left → heard nothing
          resolve(t);
        });
    });
  }

  // Push-to-talk: arm the continuous engine so the NEXT utterance becomes a command.
  // No separate recording path — the always-on capture already has the onset buffered.
  async listenOnce() {
    if (this._busy) return;
    this._lastInteraction = Date.now(); // pressing the mic counts as interaction → keep warm
    if (!this.visible) await this.show();
    if (!this._stream) { await this._initAudio(); }
    if (!this._stream) { new Notice('Ultron: microphone unavailable — check System Settings → Privacy → Microphone.', 6000); return; }
    this._earcon('/System/Library/Sounds/Tink.aiff'); // audible "I'm listening" cue
    this._armPtt();
  }

  // Wake word is handled by the continuous engine (_handleUtterance). startWake must
  // also OPEN the mic — the flag alone scanned nothing (STT-01: getUserMedia lives only
  // in _initAudio, which only listenOnce called, so "say Ultron" could never fire).
  startWake() {
    this._wakeOn = true;
    if (!this._stream && !this._micMuted) { this._initAudio().catch(() => {}); this._wakeMicNotice(); }
  }
  stopWake() { this._wakeOn = false; this._stopAudio(true); }
  _wakeMicNotice() { // surface the trade-off once per session: wake = mic stays hot
    if (this._wakeNoticeShown) return; this._wakeNoticeShown = true;
    new Notice('Ultron wake word active — the mic stays on so "Ultron" works hands-free. Toggle wake off to release it.', 6000);
  }

  // ── Mic mute/unmute (blocks ALL audio incl. wake word + PTT) ─────────────────
  toggleMic() { if (this._micMuted) this.unmuteMic(); else this.muteMic(); }
  muteMic() {
    this._micMuted = true;
    // Cancel any in-flight listen so Ultron doesn't finish an armed PTT capture
    if (this._listening || this._pttArmed) {
      this._pttArmed = false; this._listening = false;
      clearTimeout(this._pttTimer);
    }
    this._updateMicVisual();
    this.plugin.settings.voice = Object.assign({}, this.plugin.settings.voice, { micMuted: true });
    this.plugin.saveSettings();
  }
  unmuteMic() {
    this._micMuted = false;
    if (this._wakeOn && !this._stream) this._initAudio().catch(() => {}); // wake pref on → re-open the mic (STT-01)
    this._updateMicVisual();
    this.plugin.settings.voice = Object.assign({}, this.plugin.settings.voice, { micMuted: false });
    this.plugin.saveSettings();
  }
  _updateMicVisual() {
    const btn = this._micBtn;
    if (!btn) return;
    if (this._micMuted) {
      btn.textContent = '🔇';
      btn.style.filter = 'grayscale(1)';
      btn.style.background = 'rgba(220,40,40,0.25)';
      btn.title = 'Ultron mic MUTED — press your toggle key to unmute';
    } else {
      btn.textContent = '🎙';
      btn.style.filter = 'drop-shadow(0 0 6px rgba(76,168,232,.6))';
      btn.style.background = '';
      const via = this._lastBrain ? ` · last answer via ${this._lastBrain === 'cloud' ? 'cloud fallback' : this._lastBrain}` : '';
      btn.title = 'Push to talk — or say "Ultron"' + via;
    }
  }

  // Whisper mishears proper nouns — match the wake word loosely. For "ultron" accept
  // ultron / altron / oltron / "ul tron" / "all tron" / ultran / ultrons, the
  // GLUED-PREFIX class whisper.cpp base.en actually produces (pultron / voltron /
  // aultron — leading consonant fused onto the onset), AND a leading-onset-clipped
  // bare "tron" (\b keeps "electron"/"patron" out — only standalone fires).
  _wakePattern(wake) {
    // [a-z]{0,2}[uoai]l+ absorbs the glued leading consonant ("Pultron"/"Voltron")
    // that broke the old \b-anchored pattern (base.en fuses the prior word's tail onto
    // "Ultron" ~40% of the time → silent Ultron). tr[oauwy]*n+s? then absorbs the
    // vowel-cluster + plural mishears (ultron/ultraun/altron/ultran/oltron/ultrons) and
    // an onset-clipped bare "tron". The \b wrapper still keeps "electron"/"patron"/
    // "citron"/"neutron"/"control" out (e/no-l after their prefix). Recall 12/13 on real
    // whisper-server output, 0 false-positives across an 14-word precision set — verified.
    if (wake === 'ultron') return '(?:[a-z]{0,2}[uoai]l+[\\s-]?)?tr[oauwy]*n+s?';
    if (wake === 'jarvis') return 'j[ae]rvi[sc]e?';
    return wake.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  }
}

// ── Plugin ────────────────────────────────────────────────────────────────────

class CommandCenterPlugin extends Plugin {
  async onload() {
    // Load-stamp FIRST (before anything that could throw) so we can verify what build is
    // actually running: ~/ULTRON_LOADED.json. If this file is stale/missing after a reload,
    // the plugin didn't load this build (or onload threw immediately).
    try {
      const fs = require('fs'), os = require('os'), path = require('path');
      fs.writeFileSync(path.join(os.homedir(), 'ULTRON_LOADED.json'),
        JSON.stringify({ build: PLUGIN_BUILD, loaded_at: new Date().toISOString() }, null, 2));
    } catch (_) {}

    await this.loadSettings();

    this.registerView(VIEW_TYPE, leaf => new CommandCenterView(leaf, this));

    this.addRibbonIcon('layout-dashboard', 'Open Command Center', () => {
      this.openView();
    });

    this.addCommand({
      id: 'open-command-center',
      name: 'Open Command Center',
      callback: () => this.openView(),
    });

    // ── Command palette: every key action reachable via Cmd+P (keyboard-first) ──
    this.addRibbonIcon('zap', 'Quick Capture', () => new QuickCaptureModal(this.app).open());
    this.addCommand({
      id: 'quick-capture', name: 'Quick Capture (brain-dump → inbox)',
      callback: () => new QuickCaptureModal(this.app).open(),
    });
    this.addCommand({
      id: 'ask-brain', name: 'Ask your second brain',
      callback: () => new ActionPromptModal(this.app,
        { emoji: '🧠', label: 'Ask your second brain', placeholder: 'Ask anything about your vault…' },
        (q) => injectIntoTerminal(this.app, q)).open(),
    });
    this.addCommand({
      id: 'morning-brief-now', name: 'Run Morning Brief now (Ultron speaks it + writes the daily note)',
      callback: () => { if (this.orb && this.orb._runDigestNow) this.orb._runDigestNow(); },
    });
    // Expose each configured launcher as its own palette command.
    for (const a of (this.settings.actions || DEFAULT_ACTIONS)) {
      this.addCommand({
        id: 'action-' + a.id, name: `${a.label}`,
        callback: () => {
          if (a.command === '__ORB_TOGGLE__') { this.orb && this.orb.toggle(); return; }
          if (a.command === '__DIGEST_NOW__') { this.orb && this.orb._runDigestNow && this.orb._runDigestNow(); return; }
          if (a.prompt) {
            new ActionPromptModal(this.app, a, (input) =>
              injectIntoTerminal(this.app, a.command.replace(/\{input\}/g, input))).open();
          } else {
            injectIntoTerminal(this.app, a.command);
          }
        },
      });
    }

    this.addSettingTab(new CommandCenterSettingTab(this.app, this));

    // Auto-assemble Mission Control when the workspace layout is ready
    if (this.settings.autoAssemble !== false) {
      this.app.workspace.onLayoutReady(() => this._autoAssemble());
    }

    // async-crash-evidence: any unhandled promise rejection from plugin code
    // (e.g. orb.show() dying mid-flight) was console-only — invisible in every
    // investigation. Bridge it into the health log. registerDomEvent auto-
    // cleans on unload.
    this.registerDomEvent(window, 'unhandledrejection', (ev) => {
      try {
        const r = ev && ev.reason;
        const msg = (r && (r.stack || r.message)) ? String(r.stack || r.message).split('\n').slice(0, 2).join(' | ') : String(r);
        if (/main\.js|ccc|orb|jarvis|ultron/i.test(msg)) this._logHealth('unhandled rejection: ' + msg.slice(0, 300), 'warn');
      } catch (_) {}
    });

    // ── ULTRON Orb: floating audio-reactive orb inside Obsidian ──────────────
    this.orb = new JarvisOrb(this);
    // Synapse layer — visible thought: tool calls + thinking state fire sparks
    // across the file explorer (see SynapseLayer). Cheap when idle (no DOM).
    this.synapse = new SynapseLayer(this);
    this.register(() => { try { this.synapse.destroy(); } catch (_) {} });
    this.graphSynapse = new GraphSynapse(this);
    this.register(() => { try { this.graphSynapse.destroy(); } catch (_) {} });
    this.noteSynapse = new NoteSynapse(this);
    this.register(() => { try { this.noteSynapse.destroy(); } catch (_) {} });

    // ── Machine POV (HS-R2 #2): targeting reticles on entity wikilinks ───────
    // ThreatIndex is the substrate (O(1) lookups, receipts mandatory); the
    // post-processor decorates reading-view links. Live-preview (CM6) variant
    // is a follow-up — reading view ships first.
    this.threat = new ThreatIndex(this);
    this.threat.start();
    this.register(() => { try { clearTimeout(this.threat._timer); } catch (_) {} });
    this.registerMarkdownPostProcessor((el, ctx) => {
      if (!this.threat) return;
      el.querySelectorAll('a.internal-link').forEach(a => {
        const href = a.getAttribute('data-href') || a.getAttribute('href');
        if (!href) return;
        const dest = this.app.metadataCache.getFirstLinkpathDest(href.split('#')[0], ctx.sourcePath);
        if (!dest) return;
        const st = this.threat.statusFor(dest.path);
        if (!st) return;
        a.classList.add('ccc-poi', 'ccc-poi-' + st.level);
        if (st.reasons && st.reasons.length) a.setAttribute('aria-label', st.reasons.join(' · '));
      });
    });
    // ── Phantom Files (HS-R2 #1): ghost rows for missing winning-bid artifacts ─
    this.phantoms = new PhantomFiles(this);
    this.phantoms.start();
    this.register(() => { try { this.phantoms.destroy(); } catch (_) {} });

    // ── Secret Doors (HS-R2 #16): folders that secretly belong together ───────
    this.doors = new SecretDoors(this);
    this.doors.start();
    this.register(() => { try { this.doors.destroy(); } catch (_) {} });

    // ── Deck X-Ray (HS-R2 #19): brand-DNA lint off the pptx XML ───────────────
    this.addCommand({
      id: 'deck-xray',
      name: 'Deck X-Ray: brand-lint a built deck',
      callback: () => {
        const fs = require('fs'), path = require('path'), os = require('os');
        const roots = [path.join(os.homedir(), 'AI-Brain-build', 'out')];
        const decks = [];
        const walk = (dir, depth) => {
          if (depth > 3) return;
          let entries = [];
          try { entries = fs.readdirSync(dir, { withFileTypes: true }); } catch (_) { return; }
          for (const e of entries) {
            const p = path.join(dir, e.name);
            if (e.isDirectory()) walk(p, depth + 1);
            else if (e.name.endsWith('.pptx')) decks.push(p);
          }
        };
        for (const r of roots) walk(r, 0);
        if (!decks.length) { new Notice('No .pptx found under ~/AI-Brain-build/out — build a deck first.', 5000); return; }
        new DeckXRayPickModal(this.app, this, decks.sort()).open();
      },
    });

    // ── Document Tomography (HS-R2 #3): MRI mode for an RFP ───────────────────
    this.addCommand({
      id: 'tomography',
      name: 'Tomography: MRI-scan a bid’s RFP',
      callback: async () => {
        try {
          const open = JSON.parse(await this.app.vault.adapter.read('_brain_api/bid/_open.json'));
          if (!open.bids || !open.bids.length) { new Notice('No open bids.', 3000); return; }
          new TomographyPickModal(this.app, this, open.bids).open();
        } catch (e) { new Notice('Tomography: ' + e.message, 4000); }
      },
    });

    // ── Launch Control (HS-R2 #12): go/no-go poll over real gates ─────────────
    this.addCommand({
      id: 'launch-control',
      name: 'Launch Control: run the go/no-go board for a bid',
      callback: async () => {
        try {
          const open = JSON.parse(await this.app.vault.adapter.read('_brain_api/bid/_open.json'));
          if (!open.bids || !open.bids.length) { new Notice('No open bids.', 3000); return; }
          new LaunchPickModal(this.app, this, open.bids).open();
        } catch (e) { new Notice('Launch Control: ' + e.message, 4000); }
      },
    });

    // ── Sparring Chamber (HS-R2 #20): rehearse against the client ─────────────
    this.addCommand({
      id: 'sparring-chamber',
      name: 'Sparring Chamber: rehearse against a client',
      callback: () => new SparringPickModal(this.app, this).open(),
    });

    // ── Loadout Screen (HS-R2 #18): 30-second meeting draft pick ──────────────
    this.addCommand({
      id: 'loadout',
      name: 'Loadout: equip gear for a meeting',
      callback: () => new LoadoutModal(this.app, this).open(),
    });

    // ── The Forge (HS-R2 #17): craft win themes from real inventory ───────────
    this.addCommand({
      id: 'forge-win-theme',
      name: 'The Forge: craft a win-theme block',
      callback: () => new ForgeModal(this.app, this).open(),
    });

    // ── Vault CCTV (HS-R2 #10): scrub the last 24h of mutations ───────────────
    this.addCommand({
      id: 'vault-cctv',
      name: 'Vault CCTV: replay the last 24h',
      callback: () => new VaultCCTVModal(this.app, this).open(),
    });

    // ── Invisible Ink (HS-R2 #5): breathe on a brief — see what it DOESN'T say ─
    // Fog rolls over the active note while the brain compares it against what
    // the second brain already knows (warm semantic recall + the account's
    // _brain_api brief); gaps and contradictions fade in as lemon-juice ink.
    // Real sources only — each ink line must name where it knows it from.
    this.addCommand({
      id: 'invisible-ink',
      name: 'Invisible Ink: reveal what this note doesn’t say',
      checkCallback: (checking) => {
        const f = this.app.workspace.getActiveFile();
        if (!f) return false;
        if (!checking) this._invisibleInk(f).catch((e) => new Notice('Invisible Ink failed: ' + e.message, 5000));
        return true;
      },
    });

    // ── Time-Scrub Cinema (HS-R2 #4): play the active note's git history ──────
    this.addCommand({
      id: 'time-scrub',
      name: 'Time-Scrub Cinema: play this note’s history',
      checkCallback: (checking) => {
        const f = this.app.workspace.getActiveFile();
        if (!f) return false;
        if (!checking) new TimeScrubModal(this.app, this, f).open();
        return true;
      },
    });

    // ── Diagnostic Chamber (HS-R2 #8): depose any SBAP agent in first person ──
    this.addCommand({
      id: 'diagnostic-chamber',
      name: 'Diagnostic Chamber: interrogate a fleet agent',
      callback: async () => {
        try {
          const reg = JSON.parse(await this.app.vault.adapter.read('_agent_state/_registry.json'));
          const agents = (reg.agents || []).filter(a => a.agent_name);
          if (!agents.length) { new Notice('No agents in the registry.', 4000); return; }
          new AgentPickerModal(this.app, this, agents).open();
        } catch (e) { new Notice('Chamber failed to open registry: ' + e.message, 5000); }
      },
    });

    this.addCommand({
      id: 'threat-index-dump',
      name: 'Machine POV: dump threat index (console)',
      callback: () => {
        const rows = [];
        for (const [path, st] of this.threat._map) {
          if (st.level !== 'healthy') rows.push({ path, level: st.level, reasons: st.reasons.join(' · ') });
        }
        console.table(rows.length ? rows : [{ path: '(all healthy)', level: '-', reasons: '-' }]);
        new Notice(`Machine POV: ${this.threat._map.size} entities indexed, ${rows.length} flagged — table in console.`, 5000);
      },
    });
    // Proactive tick (Phase C digest + Phase D monitors) — every 60s, in-plugin, no localhost.
    // agentic-tick-interval-60s-always-runs: guard the interval itself on document visibility
    // so even the function-call overhead is skipped when Obsidian is backgrounded.
    // _agenticTick also has its own if (!this.visible) guard as belt-and-suspenders.
    this.registerInterval(window.setInterval(() => { if (document.hidden) return; try { if (this.orb && this.orb._agenticTick) this.orb._agenticTick(); } catch (_) {} }, 60000));
    // Load marker (proves THIS build loaded on reload) + an immediate first tick (no 60s wait).
    try { if (this.orb && this.orb._tickLog) this.orb._tickLog({ event: 'onload', build: PLUGIN_BUILD }); } catch (_) {}
    setTimeout(() => { try { if (this.orb && this.orb._agenticTick) this.orb._agenticTick(); } catch (_) {} }, 3000);
    // p6-#15 proactive speak on unlock: when Obsidian becomes visible / regains focus, let the orb
    // open the speak gate so the once-daily morning brief speaks on the next tick. _onAppVisible
    // self-guards (digestEnabled + digest-due-today + once-per-day) so this never spams.
    this.registerDomEvent(document, 'visibilitychange', () => { if (!document.hidden) { try { if (this.orb && this.orb._onAppVisible) this.orb._onAppVisible(); } catch (_) {} } });
    this.registerDomEvent(window, 'focus', () => { try { if (this.orb && this.orb._onAppVisible) this.orb._onAppVisible(); } catch (_) {} });
    this.addRibbonIcon('orbit', 'Ultron Orb — show/hide (Cmd+Shift+Space)', () => this.orb && this.orb.toggle());
    this.addCommand({ id: 'jarvis-orb', name: 'Ultron Orb (toggle floating voice orb)',
      callback: () => this.orb && this.orb.toggle() });
    this.addCommand({ id: 'note-synapse-demo', name: 'Note: neuron firing demo (10s, active note)',
      callback: () => {
        const ns = this.noteSynapse;
        if (!ns) return;
        ns.thinking(true);
        new Notice('NoteSynapse: thinking for 10s on the active note…', 3000);
        setTimeout(() => ns.thinking(false), 10000);
      } });
    this.addCommand({ id: 'graph-synapse-demo', name: 'Graph: neuron firing demo (open graph view first)',
      callback: () => {
        const gs = this.graphSynapse;
        if (!gs) return;
        if (!gs._renderers().length) { new Notice('Open a graph view first'); return; }
        // a real recent file lights an anchored chain; then a folder sweep; then ambient bursts
        const recent = this.app.workspace.getLastOpenFiles()[0];
        if (recent) gs.fireFile(recent, true);
        setTimeout(() => gs.sweep('02_Areas/'), 700);
        setTimeout(() => gs._ambient(true), 1500);
        setTimeout(() => gs._ambient(false), 2100);
        setTimeout(() => gs._ambient(true), 2700);
      } });
    this.addCommand({ id: 'graph-synapse-probe', name: 'Graph: synapse self-test (writes probe json)',
      callback: async () => {
        const gs = this.graphSynapse, out = { ts: new Date().toISOString() };
        try {
          const rs = gs ? gs._renderers() : [];
          out.renderers = rs.length;
          out.nodes = rs.length ? gs._nodesOf(rs[0]).length : 0;
          if (rs.length && out.nodes) {
            const sample = gs._nodesOf(rs[0]).find(n => typeof n.id === 'string' && n.id.endsWith('.md'));
            out.sampleId = sample && sample.id;
            out.sampleHasCircle = !!(sample && sample.circle);
            out.sampleNeighbors = sample ? gs._neighborIds(sample).size : 0;
            // prefer a CONNECTED node so the chain path gets exercised
            const wired = gs._nodesOf(rs[0]).find(n => typeof n.id === 'string' && n.id.endsWith('.md') && gs._neighborIds(n).size > 0) || sample;
            out.firedId = wired && wired.id;
            out.firedNeighbors = wired ? gs._neighborIds(wired).size : 0;
            if (wired) { gs.fireFile(wired.id, true); out.fired = true; }
            await new Promise(r => setTimeout(r, 350));
            out.mid = { pulses: gs._pulses.size, links: gs._links.size, wrapped: gs._wrapN.size, raf: !!gs._raf };
            await new Promise(r => setTimeout(r, 2600));
            out.after = { pulses: gs._pulses.size, links: gs._links.size, wrapped: gs._wrapN.size, wrappedLinks: gs._wrapL.size, raf: !!gs._raf };
          }
        } catch (e) { out.error = String(e && e.message || e); }
        try { await this.app.vault.adapter.write('_agent_state/claude-code/graph-synapse-probe.json', JSON.stringify(out, null, 2)); }
        catch (e) {
          try {
            const fs = require('fs'), path = require('path');
            fs.writeFileSync(path.join(this.app.vault.adapter.basePath, '_agent_state/claude-code/graph-synapse-probe.json'), JSON.stringify(Object.assign(out, { adapterWriteError: String(e && e.message || e) }), null, 2));
          } catch (_) {}
        }
      } });
    this.addCommand({ id: 'jarvis-ask', name: 'Ultron: ask (Claude speaks the answer)',
      callback: async () => {
        if (!this.orb.visible) await this.orb.show();
        new ActionPromptModal(this.app, { emoji: '🔮', label: 'Ask Ultron', placeholder: 'Speak to your second brain…' },
          (text) => this.orb.ask(text)).open();
      } });
    this.addCommand({ id: 'jarvis-listen', name: 'Ultron: listen (push to talk)',
      callback: () => this.orb.listenOnce() });
    this.addCommand({ id: 'jarvis-wake-toggle', name: 'Ultron: toggle wake word',
      callback: async () => {
        if (!this.orb.visible) await this.orb.show();
        if (this.orb._wakeOn) { this.orb.stopWake(); new Notice('Ultron wake word OFF'); }
        else { this.orb.startWake(); new Notice('Ultron wake word ON — say "Ultron"'); }
        this.settings.voice = Object.assign({}, this.settings.voice, { wakeEnabled: this.orb._wakeOn });
        this.saveSettings();
      } });
    this.addCommand({ id: 'jarvis-mic-toggle', name: 'Ultron: listen (push to talk)',
      callback: async () => {
        if (!this.orb.visible) await this.orb.show();
        this.orb.listenOnce();
        new Notice('Ultron listening — speak now 🎙', 2000);
      } });

    // ── Global listen key listener (push-to-talk) ────────────────────────────
    // Fires the configured combo (default Mod+Shift+Space) from ANYWHERE in Obsidian,
    // INCLUDING while typing in a note. The combo carries modifiers and we match it
    // exactly + preventDefault, so it never inserts text and never shadows a plain
    // editor key. (The old build bailed whenever focus was in the editor — i.e. your
    // normal state — which is why Cmd+Z silently did nothing but Undo.)
    // Registered in capture phase so it fires before Obsidian's hotkey manager and
    // CodeMirror's keymap, avoiding workspace hotkey conflicts.
    this.registerDomEvent(document, 'keydown', (e) => {
      // Must include at least one modifier so a bare key never fires while typing.
      if (!e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) return;
      const combo = _normalizeCombo(e).toUpperCase();
      // Two Ultron keys (default to the SAME combo, Cmd+Shift+Space):
      //   • listenKey (micToggleKey) — TOGGLES THE MIC. Orb hidden → summon + listen.
      //     Orb up & idle → start a listen window (unmuting first if muted). Orb up &
      //     already listening → stop listening. It never dismisses the orb — that's the
      //     × button — because "the orb vanishes instead of hearing me" is exactly the
      //     bug this key used to have.
      //   • orbKey (orbToggleKey) — only distinct if rebound separately; then it's the
      //     pure show/hide toggle.
      const listenKey = ((this.settings.voice && this.settings.voice.micToggleKey) || 'Mod+Shift+Space').toUpperCase();
      const orbKey = ((this.settings.voice && this.settings.voice.orbToggleKey) || listenKey).toUpperCase();
      if (combo !== listenKey && combo !== orbKey) return;
      e.preventDefault();
      e.stopPropagation();
      (async () => {
        const orb = this.orb;
        if (!orb) return;
        // Dedicated, separately-rebound orb key → pure show/hide.
        if (combo === orbKey && orbKey !== listenKey) {
          if (orb.visible) { orb.hide(); new Notice('Ultron dismissed', 1200); }
          else { await orb.show(); new Notice('Ultron up', 1200); }
          return;
        }
        // Listen/mic toggle key.
        if (!orb.visible) {
          await orb.show();
          if (orb._micMuted) orb.unmuteMic();
          orb.listenOnce();
          new Notice('Ultron up — listening 🎙', 2000);
          return;
        }
        if (orb._listening || orb._pttArmed) {       // already listening → stop
          orb._stopAudio(true);                       // explicit off — release mic even if wake on
          new Notice('Ultron — mic off', 1200);
        } else {                                      // idle → start listening
          if (orb._micMuted) orb.unmuteMic();
          orb.listenOnce();
          new Notice('Ultron — listening 🎙', 2000);
        }
      })();
    }, { capture: true });

    // orb-flag-fix: the flag file is the durable visibility truth; data.json's
    // orbVisible is the legacy mirror. If they disagree, trust the flag and log
    // the rescue — that disagreement IS the "orb silently disappeared" bug.
    {
      let flagVisible = false;
      try { flagVisible = require('fs').existsSync(this.orb._orbFlagPath()); } catch (_) {}
      if (flagVisible && !this.settings.orbVisible) {
        this._logHealth('orb flag rescued visibility — data.json had stale orbVisible:false', 'warn');
        this.settings.orbVisible = true;
      }
      if (flagVisible || this.settings.orbVisible) this.app.workspace.onLayoutReady(() => this.orb.show());
    }

    // ── Self-healing loop: watchdog runs on load + every 5 min ───────────────
    // term-resurrect-fix: a terminal leaf disappearing means the user closed it
    // (a crashed shell keeps its leaf open showing the exit) — the watchdog must
    // not resurrect it. Any new terminal leaf re-arms auto-heal.
    this._termClosedByUser = false;
    this._termCount = this.app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE).length;
    this.registerEvent(this.app.workspace.on('layout-change', () => {
      const n = this.app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE).length;
      if (n < this._termCount) this._termClosedByUser = true;
      else if (n > this._termCount) this._termClosedByUser = false;
      this._termCount = n;
    }));
    this._health = { status: 'ok', issues: [], last: null };
    this.app.workspace.onLayoutReady(() => this._selfHeal());
    this.registerInterval(window.setInterval(() => this._selfHeal(), 5 * 60 * 1000));
    this.addCommand({ id: 'self-heal', name: 'Run self-check & heal',
      callback: () => this._selfHeal(true) });

    console.log('[Ultron] build: shiftspace-10s 20260607d (' + PLUGIN_BUILD + ')');
    // build provenance in the health log — answers "which build is actually live?"
    // after every (re)load, and gives hot-reload QA a checkable artifact.
    this._logHealth('plugin loaded — build ' + PLUGIN_BUILD, 'info');
  }

  // Append a structured line to the plugin health log — consumed by the nightly
  // skill-health-watcher / agent-dreaming loop (self-learning), and surfaced in-app.
  async _logHealth(msg, level = 'warn') {
    try {
      const line = JSON.stringify({ ts: new Date().toISOString(), level, msg }) + '\n';
      const path = '_agent_state/claude-code/plugin-health.log';
      const ad = this.app.vault.adapter;
      let prev = '';
      try { prev = await ad.read(path); } catch (_) {}
      // keep the log bounded (last ~200 lines)
      const lines = (prev + line).split('\n').filter(Boolean).slice(-200);
      await ad.write(path, lines.join('\n') + '\n');
    } catch (_) {}
  }

  // Self-healing watchdog: verify dependencies + data, auto-recover what it can.
  async _selfHeal(announce = false) {
    // pty-leak-no-concurrency-guard: prevent concurrent _selfHeal calls from both spawning terminals
    if (this._selfHealRunning) return this._health;
    this._selfHealRunning = true;
    const issues = [];
    let healed = [];
    try {
      // 1. Terminal plugin present?
      if (!this.app.plugins.getPlugin('terminal')) issues.push('terminal plugin missing');
      // 2. Core data files readable?
      for (const f of ['_agent_state/claude-code/stats.json', '_agent_state/_registry.json']) {
        try { JSON.parse(await this.app.vault.adapter.read(f)); }
        catch (_) { issues.push('unreadable: ' + f); }
      }
      // 3. During work hours, if the dashboard is open but the Claude terminal died,
      //    re-open it (auto-heal) — covers crashes / accidental closes.
      const h = new Date().getHours();
      const dashOpen = this.app.workspace.getLeavesOfType(VIEW_TYPE).length > 0;
      const termAlive = this.app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE).length > 0; // pty-leak-selfheal-reuse-false: emulator may be undefined on unfocused leaves; leaf existence is enough
      // term-resurrect-fix (now actually wired): the flag was write-only — the
      // watchdog reopened terminals the user had deliberately closed, with focus
      // steal. A user-closed terminal stays closed until a new one re-arms it.
      if (dashOpen && !termAlive && !this._termClosedByUser && h >= 6 && h < 23 && this.app.plugins.getPlugin('terminal')) {
        try { const leaf = await _ensureTerminalLeaf(this.app, { reuse: true }); if (leaf) healed.push('reopened Claude terminal'); } // reuse:true prevents duplicate PTY spawn
        catch (_) {}
      }
    } catch (e) { issues.push('selfheal error: ' + e.message); }
    finally { this._selfHealRunning = false; } // pty-leak-no-concurrency-guard: always release

    this._health = {
      status: issues.length ? 'degraded' : 'ok',
      issues, healed, last: new Date().toISOString(),
    };
    if (issues.length || healed.length) {
      await this._logHealth(`status=${this._health.status} issues=[${issues.join('; ')}] healed=[${healed.join('; ')}]`,
        issues.length ? 'warn' : 'info');
    }
    // refresh any open view so the health dot updates
    for (const l of this.app.workspace.getLeavesOfType(VIEW_TYPE)) {
      if (l.view && l.view._renderHealthDot) l.view._renderHealthDot();
    }
    if (announce) new Notice(issues.length
      ? `⚠ Command Center: ${issues.length} issue(s)${healed.length ? ', ' + healed.length + ' auto-healed' : ''}`
      : `✓ Command Center healthy${healed.length ? ' (' + healed.join(', ') + ')' : ''}`, 6000);
    return this._health;
  }

  // config-dead-08: settings.usage was written here but never read anywhere (no adaptive UI built).
  // Stubbed out so it stops polluting data.json on every click. Call sites left intact.
  trackUsage(_key) {} // no-op

  async openView() {
    const existing = this.app.workspace.getLeavesOfType(VIEW_TYPE);
    if (existing.length > 0) {
      this.app.workspace.revealLeaf(existing[0]);
      return;
    }
    const leaf = this.app.workspace.getLeaf(true);
    await leaf.setViewState({ type: VIEW_TYPE, active: true });
    this.app.workspace.revealLeaf(leaf);
  }

  async loadSettings() {
    const saved = await this.loadData();
    this.settings = Object.assign(
      { activeTab: 'overview', autoAssemble: true, autoMorningBrief: true, lastBriefDate: null, actions: DEFAULT_ACTIONS, roi: {}, orbVisible: false, voice: {}, askHistory: [] }, // config-dead-08: removed usage:{} (trackUsage stubbed; never read)
      saved
    );
    // Merge: if saved has no actions, use defaults
    if (!this.settings.actions || this.settings.actions.length === 0) {
      this.settings.actions = DEFAULT_ACTIONS;
    } else {
      // Append any new default actions (by id) the user hasn't seen yet —
      // keeps customizations but surfaces newly-shipped launchers (e.g. Morning Brief).
      const have = new Set(this.settings.actions.map(a => a.id));
      for (const def of DEFAULT_ACTIONS) {
        if (def.id && !have.has(def.id)) this.settings.actions.push(def);
      }
      // Keep the plugin-managed morning-brief command in sync with shipped prompt
      // improvements (it's not user content — the touch-free constraints matter).
      const mb = this.settings.actions.find(a => a.id === 'morning-brief');
      if (mb) mb.command = '__DIGEST_NOW__'; // migrate saved button off the old terminal Read+Edit prompt → in-plugin spoken digest
      // repoint legacy Jarvis launcher (was the web app) to the in-Obsidian Ultron orb
      const jv = this.settings.actions.find(a => a.id === 'jarvis');
      if (jv) { jv.command = '__ORB_TOGGLE__'; jv.emoji = '🔮'; jv.label = 'Ultron Orb'; }
    }
    // ROI model defaults (balanced) — tunable in settings
    this.settings.roi = Object.assign(
      { hourlyRate: 100, minPer1kOutput: 2.5, minPerSession: 10 },
      this.settings.roi || {}
    );
  }

  // ── Invisible Ink (HS-R2 #5) implementation ────────────────────────────────
  async _invisibleInk(file) {
    const leaf = this.app.workspace.getMostRecentLeaf();
    const host = leaf && leaf.view && leaf.view.containerEl;
    if (!host) { new Notice('No active pane.', 3000); return; }
    if (host.querySelector('.ccc-ink-overlay')) return; // one breath at a time
    const overlay = host.createDiv({ cls: 'ccc-ink-overlay' });
    const status = overlay.createDiv({ cls: 'ccc-ink-status', text: '🌫 breathing on the page…' });
    overlay.addEventListener('click', () => overlay.remove());
    setTimeout(() => { try { overlay.remove(); } catch (_) {} }, 120000); // never linger
    try {
      await new Promise(r => setTimeout(r, 60)); // let the fog paint before sync recall blocks
      const body = (await this.app.vault.cachedRead(file)).slice(0, 20000);
      let apiCtx = '';
      const m = file.path.match(/^Clients\/([^/]+)\//) || file.path.match(/^02_Areas\/Accounts\/([^/]+)\//);
      if (m) {
        try { apiCtx = (await this.app.vault.adapter.read(`_brain_api/account/${m[1].toLowerCase()}/brief.json`)).slice(0, 6000); } catch (_) {}
      }
      let recall = '';
      try { recall = (this.orb && this.orb._semanticContext) ? (this.orb._semanticContext(file.basename + ' ' + body.slice(0, 300)) || '') : ''; } catch (_) {}
      const prompt = [
        `Tony is about to walk into a meeting with this note as his brief. Compare THE NOTE against THE BRAIN CONTEXT and list what the note DOESN'T say:`,
        `- GAPS: facts the brain context knows that the note omits`,
        `- CONTRADICTIONS: places the note disagrees with the brain context`,
        `Rules: max 7 lines total. Each line: "GAP:" or "CONTRA:" + one sentence + "(source: …)" naming where in the context it comes from. If the context adds nothing, output exactly "NOTHING". Never invent facts not present in the context. Plain text only.`,
        '', '── THE NOTE ──', body,
        '', '── THE BRAIN CONTEXT ──',
        apiCtx ? 'account brief.json: ' + apiCtx : '(no account endpoint)',
        recall ? 'semantic recall: ' + recall.slice(0, 8000) : '(recall daemon cold — no recall context)',
      ].join('\n');
      const cp = require('child_process');
      const bin = (this.orb && this.orb._claudeBin) ? this.orb._claudeBin() : 'claude';
      const out = await new Promise((resolve) => {
        cp.execFile(bin, ['-p', prompt], { timeout: 90000, maxBuffer: 1024 * 1024, cwd: this.app.vault.adapter.basePath },
          (err, stdout) => resolve(err && !stdout ? null : String(stdout || '').trim()));
      });
      if (!overlay.isConnected) return; // Tony dismissed mid-breath
      status.remove();
      const panel = overlay.createDiv({ cls: 'ccc-ink-panel' });
      panel.createEl('div', { cls: 'ccc-ink-title', text: '🍋 what this note doesn’t say' });
      const lines = (out && out !== 'NOTHING') ? out.split('\n').filter(l => /^(GAP|CONTRA)/i.test(l.trim())).slice(0, 7) : [];
      if (!lines.length) {
        panel.createEl('div', { cls: 'ccc-ink-line', text: out === 'NOTHING' ? '· nothing — the brain has no extra signal on this note' : '· no readable answer (claude unavailable or recall cold)' });
      }
      lines.forEach((l, i) => {
        const d = panel.createEl('div', { cls: 'ccc-ink-line' + (/^CONTRA/i.test(l.trim()) ? ' ccc-ink-contra' : ''), text: '· ' + l.trim() });
        d.style.animationDelay = (0.25 * i) + 's';
      });
      panel.createEl('div', { cls: 'ccc-ink-hint', text: 'click anywhere to dissolve' });
    } catch (e) {
      try { status.setText('🌫 ink failed: ' + e.message); } catch (_) {}
    }
  }

  async saveSettings() {
    if (this._unloading) return; // save-race-guard: dying instance must not clobber the next one's data.json
    await this.saveData(this.settings);
  }

  /**
   * Open the Command Center view + a Claude terminal at startup.
   * Called once from onLayoutReady (community plugins are fully loaded by then,
   * so no artificial delay is needed).  Wrapped in try/catch — startup must
   * never break Obsidian.
   */
  async _autoAssemble() {
    try {
      // ── 1. Ensure the Command Center view is open ───────────────────────────
      const existing = this.app.workspace.getLeavesOfType(VIEW_TYPE);
      if (existing.length === 0) {
        const leaf = this.app.workspace.getLeaf(true);
        await leaf.setViewState({ type: VIEW_TYPE, active: true });
        this.app.workspace.revealLeaf(leaf);
      }
    } catch (e) {
      console.warn('[CCC] _autoAssemble: could not open Command Center view:', e);
    }

    try {
      // ── 2. Assemble a clean Claude terminal docked below the dashboard ──────
      const terminalPlugin = this.app.plugins.getPlugin('terminal');
      if (!terminalPlugin) {
        new Notice('Command Center: terminal plugin not found — skipping terminal open.', 6000);
        return;
      }

      // term-exit-fix: _autoAssemble also re-runs on every hot-reload of THIS
      // plugin (onLayoutReady fires immediately once layout is ready), so it
      // must never detach live terminal leaves — that killed the user's Claude
      // session on every main.js edit. Only documentation strays are cleaned;
      // an existing terminal is reused, a fresh one opens only if none exists.
      this.app.workspace.getLeavesOfType('terminal:documentation').forEach(l => l.detach());

      // Make the dashboard the active pane so the terminal docks beneath IT.
      const ccc = this.app.workspace.getLeavesOfType(VIEW_TYPE);
      if (ccc.length > 0) this.app.workspace.setActiveLeaf(ccc[0], { focus: false });

      const leaf = await _ensureTerminalLeaf(this.app, { reuse: true });
      if (!leaf) {
        console.warn('[CCC] _autoAssemble: terminal leaf did not appear after open attempt');
        new Notice('Command Center: could not open the Claude terminal — open one manually (terminal plugin).', 8000);
      }
      // (morning brief no longer auto-injected into a terminal — it's the in-plugin spoken digest now)
    } catch (e) {
      console.warn('[CCC] _autoAssemble: could not open terminal:', e);
    }
  }

  /**
   * Auto-run the Morning Brief once per morning (06:00–11:59) on the first launch
   * of the day. Guarded by settings.lastBriefDate so it fires at most once daily.
   */
  async _maybeAutoBrief() {
    return; // DEPRECATED — replaced by the in-plugin spoken digest (orb._maybeDigest). No terminal Read+Edit write path.
    /* eslint-disable no-unreachable */
    if (this._briefRunning) return; // B3: prevent concurrent double-fire (startup + interval)
    this._briefRunning = true;
    try {
      if (this.settings.autoMorningBrief === false) return;
      const now = new Date();
      const h = now.getHours();
      if (h < 6 || h >= 12) return; // morning window only
      const today = localDateStr(now);
      if (this.settings.lastBriefDate === today) return; // already ran today
      const brief = (this.settings.actions || DEFAULT_ACTIONS).find(a => a.id === 'morning-brief')
        || DEFAULT_ACTIONS.find(a => a.id === 'morning-brief');
      if (!brief || !brief.command) return;
      // Ensure a Claude terminal exists (covers the case where Obsidian was left
      // running overnight and crosses into morning without a fresh startup).
      try { await _ensureTerminalLeaf(this.app, { reuse: true }); } catch (_) {}
      // Wait for the Claude shell to actually reach its prompt before injecting —
      // and only mark done AFTER delivery, so a not-yet-ready terminal retries next launch.
      let ready = false;
      for (let i = 0; i < 30; i++) {
        const lv = this.app.workspace.getLeavesOfType(TERMINAL_VIEW_TYPE);
        const v = lv.length ? lv[lv.length - 1].view : null;
        if (v && v.emulator && v.emulator.terminal) { ready = true; break; }
        await new Promise(r => setTimeout(r, 500));
      }
      if (!ready) return; // couldn't deliver — don't burn today's slot
      await new Promise(r => setTimeout(r, 2500)); // let Claude finish booting to its prompt
      await injectIntoTerminal(this.app, brief.command);
      this.settings.lastBriefDate = today;
      await this.saveSettings();
      new Notice('📋 Good morning — running your Morning Brief into today’s daily note.', 7000);
    } catch (e) {
      console.warn('[CCC] _maybeAutoBrief failed:', e);
      this._logHealth('maybeAutoBrief error: ' + e.message);
    } finally {
      this._briefRunning = false;
    }
  }

  onunload() {
    // save-race-guard: a fire-and-forget saveSettings() from this dying instance
    // can land AFTER the next instance's loadData and clobber its settings.
    this._unloading = true;
    // Tear the orb down cleanly (it lives on document.body, outside plugin DOM) —
    // without this, every reload orphans an orb and leaks a WebGL context.
    if (this.orb) { try { this.orb._teardown(); } catch (_) {} }
    // Clear any timer started by _autoAssemble (defensive — _ensureTerminalLeaf
    // uses its own internal await loop, but guard in case future code stores one)
    if (this._autoAssembleTimer) {
      clearTimeout(this._autoAssembleTimer);
      this._autoAssembleTimer = null;
    }
    // term-survive: NEVER detach terminal leaves here. The leaves (and their
    // PTYs) are owned by the 'terminal' community plugin and survive our
    // reload untouched — detaching them killed the live Claude session on
    // every hot-reload (the exact "terminal keeps restarting" symptom).
    // The old "orphaned PTY" worry was wrong: _ensureTerminalLeaf reuses the
    // existing leaf on the next load, nothing accumulates.
  }
}

module.exports = CommandCenterPlugin;

// ── Test harness export (pure helpers only) ───────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports.__test = { last7Days, normalizeBars, DEFAULT_ACTIONS, buildSkillIndex, localDateStr, currentWeekStartStr };
}
