/** @odoo-module **/
/* WAYAKIT B2B MX: behaviour of the approved static design, ported from its assembler.
   - [data-tabs]: tabs (home, "Encuentra la solución para tu operación").
   - [data-modos]: DEMO / quote toggle of the /cotizar form.
   - [data-carrusel]: press carousel, advances every 7 s, pauses on hover/focus.
   - .f01-nav: "Industrias" dropdown (hover + click on desktop, accordion on mobile) and hamburger.
   Forms are not handled here: Odoo's s_website_form widget posts them to crm.lead. */

import publicWidget from "@web/legacy/js/public/public_widget";

publicWidget.registry.WkB2bMx = publicWidget.Widget.extend({
    selector: ".wk",

    start() {
        this._quitar = [];
        // Run on destroy (e.g. entering edit mode) to put every block back in its first state:
        // the editor saves the DOM as it is, it must not save "tab 3" or "slide 4" as default.
        this._reiniciar = [];
        this.el.querySelectorAll("[data-tabs]").forEach((box) => this._iniciarTabs(box));
        this.el.querySelectorAll("[data-modos]").forEach((box) => this._iniciarModos(box));
        this.el.querySelectorAll("[data-carrusel]").forEach((box) => this._iniciarCarrusel(box));
        this.el.querySelectorAll("form").forEach((form) => this._iniciarFormulario(form));
        if (this.el.querySelector(".f01-nav")) {
            this._iniciarNav();
        }
        return this._super(...arguments);
    },

    destroy() {
        (this._quitar || []).forEach((quitar) => quitar());
        (this._reiniciar || []).forEach((reiniciar) => reiniciar());
        this._super(...arguments);
    },

    _on(target, type, handler) {
        target.addEventListener(type, handler);
        this._quitar.push(() => target.removeEventListener(type, handler));
    },

    _iniciarTabs(box) {
        const tabs = [...box.querySelectorAll("[data-tab]")];
        if (!tabs.length) {
            return;
        }
        const elegir = (tab) => {
            tabs.forEach((o) => o.setAttribute("aria-selected", o === tab ? "true" : "false"));
            box.querySelectorAll("[data-panel]").forEach((p) => {
                p.hidden = p.getAttribute("data-panel") !== tab.getAttribute("data-tab");
            });
        };
        tabs.forEach((tab) => this._on(tab, "click", () => elegir(tab)));
        this._reiniciar.push(() => elegir(tabs[0]));
    },

    _iniciarModos(box) {
        const bs = [...box.querySelectorAll("[data-modo]")];
        if (!bs.length) {
            return;
        }
        const poner = (modo) => {
            bs.forEach((b) => b.setAttribute("aria-pressed", b.getAttribute("data-modo") === modo ? "true" : "false"));
            box.querySelectorAll("[data-solo]").forEach((e) => {
                e.hidden = e.getAttribute("data-solo") !== modo;
                // The hidden "name" inputs carry the lead title of each mode: a disabled
                // input is not submitted, so the lead gets the title of the visible mode.
                if (e.tagName === "INPUT") {
                    e.disabled = e.hidden;
                }
            });
        };
        const primero = bs[0].getAttribute("data-modo");
        bs.forEach((b) => this._on(b, "click", () => poner(b.getAttribute("data-modo"))));
        poner(primero);
        this._reiniciar.push(() => poner(primero));
    },

    /* Buttons [data-ir] mark the active note and show its slide [data-diapo]. */
    _iniciarCarrusel(box) {
        const bs = box.querySelectorAll("[data-ir]");
        const ds = box.querySelectorAll("[data-diapo]");
        if (!bs.length) {
            return;
        }
        const quieto = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        let actual = 0;
        let pausa = false;
        let reloj = null;
        const ir = (i, animar = true) => {
            actual = (i + bs.length) % bs.length;
            bs.forEach((b, j) => {
                b.setAttribute("aria-selected", j === actual ? "true" : "false");
                b.classList.remove("corriendo");
            });
            ds.forEach((d, j) => d.classList.toggle("activa", j === actual));
            if (animar && !pausa && !quieto) {
                void bs[actual].offsetWidth; // restart the progress bar animation
                bs[actual].classList.add("corriendo");
            }
        };
        const programar = () => {
            clearTimeout(reloj);
            if (pausa || quieto) {
                return;
            }
            reloj = setTimeout(() => {
                if (!document.contains(box)) {
                    return;
                }
                if (!box.closest("[hidden]")) {
                    ir(actual + 1);
                }
                programar();
            }, 7000);
        };
        const parar = (valor) => {
            pausa = valor;
            bs[actual].classList.toggle("corriendo", !valor && !quieto);
            programar();
        };
        bs.forEach((b, j) => this._on(b, "click", () => {
            ir(j);
            programar();
        }));
        this._on(box, "mouseenter", () => parar(true));
        this._on(box, "mouseleave", () => parar(false));
        this._on(box, "focusin", () => parar(true));
        this._on(box, "focusout", (e) => {
            if (!box.contains(e.relatedTarget)) {
                parar(false);
            }
        });
        ir(0);
        programar();
        this._reiniciar.push(() => {
            clearTimeout(reloj);
            ir(0, false);
        });
    },

    /* Odoo's form widget only disables buttons with .s_website_form_send, which would skip the
       browser's required-field check; block double clicks here once the check has passed. */
    _iniciarFormulario(form) {
        const boton = form.querySelector("button[type=submit]");
        if (!boton) {
            return;
        }
        let reloj = null;
        this._on(form, "submit", () => {
            boton.disabled = true;
            clearTimeout(reloj);
            reloj = setTimeout(() => (boton.disabled = false), 4000);
        });
        this._reiniciar.push(() => {
            clearTimeout(reloj);
            boton.disabled = false;
        });
    },

    _iniciarNav() {
        const raiz = this.el;
        const escritorio = window.matchMedia("(min-width: 1001px) and (hover: hover)");
        let navReloj = null;
        const navDd = (dd, abrir) => {
            if (!dd) {
                return;
            }
            dd.classList.toggle("abierto", abrir);
            dd.querySelector(".f01-dd-btn").setAttribute("aria-expanded", String(abrir));
            if (!abrir) {
                dd.removeAttribute("data-hover");
            }
        };
        const navHam = (nav, abrir) => {
            if (!nav) {
                return;
            }
            const b = nav.querySelector(".f01-ham");
            nav.classList.toggle("f01-abierto", abrir);
            b.setAttribute("aria-expanded", String(abrir));
            b.setAttribute("aria-label", abrir ? "Cerrar menú" : "Abrir menú");
            if (!abrir) {
                navDd(nav.querySelector(".f01-dd"), false);
            }
        };
        // Clicks are listened on the document so a click anywhere else closes the menus.
        this._on(document, "click", (e) => {
            const t = e.target instanceof Element ? e.target : null;
            let dd, nav;
            if (t && raiz.contains(t)) {
                if ((dd = t.closest(".f01-dd-btn") && t.closest(".f01-dd"))) {
                    // If hovering already opened it, the click keeps it open instead of closing it.
                    if (dd.hasAttribute("data-hover")) {
                        dd.removeAttribute("data-hover");
                        navDd(dd, true);
                    } else {
                        navDd(dd, !dd.classList.contains("abierto"));
                    }
                    return;
                }
                if ((nav = t.closest(".f01-ham") && t.closest(".f01-nav"))) {
                    navHam(nav, !nav.classList.contains("f01-abierto"));
                    return;
                }
                if (t.closest(".f01-dd-lista a")) {
                    navDd(t.closest(".f01-dd"), false);
                    navHam(t.closest(".f01-nav"), false);
                    return;
                }
            }
            raiz.querySelectorAll(".f01-dd.abierto").forEach((d) => {
                if (!t || !d.contains(t)) {
                    navDd(d, false);
                }
            });
            raiz.querySelectorAll(".f01-nav.f01-abierto").forEach((n) => {
                if (!t || !n.contains(t)) {
                    navHam(n, false);
                }
            });
        });
        this._on(raiz, "mouseover", (e) => {
            const dd = escritorio.matches && e.target.closest(".f01-dd");
            if (!dd) {
                return;
            }
            clearTimeout(navReloj);
            if (!dd.classList.contains("abierto")) {
                navDd(dd, true);
                dd.setAttribute("data-hover", "");
            }
        });
        this._on(raiz, "mouseout", (e) => {
            const dd = escritorio.matches && e.target.closest(".f01-dd");
            if (!dd || dd.contains(e.relatedTarget)) {
                return;
            }
            clearTimeout(navReloj);
            navReloj = setTimeout(() => {
                if (!dd.contains(document.activeElement) || dd.hasAttribute("data-hover")) {
                    navDd(dd, false);
                }
            }, 200);
        });
        this._on(raiz, "focusout", (e) => {
            const dd = e.target.closest && e.target.closest(".f01-dd");
            if (dd && e.relatedTarget && !dd.contains(e.relatedTarget)) {
                navDd(dd, false);
            }
        });
        this._on(document, "keydown", (e) => {
            let dd, nav;
            if (e.key === "Escape") {
                if ((dd = raiz.querySelector(".f01-dd.abierto"))) {
                    const adentro = dd.contains(document.activeElement);
                    navDd(dd, false);
                    if (adentro) {
                        dd.querySelector(".f01-dd-btn").focus();
                    }
                } else if ((nav = raiz.querySelector(".f01-nav.f01-abierto"))) {
                    navHam(nav, false);
                    nav.querySelector(".f01-ham").focus();
                }
                return;
            }
            const t = e.target instanceof Element ? e.target : null;
            if ((e.key === "ArrowDown" || e.key === "ArrowUp") && t && raiz.contains(t) && (dd = t.closest(".f01-dd"))) {
                e.preventDefault();
                const links = [...dd.querySelectorAll(".f01-dd-lista a")];
                navDd(dd, true);
                let i = links.indexOf(document.activeElement);
                if (i < 0) {
                    i = e.key === "ArrowDown" ? 0 : links.length - 1;
                } else {
                    i = (i + (e.key === "ArrowDown" ? 1 : -1) + links.length) % links.length;
                }
                links[i].focus();
            }
        });
        this._reiniciar.push(() => {
            clearTimeout(navReloj);
            raiz.querySelectorAll(".f01-dd").forEach((d) => navDd(d, false));
            raiz.querySelectorAll(".f01-nav").forEach((n) => navHam(n, false));
        });
    },
});

export default publicWidget.registry.WkB2bMx;
