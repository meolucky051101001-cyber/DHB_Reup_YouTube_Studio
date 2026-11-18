# G19 API metadata verification

G19 adds offline-testable YouTube Data API safeguards without enabling video-audio language writes. The implementation validates a selected category before opening a resumable upload session, requests `snippet` on exact-ID readback, and preserves a bounded evidence event as soon as YouTube returns a video ID.

The catalog request uses `videoCategories.list` with `part=snippet` and `id=<selected category>`. It requires one matching item whose `snippet.assignable` is exactly `true`. Missing, duplicate, malformed, non-assignable, HTTP-error and timeout responses stop before the resumable upload `POST`; the code does not guess a region or fall back to category `22`.

The readback request uses `videos.list` with `part=snippet,status,processingDetails` and the exact returned video ID. Category is compared through `snippet.categoryId`. An explicitly expected audio language is compared through `snippet.defaultAudioLanguage`; `snippet.defaultLanguage` remains the title/description language and is never used as an audio-language substitute. Current offline capability remains gated because the fetched `videos.insert` documentation lists writable `snippet.defaultLanguage` but does not list `snippet.defaultAudioLanguage`; the upload payload never maps the title/description language field to audio language.

The optional evidence sink receives only bounded, structured events:

```json
{
  "backend": "youtube_api",
  "video_id": "11-character-id",
  "verification_state": "received|verified|mismatch|unknown",
  "expected": {"category_id": "22"},
  "observed": {"category_id": "22"},
  "code": "API_VIDEO_ID_RECEIVED"
}
```

The first `received` event is emitted immediately after a valid ID is decoded from either a final media response or a resumable status response, before progress callbacks or metadata readback. Readback failures emit `mismatch` or `unknown` and never start another upload. Sink failures stop the flow so the caller can retain the uncertain sent state. Evidence does not contain OAuth tokens, cookies, headers, resumable session URLs or raw provider response text.

Public sources checked 2026-11-15:

- https://developers.google.com/youtube/data/reference/rest/v3/videos
- https://developers.google.com/youtube/v3/docs/videos/insert
- https://developers.google.com/youtube/v3/docs/videos/list
- https://developers.google.com/youtube/v3/docs/videoCategories
- https://developers.google.com/youtube/v3/docs/videoCategories/list

The saved source excerpts and observed dates are recorded privately in `review_2026-11-15/implementation/g19-api-docs-notes.md`.
