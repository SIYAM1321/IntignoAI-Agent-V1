# v1.9 selectors & workflow reference

Sources: the collected fixture HTML files + live validation on linkedin.com (2026-10-20).
**Saved templates age fast — validate the live page first.** The adapters in
`tools/browser_automation.py` implement the layered fallbacks listed here.

Machine-readable copy: `data/v19_selectors.json`.

## linkedin
### login
- **page**: https://www.linkedin.com/login
- **note**: 2026 design renders a HIDDEN prerendered copy of each auth field (React useId _R_* ids); the visible copy carries autocomplete 'username webauthn'. Never use .first blindly.
- **email_selectors**: `input[autocomplete='username webauthn']` | `input[autocomplete='username']` | `input[type='email']` | `#username`
- **password_selectors**: `input[autocomplete='current-password']` | `input[type='password']` | `#password`
- **submit**: Enter on the password field (locale-independent); fallback visible button matching /sign in|log in/i
- **success_check**: url /feed/ + absence of password inputs

### feed_composer
- **trigger**: `div[role='button']:has(div[aria-label='Start a post'])` | `#draft-text-replaceable-component`
- **editor**: `[data-testid='ui-core-tiptap-text-editor-wrapper'] div[contenteditable='true']` | `div[contenteditable='true'][role='textbox']` | `[componentkey='ShareBox_textEditor']`
- **editor_note**: LinkedIn moved Quill(.ql-editor) -> TipTap/ProseMirror; the editor is INLINE in the feed share box, not a dialog.
- **post_button**: `button[aria-label='Post']` | `button[aria-label*='Post' i]` | `get_by_role(button, name='Post', exact=True)`

### jobs_search
- **url_template**: https://www.linkedin.com/jobs/search/?keywords={q}&location={loc}&f_TPR=r{seconds}&f_AL=true&pageNum=0
- **f_TPR**: r86400 = posted in the last 24 hours
- **f_AL**: true = Easy Apply jobs only
- **cards**: `li[data-occludable-job-id]` | `.jobs-search-results__list-item`
- **card_fields**: {'link': "a[href*='/jobs/view/']", 'title': ['.job-card-list__title', '.artdeco-entity-lockup__title', '.base-search-card__title'], 'company': ['.job-card-container__primary-link .job-card-container__company-name', '.artdeco-entity-lockup__subtitle li:first-child'], 'location': ['.job-card-container__metadata', '.artdeco-entity-lockup__caption li'], 'posted': ['.job-card-container__listed-time time', 'time']}
- **detail**: {'description': ['.jobs-description__content .show-more-less-html__markup', '.show-more-less-html__markup'], 'apply_button': ["button[aria-label^='Easy Apply']", '.jobs-apply-button--top-card'], 'easy_apply_dialog': ["[role='dialog']", '.artdeco-modal', '[data-test-modal]'], 'ats_handoff': "Many 'Easy Apply' labels redirect to an external ATS (URL gains applicantTrackingSystemName, e.g. Lever). The adapter reports ats_external instead of submitting off-platform.", 'steps': ["button[aria-label='Continue to next step']", "button[aria-label='Review your application']", "button[aria-label='Submit application']"], 'errors': ['.artdeco-inline-feedback--error']}

## youtube
### watch
- **like**: `ytd-segmented-like-dislike-button-view-model like-button-view-model button` | `ytd-watch-metadata like-button-view-model button` | `button[aria-label*='like this video' i]`
- **subscribe**: `#subscribe-button ytd-subscribe-button-view-model button` | `#subscribe-button button[aria-subscribed]` | `button[aria-label*='Subscribe' i]`
- **comment_box**: `#placeholder-area` | `ytd-comments-entry-point-header-renderer #placeholder-area`
- **comment_editor**: `#contenteditable-root`
- **comment_submit**: `#submit-button` | `button[aria-label='Comment']`
- **signed_out**: Clicking like/subscribe/comment while signed out opens yt-sign-in-dialog-view-model; the adapter reports requires_login.

### studio_upload
- **reference**: Channel content - YouTube Studio saved pages (upload flow states)
- **flow**: `studio.youtube.com -> Create -> Upload videos` | `select file (input[type=file])` | `title/description (#textbox ytd-setup), playlist/audience steps` | `Next x3 -> Publish/Save`
- **note**: Studio requires a signed-in Google account; Google sign-in must be completed by the human (plain OS window).

## microworkers
### page_shape

### mode

### records

### field_mode

### page_mode

### tools
