# Staging checklist: `wayakit_website_mx_b2b`

odoo.sh build of branch `mainTest`, website **B2B MX** (company **Wayakit SAPI de CV**, Mexico, language `es_MX`).
Tick each box and write down the result. If something fails, write down the input, what you expected and what you got, then send it to the developer.

> **Before you start**
> - **Staging = branch code + a copy of the PRODUCTION DB.** Anything you edit here (pages, website settings, domain, test leads) **is destroyed on the next build**. A new push to `mainTest` starts again from a fresh prod copy, so the "edit survives an upgrade" test (section 8) must use **Upgrade inside the same build**, not a new push.
> - **The Odoo MCP connector points to PRODUCTION.** Section 0 is the only part done through MCP (read-only, by Claude). Everything else is manual in the staging UI.
> - B2B MX has **no domain**, so in staging you reach it through the backend website switcher (section 2). The other websites' real domains (`ksasystem.wayakit.com`, `sistema.wayakit.com`) are **PRODUCTION**: never open, curl or submit forms on them during this test.
> - Use only **fake test data** in the forms: company "B2B Test SA", name "Prueba QA", email `b2b.qa+1@example.com` (change the number for each submission), phone `5500000000`. Never real customer data, and never your own email (website_crm links the lead to the logged-in user's partner when the email matches).
> - Placeholders: `$STAGING_URL` = URL of the staging build (e.g. `https://<build>.dev.odoo.com`, no trailing slash); `$B2B_MX_ID` = id of the B2B MX website in staging (*Website › Configuration › Websites* › open B2B MX › id in the browser URL).
> - Odoo emails in staging are not sent for real: they appear in the build's *Mails* tab (or *Settings › Technical › Emails*, developer mode).

The 11 pages of the module (all published, all bound to B2B MX only):

| # | URL | Browser tab title (starts with) | Has a lead form |
|---|---|---|---|
| 1 | `/inicio` (also served at `/`) | WAYAKIT B2B · Inicio | yes, `/#contacto` |
| 2 | `/aviacion` | WAYAKIT B2B · Industria · Aviación | yes, `/aviacion#cotizar` |
| 3 | `/transporte` | WAYAKIT B2B · Industria · Transporte y flotillas | yes, `/transporte#cotizar` |
| 4 | `/hospitales` | WAYAKIT B2B · Industria · Hospitales | yes, `/hospitales#cotizar` |
| 5 | `/restaurantes` | WAYAKIT B2B · Industria · Restaurantes | yes, `/restaurantes#cotizar` |
| 6 | `/productos` | WAYAKIT B2B · Productos | no |
| 7 | `/ciencia` | WAYAKIT B2B · Ciencia y registros | no |
| 8 | `/sustentabilidad` | WAYAKIT B2B · Sustentabilidad | no |
| 9 | `/nosotros` | WAYAKIT B2B · Nosotros | no |
| 10 | `/cotizar` | WAYAKIT B2B · Cotizar y contacto | yes, DEMO / quote toggle, `/cotizar#cotizacion` |
| 11 | `/prensa` | WAYAKIT B2B · Prensa | no |

---

## 0. Pre-build read-only checks (PRODUCTION, Odoo MCP, by Claude)

Read-only: no write, no create. Record the values; they are the "before" for section 3 and 4.

- [ ] 0.1 `website` with `name = 'B2B MX'` (exact): **exactly 1** record. Also search `name ilike 'B2B'` and list all: no near-duplicates ("B2B MX ", "B2B Mx", "B2B MX (copy)"). **Why:** the install looks the website up by exact name and **stops with a UserError** if it finds 0 or 2+.
- [ ] 0.2 On that record, record: `id`, `company_id` (expected **Wayakit SAPI de CV**), `default_lang_id` (expected `es_MX`), `language_ids` (if there is more than `es_MX`, note it: the pages are Spanish only and the custom header has no language selector), `domain` (expected empty), `sequence`.
- [ ] 0.3 `homepage_url` is **empty**. If it is not empty, the install will **not** reroute `/` to `/inicio` (by design): decide with Mafer before the build whether to clear it.
- [ ] 0.4 `website.page` with `url in ['/inicio', '/aviacion', '/transporte', '/hospitales', '/restaurantes', '/productos', '/ciencia', '/sustentabilidad', '/nosotros', '/cotizar', '/prensa']`:
  - with `website_id = B2B MX`: **none** expected. If any exists, Odoo would serve either page at random; the build log will warn `already has other pages on …`. Rename or unpublish it first.
  - with `website_id = False` (generic, shown on every website): **none** expected. Record any found: on the other websites those URLs will show that page instead of a 404 in section 4.
  - with any other website: record them (expected to stay as they are).
- [ ] 0.5 `website.page` with `url = '/'` and `website_id in (B2B MX, False)`: record `id`, `name`, `view_id`, `write_date` and the view's `write_date`. This is the old homepage; section 3.4 checks it is untouched.
- [ ] 0.6 `website.rewrite` with `url_from` in the 11 URLs (any website, or none): **none** expected. A redirect would hide the new page.
- [ ] 0.7 `crm_default_team_id` of B2B MX: record it (expected **Europa**). Its `company_id` must be **empty or Wayakit SAPI de CV**. Also record its `use_leads` (decides whether form leads land as *Lead* or *Opportunity*) and `active`.
- [ ] 0.8 `crm_default_user_id` of B2B MX: expected **empty**. If set, that `res.users` must have **Wayakit SAPI de CV** in `company_ids`, otherwise the lead is created with a salesperson who cannot open it.
- [ ] 0.9 `ir.module.module`: `website_crm` is **installed**; `wayakit_website_mx_b2b` is absent or **uninstalled**.
- [ ] 0.10 `res.lang` `es_MX` is **active**.
- [ ] 0.11 Record name, `domain` and `homepage_url` of the other websites: **KSA Website**, **Kit Pets KSA**, **WAYAKIT MX**, **My Website**. Section 4 compares against these.

## 1. Build and install

- [ ] 1.1 The staging build of the `mainTest` commit is **green** (no install error). In the build log there is **no** `needs exactly one website named 'B2B MX'`.
- [ ] 1.2 Build log contains the line `wayakit_website_mx_b2b: bound 12 views, 11 pages, 0 menus to website <id> (first bind: True)` (12 views = 11 pages + the header/footer layout). Write down the numbers you see.
- [ ] 1.3 Build log has **no** warning `already has other pages on` and **no** warning `default sales team belongs to`. If one appears, copy it here.
- [ ] 1.4 *Apps* (remove the "Apps" filter) › search **Wayakit - B2B MX Website** › state **Installed**.
- [ ] 1.5 *Website › Site › Pages* (switch to B2B MX, or group by website): the 11 URLs of the table appear, **Website = B2B MX**, **Published** ticked. None of them appears with an empty website or another website.
- [ ] 1.6 Developer mode › *Settings › Technical › Views* › search `WAYAKIT B2B MX: encabezado y pie`: exactly **1** record, **Active**, **Website = B2B MX**.

## 2. Reaching B2B MX in staging

- [ ] 2.1 Logged in as admin, open the backend **Website** app › website switcher (top bar) › **B2B MX**. Or open `$STAGING_URL/website/force/$B2B_MX_ID?path=/` directly. **Expected:** the B2B MX home in the frontend. Note which method worked.
- [ ] 2.2 (Needed only for the logged-out checks and the curl commands) *Website › Configuration › Websites* › B2B MX › **Domain** = `$STAGING_URL` › Save. Staging-only change: lost on the next build, **never do this in prod**. Before doing it, run 2.3.
- [ ] 2.3 **Before 2.2:** run this from a terminal. It hits whatever website staging serves by default to an anonymous visitor. **Expected:** every line `404`, except `/` (`200`, that website's own home):
  ```
  for p in / /inicio /aviacion /transporte /hospitales /restaurantes /productos /ciencia /sustentabilidad /nosotros /cotizar /prensa; do
    printf '%-18s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$STAGING_URL$p")"
  done
  curl -s "$STAGING_URL/" | grep -o '<title>[^<]*</title>'
  ```
  Write down which website the `<title>` belongs to (it must **not** be B2B MX).

## 3. Pages on B2B MX

- [ ] 3.1 Open each of the 11 URLs on B2B MX (switcher or private window after 2.2). For each: renders with no error, the **design header** (top strip "¿Compras para tu casa? Visita la tienda en línea", logo, menu Industrias / Productos / Ciencia y registros / Certificaciones / Nosotros, button "Agenda una DEMO"), the **design footer** (dark band, 4 columns, "© WAYAKIT México") and the **floating WhatsApp button** bottom-right. The Odoo standard header and footer are **not** shown, and the header appears **only once**. Tab title as in the table.
- [ ] 3.2 Every link of the header, the "Industrias" dropdown and the footer opens the right page. Anchors land on their section: `/#contacto`, `/#industrias`, `/ciencia#registros`, `/ciencia#tecnologia`, `/cotizar#cotizacion`, `/aviacion#cotizar`, `/transporte#cotizar`, `/hospitales#cotizar`, `/restaurantes#cotizar`. External links (`mx-shop.wayakit.com`, `wa.me/525519054912`, `mailto:contact@wayakit.com`) open the right target.
- [ ] 3.3 `/` shows the new home (hero "Ciencia que funciona, precio que convence"), the same content as `/inicio`. *Website › Configuration › Websites* › B2B MX: **Homepage URL = `/inicio`**.
- [ ] 3.4 The old homepage recorded in 0.5 is untouched: same record in *Site › Pages* with URL `/`, same name, and the `write_date` of the page and of its view are the same as in prod. It is only shadowed by the new homepage, not edited or deleted.
- [ ] 3.5 An unknown URL (`/no-existe-qa`) returns Odoo's 404 page on B2B MX, with no error trace.
- [ ] 3.6 Developer tools › Console: **no errors** on any of the 11 pages (reload each with the console open). Network tab: no `404` for `/wayakit_website_mx_b2b/static/src/...` images or for the Google Fonts CSS.
- [ ] 3.7 (After 2.2) Logged out / private window: the 11 pages load as a public visitor (they are published). Then run:
  ```
  for p in / /inicio /aviacion /transporte /hospitales /restaurantes /productos /ciencia /sustentabilidad /nosotros /cotizar /prensa; do
    printf '%-18s %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$STAGING_URL$p")"
  done
  curl -s "$STAGING_URL/" | grep -c 'wk wk-encabezado'          # expected 1 (header once)
  curl -s "$STAGING_URL/" | grep -c 'wk wk-pie'                 # expected 1 (footer once)
  curl -s "$STAGING_URL/" | grep -c 'id="wk-wa"'                # expected 1 (WhatsApp button)
  curl -s "$STAGING_URL/" | grep -c '<header id="top"'          # expected 0 (no Odoo header)
  curl -s "$STAGING_URL/" | grep -c 'Ciencia que funciona'      # expected 1 or more
  curl -s -o /dev/null -w '%{http_code}\n' "$STAGING_URL/wayakit_website_mx_b2b/static/src/img/logo-azul.png"   # expected 200
  ```
  **Expected:** all 11 pages `200`, then the counts in the comments. Paste the output here.

## 4. Other websites are not affected

For each website **KSA Website**, **Kit Pets KSA**, **WAYAKIT MX** and **My Website**, switch to it with the backend website switcher (never through its real domain).

- [ ] 4.1 KSA Website: home and its normal Odoo header/footer as before; **no** WAYAKIT B2B header, footer or floating WhatsApp button; `/aviacion`, `/transporte`, `/hospitales`, `/restaurantes`, `/productos`, `/ciencia`, `/sustentabilidad`, `/nosotros`, `/cotizar`, `/prensa`, `/inicio` return **404** (except pages recorded in 0.4 as existing on that website or generic). Homepage URL unchanged vs 0.11.
- [ ] 4.2 Kit Pets KSA: same checks as 4.1.
- [ ] 4.3 WAYAKIT MX: same checks as 4.1.
- [ ] 4.4 My Website: same checks as 4.1.
- [ ] 4.5 On each of them: Console with **no new errors**, and no visual change to headings, lists, buttons or form fields (the module's CSS and JS load on every website; they are scoped to `.wk` and must not leak). Compare against prod if in doubt.
- [ ] 4.6 *Site › Pages* on each of them: none of the 11 module pages is listed.

## 5. Visual check: desktop (window 1280 px or wider)

Compare against the approved design.

- [ ] 5.1 Fonts are Poppins (headings) and Roboto (text), not the Odoo theme font.
- [ ] 5.2 **Mirrored background photos** (flipped left-right on purpose; must match the design, not the original photo file):
  - Home: the press band "WAYAKIT en la prensa", the band "La ciencia de la limpieza combinada con el poder de la naturaleza", and the form band "Hacemos soluciones a tu medida" (`/#contacto`).
  - `/aviacion`, `/transporte`, `/hospitales`, `/restaurantes`: the quote form band (`#cotizar`).
  - `/ciencia`, `/nosotros`, `/prensa`: the dark hero at the top.
  - `/prensa`: both bands "En medios de México" and "En medios internacionales".
  - Not mirrored: the home hero, the industry page heroes, `/productos` hero, `/cotizar` hero.
- [ ] 5.3 "Industrias" dropdown: opens on **hover**, closes about 0.2 s after the mouse leaves; a **click** while open by hover keeps it open; a second click closes it; click anywhere else closes it; **Esc** closes it and returns focus to "Industrias"; with keyboard focus on "Industrias", **ArrowDown / ArrowUp** move through the 5 items. Clicking an item navigates and the dropdown closes.
- [ ] 5.4 Home tabs "Encuentra la solución para tu operación": 5 tabs (Aviación, Transporte y flotillas, Hospitales, Restaurantes, A tu medida). First one selected on load; clicking each shows only its panel.
- [ ] 5.5 Press carousel on the home (6 notes) and on `/prensa` (2 carousels): advances by itself every **7 s** with a progress bar under the active note; **pauses** while the mouse is over it or keyboard focus is inside, resumes when it leaves; clicking a note jumps to it. With the OS setting "reduce motion" on, it does not auto-advance (optional).
- [ ] 5.6 `/cotizar` DEMO / quote toggle: loads in **"Agenda una DEMO"** mode (title, subtitle and button say DEMO). Clicking "Solicita una cotización" switches title to "Solicita una cotización", subtitle to "Te contactaremos con tu cotización." and button to "Contáctenme →"; clicking back restores DEMO. The form fields stay the same in both modes and what you typed is kept.
- [ ] 5.7 Floating WhatsApp button stays visible while scrolling and never covers a form's submit button.

## 6. Visual check: phone (390 px)

Chrome DevTools › device toolbar › width **390** (e.g. iPhone 12/13/14), or a real phone (after 2.2).

- [ ] 6.1 All 11 pages: **no horizontal scroll**, no text cut off, images not stretched.
- [ ] 6.2 Header: menu hidden, **hamburger** shown. Tap it: menu opens as a vertical list, icon turns into an X; tap again or tap outside or **Esc**: closes.
- [ ] 6.3 Inside the open hamburger menu, "Industrias" opens as an **accordion** (no hover). Tapping an industry navigates and closes both.
- [ ] 6.4 Home tabs show in 2 columns and still switch panels. Carousels and the `/cotizar` toggle work by tap.
- [ ] 6.5 Forms are one column; fields and the privacy checkbox are tappable; the WhatsApp button does not hide the submit button.
- [ ] 6.6 Mirrored backgrounds from 5.2 still look right at this width.

## 7. Forms: leads in CRM

Submit from a **private window** after 2.2 (public visitor), or logged in with a fake email if 2.2 was not done; note which. After each submission open *CRM* (switch the company to **Wayakit SAPI de CV**; remove the default "My Pipeline" filter; check both *Leads* and *Pipeline*: the type depends on `use_leads` of the team from 0.7).

Expected lead for each form:

| Form | Lead name (`name`) | Industria in description | Origen in description |
|---|---|---|---|
| Home `/#contacto` | `B2B MX web — Cotización` | the option chosen in the select | `Página de inicio` |
| `/aviacion#cotizar` | `B2B MX web — Cotización Aviación` | `Aviación` (fixed) | `Página Aviación` |
| `/transporte#cotizar` | `B2B MX web — Cotización Transporte y flotillas` | `Transporte y flotillas` | `Página Transporte y flotillas` |
| `/hospitales#cotizar` | `B2B MX web — Cotización Hospitales` | `Hospitales` | `Página Hospitales` |
| `/restaurantes#cotizar` | `B2B MX web — Cotización Restaurantes` | `Restaurantes` | `Página Restaurantes` |
| `/cotizar`, DEMO mode | `B2B MX web — DEMO` | chosen | `Página Cotizar y contacto` |
| `/cotizar`, quote mode | `B2B MX web — Cotización` | chosen | `Página Cotizar y contacto` |

- [ ] 7.1 **Home form**: fill Empresa, Nombre, Correo, Teléfono, Industria = "Hospitales", Mensaje with **two lines, accents and `<b>prueba</b>`**, tick the privacy box › "Contáctenme →". **Expected:** the form is replaced by the card **"¡Gracias por escribirnos!" / "Recibimos tus datos. Un miembro del equipo WAYAKIT te contactará muy pronto."**; the browser stays on the page (it does not navigate to a `/website/form/...` URL or show JSON).
- [ ] 7.2 Lead of 7.1 in CRM:
  - Name `B2B MX web — Cotización` (table above).
  - **Company = Wayakit SAPI de CV**.
  - **Sales Team** = the team from 0.7 (Europa). **Salesperson** = empty (or the user from 0.8).
  - Company name, Contact name, Email, Phone = what you typed. Customer (partner) empty, no new contact created.
  - Medium = Website; language Spanish (MX) if shown.
  - **Description / Internal notes**: your message, with line breaks kept and `<b>prueba</b>` shown as **literal text, not bold**; then an "Other Information" block with `Industria : Hospitales`, `Origen : Página de inicio`, `Aviso de Privacidad : Aceptado`.
- [ ] 7.3 **One industry page** (e.g. `/aviacion#cotizar`): fill required fields only, **leave Teléfono and Mensaje empty**, tick privacy › submit. **Expected:** thank-you card; lead as in the table with `Industria : Aviación`, `Origen : Página Aviación`, consent line; phone empty; no error.
- [ ] 7.4 **`/cotizar` in DEMO mode** (do not touch the toggle): fill everything incl. Industria, **Presentación de interés** = "Bidón 20L", **Ciudad** = "Monterrey" › "Agenda una DEMO →". **Expected:** lead `B2B MX web — DEMO`; description has `Industria`, `Presentación : Bidón 20L`, `Ciudad : Monterrey`, `Origen : Página Cotizar y contacto`, consent.
- [ ] 7.5 **`/cotizar` in quote mode**: reload, click "Solicita una cotización", fill › "Contáctenme →". **Expected:** lead named exactly `B2B MX web — Cotización` (not "DEMO", not both titles joined).
- [ ] 7.6 `/cotizar` toggle back and forth: reload › quote › DEMO › submit. **Expected:** lead `B2B MX web — DEMO`.
- [ ] 7.7 **Required fields block submission** (on the home form and on `/cotizar`): try submitting with each of these missing, one at a time: Empresa, Nombre, Correo, Industria ("Selecciona tu industria"), **privacy checkbox unticked**. Also Correo = `prueba-sin-arroba`. **Expected:** the browser shows the required/invalid message, nothing is sent, **no lead** is created (count leads before and after).
- [ ] 7.8 **Double click**: on one form, double-click (or click 3 times fast) the submit button. **Expected:** exactly **one** lead.
- [ ] 7.9 Notifications: in the build's *Mails* tab, note which emails (if any) the submissions generated and to whom. With no salesperson set, probably nobody is notified. **Open question for the MX team:** who must be notified of a new B2B MX lead (salesperson, team, email alias)? Write the decision here: ______. It is a go-live blocker (section 9.6).
- [ ] 7.10 Delete the test leads afterwards (optional; staging is discarded anyway).

## 8. Website editor and upgrade

- [ ] 8.1 Before editing: on the home, select the **3rd tab**, click the **4th press note**; on `/cotizar` switch to **quote** mode. Then on each page click **Edit**, change nothing, **Save**, reload. **Expected:** home back on tab 1 and note 1, `/cotizar` back in DEMO mode (the editor must not save the clicked state).
- [ ] 8.2 On `/aviacion` › **Edit**: change one heading text (e.g. add " QA" at the end) and **replace one image** (double-click a card/photo image › pick another) › **Save**. **Expected:** saved, both changes visible after reload, logged-out too (after 2.2).
- [ ] 8.3 In edit mode, click the custom **header** and the custom **footer**. **Expected:** **no Header / Footer option panels** (no template, colour, scroll-effect or footer-template options) on the right. If they appear, do **not** change them; note what appears and report it.
- [ ] 8.4 In edit mode, click inside a **form**. Note whether Odoo's form options panel appears. Do **not** change any form option. Save, then submit that form once more (as in 7.3). **Expected:** the lead is still created with the same name, Industria, Origen and consent lines; on `/cotizar` the DEMO / quote toggle and title still work after the save.
- [ ] 8.5 Console: **no errors** while entering edit mode, editing, saving and leaving edit mode (header dropdown, hamburger, tabs and carousel keep working after leaving edit mode, without reload).
- [ ] 8.6 **Upgrade in the same build** (a new push would start from a fresh prod copy): *Apps* › Wayakit - B2B MX Website › **Upgrade** (developer mode), or in the odoo.sh shell: `odoo-bin -u wayakit_website_mx_b2b --stop-after-init` then restart. **Expected:** upgrade without error; log line `bound 12 views, 11 pages, 0 menus to website <id> (first bind: False)`.
- [ ] 8.7 After 8.6: the edit of 8.2 (heading text **and** image) **survived** (pages are `noupdate`). `/` still shows the home, Homepage URL still `/inicio`, header still shown **once** (re-run the `grep -c 'wk wk-encabezado'` from 3.7: expected 1).
- [ ] 8.8 (Information) Edit a text of the **header or footer** in the editor, save, then upgrade as in 8.6. Note whether the edit survived. The header/footer view is **not** `noupdate`, so it may be overwritten by the code on upgrade; whatever the result, write it down so the docs say where header/footer changes must be made (code vs editor).
- [ ] 8.9 *Site › Pages*: still exactly one page per URL on B2B MX (no duplicated pages after the upgrade).

## 9. Go-live blockers (not code; tracked here, owner decides)

- [ ] 9.1 **Privacy notice URL.** The "Leer aviso" link in every form (home, 4 industry pages, `/cotizar`) and "Aviso de Privacidad" in the footer point to `#`. Needs the real URL of the Mexican privacy notice.
- [ ] 9.2 **Instagram and Facebook URLs** for the footer icons: they currently have no link.
- [ ] 9.3 **Thank-you copy** "¡Gracias por escribirnos! Recibimos tus datos. Un miembro del equipo WAYAKIT te contactará muy pronto." is shown to prospects: **must be reviewed and approved by an area lead** before go-live.
- [ ] 9.4 **All public copy** of the 11 pages, header and footer (claims, registrations, figures, press notes, customer logos) **must be reviewed and approved by an area lead** before go-live.
- [ ] 9.5 **Final domain**: choose it, set it on B2B MX in prod, DNS record, odoo.sh custom domain + certificate, and the Cloudflare setup (golden rule 6). Until then, the site is not reachable by the public.
- [ ] 9.6 **Lead notification / assignment** (from 7.9): decide who is notified and whether the default team "Europa" (no company) is the right team for Mexican B2B leads, or set a salesperson on B2B MX (allowed in Wayakit SAPI de CV, see 0.8).
- [ ] 9.7 (Recommended) Spam protection on the public forms (reCAPTCHA / Turnstile): decide whether it is needed before the domain is public.
- [ ] 9.8 After go-live in prod, from a terminal, the other websites still return 404 on the new URLs (read-only GET, no form submission):
  ```
  for d in https://ksasystem.wayakit.com https://sistema.wayakit.com; do
    for p in /aviacion /cotizar /inicio; do printf '%s%-12s %s\n' "$d" "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$d$p")"; done
  done
  ```
  **Expected:** all `404` (unless 0.4 recorded an existing page there).

## 10. Rollback (uninstall)

Staging only, at the very end (it removes the pages). In prod, an uninstall deletes every page edited in the editor and cannot be undone: back up first.

- [ ] 10.1 Before uninstalling: on B2B MX, *Site › Pages* › one module page (e.g. `/nosotros`) › **Clone page** (Duplicate) › give it a new URL (e.g. `/nosotros-copia`). Write down its URL.
- [ ] 10.2 *Apps* › Wayakit - B2B MX Website › **Uninstall**. **Expected:** no error.
- [ ] 10.3 On B2B MX: the 11 pages **and the clone of 10.1** are gone (404); *Site › Pages* lists none of them.
- [ ] 10.4 B2B MX **Homepage URL is empty** again; `/` shows the old homepage recorded in 0.5 with the Odoo standard header/footer; no floating WhatsApp button.
- [ ] 10.5 The other websites (section 4) are unchanged: their pages, header/footer and Homepage URL as in 0.11.
- [ ] 10.6 Developer mode › *Technical › Views*: no view named `WAYAKIT B2B · …` or `WAYAKIT B2B MX: encabezado y pie` left.

---

**After staging:** everything done here by hand (B2B MX domain in 2.2, editor edits, test leads) is lost on the next build. In production: install happens with the merge to `main`; the domain, privacy URL, social links, notification decision and copy approvals of section 9 are done there, by hand, after the area lead's review.
