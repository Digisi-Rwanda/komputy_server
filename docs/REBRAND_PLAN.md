# Komputy rebrand plan

Goal: the product is called **Komputy** everywhere (komputy.com), with no trace of the old names "Ledgerline",
"ECL Platform" or "ECL-Server/ECL-Web" as the product name, no em dashes (U+2014) or en dashes (U+2013) anywhere, our own
logo as the browser icon, and clean, honest metadata.

**ECL stays where it names the tool.** "Expected Credit Loss (ECL)", "ECL runs", `/ecl/...` routes, the `ecl`
service id and `ecl/domain` code are the IFRS 9 module of Komputy and keep their names. ECL is never used as the
name of the whole product.

## 1. What the scan found (2026-11-15)

| Item | komputy_web | komputy_server |
|---|---|---|
| "Ledgerline" (brand text) | 125 lines, 57 files | 9 lines, 8 files |
| "ledgerline" (identifiers, package, emails, URLs) | 31 lines, 19 files | 497 lines, 95 files (mostly the Python package name) |
| "ECL-Server" / "ECL-Web" (old repo names) | 27 lines, 22 files | 5 lines, 4 files |
| Em dash (U+2014) | 154 lines, 78 files | 91 lines, 27 files |
| En dash (U+2013) | 51 lines, 39 files | 25 lines, 11 files |
| Browser icon | `src/app/favicon.ico` is the default Next.js icon | n/a |
| "ECL Platform" tab title | Not in our code: that tab was the old Vercel site (ECL-Web), now replaced by the DNS switch | n/a |

Other problems found on the way, fixed in the same pass:

1. **Third-party images as our share previews.** `public/brand/og-preview.jpg` (2.4 MB) and `og-landing.jpg` come from
   framerusercontent.com, so a link to komputy.com shared on WhatsApp, LinkedIn or Slack shows someone else's artwork.
2. **`public/inspiration/` (3.3 MB) is published.** Reference screenshots and other companies' logos are served from
   komputy.com.
3. **`/dev/assets` is reachable in production.** It is an internal asset gallery.
4. **Demo identities and URLs** in seed and mock data use `@ledgerline.app` and `app.ledgerline.com`.
5. **Public contact addresses** on the Contact, Legal and Privacy pages are `sales@`, `support@`, `security@`,
   `privacy@ledgerline.app`, a domain we don't own: those emails would be lost.

## 2. Naming rules (the reference for every edit)

| Context | Use |
|---|---|
| Product name in text, titles, emails | **Komputy** |
| Tagline | IFRS 9 and IFRS 16 reporting, from upload to sign-off |
| Metadata title | `Komputy` default, template `%s · Komputy` |
| Metadata description | IFRS 9 expected credit loss and IFRS 16 lease accounting for banks and finance teams, from upload to sign-off. |
| Lowercase identifiers | `komputy` (cookie prefix, package names, file names, JWT issuer `komputy-web`, audience `komputy-engine`) |
| Python package | `komputy` (`src/komputy/...`), distribution `komputy-server` |
| Repos and services | `komputy_web`, `komputy_server`, Render `komputy-*` (already done) |
| Email sender | `Komputy <no-reply@komputy.com>` |
| Public addresses | `sales@komputy.com`, `support@komputy.com`, `security@komputy.com`, `privacy@komputy.com` |
| Demo/test data | `@komputy.com` staff, `app.komputy.com` style links become `komputy.com/...` |
| The IFRS 9 module | "Expected Credit Loss (ECL)" or "ECL" (unchanged) |
| The old engine in comments | "the engine" or `komputy_server` (never ECL-Server) |

**Dashes.** Every em dash and en dash is replaced by what the sentence needs:

| Use of the dash | Replacement | Example |
|---|---|---|
| Aside or explanation in a sentence | comma, colon, period or parentheses | "Read-only [em dash] this workspace is suspended" becomes "Read-only: this workspace is suspended" |
| Title separator | middle dot `·` or colon | "Komputy [em dash] IFRS reporting" becomes "Komputy · IFRS reporting" |
| Empty value in a table or field | the word that fits ("None", "Not set", "No date") or a plain hyphen `-` in dense numeric tables | `[em dash]` in an empty ECL cell becomes `-` |
| Number or date range | "to" in text, hyphen in compact UI | "1[en dash]5 days" becomes "1 to 5 days" |
| Code comments and docs | same rules | no exceptions |

The dash pass is done **by hand, file by file**, not by a blind search-and-replace, because each case needs the right
punctuation. A CI check then keeps them out for good (section 4, step R7).

## 3. Logo and icons

The Komputy mark is the existing symbol in `src/components/atoms/Logo.tsx` (three offset bars). It becomes the
single source for every icon:

| File (Next.js App Router conventions) | Content |
|---|---|
| `src/app/icon.svg` | The mark, with a `prefers-color-scheme` style so it stays visible on light and dark browser tabs |
| `src/app/favicon.ico` | 16, 32 and 48 px renders of the mark (replaces the Next.js default) |
| `src/app/apple-icon.png` | 180 px, mark on the brand background (iPhone home screen) |
| `src/app/manifest.ts` | Name "Komputy", short name, theme colours, 192 and 512 px icons |
| `src/app/opengraph-image.tsx` + `twitter-image.tsx` | Generated 1200x630 share card: mark, "Komputy", tagline, komputy.com, in the site's fonts and colours (no third-party images) |
| `Logo.tsx` | Wordmark text "Komputy", `aria-label` "Komputy" |

Icons are rendered once from the SVG with a small script (`scripts/build-icons.mjs` using `sharp`, already installed),
and the outputs are committed.

## 4. Work plan (in order; each step ends with its check)

**R1. Metadata and icons (web).** Root `layout.tsx` (title, template, description, `applicationName`, `openGraph`
site name, `twitter`), marketing page metadata, `robots.ts`, `sitemap.ts`, RSS feed title, `manifest.ts`, new icons
and share images, delete the Framer images. *Check:* view source of `/`, `/sign-in`, `/services/ecl` shows Komputy
titles, our icons and our share image; the share card renders at `/opengraph-image`.

**R2. Brand text (web).** Every visible "Ledgerline": logo wordmark, header and footer, marketing copy, legal pages,
auth screens, emails (`src/server/email/*`), notification texts, onboarding, help, platform admin, page titles,
toasts, downloads (`ledgerline-backup-codes.txt` becomes `komputy-backup-codes.txt`, `ledgerline-runs-*` exports
become `komputy-runs-*`). Contact and legal addresses move to `@komputy.com`. *Check:* `grep -ri ledgerline src`
returns nothing.

**R3. Identifiers (web and server together, one release).**

- Better Auth cookie prefix `ledgerline` → `komputy`. Everyone is signed out once, which is fine before launch.
- JWT issuer `ledgerline-web` → `komputy-web` and audience `ecl-engine` → `komputy-engine`. Change both sides in the
  same release; tokens live 5 minutes.
- Database `application_name`, the SSE channel comment and log fields.
- Old `ECL-Server` mentions in comments become "the engine".

*Check:* sign-in works, and an engine call from the web succeeds after deploy.

**R4. Python package (server).**

1. `git mv src/ledgerline src/komputy`.
2. Rewrite imports (`ledgerline.` → `komputy.`).
3. Update `pyproject.toml` (name `komputy-server`, scripts, ruff per-file paths), `alembic/env.py`, `Dockerfile`,
   `render.yaml` worker commands (`--app=komputy.queue.app`), `Makefile`, the CI workflow and `README.md`.
4. Rename the test database to `komputy_engine_test` and update the docs.

The approved calculation code in `ecl/domain` and `leases/domain` changes **only in its import lines** (13 files),
never in a formula. The diff is checked to prove it: every changed line in those folders must be an import.

*Check:* all 142 tests pass, ruff is clean, and the Docker image builds.

**R5. API contract and typed client.** Regenerate `contracts/openapi.json` (title "Komputy Engine"; schema names
change from `ledgerline__apps__...` to `komputy__apps__...`), regenerate `src/server/engine/schema.d.ts`, and update
the two web files that use those schema names. *Check:* web typecheck passes.

**R6. Data and fixtures.** Seed and mock data (`src/data/mock/*`, `scripts/seed.mts`): staff emails `@komputy.com`,
links `komputy.com/...`, demo password `Komputy-Demo-2026`. e2e scripts updated to match. Production holds no demo
data, so only test data changes. *Check:* the 3 e2e suites pass locally on a fresh database.

**R7. Dashes, everywhere.** Hand edit of all ~321 lines with the rules in section 2 (UI text first, then
emails, docs, comments, tests). Then a guard so they never come back: a CI step in both repos fails on `[em dash]` or `[en dash]` in
source and docs (`grep -rIn "[[em dash][en dash]]" src docs ...`). *Check:* the guard passes in CI.

**R8. Production hygiene.**

- Remove `public/inspiration/` from the published site (keep it outside `public/` if still wanted for reference).
- Make `/dev/*` return 404 in production.
- Remove `public/ASSETS.md` from the public folder.

*Check:* `https://komputy.com/inspiration/...`, `/dev/assets` and `/ASSETS.md` all return 404.

**R9. Deploy and verify live.** Push both repos (CI-gated auto-deploy). On https://komputy.com:

- the browser tab shows the Komputy icon and title;
- the page source has no "Ledgerline", "ECL Platform", `[em dash]` or `[en dash]`;
- the share preview shows the Komputy card (checked with the Open Graph debugger output of `/opengraph-image`);
- sign-in, a password-reset email (sender "Komputy") and an engine call all work.

A final scan of both repos and of the rendered HTML of the main public pages must return zero hits for the old
names and the dashes.

**R10. Housekeeping.** Add a one-line naming rule to the README of both repos ("Komputy; ECL only for the IFRS 9
module; no em/en dashes"), and update `DEPLOYMENT_PLAN.md` and `RUNBOOK.md`.

## 5. What stays as it is, on purpose

- The IFRS 9 module name **ECL** (`/ecl` routes, `ecl` service id, "ECL runs", `ecl/domain`).
- The Render resource suffix `-woqc` (part of each resource's identity, see RUNBOOK).
- Database tables and columns (no table carries the old brand name).
- Third-party package names in lock files.

## 6. Risks and how they're handled

| Risk | Handling |
|---|---|
| Users signed out by the cookie rename | Pre-launch, only the admin account exists; announced in the release note |
| Web and engine disagree on the JWT issuer during deploy | Engine accepts both old and new issuer for one release, then the old one is removed |
| Package rename breaks a worker command | `render.yaml`, Dockerfile and CI change in the same commit; the Docker build and a worker start-up are checked locally first |
| A dash replaced with the wrong punctuation | Hand edit and review of every line, not a blind replace |
| The calculation code changes by accident | Diff check on `ecl/domain` and `leases/domain`: only import lines may differ; the 130 domain tests must pass |

Estimated size: about 230 files touched across both repos, delivered as one coordinated release after all checks pass.
