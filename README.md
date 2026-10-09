# Loopi — YouTube crochet tutorial → written pattern PDF

Paste a YouTube URL from a channel you own. Loopi pulls YouTube's own caption track, has Claude
rewrite the spoken tutorial as a proper US-terminology crochet pattern, decides per step where the
reader needs a photo and at which second, screenshots the video there, lets you review every step
and frame in a web UI, and prints an A4 PDF.

- **Port** 3300 — `http://localhost:3300`
- **Start** `./start.sh` (creates `.venv` and installs on first run)
- **Stack** Python 3 stdlib HTTP server, SQLite, `yt-dlp`, bundled ffmpeg (`imageio-ffmpeg`),
  Anthropic SDK, Playwright Chromium for PDF, zero-build vanilla front end
- **Data** `data/bobbin.db` + `data/projects/<id>/` (video.mp4, thumb.jpg, captions.json3, frames/, uploads/, pattern.pdf) + `data/library/` (a copy of every rendered PDF, named from the title and date; survives project deletion, this is the folder to back up) + `data/brand/` (logo)
- **Auth** none, binds 127.0.0.1
- **Claude engine** — two options, switched in the UI footer, with a model per job (clean / extract / frame pick):
  - **Claude Code** (default): shells out to the local `claude -p` CLI, so usage counts against the
    Claude subscription. Auth is either the CLI's own login (run `claude` anywhere once and sign in; it is per user, not per folder) or a long-lived token: run `claude setup-token`, paste the result into `LOOPI/.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`, restart. The token is passed to every child `claude` process.
    Defaults (Farhat's decision, 2026-09-19): Opus 5 for every job, effort `high` for clean and extract, `low` for frame picks. Each job has a model and an effort dropdown. Opus 5 variants from the Claude Code docs: `opus` (alias, resolves to Opus 5 on Max), `claude-opus-5`, and the 1M-context `opus[1m]` / `claude-opus-5[1m]`. Effort levels on Opus 5: low, medium, high, xhigh, max.
  - **API key**: Anthropic SDK with `ANTHROPIC_API_KEY` (`start.sh` reads it from `~/.zshrc` if unset).
  "Test engine" in the footer runs a one-line probe. `start.sh` unsets `ANTHROPIC_BASE_URL`, which
  Claude Code exports into child shells and which otherwise misroutes API calls.
- **Log** `/tmp/loopi.log` when started detached. Stop with `kill $(lsof -tiTCP:3300 -sTCP:LISTEN)`.

## Renamed 2026-10-01

BOBBIN is now **Loopi**. Folder `AGENTS_NEST/BOBBIN` → `AGENTS_NEST/LOOPI`, page title, logo, messages and docs all moved. Deliberately unchanged, same rule as the MEERKAT/CHAPERONE rename: the Python package folder `bobbin/` and the database file `data/bobbin.db`. Env vars are now `LOOPI_HOST` / `LOOPI_PORT`. Log is `/tmp/loopi.log`.

## Pages

- **Library** (`#/`) — one tile per tutorial: thumbnail, pattern title, channel, skill level, link to the original video, stage badge, PDF count, Open / PDF / Re-render. Search box. Polls while anything is running.
- **New pattern** (`#/new`) — YouTube URL, image density, and three optional upload slots: featured image (PDF cover) and two final images (finished object). A URL already in the library opens the existing project instead of re-downloading.
- **Pattern workspace** (`#/p/<id>`) — stage stepper, your three image slots, video scrubber, checks, front matter, every step with its frames and alternates, finished-object frames, transcript, PDF version history.
- **Settings** (`#/settings`) — Brand (author name printed on the cover and in the page footer, optional logo, optional footer line), Claude engine (engine, model and effort per job, test button), library folder listing.

## Parallel runs and YouTube limits

- Up to N patterns process at once (Settings → Claude engine → Parallel, default 3, max 6). Each project has its own folder, database row and Claude processes, so parallel runs are independent and get the same models, effort and prompts as a single run.
- Video fetches are serialized: only one project talks to YouTube at a time; the others show "Waiting for another video to finish downloading".
- Captions are one request: yt-dlp reads the video info once, Loopi picks a single English track (creator's own first, else YouTube's original ASR) and fetches that json3 file with a Chrome fingerprint (`curl_cffi`). The file is cached, so a retry never re-downloads it.
- Order is info, thumbnail, video, then captions. If YouTube blocks captions (HTTP 429, an IP-level block on the caption endpoint that can last hours), the project goes to **waiting** instead of error and retries by itself after 15, 30, 60 and 120 minutes. "Try now" in the pattern header forces an immediate attempt. Waiting projects are re-armed after a server restart.
- yt-dlp runs with Node as its JS runtime (`--js-runtimes node:...`, via `yt-dlp-ejs`), `curl_cffi` for impersonation, request pacing, and on HTTP 429 it waits 20 s, 60 s, then 150 s before giving up with a clear "wait and Re-fetch" message.
- Default model is Claude Opus 5.5, pinned as `claude-opus-5-5`. It needs Claude Code CLI 2.1.280 or newer; if Test engine reports a version error, run `claude update`.

## Storage

Each downloaded video is 200-500 MB and stays in `data/projects/<id>/video.mp4`. Settings → Storage shows usage and offers two clean-ups, each behind a confirm: delete unfinished downloads (`video.*` leftovers from failed downloads), and delete videos of patterns that are Ready and already have a PDF. Patterns, frames and PDFs are never deleted. If a frame is regrabbed later, the video is downloaded again automatically; no Claude work is redone. Downloads refuse to start below 5 GB free (`MIN_FREE_GB` in `bobbin/youtube.py`), and a failed download removes its own partial files.

## YouTube sign-in (captions fallback)

Settings → YouTube sign-in. "Open sign-in window" launches Chrome with its own profile (`data/yt-chrome`), separate from everyday Chrome. Sign in there, quit it with Cmd+Q, press "Save sign-in": the YouTube/Google cookies are exported once to `data/yt-cookies.txt` (mode 600). macOS asks once for access to "Chrome Safe Storage".

The sign-in is used for exactly one request per video, the caption file, and only after the guest request got HTTP 429. Metadata and video downloads stay anonymous. yt-dlp's docs warn an account used with it can be limited or banned; a spare Google account is safest. Farhat chose to use this on 2026-09-26 after the guest caption block persisted through all retries. "Sign out" deletes both the cookies file and the profile.

## Voice rules (the pattern stands alone)

- No mention of the video, tutorial, creator, camera, screen or transcript anywhere in the pattern text. The validator flags any that slip through.
- Gaps the creator skipped are judged, not filled blindly: standard skills (fasten off, weave in ends, magic ring, turning chains) get one line at most; components usually bought or chosen (straps, rings, clips, zips, snaps, linings, ribbon) are named as the maker's choice in one line, never written as a crochet procedure; only genuinely pattern-specific steps get written out. If in doubt, say less.
- Maker's-choice numbers are phrased as a choice with a way to decide them, never as "not given". Unknown stitch counts stay null and the instruction is written to work without one.
- **Fix wording** (button in the Checks card) runs one Opus pass that applies these rules to an existing pattern's summary, notes, finishing, materials and step text while keeping every step id, frame and timestamp. Use it on patterns extracted before a prompt change instead of re-extracting.

## PDF rules

- Front matter always has Materials, Finished size, Gauge (when known), Yarn substitution, Abbreviations, Notes.
- A pattern is never rendered unreviewed: if `pattern.review` is missing (patterns made before 2026-10-06), Render runs the quality review first (a few minutes). The review is fully automatic, there is no button (Farhat, 2026-10-06); it never re-runs on a reviewed pattern, so manual edits made afterwards are kept.
- Each section heading is kept together with its first instruction (`.head-keep`); `break-after: avoid` on the heading alone made Chromium print the heading on top of "Rnd 1".
- An instruction that repeats its own label ("Rnd 2: Rnd 2: ...") is printed once.

- Cover image: uploaded featured image → YouTube thumbnail → best AI final frame.
- Finishing images: uploaded finals (max 2) replace the AI final frames unless "keep AI finals" is ticked.
- Cover shows title, summary, a skill-level pill and the brand name/logo. No channel attribution, no terminology line, no disclaimer footer.
- Image grids are balanced: 4 → 2+2, 5 → 3+2, never a single orphan.
- Every render is recorded in `pdf_versions` (pages, size, engine settings) and copied to `data/library/`.

## Pipeline

| Stage | What happens | Where |
|---|---|---|
| fetch | yt-dlp: metadata + creator chapters, English caption track (manual preferred, auto fallback) as json3 with word offsets, video ≤720p | `bobbin/youtube.py` |
| clean | Claude merges caption fragments into sentences with faithful start/end, fixes crochet mishearings, resolves self-corrections, tags each sentence (instruction, count, materials, tip, structure, meta, chatter, sponsor). Chunks of 320 cues. | `bobbin/llm.py` `clean_captions` |
| extract | Claude writes the pattern JSON: front matter, sections, steps in standard notation with stitch count and delta, per-step image tier (none / anchor / sequence / walkthrough) and exact timestamps + descriptions for each image, final-object timestamps. Always US terms; UK is converted and flagged. | `bobbin/llm.py` `extract_pattern`, `glossary.md` |
| validate | Deterministic: previous count + delta = count, undefined abbreviations, numbering gaps, empty steps. Flags only, never fixes. | `bobbin/validate.py` |
| frames | For every image: 8 frames from t−3s to t+4s, Claude vision picks the one matching the description. Alternates kept for the review UI. | `bobbin/frames.py`, `bobbin/jobs.py` |
| quality review | After frames: Opus 5.5 reads the pattern, the whole transcript, the video description and up to 18 chosen frames (Read tool), and fixes what a buyer cannot guess: yarn weight category + texture, hook in mm + US size, eye sizes in mm (stated values win; otherwise inferred and marked "approx."), finished size, yarn substitution, gauge line, open-ended counts ("2 or 3 rounds", "about 10 sts", "as many as you want"), label/count arithmetic, placement, text contradicting the photos, glued words. Then the deterministic quality checks run; anything still flagged gets a second targeted pass. Every fix is stored in `pattern.review.fixes` and listed in the Checks card. Colour choices and colour-change wording are out of scope (Farhat, 2026-10-06). | `bobbin/review.py`, `bobbin/llm.py` `review_pattern`, `bobbin/validate.py` `quality` |
| review | Edit any field, change tiers, pick another candidate, regrab a burst or an exact second, grab at the video playhead, upload your own step / featured / final photos. Every edit is logged in `review_log`. | `static/app.js` |
| render | HTML (`templates/pattern.css`) → PDF via headless Chromium. | `bobbin/render.py` |

Stages can be re-run from any point with the buttons at the top of a project. Changing density
(concise / standard / beginner) shifts every image tier one notch and needs a re-extract.

## Design decisions worth not re-litigating

- **YouTube captions, not local Whisper.** The auto track carries per-word offsets and the creator's
  own manual track when present. No audio pipeline needed.
- **Image count is decided per step, not per pattern.** A one-minute video with one hard move gets a
  six-frame walkthrough on that move and nothing elsewhere. Claude judges from the transcript; the
  strongest signal is time spent per stitch.
- **Claude names the second, then vision picks the frame.** Speech precedes the result on screen by a
  few seconds and hands are often mid-motion, so a single frame at the spoken time is a coin flip.
  The burst costs ~8 small images per chosen frame.
- **Stitch math is checked deterministically.** Claude outputs count and delta; the validator does
  the arithmetic. A model that is wrong about a count is caught before the PDF exists.
- **Never invent a count.** If the creator never says it, the pattern says so in notes.

## Not built yet

- OCR of on-screen instruction overlays as a second source of truth (many creators burn the pattern
  text into the video; that card outranks the speech when present).
- Multi-video patterns (part 1 / part 2).
- Using the `review_log` to auto-tune tier prompts from accumulated corrections.
- OCR is doubly worth it here: the sample video burns every instruction into a pink card on screen.
