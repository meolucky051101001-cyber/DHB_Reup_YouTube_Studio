# G21b — Shorts remix capability

Outcome for YouTube API and Studio: **unsupported_with_reason** for remix changes. Both allow the uploader to leave the provider setting untouched.

| Request | YouTube API | Studio |
| --- | --- | --- |
| Keep provider setting | Omit remix from the request | Leave the Studio setting alone |
| Disable audio and video remixing | Block before upload | Block before upload |
| Allow audio only | Unavailable in the tool | Unavailable in the tool |
| Allow audio and video | Unavailable in the tool | Unavailable in the tool |

The Data API's documented upload and update fields do not expose a remix setting. The native Studio help distinguishes opting out of video remixing from opting out of both media types and describes the latter for partners using Studio Content Manager. The tool has no verified Studio control, eligibility check, or exact-video readback for the complete restriction. [Data API insert](https://developers.google.com/youtube/data/reference/rest/v3/videos/insert), [Data API update](https://developers.google.com/youtube/data/reference/rest/v3/videos/update), [YouTube Help](https://support.google.com/youtube/answer/10623810?hl=en)

Canonical `remix=disable` and legacy `disable_shorts_remix=1` both require the full restriction. Preflight returns `UPLOAD_REMIX_UNSUPPORTED` for Shorts and regular videos on either backend. Save retains the requirement locally. To withdraw the requirement, the operator can select **Giữ thiết lập YouTube** in the upload options, confirm, and preview again. Legacy zero and canonical keep leave the YouTube setting untouched.

Local acceptance exercises both API entry points, direct Studio entry, dispatcher and preflight for the restriction. Existing queue/worker and mixed-backend Monitor tests exercise rejection before provider work. The configuration test saves an explicit request through the local API and verifies the preview still blocks. No authenticated Studio session or provider write is part of these checks.
