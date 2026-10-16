# GUI repair and download performance

## Goal
Restore the existing desktop GUI, improve the shared downloader where evidence supports it, and evaluate the suggested browser extension without discarding current work.

## Next Step
PornHub search repair is verified; restart Fetcher to load the updated parser.

### Phase 1: Diagnose and review
**Status:** complete
- Inspect existing changes, launchers, GUI services, and transfer paths.
- Run baseline tests and reproduce the visible failure.
- Inspect the linked extension and identify relevant techniques.

### Phase 2: Fix with regressions
**Status:** complete
- Add failing regression tests for confirmed bugs.
- Make bounded fixes shared by GUI/TUI where appropriate.
- Document behavior and dependency changes.

### Phase 3: Verify
**Status:** complete
- Run the complete relevant suite.
- Exercise the rendered GUI and a controlled media download.
- Report measured results and remaining external limitations.

## Verified outcomes and remaining limits
- 242 tests and 8 subtests pass, including the real Chrome runtime-player fixture.
- Final Wayland GUI smoke: successful 720p queue download, deliberate 404 with full details, playback continues through hover and late thumbnails.
- Native versus accelerated byte-identical local HTTP benchmark: 4.273s versus 2.016s (2.12x).
- Live production-service Eporner transfer completed at 2160p, 477847673 bytes, 189.869 seconds.
- Wheel and source distribution build; wheel includes new core/GUI modules and excludes runtime media/cookies/state.
- External unresolved cases: sampled PornHub CDN returns 410, xHamster exposes no stream in headless Chrome, sampled RedTube page supplies 240p only and fails the existing 720p minimum. These are not reported as repaired provider downloads.

## Constraints
- Preserve pre-existing tracked and untracked changes.
- Keep credentials, cookies, media, and runtime artifacts outside publishable source.
- No commits, publishing, or service restarts required.

## Errors Encountered
- Quoted the GitHub tree query after zsh treated its question mark as a glob.
- Extension transfer-core repository returns 404; integrate independently implemented page-data extraction instead.
- SettingsStore probe initially omitted required constructor arguments; corrected using AppSettings defaults.

## PornHub search correction

- Diagnosis: complete. Whole-page cards, loose metadata windows, and non-search redirects reproduce unrelated results.
- Regression tests: complete. Ten initial cases failed before repair; three legacy API cases failed before validation was tightened.
- Parser repair: complete. Parse individual cards in videoSearchResult; validate response host, path, query, and status.
- Verification: complete. Full suite: 257 passed and 8 subtests passed. Live search: 31 unique results with titles, durations, and thumbnails; no provider errors. Reviewed scope and parser boundaries; git diff --check passed.
