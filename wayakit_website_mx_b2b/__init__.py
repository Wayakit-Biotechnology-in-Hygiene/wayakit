from . import models


def uninstall_hook(env):
    # The pages go away with the module: do not leave "/" of B2B MX rerouted to our home
    # (its URL may have been renamed in the UI; Odoo keeps homepage_url in sync with it).
    page = env.ref('wayakit_website_mx_b2b.page_inicio_page', raise_if_not_found=False)
    if page and page.website_id and page.website_id.homepage_url == page.url:
        page.website_id.homepage_url = False
