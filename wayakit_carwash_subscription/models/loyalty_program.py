import logging

from odoo import api, models

_logger = logging.getLogger(__name__)

KSA_COMPANY_ID = 1
CW_PROGRAM_PATTERN = 'CW Sub – %'
CW_TEMPLATE_XMLID = 'wayakit_carwash_subscription.mail_template_cw_coupon'
CORE_TEMPLATE_XMLID = 'loyalty.mail_template_loyalty_card'


class LoyaltyProgram(models.Model):
    _inherit = 'loyalty.program'

    @api.model
    def _cw_use_coupon_template(self):
        """Point the "At creation" e-mail of the "CW Sub – …" programs to the Car Wash template.

        Shared by the 17.0.1.2.0 migration and scripts/setup_coupon_programs.py, and safe
        to run again. Only the core "Coupon Information" template is replaced: a template
        someone chose by hand is kept. Returns the number of programs changed.
        """
        template = self.env.ref(CW_TEMPLATE_XMLID, raise_if_not_found=False)
        if not template:
            return 0
        core = self.env.ref(CORE_TEMPLATE_XMLID, raise_if_not_found=False)
        # en_US: the name is translatable, the pattern is the English one.
        programs = self.sudo().with_context(lang='en_US', active_test=False).search([
            ('company_id', '=', KSA_COMPANY_ID),
            ('name', '=like', CW_PROGRAM_PATTERN),
        ]).with_context(active_test=True)  # archived e-mail lines don't send, ignore them
        changed = 0
        for program in programs:
            lines = program.communication_plan_ids.filtered(lambda m: m.trigger == 'create')
            if not lines:
                program.communication_plan_ids = [(0, 0, {'trigger': 'create', 'mail_template_id': template.id})]
                changed += 1
                continue
            to_switch = lines.filtered(lambda m: core and m.mail_template_id == core)
            if to_switch:
                to_switch.mail_template_id = template
                changed += 1
            others = lines - to_switch
            if others.filtered(lambda m: m.mail_template_id != template):
                _logger.warning(
                    "wayakit_carwash_subscription: program %s (%s) keeps a hand-picked creation e-mail template",
                    program.id, program.name)
        _logger.info(
            "wayakit_carwash_subscription: Car Wash coupon template set on %s of %s CW Sub programs",
            changed, len(programs))
        return changed
