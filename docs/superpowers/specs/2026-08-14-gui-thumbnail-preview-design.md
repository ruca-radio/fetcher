# GUI Thumbnail Preview Design

## Goal

Make search thumbnails readable and use the bottom preview area without rendering artifacts. Clicking a result pins its thumbnail, hovering temporarily previews another result, and starting playback replaces the thumbnail with video.

## Root Causes

- Search thumbnails are supplied through `DecorationRole`, but the table retains Qt's default approximately 30-pixel row height, clipping 16:9 images.
- `MediaPlayerWidget` keeps a native `QVideoWidget` visible even when no media is playing. On real display backends this surface can retain stale or undefined frames while pages repaint.
- Thumbnail loading terminates in `SearchResultsModel`; no signal carries the selected or hovered thumbnail to the bottom preview.

## Search Table

The result table will use a consistent `160 × 90` thumbnail display size. Its vertical header will use a fixed section height sufficient for the image and cell margins, approximately 98 pixels. The title column will stretch into remaining horizontal space.

Thumbnail pixmaps will be scaled once with preserved aspect ratio and smooth transformation before display. Result identity remains the composite `(site, id)` key so providers with matching local IDs do not collide.

## Preview Surface

`MediaPlayerWidget` will replace its always-visible video surface with a stacked display:

1. A black thumbnail/placeholder label is the default page.
2. The existing `QVideoWidget` is shown only when a playable source is intentionally opened.

The thumbnail label will preserve image aspect ratio and rescale its source pixmap when the widget changes size. With no image, it will show a stable placeholder instead of exposing the native video surface. Both display pages will have an explicit black background.

`show_thumbnail(video, pixmap)` will stop or pause stale playback as appropriate, select the thumbnail page, and show either the image or a loading/unavailable placeholder. `set_source()` will retain its existing media setup behavior and select the video page.

## Interaction

- Clicking or selecting a result pins that result as the persistent thumbnail preview.
- Hovering a different result temporarily displays its thumbnail without changing selection or resolving media.
- Leaving the result table restores the pinned result thumbnail.
- If an active result's thumbnail has not loaded, the preview displays a loading placeholder. When that thumbnail arrives asynchronously, the active preview updates only if its `(site, id)` still matches the selected or hovered result.
- The Preview button and existing explicit playback action resolve the media URL, then switch the same preview surface to video.
- Selecting another result continues to invalidate stale media-resolution work.

## Data Flow

`DiscoverPage` will track selected and hovered composite identities. It will emit a focused-result signal containing the `Video` and its current `QPixmap` or no image. `MainWindow` will connect this signal to `MediaPlayerWidget.show_thumbnail()` while retaining the existing resolved-source connection to `set_source()`.

Mouse tracking and the table's `entered` signal provide hover updates. A viewport leave event restores selection. Thumbnail completion checks the current search generation and composite identity before updating either the table or preview.

## Error Handling

Thumbnail download or decoding failure leaves a deterministic unavailable placeholder. It does not activate the video surface or interrupt search interaction. Stale asynchronous thumbnail and media responses remain ignored through generation and identity checks.

## Testing

Automated Qt tests will verify:

- the configured icon size and actual row height fit a `160 × 90` thumbnail;
- clicking a row displays its thumbnail without invoking media resolution;
- hovering displays the hovered result and leaving restores the pinned result;
- late thumbnail arrival updates the active preview;
- equal IDs from different providers display the correct thumbnail;
- the player initially shows the placeholder/thumbnail page;
- showing a thumbnail hides the native video page;
- setting a playable source switches to the video page;
- the full existing suite remains green.

## Scope

This change does not redesign the result table as cards, add animated thumbnail playback, or change provider/network behavior. It is limited to thumbnail sizing, preview interaction, and video-surface lifecycle.
