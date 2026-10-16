# Findings

- Working tree already contains substantial GUI, shared-core, and test changes. Preserve them.
- Both X11 and Wayland display variables are available; Xvfb is installed for isolated GUI testing.
- ffmpeg is installed; aria2c is not currently on PATH.
- No relevant prior-run memory matched this repository.
- User-suggested extension is a Chrome Manifest V3 application that extracts stream URLs in page context. Source inspection pending.
- Baseline: 219 tests and 8 subtests pass. A production-composed Wayland GUI renders and a real queued 720p HTTP fixture completes successfully.
- Live provider probes: PornHub has no formats, RedTube cannot extract media, and xHamster's short search URLs do not match the installed extractor. Eporner resolves and transfers media.
- The linked extension reads runtime window player objects; its transfer core submodule is unavailable (GitHub 404), so do not vendor the extension or obfuscated core.
- Every shared-manager progress notification currently serializes, fsyncs, and replaces the entire job history under the manager lock. This can impede frequent progress updates and parallel transfers.
- Source checkout settings currently request 10 simultaneous downloads and four HLS fragments. Other existing downloader processes are user-owned and must be preserved.
- Regression RED/GREEN fixes cover stop launching retries, per-progress history fsyncs, browser extraction after no-formats failures, multiline/invalid player data, aria2 progress, and download selection/error presentation.
- Controlled 8 MiB byte-verified HTTP benchmark: native 4.273s; four aria2 connections 2.016s (2.12x overall). The first benchmark caught a protocol-selector mistake; parsing real yt-dlp options now has regression coverage. Five range requests were observed in the accelerated case.
- A Wayland GUI smoke run queued two real HTTP jobs: 720p success and deliberate HTTP 404 failure. Full failure text rendered, selection remained stable, and the successful file played with a 2000ms duration.
- Live RedTube search API omits all quality/HD fields. Fixed the adapter's incorrect inference that missing metadata means 480p; the normal HD search now returns 20 results for the probe query.
- Completed-but-rejected transfers previously retried the entire file through weaker formats. They now retain the final validation/placement failure without another download.
- PornHub runtime HLS sample returned CDN HTTP 410; its media API returned an HTML page rather than JSON. xHamster runtime player settings were null in the sampled headless page. These are unresolved external limitations, not successful downloads.
- Bounded live transfer through production GUI services completed an Eporner video: 477847673 bytes, 2160p, 189.869 seconds. First RedTube fallback downloaded a 240p player preview and correctly rejected it under the 720p policy. Updated extraction to read the runtime quality API rather than using current playback quality; local real-Chrome fixture proves it selects 1080p from the API.
- CodeRabbit reviewed tracked working-tree changes and raised three issues: hover interrupting playback (major), non-daemon search shutdown (minor), and dropped censored-filter preference (minor). All were reproduced with failing tests and fixed. New core modules are not included by CodeRabbit's uncommitted scan and receive local regression/runtime verification and self-review.

## PornHub search correction

- Controlled HTML probes reproduce recommendations entering results, nearby banner metadata being assigned to a video, and generic video-page redirects being accepted.
- Selecting PornHub invokes only its provider. Baseline provider suite: 7 passed.
- Live search uses a ul with id videoSearchResult. Native cards contain nested elements and lazy thumbnails, so the parser now uses the standard HTML parser and keeps card boundaries.
- Legacy API responses also lacked host, query, and HTTP-status validation; regression tests proved and cover this path.
- Live repaired provider: 31 unique rows, all with titles, durations, and thumbnails, all URLs on PornHub, and no provider errors.
