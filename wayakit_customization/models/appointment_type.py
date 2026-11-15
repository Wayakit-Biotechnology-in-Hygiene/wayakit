from odoo import models, fields
from datetime import datetime, timedelta
import requests
import logging

from .appointment_phone import KSA_COMPANY_ID, phone_input_attrs

_logger = logging.getLogger(__name__)

class AppointmentType(models.Model):
    _inherit = 'appointment.type'

    api_service = fields.Boolean(string="Api Service")
    api_description = fields.Text(string="Description")
    service_type = fields.Many2many('service.type' , string="Service Types")
    # reminder_featureName = fields.Char()
    reminder_notification_title = fields.Char()
    reminder_notification_body = fields.Text()
    # completion_featureName = fields.Char()
    completion_notification_title = fields.Char()
    completion_notification_body = fields.Text()
    product_tmpl_id = fields.Many2one('product.template', string='Product')

    def _wk_is_ksa(self):
        """True for types published on a KSA website. Single source for the
        form template and the submit controller; never matched by name."""
        # sudo: the public user cannot read res.company; exists(): the
        # controller browses the id straight from the URL
        return self.sudo().exists()[:1].website_id.company_id.id == KSA_COMPANY_ID

    def _wk_phone_input_attrs(self):
        return phone_input_attrs(self._wk_is_ksa())

    def status_notification_update(self):
        token = self.env['ir.config_parameter'].sudo().get_param('wayakit_customization.notification_token')
        url = self.env['ir.config_parameter'].sudo().get_param('wayakit_customization.notification_url')
        if token and url:
            current_datetime = fields.Datetime.now()
            _logger.info("current datetime: %s", current_datetime)
            from_datetime = current_datetime - timedelta(minutes=7)
            to_datetime = current_datetime + timedelta(minutes=7)
            Appointments = self.search([('api_service', '=', True)])
            for appointment in Appointments:
                events_reminder = appointment.meeting_ids.filtered(lambda x: not x.reminder_notification)
                reminder_kaust_id = []
                reminder_event_ids = []
                completion_kaust_id = []
                completion_event_ids = []
                for event in events_reminder:
                    start = event.start - timedelta(hours=1)
                    if from_datetime <= start <= to_datetime:
                        reminder_kaust_id.append(event.appointment_booker_id.customer_kaust_id) if event.appointment_booker_id and event.appointment_booker_id.customer_kaust_id else None
                        reminder_event_ids.append(event.id)
                events_completion = appointment.meeting_ids.filtered(lambda x: x.reminder_notification and not x.completion_notification)
                for events_compl in events_completion:
                    if events_compl.stop <= current_datetime:
                        completion_kaust_id.append(events_compl.appointment_booker_id.customer_kaust_id) if events_compl.appointment_booker_id and events_compl.appointment_booker_id.customer_kaust_id else None
                        completion_event_ids.append(event.id)
                if reminder_kaust_id:
                    data = self.prepare_notification_api_values(appointment.reminder_notification_title,
                                                                appointment.reminder_notification_body,
                                                                reminder_kaust_id)
                    response = self.status_update_notification_api(url, token, data)
                    if response:
                        reminder_events = self.env['calendar.event'].browse(reminder_event_ids)
                        reminder_events.write({'reminder_notification': True})

                if completion_kaust_id:
                    data = self.prepare_notification_api_values(appointment.completion_notification_title,
                                                                appointment.completion_notification_body,
                                                                completion_kaust_id)
                    response = self.status_update_notification_api(url, token, data)
                    if response:
                        completion_events = self.env['calendar.event'].browse(completion_event_ids)
                        completion_events.write({'completion_notification': True})


    def prepare_notification_api_values(self, title, body, kaust_id):
        return{
            "featureName": "My GA",
            "kaustId": kaust_id,
            "userCodes": [],
            "notificationTitle": title,
            "notificationBody": body
        }

    def status_update_notification_api(self, url, token, data):
        headers = {
            "Content-Type": "application/json",
            "Authorization": "Bearer" + " " + token
        }
        _logger.info("Api data: %s", data)
        try:
            response = requests.post(url, headers=headers, json=data)
            data = response.json()
            _logger.info("Api response: %s", data)
            if data.get('status') == 200:
                return True
        except Exception as e:
            _logger.error('Notification Update Error: %s', e)